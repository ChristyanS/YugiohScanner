"""Comando `scan` pela CLI real (Fase 5: agora fecha o ciclo até a coleção).

Usa o `catalog_template` compartilhado do conftest (migrado + sincronizado com
as fixtures uma vez por sessão) copiado direto para o `data/` da CLI — nenhum
sync de verdade roda aqui, mas o matching resolve contra cartas reais
(Blue-Eyes White Dragon, Dark Magician, ...), o que deixa os testes de
plumbing da CLI honestos sem o custo de rede.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.factories import make_card_image, make_corrupted_image
from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache
from yugioh_scanner.errors import YugiohScannerError
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
def scripted_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_template: Path
) -> Iterator[None]:
    """Registra um OCR determinístico e aponta a CLI para o catálogo de teste."""
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


class TestHappyPath:
    def test_reads_every_image(self, cards_folder: Path) -> None:
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.stdout)
        assert payload["total_images"] == 3
        assert payload["processed"] == 3

    def test_reports_ocr_raw_and_resolved_card(self, cards_folder: Path) -> None:
        """O texto bruto do OCR e a carta resolvida são campos distintos
        (plano §12: "Visualizar resultado do OCR" e "carta identificada")."""
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        by_file = {row["file"]: row for row in json.loads(result.stdout)["results"]}
        assert by_file["IMG_001.jpg"]["ocr_name"] == "BLUE-EYES WHITE DRAGON"
        assert by_file["IMG_001.jpg"]["name"] == "Blue-Eyes White Dragon"
        assert by_file["IMG_001.jpg"]["set_code"] == "LOB-001"

    def test_human_output_has_a_table(self, cards_folder: Path) -> None:
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1"])
        assert result.exit_code == 0
        assert "Blue-Eyes White Dragon" in result.stdout

    def test_limit(self, cards_folder: Path) -> None:
        result = runner.invoke(
            app, ["scan", str(cards_folder), "--workers", "1", "--limit", "2", "--json"]
        )
        assert json.loads(result.stdout)["total_images"] == 2

    def test_recursive(self, cards_folder: Path) -> None:
        make_card_image(cards_folder / "sub" / "IMG_009.jpg")
        flat = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        deep = runner.invoke(
            app, ["scan", str(cards_folder), "--workers", "1", "--recursive", "--json"]
        )
        assert json.loads(deep.stdout)["total_images"] > json.loads(flat.stdout)["total_images"]

    def test_provider_override(self, cards_folder: Path) -> None:
        register_provider("outro", lambda _settings: FakeOCRProvider(default={"name": "X"}))
        try:
            result = runner.invoke(
                app,
                ["scan", str(cards_folder), "--workers", "1", "--provider", "outro", "--json"],
            )
            payload = json.loads(result.stdout)
            assert payload["provider"] == "outro"
            assert payload["results"][0]["ocr_name"] == "X"
            assert payload["results"][0]["name"] is None  # "X" não casa com nada
        finally:
            unregister_provider("outro")

    def test_job_is_persisted(self, cards_folder: Path) -> None:
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert json.loads(result.stdout)["job_id"] is not None


class TestSecondRunIsIdempotent:
    def test_running_twice_skips_everything(self, cards_folder: Path) -> None:
        runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        payload = json.loads(result.stdout)
        assert payload["skipped"] == 3
        assert payload["processed"] == 0

    def test_reprocess_reads_again(self, cards_folder: Path) -> None:
        runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        result = runner.invoke(
            app, ["scan", str(cards_folder), "--workers", "1", "--reprocess", "--json"]
        )
        payload = json.loads(result.stdout)
        assert payload["processed"] == 3
        assert payload["skipped"] == 0


class TestDryRunAndNoAuto:
    def test_dry_run_writes_nothing(self, cards_folder: Path) -> None:
        result = runner.invoke(
            app, ["scan", str(cards_folder), "--workers", "1", "--dry-run", "--json"]
        )
        payload = json.loads(result.stdout)
        assert payload["job_id"] is None

        # Rodar de novo (sem --dry-run) não deveria pular nada: o dry-run
        # anterior não persistiu.
        second = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert json.loads(second.stdout)["skipped"] == 0

    def test_apply_is_the_default(self, cards_folder: Path) -> None:
        """O plano diz que `scan PASTA` sozinho já grava (§8)."""
        runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        second = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert json.loads(second.stdout)["skipped"] == 3  # a 1ª chamada gravou

    def test_no_auto_forces_pending(self, cards_folder: Path) -> None:
        result = runner.invoke(
            app, ["scan", str(cards_folder), "--workers", "1", "--no-auto", "--json"]
        )
        payload = json.loads(result.stdout)
        assert payload["auto_added"] == 0
        assert all(row["decision"] != "auto" for row in payload["results"])


class TestResilience:
    def test_a_broken_image_does_not_stop_the_others(self, cards_folder: Path) -> None:
        """Requisito explícito do briefing §13."""
        make_corrupted_image(cards_folder / "IMG_004.jpg")

        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        assert result.exit_code == 0

        payload = json.loads(result.stdout)
        assert payload["failed"] == 1
        assert payload["processed"] == 4

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
            assert payload["failed"] == 1
            failed_row = next(r for r in payload["results"] if r["status"] == "error")
            assert failed_row["file"] == "IMG_002.jpg"
        finally:
            unregister_provider("instavel")

    def test_empty_folder(self, tmp_path: Path) -> None:
        empty = tmp_path / "vazia"
        empty.mkdir()
        result = runner.invoke(app, ["scan", str(empty), "--workers", "1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["total_images"] == 0

    def test_missing_folder_is_an_actionable_error(self, tmp_path: Path) -> None:
        """Regressão: uma `YugiohScannerError` levantada direto do corpo do
        comando (não via `require_database`) precisa do mesmo tratamento —
        exit code da própria exceção e mensagem amigável, não um traceback
        cru com exit code genérico."""
        result = runner.invoke(app, ["scan", str(tmp_path / "nao_existe")])
        assert result.exit_code == 2
        assert not isinstance(result.exception, YugiohScannerError), (
            "a exceção do domínio não pode escapar crua até o CliRunner"
        )
        assert "não encontrada" in result.output

    def test_folder_outside_allowlist_is_refused(
        self, cards_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        allowed = tmp_path / "permitida"
        allowed.mkdir()
        monkeypatch.setenv("YGS_ALLOWED_SCAN_ROOTS", json.dumps([str(allowed)]))
        reset_settings_cache()

        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1"])
        assert result.exit_code == 2
        assert not isinstance(result.exception, YugiohScannerError)
        assert "fora das raízes" in result.output

    def test_missing_database_is_an_actionable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem `db upgrade`, a mensagem precisa apontar para `init` — nunca
        um traceback de SQL (plano §16)."""
        monkeypatch.setenv("YGS_DATA_PATH", str(tmp_path / "outro-data"))
        reset_settings_cache()
        result = runner.invoke(app, ["scan", str(tmp_path)])
        assert result.exit_code == 2
        assert "init" in result.output


class TestOutputHygiene:
    def test_json_is_parseable(self, cards_folder: Path) -> None:
        """Logs vão para stderr; o stdout tem de ser pipeável."""
        result = runner.invoke(app, ["scan", str(cards_folder), "--workers", "1", "--json"])
        json.loads(result.stdout)
