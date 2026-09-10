"""Comando `export` pela CLI real (Fase 7).

Mesmo padrão de `test_cli_collection.py`: banco copiado do `catalog_template`
compartilhado, comandos disparados pelo `CliRunner` do Typer.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache

runner = CliRunner()


@pytest.fixture(autouse=True)
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_template: Path
) -> Iterator[None]:
    data_path = tmp_path / "data"
    monkeypatch.setenv("YGS_DATA_PATH", str(data_path))
    monkeypatch.setenv("YGS_LOG_LEVEL", "WARNING")
    reset_settings_cache()

    data_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(catalog_template, data_path / "yugioh.db")
    yield
    reset_settings_cache()


def _add(name: str, *extra: str) -> None:
    result = runner.invoke(app, ["collection", "add", name, *extra])
    assert result.exit_code == 0, result.output


class TestHelp:
    def test_help_exits_cleanly(self) -> None:
        result = runner.invoke(app, ["export", "--help"])
        assert result.exit_code == 0


class TestListProfiles:
    def test_lists_every_registered_profile(self) -> None:
        result = runner.invoke(app, ["export", "--list-profiles"])
        assert result.exit_code == 0
        assert "csv:ygoprodeck" in result.stdout
        assert "csv:ygopocket" in result.stdout
        assert "csv:full" in result.stdout
        assert "txt:list" in result.stdout

    def test_works_with_an_empty_collection(self) -> None:
        result = runner.invoke(app, ["export", "--list-profiles"])
        assert result.exit_code == 0


class TestFormatRequired:
    def test_missing_format_fails_cleanly(self) -> None:
        result = runner.invoke(app, ["export"])
        assert result.exit_code == 1
        assert "--format" in result.output

    def test_unknown_profile_fails_with_available_list(self) -> None:
        result = runner.invoke(app, ["export", "--format", "csv", "--profile", "nope"])
        assert result.exit_code == 2
        assert "nope" in result.output


class TestStdoutOutput:
    def test_default_csv_profile_is_ygoprodeck(self) -> None:
        _add("Blue-Eyes White Dragon", "--set-code", "CT13-EN008")
        result = runner.invoke(app, ["export", "--format", "csv"])
        assert result.exit_code == 0
        assert result.stdout.splitlines()[0].startswith("Card Name,Card Quantity")
        assert "Blue-Eyes White Dragon" in result.stdout

    def test_txt_format(self) -> None:
        _add("Dark Magician")
        result = runner.invoke(app, ["export", "--format", "txt"])
        assert result.exit_code == 0
        assert "1x Dark Magician" in result.stdout


class TestFileOutput:
    def test_writes_to_the_given_path(self, tmp_path: Path) -> None:
        _add("Pot of Greed", "--qty", "5")
        out = tmp_path / "out.csv"

        result = runner.invoke(app, ["export", "--format", "csv", "-o", str(out)])

        assert result.exit_code == 0
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "Pot of Greed,5" in content
        assert "linha(s) escrita(s)" in result.stderr

    def test_ygopocket_file_matches_the_real_export_header_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        _add("Dark Magician")
        out = tmp_path / "pocket.csv"

        result = runner.invoke(
            app, ["export", "--format", "csv", "--profile", "ygopocket", "-o", str(out)]
        )
        assert result.exit_code == 0

        sample = Path("export-samples/Minha-cole-o.csv").read_bytes()
        generated = out.read_bytes()
        assert generated.split(b"\r\n", 1)[0] == sample.split(b"\r\n", 1)[0]
        assert generated.startswith(b"\xef\xbb\xbf")


class TestSkipUnresolved:
    def test_omits_items_without_a_print(self) -> None:
        _add("Dark Magician")  # sem --set-code: fica sem print

        with_unresolved = runner.invoke(app, ["export", "--format", "csv"])
        skipped = runner.invoke(app, ["export", "--format", "csv", "--skip-unresolved"])

        assert "Dark Magician" in with_unresolved.stdout
        assert "Dark Magician" not in skipped.stdout
