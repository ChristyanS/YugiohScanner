"""Comandos `db …` pela CLI real (plano §19.3)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Aponta a CLI para um `data/` temporário via ambiente."""
    data_path = tmp_path / "data"
    monkeypatch.setenv("YGS_DATA_PATH", str(data_path))
    monkeypatch.setenv("YGS_DATABASE_URL", f"sqlite:///{(data_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("YGS_LOG_LEVEL", "WARNING")
    reset_settings_cache()
    yield data_path
    reset_settings_cache()


class TestStatusBeforeInit:
    def test_reports_missing_database_without_crashing(self, cli_env: Path) -> None:
        """Banco ausente é um estado previsto, não um traceback."""
        result = runner.invoke(app, ["db", "status"])
        assert result.exit_code == 0
        assert "não encontrado" in result.output

    def test_json_output_says_exists_false(self, cli_env: Path) -> None:
        result = runner.invoke(app, ["db", "status", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["exists"] is False

    def test_check_fails_with_actionable_exit_code(self, cli_env: Path) -> None:
        result = runner.invoke(app, ["db", "check"])
        assert result.exit_code == 2


class TestUpgrade:
    def test_creates_the_database(self, cli_env: Path) -> None:
        result = runner.invoke(app, ["db", "upgrade"])
        assert result.exit_code == 0
        assert (cli_env / "cli.db").exists()

    def test_creates_the_data_tree(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        assert (cli_env / "images" / "cards_small").is_dir()
        assert (cli_env / "logs").is_dir()

    def test_second_run_is_a_noop(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        result = runner.invoke(app, ["db", "upgrade"])
        assert result.exit_code == 0
        assert "Nada a fazer" in result.output


class TestStatusAfterInit:
    def test_reports_empty_catalog(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        result = runner.invoke(app, ["db", "status", "--json"])
        payload = json.loads(result.stdout)
        assert payload["exists"] is True
        assert payload["schema_up_to_date"] is True
        assert payload["cards"] == 0
        assert payload["fts_rows"] == 0

    def test_warns_that_catalog_is_empty(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        result = runner.invoke(app, ["db", "status"])
        assert "Catálogo vazio" in result.output

    def test_check_passes(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        result = runner.invoke(app, ["db", "check"])
        assert result.exit_code == 0

    def test_vacuum_runs(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        result = runner.invoke(app, ["db", "vacuum"])
        assert result.exit_code == 0


class TestDowngrade:
    def test_requires_confirmation(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        result = runner.invoke(app, ["db", "downgrade", "base"], input="n\n")
        assert result.exit_code != 0, "sem confirmação, nada pode ser destruído"

    def test_with_yes_flag_reverts_and_backs_up(self, cli_env: Path) -> None:
        runner.invoke(app, ["db", "upgrade"])
        result = runner.invoke(app, ["db", "downgrade", "base", "--yes"])
        assert result.exit_code == 0
        backups = list((cli_env / "backups").glob("*.db"))
        assert backups, "toda operação destrutiva faz backup antes"


class TestConfigCommand:
    def test_show_masks_the_api_key(self, cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-supersecreto-xyz")
        reset_settings_cache()
        result = runner.invoke(app, ["config", "show", "--json"])
        assert result.exit_code == 0
        assert "supersecreto" not in result.stdout

    def test_unknown_action_is_a_usage_error(self, cli_env: Path) -> None:
        result = runner.invoke(app, ["config", "destruir"])
        assert result.exit_code == 1


class TestTopLevel:
    def test_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "yugioh-scanner" in result.stdout

    def test_help_lists_db_group(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "db" in result.stdout
