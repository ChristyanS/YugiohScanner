"""Perfis de exportação (plano §15 e Fase 7).

Golden files por perfil, injeção de CSV e o parser do perfil `full` — tudo
com `CollectionRow` sintético, sem tocar banco (plano §19: exporters é
`zero I/O`, o ORM entra só em `services/export_service.py`).
"""

from __future__ import annotations

from io import StringIO

import pytest

from yugioh_scanner.exporters import DEFAULT_PROFILE, create_profile, known_profiles
from yugioh_scanner.exporters.base import CollectionRow, is_blank, sanitize_csv_cell
from yugioh_scanner.exporters.full import COLUMNS as FULL_COLUMNS
from yugioh_scanner.exporters.full import parse_full_csv
from yugioh_scanner.exporters.text import build_deck, build_list
from yugioh_scanner.exporters.ygopocket import build_csv as build_ygopocket_csv
from yugioh_scanner.exporters.ygopocket import build_txt as build_ygopocket_txt
from yugioh_scanner.exporters.ygoprodeck import build as build_ygoprodeck

BLUE_EYES = CollectionRow(
    item_id=1,
    card_id=89631139,
    card_name="Blue-Eyes White Dragon",
    quantity=2,
    condition="Near Mint",
    edition="1st Edition",
    language="EN",
    notes=None,
    source="manual",
    added_at="2026-09-09T12:00:00",
    card_print_id=4316,
    set_code_full="LOB-001",
    set_prefix="LOB",
    region=None,
    set_name="Legend of Blue Eyes White Dragon",
    rarity="Ultra Rare",
)

UNRESOLVED = CollectionRow(
    item_id=2,
    card_id=71413901,
    card_name="Time Wizard",
    quantity=1,
    condition="Near Mint",
    edition="Unlimited",
    language="EN",
    notes=None,
    source="scan",
    added_at="2026-09-09T12:05:00",
)

INJECTION = CollectionRow(
    item_id=3,
    card_id=1,
    card_name="=cmd|'/c calc'!A1",
    quantity=1,
    condition="Near Mint",
    edition="Unlimited",
    language="EN",
    notes="@SUM(A1:A9)",
    source="manual",
    added_at="2026-09-09T12:10:00",
)


def _render(profile: object, rows: list[CollectionRow]) -> str:
    buffer = StringIO(newline="")
    profile.render(rows, buffer)  # type: ignore[attr-defined]
    return buffer.getvalue()


class TestYgoProDeckProfile:
    def test_golden_header_and_row(self) -> None:
        text = _render(build_ygoprodeck(), [BLUE_EYES])
        assert text == (
            "Card Name,Card Quantity,Card Rarity,Card Condition,Card Edition,"
            "Card Set,Card Set Code\n"
            "Blue-Eyes White Dragon,2,Ultra Rare,Near Mint,1st Edition,"
            "Legend of Blue Eyes White Dragon,LOB-001\n"
        )

    def test_no_cardid_column(self) -> None:
        profile = build_ygoprodeck()
        assert "cardid" not in [c.lower() for c in profile.columns]
        assert len(profile.columns) == 7

    def test_unresolved_item_exports_with_empty_set_fields(self) -> None:
        text = _render(build_ygoprodeck(), [UNRESOLVED])
        row = text.splitlines()[1]
        assert row == "Time Wizard,1,,Near Mint,Unlimited,,"


class TestYgoPocketCsvProfile:
    def test_golden_header_matches_real_export_byte_for_byte(self) -> None:
        """Cabeçalho verificado contra `export-samples/Minha-cole-o.csv` (plano §0.4)."""
        text = _render(build_ygopocket_csv(), [])
        header = text.split("\r\n", 1)[0]
        assert header == (
            "card_id,card_name,quantity,set_code,rarity,art_variant,variant_label,"
            "condition,language,printing_region,notes,storage_location,"
            "grading_company,grade,certification_number,subgrade_centering,"
            "subgrade_corners,subgrade_edges,subgrade_surface,sealed,signed,"
            "altered,source,edition,purchase_price,market_value_override"
        )

    def test_encoding_and_newline_match_the_real_app(self) -> None:
        profile = build_ygopocket_csv()
        assert profile.encoding == "utf-8-sig"
        assert profile.newline == "\r\n"

    def test_condition_is_abbreviated(self) -> None:
        text = _render(build_ygopocket_csv(), [BLUE_EYES])
        fields = text.splitlines()[1].split(",")
        assert fields[7] == "NM"

    def test_edition_only_emits_1st_edition(self) -> None:
        unlimited = UNRESOLVED
        text = _render(build_ygopocket_csv(), [unlimited])
        fields = text.splitlines()[1].split(",")
        assert fields[23] == ""

        first_edition_text = _render(build_ygopocket_csv(), [BLUE_EYES])
        fields = first_edition_text.splitlines()[1].split(",")
        assert fields[23] == "1st Edition"

    def test_card_id_is_the_passcode(self) -> None:
        text = _render(build_ygopocket_csv(), [BLUE_EYES])
        fields = text.splitlines()[1].split(",")
        assert fields[0] == "89631139"


class TestYgoPocketTxtProfile:
    def test_matches_real_sample_format(self) -> None:
        """`1 Flame Administrator`, sem cabeçalho, sem newline final (amostra real)."""
        text = _render(build_ygopocket_txt(), [BLUE_EYES])
        assert text == "2 Blue-Eyes White Dragon"

    def test_multiple_rows_join_with_lf_no_trailing_newline(self) -> None:
        text = _render(build_ygopocket_txt(), [BLUE_EYES, UNRESOLVED])
        assert text == "2 Blue-Eyes White Dragon\n1 Time Wizard"
        assert not text.endswith("\n")


class TestTextProfiles:
    def test_list_style_shows_set_and_rarity(self) -> None:
        text = _render(build_list(), [BLUE_EYES])
        assert text == "2x Blue-Eyes White Dragon [LOB-001] (Ultra Rare)"

    def test_list_style_marks_unresolved_set(self) -> None:
        text = _render(build_list(), [UNRESOLVED])
        assert "[set desconhecido]" in text

    def test_deck_style_is_one_line_per_copy(self) -> None:
        text = _render(build_deck(), [BLUE_EYES])
        assert text == "Blue-Eyes White Dragon\nBlue-Eyes White Dragon"

    def test_deck_and_ygopocket_txt_are_different_formats(self) -> None:
        """Documentado como intencional — não são o mesmo perfil (plano §15.4)."""
        deck_text = _render(build_deck(), [BLUE_EYES])
        pocket_text = _render(build_ygopocket_txt(), [BLUE_EYES])
        assert deck_text != pocket_text


class TestFullProfile:
    def test_round_trips_through_parse(self) -> None:
        profile = create_profile("csv", "full")
        text = _render(profile, [BLUE_EYES, UNRESOLVED])

        parsed = parse_full_csv(text)
        assert len(parsed) == 2
        assert parsed[0].card_id == BLUE_EYES.card_id
        assert parsed[0].card_print_id == BLUE_EYES.card_print_id
        assert parsed[1].card_print_id is None

    def test_parse_tolerates_blank_lines(self) -> None:
        profile = create_profile("csv", "full")
        text = _render(profile, [BLUE_EYES])
        text_with_blanks = f"\n{text}\n\n"
        parsed = parse_full_csv(text_with_blanks)
        assert len(parsed) == 1

    def test_parse_rejects_wrong_header(self) -> None:
        with pytest.raises(ValueError, match="Cabeçalho"):
            parse_full_csv("a,b,c\n1,2,3\n")

    def test_all_internal_ids_are_present(self) -> None:
        assert "item_id" in FULL_COLUMNS
        assert "card_id" in FULL_COLUMNS
        assert "card_print_id" in FULL_COLUMNS


class TestCsvInjection:
    @pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
    def test_sanitize_prefixes_formula_triggers(self, prefix: str) -> None:
        assert sanitize_csv_cell(f"{prefix}evil") == f"'{prefix}evil"

    def test_sanitize_leaves_normal_text_alone(self) -> None:
        assert sanitize_csv_cell("Blue-Eyes White Dragon") == "Blue-Eyes White Dragon"

    def test_sanitize_handles_empty_string(self) -> None:
        assert sanitize_csv_cell("") == ""

    def test_malicious_card_name_is_neutralized_in_every_csv_profile(self) -> None:
        """A célula deve ficar prefixada com `'` — nunca abrir com `=` puro."""
        for factory in (
            build_ygoprodeck,
            build_ygopocket_csv,
            lambda: create_profile("csv", "full"),
        ):
            text = _render(factory(), [INJECTION])
            row = text.splitlines()[1]
            fields = row.split(",")
            assert "'=cmd|'/c calc'!A1" in fields
            assert "=cmd|'/c calc'!A1" not in fields

    def test_malicious_notes_is_neutralized(self) -> None:
        text = _render(create_profile("csv", "full"), [INJECTION])
        assert "'@SUM" in text


class TestRegistry:
    def test_known_profiles_lists_every_registered_pair(self) -> None:
        pairs = known_profiles()
        assert ("csv", "ygoprodeck") in pairs
        assert ("csv", "ygopocket") in pairs
        assert ("csv", "full") in pairs
        assert ("txt", "list") in pairs
        assert ("txt", "deck") in pairs
        assert ("txt", "ygopocket") in pairs

    def test_known_profiles_filters_by_format(self) -> None:
        assert known_profiles("csv") == [
            ("csv", "full"),
            ("csv", "ygopocket"),
            ("csv", "ygoprodeck"),
        ]

    def test_create_profile_without_name_uses_the_default(self) -> None:
        assert create_profile("csv").name == DEFAULT_PROFILE["csv"]
        assert create_profile("txt").name == DEFAULT_PROFILE["txt"]

    def test_ygopocket_exists_as_both_csv_and_txt_with_different_shapes(self) -> None:
        csv_profile = create_profile("csv", "ygopocket")
        txt_profile = create_profile("txt", "ygopocket")
        assert csv_profile.columns != txt_profile.columns
        assert csv_profile.format == "csv"
        assert txt_profile.format == "txt"

    def test_unknown_profile_raises_with_available_list(self) -> None:
        from yugioh_scanner.errors import UnknownExportProfileError

        with pytest.raises(UnknownExportProfileError):
            create_profile("csv", "does-not-exist")


class TestIsBlank:
    @pytest.mark.parametrize("line", ["", "   ", "\t", "\n"])
    def test_blank_lines(self, line: str) -> None:
        assert is_blank(line) is True

    def test_non_blank_line(self) -> None:
        assert is_blank("card_id,card_name") is False
