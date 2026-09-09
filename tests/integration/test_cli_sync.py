"""Comandos `init`, `sync` e `check-updates` pela CLI real."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache

from ..fixtures_api import BASE_URL, load

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    data_path = tmp_path / "data"
    monkeypatch.setenv("YGS_DATA_PATH", str(data_path))
    monkeypatch.setenv("YGS_DATABASE_URL", f"sqlite:///{(data_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("YGS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("YGS_HTTP_RATE_LIMIT_PER_S", "100000")
    monkeypatch.setenv("YGS_HTTP_BACKOFF_BASE_S", "0")
    reset_settings_cache()
    yield data_path
    reset_settings_cache()


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/checkDBVer.php").mock(
            return_value=httpx.Response(200, json=load("checkdbver"))
        )
        router.get("/cardsets.php").mock(return_value=httpx.Response(200, json=load("cardsets")))
        pages = {0: load("cards_page1"), 3: load("cards_page2")}

        def card_page(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", 0))
            # A CLI usa o tamanho de página padrão; servimos tudo na primeira.
            if offset == 0:
                merged: dict[str, Any] = {
                    "data": pages[0]["data"] + pages[3]["data"],
                    "meta": {**pages[3]["meta"], "current_rows": 5, "pages_remaining": 0},
                }
                return httpx.Response(200, json=merged)
            return httpx.Response(200, json={"data": []})

        router.get("/cardinfo.php").mock(side_effect=card_page)
        yield router


class TestInit:
    def test_creates_database_and_downloads_catalog(
        self, cli_env: Path, api: respx.MockRouter
    ) -> None:
        result = runner.invoke(app, ["init", "--json"])
        assert result.exit_code == 0, result.output
        assert (cli_env / "cli.db").exists()

        status = runner.invoke(app, ["db", "status", "--json"])
        payload = json.loads(status.stdout)
        assert payload["cards"] == 5
        assert payload["sets"] == 5
        assert payload["ygoprodeck_database_version"] == "146.68"

    def test_skip_sync_creates_only_the_schema(self, cli_env: Path, api: respx.MockRouter) -> None:
        result = runner.invoke(app, ["init", "--skip-sync"])
        assert result.exit_code == 0
        status = runner.invoke(app, ["db", "status", "--json"])
        assert json.loads(status.stdout)["cards"] == 0

    def test_running_init_twice_does_not_duplicate(
        self, cli_env: Path, api: respx.MockRouter
    ) -> None:
        runner.invoke(app, ["init", "--json"])
        runner.invoke(app, ["init", "--json"])
        status = runner.invoke(app, ["db", "status", "--json"])
        assert json.loads(status.stdout)["cards"] == 5

    def test_force_requires_confirmation(self, cli_env: Path, api: respx.MockRouter) -> None:
        runner.invoke(app, ["init", "--json"])
        result = runner.invoke(app, ["init", "--force"], input="n\n")
        assert result.exit_code != 0, "recriar o banco apaga a coleção: precisa confirmar"


class TestSync:
    def test_requires_an_initialized_database(self, cli_env: Path) -> None:
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 2
        assert "init" in result.output

    def test_second_sync_is_a_noop(self, cli_env: Path, api: respx.MockRouter) -> None:
        runner.invoke(app, ["init", "--json"])
        result = runner.invoke(app, ["sync", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["performed"] is False

    def test_force_reimports_without_duplicating(
        self, cli_env: Path, api: respx.MockRouter
    ) -> None:
        runner.invoke(app, ["init", "--json"])
        result = runner.invoke(app, ["sync", "--force", "--json"])
        payload = json.loads(result.stdout)
        assert payload["performed"] is True
        assert payload["cards_inserted"] == 0
        assert payload["total_cards"] == 5

    def test_sets_only(self, cli_env: Path, api: respx.MockRouter) -> None:
        runner.invoke(app, ["init", "--skip-sync"])
        result = runner.invoke(app, ["sync", "--sets-only", "--json"])
        payload = json.loads(result.stdout)
        assert payload["total_sets"] == 5
        assert payload["total_cards"] == 0


class TestCheckUpdates:
    def test_reports_up_to_date(self, cli_env: Path, api: respx.MockRouter) -> None:
        runner.invoke(app, ["init", "--json"])
        result = runner.invoke(app, ["check-updates", "--json"])
        assert json.loads(result.stdout)["needs_sync"] is False

    def test_reports_new_version(self, cli_env: Path, api: respx.MockRouter) -> None:
        runner.invoke(app, ["init", "--json"])
        api.get("/checkDBVer.php").mock(
            return_value=httpx.Response(
                200, json=[{"database_version": "999.0", "last_update": "2026-09-09 00:00:00"}]
            )
        )
        result = runner.invoke(app, ["check-updates", "--json"])
        payload = json.loads(result.stdout)
        assert payload["needs_sync"] is True
        assert payload["remote_version"] == "999.0"


class TestOutputHygiene:
    def test_json_output_is_parseable_without_log_noise(
        self, cli_env: Path, api: respx.MockRouter
    ) -> None:
        """Logs vão para stderr; o stdout precisa ser pipeável para o `jq`."""
        runner.invoke(app, ["init", "--json"])
        result = runner.invoke(app, ["db", "status", "--json"])
        json.loads(result.stdout)  # levanta se houver qualquer linha de log junto
