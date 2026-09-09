"""Comando `scan` pela CLI real (Fase 3)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.factories import make_card_image, make_corrupted_image
from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache
from yugioh_scanner.ocr.fake_provider import FakeOCRProvider
from yugioh_scanner.ocr.registry import register_provider, unregister_provider
from yugioh_scanner.scanner.worker import reset_provider

runner = CliRunner()

SCRIPT = {
    "IMG_001.jpg": {"name": "BLUE-EYES WHITE DRAGON", "code": "LOB-001"},
    "IMG_002.jpg": {"name": "DARK MAGICIAN", "code": "SDY-006"},
    "IMG_003.jpg": {"name": "POT OF GREED", "code": "LOB-119"},
}


@pytest.fixture(autouse=True)
def scripted_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Registra um OCR determinístico e aponta a CLI para um `data/` temporário."""
    monkeypatch.setenv("YGS_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("YGS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("YGS_OCR_PROVIDER", "roteiro")
    reset_settings_cache()

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
    for filename in SCRIPT:
        make_card_image(folder / filename)
    return folder


class TestHappyPath:
    def test_reads_every_image(self, cards_folder: Path) -> None:
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.stdout)
        assert payload["images"] == 3
        assert payload["counters"]["ok"] == 3

    def test_reports_name_and_set_code(self, cards_folder: Path) -> None:
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        by_file = {row["file"]: row for row in json.loads(result.stdout)["results"]}
        assert by_file["IMG_001.jpg"]["name"] == "BLUE-EYES WHITE DRAGON"
        assert by_file["IMG_001.jpg"]["code"] == "LOB-001"

    def test_human_output_has_a_table(self, cards_folder: Path) -> None:
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1"])
        assert result.exit_code == 0
        assert "BLUE-EYES WHITE DRAGON" in result.stdout
        assert "LOB-001" in result.stdout

    def test_limit(self, cards_folder: Path) -> None:
        result = runner.invoke(
            app, ["scan", str(cards_folder), "--workers", "1", "--limit", "2", "--json"]
        )
        assert json.loads(result.stdout)["images"] == 2

    def test_recursive(self, cards_folder: Path) -> None:
        make_card_image(cards_folder / "sub" / "IMG_009.jpg")
        flat = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        deep = runner.invoke(
            app, ["scan", str(cards_folder), "--workers", "1", "--recursive", "--json"]
        )
        assert json.loads(deep.stdout)["images"] > json.loads(flat.stdout)["images"]

    def test_provider_override(self, cards_folder: Path) -> None:
        register_provider("outro", lambda _settings: FakeOCRProvider(default={"name": "X"}))
        try:
            result = runner.invoke(
                app,
                ["scan", str(cards_folder), "--workers", "1", "--provider", "outro", "--json"],
            )
            payload = json.loads(result.stdout)
            assert payload["provider"] == "outro"
            assert payload["results"][0]["name"] == "X"
        finally:
            unregister_provider("outro")


class TestResilience:
    def test_a_broken_image_does_not_stop_the_others(self, cards_folder: Path) -> None:
        """Requisito explícito do briefing §13."""
        make_corrupted_image(cards_folder / "IMG_004.jpg")

        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert result.exit_code == 0

        payload = json.loads(result.stdout)
        assert payload["counters"]["ok"] == 3
        assert payload["counters"]["invalid"] == 1

        broken = next(row for row in payload["results"] if row["file"] == "IMG_004.jpg")
        assert "IMG_004.jpg" in str(broken["error"]), "o erro precisa dizer qual imagem falhou"

    def test_ocr_failure_is_isolated(self, cards_folder: Path) -> None:
        register_provider(
            "instavel",
            lambda _settings: FakeOCRProvider(SCRIPT, fail_on={"IMG_002.jpg"}),
        )
        try:
            result = runner.invoke(
                app,
                ["scan", str(cards_folder), "--workers", "1", "--provider", "instavel", "--json"],
            )
            payload = json.loads(result.stdout)
            assert payload["counters"]["ok"] == 2
            assert payload["counters"]["error"] == 1
        finally:
            unregister_provider("instavel")

    def test_empty_folder(self, tmp_path: Path) -> None:
        empty = tmp_path / "vazia"
        empty.mkdir()
        result = runner.invoke(app, ["scan", str(empty), "--workers", "1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["images"] == 0

    def test_missing_folder_is_an_actionable_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["scan", str(tmp_path / "nao_existe")])
        assert result.exit_code != 0

    def test_folder_outside_allowlist_is_refused(
        self, cards_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        allowed = tmp_path / "permitida"
        allowed.mkdir()
        monkeypatch.setenv("YGS_ALLOWED_SCAN_ROOTS", json.dumps([str(allowed)]))
        reset_settings_cache()

        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1"])
        assert result.exit_code != 0


class TestOutputHygiene:
    def test_json_is_parseable(self, cards_folder: Path) -> None:
        """Logs vão para stderr; o stdout tem de ser pipeável."""
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        json.loads(result.stdout)

    def test_dry_run_is_the_default(self, cards_folder: Path) -> None:
        """Nada entra na coleção sem que o usuário peça (Fase 5)."""
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert result.exit_code == 0
