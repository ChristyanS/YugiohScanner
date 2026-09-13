"""Busca de candidatos por nome (plano §7.3).

A escada barato → caro existe para que o caso comum custe uma busca por índice
e o fuzzy caro só rode quando o barato falhou:

| Tier | Técnica                                   | Custo    |
|------|-------------------------------------------|----------|
| 0    | igualdade exata em `name_normalized`      | µs       |
| 2    | `card_fts MATCH` → top-50                 | < 5 ms   |
| 3    | RapidFuzz sobre os candidatos do tier 2   | < 5 ms   |
| 4    | RapidFuzz sobre o catálogo inteiro        | ~20-40ms |

O tier 4 é a rede de segurança: só roda quando o FTS não devolve nada, o que
acontece quando o OCR errou o começo de todas as palavras.

Todos os tiers também enxergam `card_alt_name` (nomes em FR/DE/IT/PT — plano
§7.1 multilíngue): uma carta fotografada em outro idioma casa pelo nome
alternativo, mas o candidato devolvido sempre exibe o nome canônico em
inglês — é o que já é gravado/exportado.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.fts import search_alt_card_ids, search_card_ids
from ..db.tables import Card, CardAltName
from ..domain.normalization import normalize_fuzzy, normalize_strict
from ..logging_setup import get_logger

log = get_logger(__name__)

#: Quantos candidatos o FTS devolve antes do reranking fuzzy.
FTS_LIMIT = 50

#: Quantos candidatos guardamos para a tela de revisão.
TOP_N = 5

#: `normalize_strict`/`normalize_fuzzy` mantêm só `[a-z0-9 '-]` — um nome
#: alternativo só em coreano ou japonês (`CardAltName`) normaliza para quase
#: nada (achado real: o nome KO de "H - Heated Heart" vira `"h-"`; o de
#: "Arcana Force V" vira `"v-"`). Um texto de OCR embaralhado que sobre só uma
#: letra solta (ex.: um "h" ou "v" perdido no meio de kanji ilegível) então
#: bate 100% contra esse nome degenerado — carta errada, confiança máxima.
#: Reproduzido e confirmado na calibração de 2026-09-13 (`docs/adr/0001`).
#: Nomes cuja forma normalizada é mais curta que isto carregam sinal
#: insuficiente para casar por texto e são ignorados nos tiers 0/3/4 — a
#: carta ainda pode ser identificada pelo set code (`domain/setcode.py`).
MIN_MATCHABLE_LENGTH = 4


def _is_matchable(normalized: str) -> bool:
    return len(normalized) >= MIN_MATCHABLE_LENGTH


@dataclass(frozen=True, slots=True)
class NameCandidate:
    """Uma carta candidata, com o quanto o nome lido se parece com o dela."""

    card_id: int
    name: str
    #: 0..1
    score: float
    #: Em qual tier da escada este candidato apareceu.
    tier: int
    #: Idioma da variante de nome que gerou o score — "EN" para o nome
    #: canônico, ou o idioma de `card_alt_name` (FR/DE/IT/PT) quando o texto
    #: lido bateu melhor com uma tradução. É o sinal de "em que idioma essa
    #: carta física provavelmente está impressa" (continuação do plano de
    #: idiomas — ver `matching/print_language.py`).
    language: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "score": round(self.score, 4),
            "tier": self.tier,
            "language": self.language,
        }


class NameIndex:
    """Catálogo de nomes em memória, para o fuzzy do tier 4.

    ~14.500 nomes ≈ 1,5 MB. Carregado uma vez por processo e reaproveitado; sem
    isso, cada leitura difícil faria uma varredura no banco.

    O índice é invalidado por contagem de cartas: depois de um `sync` que
    adiciona cartas, ele se recarrega sozinho.

    Cada carta entra com o nome canônico **e** todos os nomes alternativos
    conhecidos (FR/DE/IT/PT — plano §7.1 multilíngue): várias linhas nos
    arrays paralelos apontando para o mesmo `card_id`, sempre exibindo o nome
    em inglês (`_names`). `size` conta cartas distintas, não linhas — é o que
    o comparativo "cresceu depois do sync" espera.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._card_ids: list[int] = []
        self._names: list[str] = []
        self._fuzzy_names: list[str] = []
        self._languages: list[str] = []
        self._distinct_cards = 0
        self._loaded_count: int | None = None

    def ensure_loaded(self, session: Session) -> None:
        current = session.scalar(select(func.count()).select_from(Card)) or 0
        if self._loaded_count == current:
            return
        with self._lock:
            if self._loaded_count == current:  # pragma: no cover - corrida
                return
            rows = session.execute(select(Card.id, Card.name, Card.name_normalized)).all()
            canonical_names = {row.id: row.name for row in rows}

            card_ids = [row.id for row in rows]
            names = [row.name for row in rows]
            # A forma fuzzy é derivada da estrita: uma única regra de
            # normalização vale para o catálogo e para o OCR (plano §7.1).
            fuzzy_names = [normalize_fuzzy(row.name_normalized) for row in rows]
            languages = ["EN"] * len(rows)

            alt_rows = session.execute(
                select(CardAltName.card_id, CardAltName.name_normalized, CardAltName.language)
            ).all()
            for alt in alt_rows:
                canonical = canonical_names.get(alt.card_id)
                if canonical is None:  # pragma: no cover - FK garante consistência
                    continue
                alt_fuzzy = normalize_fuzzy(alt.name_normalized)
                if not _is_matchable(alt_fuzzy):
                    continue
                card_ids.append(alt.card_id)
                names.append(canonical)
                fuzzy_names.append(alt_fuzzy)
                languages.append(alt.language)

            self._card_ids = card_ids
            self._names = names
            self._fuzzy_names = fuzzy_names
            self._languages = languages
            self._distinct_cards = len(rows)
            self._loaded_count = current
            log.debug("matching.index_loaded", cards=current, alt_names=len(alt_rows))

    @property
    def size(self) -> int:
        return self._distinct_cards

    def search(self, query: str, *, cutoff: int, limit: int = TOP_N) -> list[NameCandidate]:
        """Melhores correspondências no catálogo inteiro.

        Uma carta pode aparecer várias vezes na lista achatada (canônico +
        alternativos); pede-se mais candidatos brutos do que `limit` para
        sobrar espaço depois de reduzir por `card_id`, mantendo só o melhor
        score de cada carta — senão duas linhas da mesma carta (ex.: nome em
        inglês e em português ambos parecidos) poderiam ocupar duas das
        `limit` vagas e empurrar outra carta candidata para fora.
        """
        if not query or not self._fuzzy_names:
            return []
        raw_matches = process.extract(
            query,
            self._fuzzy_names,
            scorer=fuzz.WRatio,
            score_cutoff=cutoff,
            limit=max(limit * 5, 25),
        )
        best_by_card: dict[int, tuple[float, int]] = {}
        for _match, score, index in raw_matches:
            card_id = self._card_ids[index]
            current_best = best_by_card.get(card_id)
            if current_best is None or score > current_best[0]:
                best_by_card[card_id] = (score, index)

        ranked = sorted(best_by_card.items(), key=lambda item: item[1][0], reverse=True)
        return [
            NameCandidate(
                card_id=card_id,
                name=self._names[index],
                score=score / 100.0,
                tier=4,
                language=self._languages[index],
            )
            for card_id, (score, index) in ranked[:limit]
        ]


class CandidateFinder:
    """Percorre a escada de tiers para um texto lido pelo OCR."""

    def __init__(
        self,
        session: Session,
        *,
        index: NameIndex | None = None,
        fuzzy_cutoff: int = 70,
    ) -> None:
        self.session = session
        self.index = index or NameIndex()
        self.fuzzy_cutoff = fuzzy_cutoff
        #: Quantas vezes cada tier **resolveu** uma leitura.
        self.tier_hits: dict[int, int] = {0: 0, 2: 0, 3: 0, 4: 0}
        #: Quantas vezes cada tier **rodou**, com ou sem sucesso.
        #: A distinção importa: o critério "o tier 4 só roda quando o tier 2
        #: falha" é sobre execução — contar apenas sucessos esconderia um tier
        #: caro rodando à toa e não achando nada.
        self.tier_attempts: dict[int, int] = {0: 0, 2: 0, 3: 0, 4: 0}

    def find(self, raw_name: str, *, limit: int = TOP_N) -> list[NameCandidate]:
        """Candidatos ordenados por score, do melhor para o pior."""
        if not raw_name or not raw_name.strip():
            return []

        strict = normalize_strict(raw_name)
        fuzzy = normalize_fuzzy(raw_name)
        if not _is_matchable(fuzzy):
            # Uma leitura que sobra quase nada depois de normalizar (ex.: OCR
            # embaralhou um nome só em kanji/hangul e deixou uma letra solta)
            # não carrega sinal de texto nenhum — qualquer candidato "achado"
            # aqui seria coincidência de RapidFuzz com uma string curta demais
            # para significar algo, nunca uma leitura real. A carta ainda pode
            # ser identificada pelo set code (`_from_code_only`, adiante).
            return []

        self.tier_attempts[0] += 1
        exact = self._tier0_exact(strict)
        if exact is not None:
            self.tier_hits[0] += 1
            return [exact]

        self.tier_attempts[2] += 1
        shortlist = self._tier2_fts(raw_name)
        if shortlist:
            self.tier_attempts[3] += 1
            candidates = self._tier3_rerank(fuzzy, shortlist, limit=limit)
            if candidates:
                self.tier_hits[2] += 1
                self.tier_hits[3] += 1
                return candidates

        # Só chega aqui quando o FTS não ajudou — o OCR errou o começo das
        # palavras e não há prefixo para casar.
        self.tier_attempts[4] += 1
        self.index.ensure_loaded(self.session)
        candidates = self.index.search(fuzzy, cutoff=self.fuzzy_cutoff, limit=limit)
        if candidates:
            self.tier_hits[4] += 1
        return candidates

    # --------------------------------------------------------------- tiers

    def _tier0_exact(self, strict_name: str) -> NameCandidate | None:
        """Igualdade exata: o caso mais comum e o mais barato."""
        if not _is_matchable(strict_name):
            return None
        row = self.session.execute(
            select(Card.id, Card.name).where(Card.name_normalized == strict_name).limit(1)
        ).first()
        if row is not None:
            return NameCandidate(card_id=row.id, name=row.name, score=1.0, tier=0, language="EN")

        # Sem igualdade em inglês: tenta um nome alternativo (FR/DE/IT/PT —
        # plano §7.1 multilíngue). O nome exibido continua o canônico.
        alt_row = self.session.execute(
            select(Card.id, Card.name, CardAltName.language)
            .join(CardAltName, CardAltName.card_id == Card.id)
            .where(CardAltName.name_normalized == strict_name)
            .limit(1)
        ).first()
        if alt_row is None:
            return None
        return NameCandidate(
            card_id=alt_row.id, name=alt_row.name, score=1.0, tier=0, language=alt_row.language
        )

    def _tier2_fts(self, raw_name: str) -> list[int]:
        """FTS5 devolve um conjunto pequeno de plausíveis, sem varrer o banco.

        Busca no nome canônico e nos alternativos (FR/DE/IT/PT) e une os dois
        conjuntos — uma carta fotografada em outro idioma só entra aqui pelo
        `card_alt_fts` (plano §7.1 multilíngue). Limite combinado igual ao de
        uma busca só, para o custo do tier 3 continuar previsível.
        """
        primary = search_card_ids(self.session, raw_name, limit=FTS_LIMIT)
        alt = search_alt_card_ids(self.session, raw_name, limit=FTS_LIMIT)
        if not alt:
            return primary

        combined = list(primary)
        seen = set(primary)
        for card_id in alt:
            if card_id not in seen:
                seen.add(card_id)
                combined.append(card_id)
        return combined[:FTS_LIMIT]

    def _tier3_rerank(
        self, fuzzy_query: str, card_ids: list[int], *, limit: int
    ) -> list[NameCandidate]:
        """Reordena os candidatos do FTS por similaridade real.

        O FTS ordena por bm25, que mede relevância de tokens — não distância de
        edição. É o rerank que sabe que `WH1TE` está a um caractere de `WHITE`.

        Um candidato pode ter vindo do FTS por um nome alternativo: comparar
        só contra o nome em inglês o descartaria (score baixo contra um
        idioma que não é o da leitura). O score de cada carta é o **máximo**
        entre o nome canônico e todos os nomes alternativos dela (plano §7.1
        multilíngue) — e o idioma da variante vencedora vira `language` do
        candidato, o palpite de em que idioma a carta física está impressa.
        """
        rows = self.session.execute(
            select(Card.id, Card.name, Card.name_normalized).where(Card.id.in_(card_ids))
        ).all()
        if not rows:
            return []

        alt_names: dict[int, list[tuple[str, str]]] = {}
        for alt in self.session.execute(
            select(
                CardAltName.card_id, CardAltName.language, CardAltName.name_normalized
            ).where(CardAltName.card_id.in_(card_ids))
        ):
            alt_names.setdefault(alt.card_id, []).append((alt.language, alt.name_normalized))

        scored: list[NameCandidate] = []
        for row in rows:
            variants = (("EN", row.name_normalized), *alt_names.get(row.id, ()))
            best_language, best_score = "EN", -1.0
            for language, variant in variants:
                variant_fuzzy = normalize_fuzzy(variant)
                if not _is_matchable(variant_fuzzy):
                    continue
                variant_score = fuzz.WRatio(fuzzy_query, variant_fuzzy)
                if variant_score > best_score:
                    best_score, best_language = variant_score, language
            if best_score >= self.fuzzy_cutoff:
                scored.append(
                    NameCandidate(
                        card_id=row.id,
                        name=row.name,
                        score=best_score / 100.0,
                        tier=3,
                        language=best_language,
                    )
                )
        scored.sort(key=lambda candidate: candidate.score, reverse=True)
        return scored[:limit]
