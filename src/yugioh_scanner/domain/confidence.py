"""Score de confiança e política de decisão (plano §7.4 e §7.5).

Função pura, sem I/O: recebe números, devolve um veredito. É o módulo mais
importante de calibrar (Fase 9) e o mais barato de testar.

A ideia central é que **score absoluto sozinho engana**. Um nome que casa 96%
com duas cartas diferentes ("Cyber Dragon" e "Cyber Dragon Core") é *menos*
confiável que um que casa 90% com uma só. Por isso a `margin` — a distância
para o segundo colocado — entra na decisão com peso próprio.

A segunda ideia é que **evidências independentes que concordam valem mais que
uma evidência forte**. Quando o set code lido aponta para a mesma carta que o
nome lido, isso é confirmação cruzada: duas leituras diferentes, da mesma foto,
chegando ao mesmo lugar. Vale mais que 96% de similaridade textual.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    """O que fazer com uma leitura."""

    #: Confiança alta e sem ambiguidade: entra na coleção sozinha.
    AUTO = "auto"
    #: Provavelmente certa: um clique para confirmar.
    PENDING = "pending"
    #: Precisa de olho humano — mostramos os candidatos e o OCR bruto.
    MANUAL = "manual"
    #: Nenhum candidato. Registrado, não descartado.
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class ConfidenceThresholds:
    """Limiares da política. Vêm de `Settings`, para serem calibráveis."""

    #: Acima disto (com margem) entra sozinha.
    auto: float = 0.93
    #: Abaixo disto exige revisão manual.
    review: float = 0.70
    #: Limiar reduzido quando nome e set code concordam entre si.
    agreement_auto: float = 0.88
    #: Margem mínima para considerar o primeiro colocado destacado.
    min_margin: float = 0.08
    #: Abaixo disto os dois primeiros estão praticamente empatados.
    ambiguous_margin: float = 0.05

    @classmethod
    def from_settings(cls, settings: object) -> ConfidenceThresholds:
        """Constrói a partir de `Settings` sem acoplar este módulo a ele."""
        defaults = cls()
        return cls(
            auto=float(getattr(settings, "confidence_auto", defaults.auto)),
            review=float(getattr(settings, "confidence_review", defaults.review)),
        )


@dataclass(frozen=True, slots=True)
class ConfidenceInput:
    """Tudo o que a política precisa saber sobre uma leitura."""

    #: Similaridade do melhor candidato de nome (0..1).
    name_score: float
    #: 1.0 = código validado no banco; 0.5 = casou a regex mas não validou;
    #: 0.0 = ausente.
    code_score: float = 0.0
    #: `name_score` do 1º menos o do 2º colocado.
    margin: float = 1.0
    #: O print do código pertence à carta do nome?
    #: True = confirmam; False = conflitam; None = não havia código.
    agreement: bool | None = None
    #: Confiança média que o motor de OCR reportou na região do nome.
    ocr_quality: float = 1.0
    #: Havia algum candidato?
    has_candidate: bool = True


#: Peso da confiança do OCR: no máximo isto pode ser descontado do score do
#: nome quando o motor reporta leitura ruim.
_OCR_QUALITY_WEIGHT = 0.15

#: Quanto a concordância entre nome e set code acrescenta.
_AGREEMENT_BONUS = 0.15

#: Teto aplicado quando nome e código apontam para cartas diferentes.
_CONFLICT_FACTOR = 0.45

#: Bônus por o primeiro colocado estar bem destacado dos demais.
_MARGIN_BONUS = 0.05
_MARGIN_BONUS_THRESHOLD = 0.15
#: Penalidade por empate técnico entre os dois primeiros.
_AMBIGUITY_PENALTY = 0.15


def compute_confidence(data: ConfidenceInput, thresholds: ConfidenceThresholds) -> float:
    """Combina as evidências em um número de 0 a 1."""
    if not data.has_candidate:
        return 0.0

    name = _clamp(data.name_score)
    code = _clamp(data.code_score)

    if data.agreement is False:
        # Conflito. O teto de 0.45 garante que isto nunca chegue perto do
        # limiar de auto, por melhor que a leitura do nome pareça.
        return _clamp(_CONFLICT_FACTOR * name)

    # A similaridade do nome é a evidência principal. A confiança que o motor
    # de OCR reportou **modula** esse valor — nunca soma a ele. (Somar era o
    # que a primeira versão fazia, e como o default é 1.0 virava um bônus fixo
    # de +0,20 que fazia um nome de 85% ser aceito automaticamente.)
    confidence = name * (1.0 - _OCR_QUALITY_WEIGHT + _OCR_QUALITY_WEIGHT * _clamp(data.ocr_quality))

    if data.agreement is True:
        # Confirmação cruzada: duas leituras independentes da mesma foto
        # chegando à mesma carta. É o que torna confiável um nome mediano.
        confidence += _AGREEMENT_BONUS * code

    if data.margin > _MARGIN_BONUS_THRESHOLD:
        confidence += _MARGIN_BONUS
    elif data.margin < thresholds.ambiguous_margin:
        confidence -= _AMBIGUITY_PENALTY

    return _clamp(confidence)


def decide(
    data: ConfidenceInput,
    thresholds: ConfidenceThresholds,
    confidence: float | None = None,
) -> tuple[Decision, str]:
    """Roteia a leitura. Devolve a decisão e o motivo, em português.

    O motivo vai para o log e para a tela de revisão: quando algo cai em manual,
    o usuário precisa saber *por quê* sem ler o código.
    """
    if not data.has_candidate:
        return Decision.UNMATCHED, "nenhum candidato encontrado"

    score = compute_confidence(data, thresholds) if confidence is None else confidence

    if data.agreement is False:
        return Decision.MANUAL, "o set code lido aponta para outra carta"

    if score >= thresholds.auto and data.margin >= thresholds.min_margin:
        return Decision.AUTO, f"confiança {score:.0%} com margem folgada"

    if data.agreement is True and score >= thresholds.agreement_auto:
        # Duas leituras independentes concordando é evidência mais forte que
        # similaridade textual alta — por isso o limiar aqui é menor.
        return Decision.AUTO, f"nome e set code concordam (confiança {score:.0%})"

    if data.margin < thresholds.ambiguous_margin:
        return Decision.MANUAL, "dois candidatos praticamente empatados"

    if score >= thresholds.review:
        return Decision.PENDING, f"confiança {score:.0%}: confirme antes de incluir"

    return Decision.MANUAL, f"confiança baixa ({score:.0%})"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
