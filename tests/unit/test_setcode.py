"""Parsing de set codes (plano §8 e §7.2)."""

from __future__ import annotations

import pytest

from yugioh_scanner.domain.setcode import (
    SetCode,
    extract_prefix,
    looks_like_set_code,
    normalize_set_code,
    parse_set_code,
)


class TestRealFormats:
    """Todos estes existem de verdade — nenhum formato foi inventado."""

    @pytest.mark.parametrize(
        ("raw", "prefix", "region", "number"),
        [
            ("LOB-001", "LOB", None, "001"),
            ("SDK-001", "SDK", None, "001"),
            ("MAGO-EN001", "MAGO", "EN", "001"),
            ("RA01-EN001", "RA01", "EN", "001"),
            ("MP24-EN001", "MP24", "EN", "001"),
            ("CT13-EN008", "CT13", "EN", "008"),
            ("KC01-EN000", "KC01", "EN", "000"),
            ("MVP1-ENV04", "MVP1", "EN", "V04"),
            ("LOB-FR001", "LOB", "FR", "001"),
            ("SDK-DE001", "SDK", "DE", "001"),
            ("BLAR-JP001", "BLAR", "JP", "001"),
            ("STAX-EN001", "STAX", "EN", "001"),
        ],
    )
    def test_decomposition(self, raw: str, prefix: str, region: str | None, number: str) -> None:
        parsed = parse_set_code(raw)
        assert parsed is not None
        assert (parsed.prefix, parsed.region, parsed.number) == (prefix, region, number)

    def test_normalized_roundtrips(self) -> None:
        for raw in ["LOB-001", "MAGO-EN001", "MVP1-ENV04", "CT13-EN008"]:
            assert normalize_set_code(raw) == raw


class TestCleaning:
    @pytest.mark.parametrize(
        "raw",
        ["lob-001", " LOB-001 ", "LOB–001", "LOB—001", "LOB−001", "LOB-001.", "|LOB-001|"],
    )
    def test_dirty_input_normalizes_to_the_same_code(self, raw: str) -> None:
        """Traços tipográficos e lixo de OCR nas bordas não podem atrapalhar."""
        assert normalize_set_code(raw) == "LOB-001"

    def test_missing_separator_is_recovered(self) -> None:
        """O OCR come o hífen com frequência."""
        parsed = parse_set_code("LOB001")
        assert parsed is not None
        assert parsed.normalized == "LOB-001"


class TestRejection:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "Blue-Eyes",
            "Blue-Eyes White Dragon",
            "DRAGON",
            "12",
            "ATK/2500",
            "-",
        ],
    )
    def test_non_codes_are_rejected(self, raw: str) -> None:
        """Melhor não identificar o set do que inventar um (plano §8)."""
        assert parse_set_code(raw) is None
        assert not looks_like_set_code(raw)

    def test_words_with_hyphen_but_no_digits_are_rejected(self) -> None:
        """`BLUE-EYES` casa a forma da regex; só os dígitos o desqualificam."""
        assert parse_set_code("BLUE-EYES") is None

    def test_unparsable_normalizes_to_cleaned_text_not_invented_structure(self) -> None:
        assert normalize_set_code("qualquer coisa") == "QUALQUERCOISA"
        assert normalize_set_code("JUMP") == "JUMP"


class TestRealCatalogCases:
    """Casos encontrados nos 44.517 prints reais do catálogo YGOPRODeck.

    Cada um destes já quebrou o parser em algum momento — ficam aqui para não
    quebrarem de novo.
    """

    def test_special_edition_is_not_a_region(self) -> None:
        """`IOC-SE1` = Special Edition 1, não região "S" + número "E1"."""
        parsed = parse_set_code("IOC-SE1")
        assert parsed is not None
        assert parsed.region is None
        assert parsed.number == "SE1"

    def test_token_suffix_parses(self) -> None:
        """`SR03-ENTKN` é um token: o número não tem dígito nenhum."""
        parsed = parse_set_code("SR03-ENTKN")
        assert parsed is not None
        assert (parsed.prefix, parsed.region, parsed.number) == ("SR03", "EN", "TKN")

    def test_early_european_single_letter_region(self) -> None:
        """`PSV-E088` usa a região de uma letra dos prints europeus antigos."""
        parsed = parse_set_code("PSV-E088")
        assert parsed is not None
        assert (parsed.region, parsed.number) == ("E", "088")

    def test_unknowable_number_is_rejected(self) -> None:
        """A própria API traz `MF03-EN0??`; guardar como texto é o certo."""
        assert parse_set_code("MF03-EN0??") is None

    def test_bare_set_prefix_is_not_a_print_code(self) -> None:
        """`DB5` e `DB9` aparecem em `card_sets[]` sem número de carta."""
        assert parse_set_code("DB5") is None
        assert parse_set_code("DB9") is None


class TestRegionAmbiguity:
    def test_region_is_not_stolen_from_the_number(self) -> None:
        """`LOB-001` não pode virar região "0"."""
        parsed = parse_set_code("LOB-001")
        assert parsed is not None
        assert parsed.region is None

    def test_longer_region_wins(self) -> None:
        """`EN` antes de `E`: senão `EN001` viraria região E + número N001."""
        parsed = parse_set_code("MAGO-EN001")
        assert parsed is not None
        assert parsed.region == "EN"
        assert parsed.number == "001"


class TestHelpers:
    def test_extract_prefix(self) -> None:
        assert extract_prefix("MP24-EN001") == "MP24"
        assert extract_prefix("não é código") is None

    def test_setcode_str_is_the_normalized_form(self) -> None:
        parsed = parse_set_code("mago-en001")
        assert parsed is not None
        assert str(parsed) == "MAGO-EN001"

    def test_is_frozen(self) -> None:
        parsed = SetCode(raw="LOB-001", prefix="LOB", region=None, number="001")
        with pytest.raises(AttributeError):
            parsed.prefix = "XXX"  # type: ignore[misc]
