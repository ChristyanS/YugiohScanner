"""Motor de matching: texto lido → carta + print + decisão (plano §7).

Junta as três peças — candidatos por nome, resolução de set code e política de
confiança — e produz um veredito auditável: além do resultado, o motivo, os
scores e o top-5, para que a tela de revisão não precise re-rodar nada.

Uma escolha que atravessa o módulo: **o código validado tem prioridade sobre o
nome**. Um set code que existe no catálogo é um identificador; um nome lido por
OCR é uma aproximação. Quando os dois discordam, ninguém ganha sozinho — vai
para revisão manual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.tables import Card
from ..domain.confidence import (
    ConfidenceInput,
    ConfidenceThresholds,
    Decision,
    compute_confidence,
    decide,
)
from ..logging_setup import get_logger
from .candidates import CandidateFinder, NameCandidate, NameIndex
from .passcode import resolve_passcode
from .resolver import CodeResolution, PrintResolver

log = get_logger(__name__)

#: Distância máxima de similaridade para um candidato ser promovido pelo set
#: code. Acima disso, o nome foi lido com clareza suficiente para que a
#: divergência conte como conflito de verdade.
_PROMOTION_MAX_GAP = 0.10


def _identity_candidate(card_id: int, name: str | None, *, marker: str) -> dict[str, Any]:
    """Candidato sintético para uma identidade resolvida por código/passcode
    que conflita com o nome (ou não tem nome nenhum para comparar).

    Achado real do usuário: quando código/passcode identificam uma carta que
    não está entre os candidatos de nome (fuzzy), o `MatchResult.candidates`
    ficava só com os candidatos de nome — a carta certa nunca aparecia como
    opção na revisão, mesmo o sistema já sabendo qual era. `marker` deixa
    claro na própria lista que veio de uma leitura mais forte que texto
    aproximado, não de coincidência textual.
    """
    label = f"{name} ({marker})" if name else f"Carta #{card_id} ({marker})"
    return {"card_id": card_id, "name": label, "score": 1.0, "tier": None, "language": None}


def _ensure_identity_present(
    candidate_dicts: list[dict[str, Any]], card_id: int, name: str | None, *, marker: str
) -> list[dict[str, Any]]:
    """Garante que `card_id` apareça na lista — na frente, por ser a
    evidência mais forte — sem duplicar se ele já estiver lá (achado real:
    quando o código promove um candidato já competitivo, ele já está na
    lista; só falta quando a carta certa nem aparece no fuzzy de nome)."""
    if any(c["card_id"] == card_id for c in candidate_dicts):
        return candidate_dicts
    return [_identity_candidate(card_id, name, marker=marker), *candidate_dicts]


@dataclass
class MatchResult:
    """Tudo o que se sabe sobre uma leitura, pronto para virar `ScanResult`."""

    card_id: int | None = None
    card_print_id: int | None = None
    card_name: str | None = None
    set_code: str | None = None

    name_score: float = 0.0
    code_score: float = 0.0
    confidence: float = 0.0
    margin: float = 0.0

    decision: Decision = Decision.UNMATCHED
    reason: str = ""
    #: Top-5 candidatos com score — o que a revisão manual exibe.
    candidates: list[dict[str, Any]] = field(default_factory=list)
    #: Prints possíveis quando o código é ambíguo (mesma carta, raridades).
    print_options: list[dict[str, Any]] = field(default_factory=list)
    tier: int | None = None
    #: Nome e set code lidos apontam para a mesma carta? Espelha
    #: `ConfidenceInput.agreement` (plano §7.4) — guardado aqui para que quem
    #: recebe só o `MatchResult` (ex.: `scripts/calibrate_thresholds.py`)
    #: consiga reconstruir a decisão sem recalcular o matching inteiro.
    agreement: bool | None = None
    #: Idioma do candidato de nome vencedor (`NameCandidate.language`) — o
    #: palpite de em que idioma a carta física está impressa. `None` quando
    #: não há candidato de nome (`_from_code_only`: só o set code foi lido).
    matched_language: str | None = None
    #: A identidade veio do passcode ("Card ID") validado contra o catálogo —
    #: a fonte mais forte que existe, mais forte que nome ou set code
    #: (`_from_passcode`). `False` quando a identidade veio do caminho normal
    #: de nome/código, mesmo que o passcode tenha sido lido mas não validado.
    passcode_verified: bool = False

    @property
    def matched(self) -> bool:
        return self.card_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "card_print_id": self.card_print_id,
            "card_name": self.card_name,
            "set_code": self.set_code,
            "name_score": round(self.name_score, 4),
            "code_score": round(self.code_score, 4),
            "confidence": round(self.confidence, 4),
            "margin": round(self.margin, 4),
            "decision": self.decision.value,
            "reason": self.reason,
            "tier": self.tier,
            "candidates": self.candidates,
            "print_options": self.print_options,
            "matched_language": self.matched_language,
            "passcode_verified": self.passcode_verified,
        }


class MatchingEngine:
    """Resolve uma leitura de OCR contra o catálogo local."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        index: NameIndex | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.thresholds = ConfidenceThresholds.from_settings(settings)
        self.finder = CandidateFinder(session, index=index, fuzzy_cutoff=settings.fuzzy_cutoff)
        self.resolver = PrintResolver(session)

    @property
    def tier_hits(self) -> dict[int, int]:
        """Quantas leituras foram resolvidas em cada tier (diagnóstico)."""
        return self.finder.tier_hits

    def log_tier_stats(self) -> dict[str, int]:
        """Emite as estatísticas da escada de tiers e as devolve.

        É como se verifica, em produção, que o tier 4 (fuzzy sobre o catálogo
        inteiro) continua sendo exceção e não regra — se ele começar a dominar,
        alguma coisa quebrou no FTS ou na normalização.
        """
        stats = {
            f"tier{tier}_{kind}": counters[tier]
            for kind, counters in (
                ("attempts", self.finder.tier_attempts),
                ("hits", self.finder.tier_hits),
            )
            for tier in sorted(counters)
        }
        log.info("matching.tier_stats", **stats)
        return stats

    def match(
        self,
        raw_name: str,
        raw_code: str | None = None,
        raw_passcode: str | None = None,
    ) -> MatchResult:
        """Casa nome, código e passcode lidos com o catálogo.

        O passcode ("Card ID", canto inferior-esquerdo) é a chave primária
        real do catálogo (`Card.id`) — quando lido e validado
        (`matching/passcode.py`), decide a identidade da carta sozinho
        (`_from_passcode`), com nome/código entrando só como checagem de
        conflito e resolução de print. Ausente ou não validado, cai
        exatamente no caminho de sempre (nome/código) — fallback, não
        substituição.
        """
        code = self.resolver.resolve(raw_code)
        candidates = self.finder.find(raw_name)
        passcode_card_id = resolve_passcode(self.session, raw_passcode)

        if passcode_card_id is not None:
            return self._from_passcode(passcode_card_id, candidates, code)

        if not candidates and not code.validated:
            return MatchResult(
                decision=Decision.UNMATCHED,
                reason="nem o nome nem o set code casaram com o catálogo",
                code_score=code.score,
                set_code=code.matched_code,
            )

        # Sem nome utilizável, mas com código válido: o código identifica.
        if not candidates:
            return self._from_code_only(code)

        candidates, agreement = self._reconcile(candidates, code)
        best = candidates[0]
        margin = best.score - candidates[1].score if len(candidates) > 1 else 1.0

        data = ConfidenceInput(
            name_score=best.score,
            code_score=code.score,
            margin=margin,
            agreement=agreement,
            has_candidate=True,
        )
        confidence = compute_confidence(data, self.thresholds)
        decision, reason = decide(data, self.thresholds, confidence)

        card_id, print_id, card_name = self._pick_target(best, code, agreement)

        candidate_dicts = [candidate.as_dict() for candidate in candidates]
        if agreement is False and code.card_id is not None:
            # Conflito de verdade: o código aponta para uma carta que nem
            # aparece entre os candidatos de nome (`_reconcile`) — sem isto,
            # a carta certa nunca vira opção na revisão, só as adivinhações
            # por texto (achado real do usuário).
            code_card_name = self.session.scalar(
                select(Card.name).where(Card.id == code.card_id)
            )
            candidate_dicts = _ensure_identity_present(
                candidate_dicts, code.card_id, code_card_name, marker="código"
            )

        return MatchResult(
            card_id=card_id,
            card_print_id=print_id,
            card_name=card_name,
            set_code=code.matched_code,
            name_score=best.score,
            code_score=code.score,
            confidence=confidence,
            margin=margin,
            decision=decision,
            reason=reason,
            candidates=candidate_dicts,
            print_options=self._print_options(card_id, code),
            tier=best.tier,
            agreement=agreement,
            # O código impresso é o sinal de idioma mais direto que existe —
            # tem prioridade sobre o nome (plano de idiomas, continuação:
            # docs/adr/0009) quando os dois estão disponíveis.
            matched_language=code.detected_region or best.language,
        )

    # ------------------------------------------------------------- internos

    def _reconcile(
        self, candidates: list[NameCandidate], code: CodeResolution
    ) -> tuple[list[NameCandidate], bool | None]:
        """Cruza as duas evidências e, se elas convergirem, reordena.

        O ponto sutil: o set code não precisa concordar com o **primeiro**
        candidato para ser confirmação. Caso real do catálogo — o OCR lê
        `RED-EYES B DRAGON` e a similaridade textual põe "Red-Eyes Baby Dragon"
        (0,914) à frente de "Red-Eyes Black Dragon" (0,889), enquanto o código
        `LOB-070` aponta sem ambiguidade para a segunda. Comparar só com o
        top-1 chamaria isso de conflito e mandaria para revisão manual; o certo
        é reconhecer que duas evidências independentes convergiram e promover
        aquele candidato.

        Conflito de verdade é quando a carta do código **não aparece** entre os
        candidatos plausíveis do nome.

        `None` quando não há código validado — ausência de evidência não é
        evidência de conflito.
        """
        code_card = code.card_id
        if code_card is None:
            return candidates, None

        for position, candidate in enumerate(candidates):
            if candidate.card_id != code_card:
                continue
            if position == 0:
                return candidates, True
            # Guarda: só promovemos um candidato que já era textualmente
            # competitivo. Sem isso, um código lido errado poderia arrastar
            # uma carta mal classificada para cima de um nome lido com clareza.
            if candidates[0].score - candidate.score > _PROMOTION_MAX_GAP:
                return candidates, False
            log.debug(
                "matching.candidate_promoted",
                promoted=candidate.name,
                over=candidates[0].name,
                by_code=code.matched_code,
            )
            reordered = [candidate, *candidates[:position], *candidates[position + 1 :]]
            return reordered, True

        return candidates, False

    def _pick_target(
        self, best: NameCandidate, code: CodeResolution, agreement: bool | None
    ) -> tuple[int | None, int | None, str | None]:
        """Escolhe carta e print finais.

        Em conflito devolvemos a carta do **nome**, não a do código: é ela que a
        tela de revisão mostra como primeira opção, e a decisão já é `MANUAL`,
        então nada entra na coleção sem alguém olhar.
        """
        if agreement is True:
            return best.card_id, code.print_id, best.name
        return best.card_id, None, best.name

    def _from_passcode(
        self,
        passcode_card_id: int,
        candidates: list[NameCandidate],
        code: CodeResolution,
    ) -> MatchResult:
        """Identidade confirmada por chave exata — a evidência mais forte que
        existe, mais forte que nome ou set code (pedido do usuário: o
        passcode é um identificador direto, não uma aproximação fuzzy).

        Conflito de verdade é quando nome ou código, lidos independentemente,
        apontam para **outra** carta — mesma filosofia de `_reconcile`: duas
        evidências discordando nunca decidem sozinhas, mesmo com uma delas
        sendo tão forte quanto o passcode (ele também pode ter sido lido
        errado, ainda que a validação contra o catálogo torne isso raro).
        """
        name_conflict = bool(candidates) and candidates[0].card_id != passcode_card_id
        code_conflict = code.card_id is not None and code.card_id != passcode_card_id
        # O código também confirma a mesma carta que o passcode — duas
        # evidências validadas e independentes concordando. Achado real do
        # usuário (Dragão Agave, SOFU-PT048): nome ilegível ("D R C GA VF")
        # casou por acaso com "D/D/D Contract Change" a 85,5% de
        # similaridade — score alto o bastante para nunca ser filtrado por
        # um limiar, mas ainda assim ruído de um texto quase sem conteúdo.
        # Um nome divergente não pode derrubar duas leituras fortes que já
        # concordam entre si; só um CÓDIGO divergente (evidência tão forte
        # quanto o passcode) é conflito de verdade aqui.
        code_confirms = code.card_id is not None and code.card_id == passcode_card_id
        card_name = self.session.scalar(select(Card.name).where(Card.id == passcode_card_id))
        real_name_score = candidates[0].score if candidates else 0.0

        if code_conflict or (name_conflict and not code_confirms):
            data = ConfidenceInput(
                name_score=real_name_score,
                code_score=code.score,
                margin=1.0,
                agreement=False,
                has_candidate=True,
            )
            confidence = compute_confidence(data, self.thresholds)
            # O passcode é a evidência mais forte que existe — mesmo em
            # conflito, ele precisa aparecer como opção na revisão, na
            # frente dos candidatos de nome (achado real do usuário: Access
            # Code Talker/Dragão Agave identificados pelo Card ID, mas
            # ausentes da lista de candidatos, que só mostrava adivinhações
            # de texto sem nenhuma relação com a carta de verdade).
            candidate_dicts = _ensure_identity_present(
                [candidate.as_dict() for candidate in candidates],
                passcode_card_id,
                card_name,
                marker="Card ID",
            )
            return MatchResult(
                card_id=passcode_card_id,
                card_print_id=None,
                card_name=card_name,
                set_code=code.matched_code,
                name_score=real_name_score,
                code_score=code.score,
                confidence=confidence,
                margin=1.0,
                decision=Decision.MANUAL,
                reason="passcode diverge do nome/código lidos",
                candidates=candidate_dicts,
                print_options=self._print_options(passcode_card_id, code),
                tier=candidates[0].tier if candidates else None,
                agreement=False,
            )

        # Sem conflito: identidade confirmada por chave exata. Modelado como
        # `name_score=1.0, agreement=True` de propósito — reaproveita
        # `compute_confidence`/`decide` (domain/confidence.py, puro, 98%
        # coberto) em vez de duplicar a lógica de decisão para um segundo
        # caminho de confiança paralelo. `name_score`/`margin` gravados no
        # `MatchResult` continuam sendo os valores reais (auditoria) — só o
        # `ConfidenceInput` usa a substituição. Print/set continuam vindo só
        # do código: o passcode nunca resolve set/raridade (o mesmo passcode
        # existe em toda reimpressão da carta).
        print_id = code.print_id if code.card_id == passcode_card_id else None
        data = ConfidenceInput(
            name_score=1.0,
            code_score=code.score,
            margin=1.0,
            agreement=True,
            has_candidate=True,
        )
        confidence = compute_confidence(data, self.thresholds)
        decision, reason = decide(data, self.thresholds, confidence)

        return MatchResult(
            card_id=passcode_card_id,
            card_print_id=print_id,
            card_name=card_name,
            set_code=code.matched_code,
            name_score=real_name_score,
            code_score=code.score,
            confidence=confidence,
            margin=1.0,
            decision=decision,
            reason=f"Card ID confirmado — {reason}",
            candidates=[candidate.as_dict() for candidate in candidates],
            print_options=self._print_options(passcode_card_id, code),
            tier=candidates[0].tier if candidates else None,
            agreement=True,
            matched_language=code.detected_region
            or (candidates[0].language if candidates else None),
            passcode_verified=True,
        )

    def _from_code_only(self, code: CodeResolution) -> MatchResult:
        """Nome ilegível, código válido: o código carrega a identificação."""
        card_id = code.card_id
        card_name = None
        if card_id is not None:
            card_name = self.session.scalar(select(Card.name).where(Card.id == card_id))

        data = ConfidenceInput(
            name_score=0.0,
            code_score=code.score,
            margin=1.0,
            agreement=None,
            has_candidate=card_id is not None,
        )
        # A decisão aqui é fixa, não vem de `decide`: sem o nome não existe
        # segunda evidência, então por mais que o código seja válido a leitura
        # sempre passa por confirmação humana. O score serve só para informar.
        confidence = compute_confidence(data, self.thresholds)

        return MatchResult(
            card_id=card_id,
            card_print_id=code.print_id,
            card_name=card_name,
            set_code=code.matched_code,
            name_score=0.0,
            code_score=code.score,
            confidence=confidence,
            margin=1.0,
            # O set code identifica, mas sem o nome não há segunda evidência —
            # sempre passa por confirmação humana.
            decision=Decision.MANUAL if card_id else Decision.UNMATCHED,
            reason=(
                "só o set code foi lido; confirme a carta" if card_id else "nada legível na imagem"
            ),
            print_options=self._print_options(card_id, code),
            matched_language=code.detected_region,
        )

    def _print_options(self, card_id: int | None, code: CodeResolution) -> list[dict[str, Any]]:
        """Prints entre os quais o usuário precisa escolher, se houver mais de um."""
        if card_id is None or len(code.prints) <= 1:
            return []
        return [
            print_match.as_dict() for print_match in code.prints if print_match.card_id == card_id
        ]
