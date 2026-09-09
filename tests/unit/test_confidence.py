"""Score de confiança e política de decisão (plano §7.4 e §7.5).

Estes testes fixam o **comportamento** da política, não os números exatos: os
limiares serão recalibrados na Fase 9 com fotos reais, e os testes precisam
sobreviver a isso. Onde um número específico importa (as bordas dos limiares),
ele é construído a partir do próprio `ConfidenceThresholds`.
"""

from __future__ import annotations

import pytest

from yugioh_scanner.domain.confidence import (
    ConfidenceInput,
    ConfidenceThresholds,
    Decision,
    compute_confidence,
    decide,
)

T = ConfidenceThresholds()


def verdict(**kwargs: object) -> Decision:
    data = ConfidenceInput(**kwargs)  # type: ignore[arg-type]
    return decide(data, T)[0]


class TestScoreShape:
    def test_perfect_reading_scores_high(self) -> None:
        score = compute_confidence(
            ConfidenceInput(name_score=1.0, code_score=1.0, margin=1.0, agreement=True), T
        )
        assert score >= 0.95

    def test_agreement_raises_confidence_for_the_same_name_score(self) -> None:
        """Confirmação cruzada tem de valer alguma coisa, e sempre a mesma.

        Comparação feita com o **mesmo** score de nome: comparar nomes
        diferentes mediria outra coisa, e no topo da escala o teto de 1.0
        esconderia a diferença.
        """
        for name_score in (0.60, 0.75, 0.85):
            with_code = compute_confidence(
                ConfidenceInput(name_score=name_score, code_score=1.0, margin=0.3, agreement=True),
                T,
            )
            alone = compute_confidence(
                ConfidenceInput(name_score=name_score, margin=0.3, agreement=None), T
            )
            assert with_code > alone

    def test_ocr_quality_modulates_instead_of_adding(self) -> None:
        """Qualidade de OCR não pode virar bônus fixo (bug da 1ª versão).

        Um nome que casa 85% precisa ficar abaixo do limiar de automático,
        mesmo com o motor de OCR reportando confiança máxima.
        """
        good = compute_confidence(ConfidenceInput(name_score=0.85, margin=0.3, ocr_quality=1.0), T)
        poor = compute_confidence(ConfidenceInput(name_score=0.85, margin=0.3, ocr_quality=0.2), T)
        assert poor < good
        assert good < T.auto, "85% de similaridade não é automático"

    def test_conflict_is_capped_far_below_auto(self) -> None:
        """Nome perfeito + código de outra carta nunca pode virar automático."""
        score = compute_confidence(
            ConfidenceInput(name_score=1.0, code_score=1.0, margin=1.0, agreement=False), T
        )
        assert score < T.review

    def test_ambiguity_lowers_the_score(self) -> None:
        clear = compute_confidence(ConfidenceInput(name_score=0.95, margin=0.30), T)
        tied = compute_confidence(ConfidenceInput(name_score=0.95, margin=0.01), T)
        assert tied < clear

    def test_no_candidate_scores_zero(self) -> None:
        assert compute_confidence(ConfidenceInput(name_score=0.9, has_candidate=False), T) == 0.0

    def test_score_stays_in_range(self) -> None:
        for name in (0.0, 0.5, 1.0):
            for margin in (0.0, 0.5, 1.0):
                for agreement in (True, False, None):
                    score = compute_confidence(
                        ConfidenceInput(
                            name_score=name,
                            code_score=1.0,
                            margin=margin,
                            agreement=agreement,
                        ),
                        T,
                    )
                    assert 0.0 <= score <= 1.0


class TestDecisions:
    def test_excellent_reading_is_automatic(self) -> None:
        assert verdict(name_score=1.0, code_score=1.0, margin=1.0, agreement=True) == (
            Decision.AUTO
        )

    def test_good_name_alone_can_be_automatic(self) -> None:
        assert verdict(name_score=1.0, margin=0.5, agreement=None) == Decision.AUTO

    def test_medium_confidence_asks_for_confirmation(self) -> None:
        assert verdict(name_score=0.85, margin=0.30, agreement=None) == Decision.PENDING

    def test_low_confidence_goes_to_manual(self) -> None:
        assert verdict(name_score=0.55, margin=0.30, agreement=None) == Decision.MANUAL

    def test_no_candidate_is_unmatched(self) -> None:
        assert verdict(name_score=0.0, has_candidate=False) == Decision.UNMATCHED


class TestConflictNeverAutomates:
    """Critério de aceitação explícito da Fase 4."""

    @pytest.mark.parametrize("name_score", [0.70, 0.85, 0.95, 1.0])
    def test_conflict_always_goes_to_manual(self, name_score: float) -> None:
        assert (
            verdict(name_score=name_score, code_score=1.0, margin=1.0, agreement=False)
            == Decision.MANUAL
        )

    def test_the_reason_explains_the_conflict(self) -> None:
        _decision, reason = decide(
            ConfidenceInput(name_score=0.99, code_score=1.0, margin=1.0, agreement=False), T
        )
        assert "outra carta" in reason


class TestAmbiguityNeverAutomates:
    """`Cyber Dragon` vs `Cyber Dragon Core`: score alto, decisão insegura."""

    def test_tied_candidates_go_to_manual(self) -> None:
        assert verdict(name_score=0.96, margin=0.01, agreement=None) == Decision.MANUAL

    def test_high_score_without_margin_is_not_automatic(self) -> None:
        assert verdict(name_score=0.99, margin=0.02, agreement=None) != Decision.AUTO

    def test_same_score_with_margin_is_automatic(self) -> None:
        assert verdict(name_score=0.99, margin=0.40, agreement=None) == Decision.AUTO

    def test_reason_mentions_the_tie(self) -> None:
        _decision, reason = decide(ConfidenceInput(name_score=0.96, margin=0.01), T)
        assert "empatados" in reason


class TestThresholdBoundaries:
    """Bordas construídas a partir dos próprios limiares, não de literais."""

    def test_just_below_auto_is_pending(self) -> None:
        # Score puro de nome: confidence = 0.80*name + 0.20*ocr + bônus.
        assert verdict(name_score=T.auto - 0.15, margin=0.30, ocr_quality=0.5) != Decision.AUTO

    def test_margin_below_minimum_blocks_auto(self) -> None:
        assert verdict(name_score=1.0, margin=T.min_margin - 0.01, agreement=None) != Decision.AUTO

    def test_agreement_uses_a_lower_bar(self) -> None:
        """Concordância cruzada permite auto com score que sozinho não bastaria."""
        data = ConfidenceInput(name_score=0.75, code_score=1.0, margin=0.10, agreement=True)
        score = compute_confidence(data, T)
        assert score < T.auto, "o score sozinho não alcançaria o limiar normal"
        assert decide(data, T, score)[0] == Decision.AUTO


class TestCalibration:
    def test_thresholds_come_from_settings(self) -> None:
        from yugioh_scanner.config import Settings

        settings = Settings(confidence_auto=0.80, confidence_review=0.50)
        thresholds = ConfidenceThresholds.from_settings(settings)
        assert thresholds.auto == 0.80
        assert thresholds.review == 0.50

    def test_lowering_the_bar_promotes_decisions(self) -> None:
        """A calibração da Fase 9 precisa realmente mudar o comportamento."""
        data = ConfidenceInput(name_score=0.85, margin=0.30, agreement=None)
        strict = decide(data, ConfidenceThresholds(auto=0.99, review=0.95))[0]
        lenient = decide(data, ConfidenceThresholds(auto=0.60, review=0.40))[0]
        assert strict == Decision.MANUAL
        assert lenient == Decision.AUTO


class TestReasons:
    def test_every_decision_explains_itself(self) -> None:
        """Quem revisa precisa saber o porquê sem ler o código-fonte."""
        cases = [
            ConfidenceInput(name_score=1.0, code_score=1.0, margin=1.0, agreement=True),
            ConfidenceInput(name_score=0.85, margin=0.30),
            ConfidenceInput(name_score=0.40, margin=0.30),
            ConfidenceInput(name_score=0.0, has_candidate=False),
            ConfidenceInput(name_score=0.9, code_score=1.0, margin=1.0, agreement=False),
        ]
        for data in cases:
            _decision, reason = decide(data, T)
            assert reason and len(reason) > 5
