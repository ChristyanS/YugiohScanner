"""Parsing de passcode (o "Card ID" impresso no canto inferior-esquerdo)."""

from __future__ import annotations

import pytest

from yugioh_scanner.domain.passcode import clean_passcode


class TestCleanPasscode:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("89631139", "89631139"),  # Blue-Eyes White Dragon
            ("46986414", "46986414"),  # Dark Magician
            ("55144522", "55144522"),  # Pot of Greed
            (" 89631139 ", "89631139"),
            ("8963-1139", None),  # hífen sobra fora do map de confusões
        ],
    )
    def test_valid_forms(self, raw: str, expected: str | None) -> None:
        assert clean_passcode(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("8963ll39", "89631139"),  # 'l' minúsculo -> "L" -> "1" após upper()
            ("8963II39", "89631139"),  # 'I' -> "1"
            ("O9631139", "09631139"),  # 'O' -> "0"
            ("896311S9", "89631159"),  # 'S' -> "5"
            ("8963B139", "89638139"),  # 'B' -> "8"
            ("896Z1139", "89621139"),  # 'Z' -> "2"
        ],
    )
    def test_corrects_letter_to_digit_confusions(self, raw: str, expected: str) -> None:
        assert clean_passcode(raw) == expected

    @pytest.mark.parametrize("raw", ["1234", "123456789", "", None])
    def test_rejects_wrong_length_or_empty(self, raw: str | None) -> None:
        assert clean_passcode(raw) is None

    def test_rejects_leftover_letters_outside_the_confusion_map(self) -> None:
        # 'X' não é uma confusão de dígito conhecida — sobra letra, rejeitado.
        assert clean_passcode("8963X139") is None

    def test_rejects_non_digit_garbage(self) -> None:
        assert clean_passcode("BLUE-EYES") is None


class TestDigitRunExtraction:
    """Achado real da calibração contra fotos de celular: `PASSCODE_ROI`
    precisou de folga vertical (variação de `detect_card_bounds` entre
    fotos), e essa folga às vezes pega a linha "1ª Edição"/copyright junto,
    que o OCR funde numa caixa só com o passcode (ex. "41739381 1Edicao").
    Extrai o maior run de dígitos em vez de rejeitar a leitura inteira."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("41739381 1Edicao", "41739381"),
            ("042802581tEditior", "04280258"),  # sem espaço nenhum entre os dois
            ("73898890 1Edicac", "73898890"),
            ("98630720 1Edica", "98630720"),
        ],
    )
    def test_extracts_the_passcode_from_a_merged_line(self, raw: str, expected: str) -> None:
        assert clean_passcode(raw) == expected

    def test_a_lone_lower_digit_run_never_wins_over_the_real_passcode(self) -> None:
        # "1ª Edição" sozinho não tem 5+ dígitos seguidos — não pode virar
        # "passcode" por acaso mesmo se aparecesse sem o número de verdade.
        assert clean_passcode("1a Edicao") is None

    def test_pure_digits_with_wrong_length_is_still_rejected_not_truncated(self) -> None:
        """Diferente do caso de texto misturado: 9 dígitos *seguidos*, sem
        nada mais, não deve ser "salvo" cortando para os primeiros 8 — isso
        inventaria um passcode a partir de um comprimento errado, não
        extrairia um sinal real de um vizinho conhecido (1ª Edição)."""
        assert clean_passcode("123456789") is None
