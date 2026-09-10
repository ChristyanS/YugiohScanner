"""Comando `review` e `scan --interactive` pela CLI real (Fase 6).

Usa um `FakeOCRProvider` roteirizado (como `test_cli_scan.py`) para produzir
pendências determinísticas, e o catálogo real sincronizado para o matching
resolver candidatos de verdade.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.factories import make_card_image
from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache
from yugioh_scanner.ocr.fake_provider import FakeOCRProvider
from yugioh_scanner.ocr.registry import register_provider, unregister_provider
from yugioh_scanner.scanner.worker import reset_provider

runner = CliRunner()

BLUE_EYES = 89631139

#: `--no-auto` força tudo para "pending", independente da qualidade da
#: leitura — é o jeito determinístico de produzir pendências para testar.
SCRIPT = {
    "IMG_001.jpg": {"name": "Blue-Eyes White Dragon", "code": "CT13-EN008"},
    "IMG_002.jpg": {"name": "Dark Magician", "code": "SDK-001"},
}


@pytest.fixture(autouse=True)
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_template: Path
) -> Iterator[None]:
    data_path = tmp_path / "data"
    monkeypatch.setenv("YGS_DATA_PATH", str(data_path))
    monkeypatch.setenv("YGS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("YGS_OCR_PROVIDER", "roteiro")
    reset_settings_cache()

    data_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(catalog_template, data_path / "yugioh.db")

    register_provider("roteiro", lambda _settings: FakeOCRProvider(SCRIPT))
    try:
        yield
    finally:
        unregister_provider("roteiro")
        reset_provider()
        reset_settings_cache()


@pytest.fixture
def cards_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cards"
    for filename, reading in SCRIPT.items():
        make_card_image(folder / filename, name=reading["name"], set_code=reading["code"])
    return folder


def scan_no_auto(folder: Path) -> None:
    result = runner.invoke(app, ["scan", str(folder), "--workers", "1", "--no-auto", "--json"])
    assert result.exit_code == 0, result.output


class TestReviewJson:
    def test_lists_pending_without_interacting(self, cards_folder: Path) -> None:
        scan_no_auto(cards_folder)
        result = runner.invoke(app, ["review", "--json"])
        payload = json.loads(result.stdout)
        assert len(payload) == 2
        assert {row["ocr_name"] for row in payload} == {
            "Blue-Eyes White Dragon",
            "Dark Magician",
        }

    def test_empty_queue(self) -> None:
        result = runner.invoke(app, ["review", "--json"])
        assert json.loads(result.stdout) == []


class TestReviewInteractive:
    def test_confirming_the_top_candidate_lands_in_the_collection(self, cards_folder: Path) -> None:
        scan_no_auto(cards_folder)
        result = runner.invoke(app, ["review"], input="1\n1\n")
        assert result.exit_code == 0

        collection = runner.invoke(app, ["collection", "list", "--json"])
        items = json.loads(collection.stdout)
        assert len(items) == 2

    def test_rejecting_leaves_the_collection_empty(self, cards_folder: Path) -> None:
        scan_no_auto(cards_folder)
        result = runner.invoke(app, ["review"], input="x\nx\n")
        assert result.exit_code == 0
        assert "rejeitadas" in result.output

        collection = runner.invoke(app, ["collection", "list", "--json"])
        assert json.loads(collection.stdout) == []

    def test_quit_stops_early(self, cards_folder: Path) -> None:
        scan_no_auto(cards_folder)
        result = runner.invoke(app, ["review"], input="q\n")
        assert result.exit_code == 0

        # Nada foi decidido: as duas continuam pendentes.
        pending = runner.invoke(app, ["review", "--json"])
        assert len(json.loads(pending.stdout)) == 2

    def test_confirmed_result_leaves_the_queue(self, cards_folder: Path) -> None:
        scan_no_auto(cards_folder)
        runner.invoke(app, ["review"], input="1\nx\n")

        pending = runner.invoke(app, ["review", "--json"])
        assert json.loads(pending.stdout) == []

    def test_empty_queue_says_nothing_to_review(self) -> None:
        result = runner.invoke(app, ["review"])
        assert result.exit_code == 0
        assert "Nada para revisar" in result.output


class TestScanInteractiveFlag:
    def test_reviews_right_after_the_scan(self, cards_folder: Path) -> None:
        result = runner.invoke(
            app,
            ["scan", str(cards_folder), "--workers", "1", "--no-auto", "--interactive"],
            input="1\n1\n",
        )
        assert result.exit_code == 0
        assert "Revisão" in result.output

        collection = runner.invoke(app, ["collection", "list", "--json"])
        assert len(json.loads(collection.stdout)) == 2

    def test_json_mode_never_prompts(self, cards_folder: Path) -> None:
        """`--interactive` com `--json` não pode travar esperando stdin."""
        result = runner.invoke(
            app,
            [
                "scan",
                str(cards_folder),
                "--workers",
                "1",
                "--no-auto",
                "--interactive",
                "--json",
            ],
        )
        assert result.exit_code == 0
        json.loads(result.stdout)

    def test_dry_run_never_prompts(self, cards_folder: Path) -> None:
        """Nada foi persistido em --dry-run: não há o que revisar."""
        result = runner.invoke(
            app,
            [
                "scan",
                str(cards_folder),
                "--workers",
                "1",
                "--no-auto",
                "--dry-run",
                "--interactive",
            ],
        )
        assert result.exit_code == 0
        assert "Revisão" not in result.output

    def test_without_interactive_flag_nothing_is_prompted(self, cards_folder: Path) -> None:
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--no-auto"])
        assert result.exit_code == 0
        pending = runner.invoke(app, ["review", "--json"])
        assert len(json.loads(pending.stdout)) == 2
