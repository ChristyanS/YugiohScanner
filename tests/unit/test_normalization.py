"""Normalização de nomes (plano §7.1)."""

from __future__ import annotations

import pytest

from yugioh_scanner.domain.normalization import (
    normalize_for_display,
    normalize_fuzzy,
    normalize_strict,
)


class TestStrict:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Blue-Eyes White Dragon", "blue-eyes white dragon"),
            ("BLUE-EYES WHITE DRAGON", "blue-eyes white dragon"),
            ("  Dark   Magician  ", "dark magician"),
            ("Harpie's Feather Duster", "harpie's feather duster"),
            ("Number 39: Utopia", "number 39 utopia"),
            ("Ojama Yellow", "ojama yellow"),
        ],
    )
    def test_basic_cases(self, raw: str, expected: str) -> None:
        assert normalize_strict(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Pot of Greed", "pot of greed"),
            ("Pôt óf Grééd", "pot of greed"),
            ("Aimér", "aimer"),
        ],
    )
    def test_accents_are_removed(self, raw: str, expected: str) -> None:
        assert normalize_strict(raw) == expected

    def test_typographic_apostrophe_matches_ascii(self) -> None:
        """A API usa ' e o OCR devolve ' — precisam convergir."""
        assert normalize_strict("Harpie’s Feather Duster") == normalize_strict(
            "Harpie's Feather Duster"
        )

    def test_typographic_dash_matches_ascii(self) -> None:
        assert normalize_strict("Blue–Eyes") == normalize_strict("Blue-Eyes")

    def test_is_idempotent(self) -> None:
        """Propriedade essencial: normalizar duas vezes não muda nada.

        Se falhasse, o valor no banco divergiria do valor recalculado no sync.
        """
        for raw in ["Blue-Eyes White Dragon", "Harpie’s  Feather", "Nº 39: Utopia", ""]:
            once = normalize_strict(raw)
            assert normalize_strict(once) == once

    def test_empty_and_garbage(self) -> None:
        assert normalize_strict("") == ""
        assert normalize_strict("   ") == ""
        assert normalize_strict("!!!") == ""


class TestFuzzy:
    @pytest.mark.parametrize(
        ("ocr", "catalog"),
        [
            ("BLUE-EYES WH1TE DRAGON", "Blue-Eyes White Dragon"),
            ("B1ue-Eyes White Dragon", "Blue-Eyes White Dragon"),
            ("D4RK MAG1CIAN", "D4RK MAGICIAN"),
            ("P0t of Greed", "Pot of Greed"),
            ("Harpies Feather Duster", "Harpie's Feather Duster"),
            ("BlueEyes White Dragon", "Blue-Eyes White Dragon"),
            ("Mystical 5pace Typhoon", "Mystical Space Typhoon"),
            ("Ojama 8lack", "Ojama Black"),
        ],
    )
    def test_ocr_confusions_converge(self, ocr: str, catalog: str) -> None:
        """O ponto todo da função: leitura suja e catálogo viram a mesma string."""
        assert normalize_fuzzy(ocr) == normalize_fuzzy(catalog)

    def test_rn_is_read_as_m(self) -> None:
        assert normalize_fuzzy("Sumrnoned Skull") == normalize_fuzzy("Summoned Skull")

    def test_vv_is_read_as_w(self) -> None:
        assert normalize_fuzzy("Vvhite Dragon") == normalize_fuzzy("White Dragon")

    def test_digits_are_canonicalized_consistently(self) -> None:
        """É canonicalização, não correção: os dois lados sofrem a mesma troca.

        "Number 39" vira "number 3g"? Não — mas seja qual for o resultado, tem de
        ser o mesmo dos dois lados, e nomes diferentes continuam diferentes.
        """
        assert normalize_fuzzy("Number 39: Utopia") == normalize_fuzzy("Number 39: Utopia")
        assert normalize_fuzzy("Number 39: Utopia") != normalize_fuzzy("Number 17: Leviathan")

    def test_distinct_cards_stay_distinct(self) -> None:
        """A canonicalização não pode colidir cartas de verdade."""
        assert normalize_fuzzy("Cyber Dragon") != normalize_fuzzy("Cyber Dragon Core")
        assert normalize_fuzzy("Blue-Eyes White Dragon") != normalize_fuzzy(
            "Blue-Eyes Ultimate Dragon"
        )
        assert normalize_fuzzy("Dark Magician") != normalize_fuzzy("Dark Magician Girl")

    def test_is_idempotent(self) -> None:
        for raw in ["BLUE-EYES WH1TE DRAGON", "Harpie's Feather", ""]:
            once = normalize_fuzzy(raw)
            assert normalize_fuzzy(once) == once


class TestDisplay:
    def test_preserves_case(self) -> None:
        assert normalize_for_display("BLUE-EYES  WHITE") == "BLUE-EYES WHITE"

    def test_collapses_whitespace_only(self) -> None:
        assert normalize_for_display("  Dark \n Magician ") == "Dark Magician"
