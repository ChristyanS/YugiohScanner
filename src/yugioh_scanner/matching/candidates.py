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
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.fts import search_card_ids
from ..db.tables import Card
from ..domain.normalization import normalize_fuzzy, normalize_strict
from ..logging_setup import get_logger

log = get_logger(__name__)

#: Quantos candidatos o FTS devolve antes do reranking fuzzy.
FTS_LIMIT = 50

#: Quantos candidatos guardamos para a tela de revisão.
TOP_N = 5


@dataclass(frozen=True, slots=True)
class NameCandidate:
    """Uma carta candidata, com o quanto o nome lido se parece com o dela."""

    card_id: int
    name: str
    #: 0..1
    score: float
    #: Em qual tier da escada este candidato apareceu.
    tier: int

    def as_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "score": round(self.score, 4),
            "tier": self.tier,
        }


class NameIndex:
    """Catálogo de nomes em memória, para o fuzzy do tier 4.

    ~14.500 nomes ≈ 1,5 MB. Carregado uma vez por processo e reaproveitado; sem
    isso, cada leitura difícil faria uma varredura no banco.

    O índice é invalidado por contagem de cartas: depois de um `sync` que
    adiciona cartas, ele se recarrega sozinho.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._card_ids: list[int] = []
        self._names: list[str] = []
        self._fuzzy_names: list[str] = []
        self._loaded_count: int | None = None

    def ensure_loaded(self, session: Session) -> None:
        current = session.scalar(select(func.count()).select_from(Card)) or 0
        if self._loaded_count == current:
            return
        with self._lock:
            if self._loaded_count == current:  # pragma: no cover - corrida
                return
            rows = session.execute(select(Card.id, Card.name, Card.name_normalized)).all()
            self._card_ids = [row.id for row in rows]
            self._names = [row.name for row in rows]
            # A forma fuzzy é derivada da estrita: uma única regra de
            # normalização vale para o catálogo e para o OCR (plano §7.1).
            self._fuzzy_names = [normalize_fuzzy(row.name_normalized) for row in rows]
            self._loaded_count = current
            log.debug("matching.index_loaded", cards=current)

    @property
    def size(self) -> int:
        return len(self._card_ids)

    def search(self, query: str, *, cutoff: int, limit: int = TOP_N) -> list[NameCandidate]:
        """Melhores correspondências no catálogo inteiro."""
        if not query or not self._fuzzy_names:
            return []
        matches = process.extract(
            query,
            self._fuzzy_names,
            scorer=fuzz.WRatio,
            score_cutoff=cutoff,
            limit=limit,
        )
        return [
            NameCandidate(
                card_id=self._card_ids[index],
                name=self._names[index],
                score=score / 100.0,
                tier=4,
            )
            for _match, score, index in matches
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
        if not fuzzy:
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
        row = self.session.execute(
            select(Card.id, Card.name).where(Card.name_normalized == strict_name).limit(1)
        ).first()
        if row is None:
            return None
        return NameCandidate(card_id=row.id, name=row.name, score=1.0, tier=0)

    def _tier2_fts(self, raw_name: str) -> list[int]:
        """FTS5 devolve um conjunto pequeno de plausíveis, sem varrer o banco."""
        return search_card_ids(self.session, raw_name, limit=FTS_LIMIT)

    def _tier3_rerank(
        self, fuzzy_query: str, card_ids: list[int], *, limit: int
    ) -> list[NameCandidate]:
        """Reordena os candidatos do FTS por similaridade real.

        O FTS ordena por bm25, que mede relevância de tokens — não distância de
        edição. É o rerank que sabe que `WH1TE` está a um caractere de `WHITE`.
        """
        rows = self.session.execute(
            select(Card.id, Card.name, Card.name_normalized).where(Card.id.in_(card_ids))
        ).all()
        if not rows:
            return []

        scored: list[NameCandidate] = []
        for row in rows:
            score = fuzz.WRatio(fuzzy_query, normalize_fuzzy(row.name_normalized))
            if score >= self.fuzzy_cutoff:
                scored.append(
                    NameCandidate(card_id=row.id, name=row.name, score=score / 100.0, tier=3)
                )
        scored.sort(key=lambda candidate: candidate.score, reverse=True)
        return scored[:limit]
