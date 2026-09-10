"""`ExportService`: monta linhas do ORM e escreve em arquivo/string (Fase 7).

Usa o `catalog` compartilhado (mesma fixture de `test_collection_service.py`)
— o que este arquivo testa é a integração com o banco real (achatamento de
`CollectionItem`/`CardPrint`, escrita em disco, round-trip do perfil `full`),
não a formatação em si, que já tem suíte própria em `tests/unit/test_exporters.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text as sql_text

from yugioh_scanner.db.session import Database
from yugioh_scanner.exporters import import_full_csv
from yugioh_scanner.services.collection_service import CollectionService
from yugioh_scanner.services.export_service import ExportService

BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414


@pytest.fixture
def service(catalog: Database) -> Iterator[ExportService]:
    with catalog.session() as session:
        # CT13-EN008 é print único (sem ambiguidade de raridade) na fixture —
        # LOB-001 tem duas (plano §0.3.3) e não serve aqui.
        CollectionService(session).add_manual("Blue-Eyes White Dragon", set_code="CT13-EN008")
        CollectionService(session).add_manual("Dark Magician", quantity=3)  # sem set
        yield ExportService(session)


class TestBuildRows:
    def test_flattens_print_metadata(self, service: ExportService) -> None:
        rows = service.build_rows()
        blue_eyes = next(r for r in rows if r.card_id == BLUE_EYES)
        assert blue_eyes.set_code_full == "CT13-EN008"
        assert blue_eyes.rarity is not None
        assert blue_eyes.has_print is True

    def test_item_without_print_has_empty_set_fields(self, service: ExportService) -> None:
        rows = service.build_rows()
        dark_magician = next(r for r in rows if r.card_id == DARK_MAGICIAN)
        assert dark_magician.card_print_id is None
        assert dark_magician.set_code_full is None
        assert dark_magician.has_print is False


class TestExportToString:
    def test_skip_unresolved_omits_items_without_print(self, service: ExportService) -> None:
        full_text, _ = service.export_to_string(fmt="csv", profile_name="ygoprodeck")
        filtered_text, _ = service.export_to_string(
            fmt="csv", profile_name="ygoprodeck", skip_unresolved=True
        )
        assert "Dark Magician" in full_text
        assert "Dark Magician" not in filtered_text
        assert "Blue-Eyes White Dragon" in filtered_text

    def test_default_profile_used_when_name_omitted(self, service: ExportService) -> None:
        text, profile = service.export_to_string(fmt="csv")
        assert profile.name == "ygoprodeck"
        assert "Card Name" in text.splitlines()[0]


class TestExportToPath:
    def test_writes_the_file_with_the_profiles_encoding(
        self, service: ExportService, tmp_path: Path
    ) -> None:
        out = tmp_path / "export" / "collection.csv"
        report = service.export_to_path(out, fmt="csv", profile_name="ygopocket")

        assert out.exists()
        raw = out.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # BOM do perfil ygopocket
        assert b"\r\n" in raw
        assert report.rows_written == 2
        assert report.rows_skipped == 0
        assert report.path == out

    def test_skip_unresolved_is_reflected_in_the_report(
        self, service: ExportService, tmp_path: Path
    ) -> None:
        out = tmp_path / "collection.csv"
        report = service.export_to_path(
            out, fmt="csv", profile_name="ygoprodeck", skip_unresolved=True
        )
        assert report.rows_written == 1
        assert report.rows_skipped == 1


class TestFullProfileRoundTrip:
    """Round-trip na MESMA sessão do `service` — nunca misturar sessões aqui:
    o banco de teste é WAL, e uma segunda sessão manteria um snapshot antigo
    até seu próprio commit, mascarando o round-trip com dado desatualizado.
    """

    def test_export_then_import_restores_the_same_collection(self, service: ExportService) -> None:
        csv_text, _ = service.export_to_string(fmt="csv", profile_name="full")
        before = {(r.card_id, r.card_print_id): r.quantity for r in service.build_rows()}

        service.session.execute(sql_text("DELETE FROM collection_item"))
        stats = import_full_csv(service.session, csv_text)

        assert stats.rows_read == len(before)
        assert stats.items_created == len(before)

        after = {(r.card_id, r.card_print_id): r.quantity for r in service.build_rows()}
        assert after == before

    def test_importing_twice_sums_instead_of_duplicating(self, service: ExportService) -> None:
        csv_text, _ = service.export_to_string(fmt="csv", profile_name="full")
        before_total = sum(r.quantity for r in service.build_rows())

        import_full_csv(service.session, csv_text)

        after_total = sum(r.quantity for r in service.build_rows())
        assert after_total == before_total * 2
        assert service.repo.count_items() == 2  # mesmas duas linhas, quantidades somadas
