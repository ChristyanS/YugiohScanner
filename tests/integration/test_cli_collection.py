"""Comandos `collection …` pela CLI real (Fase 6).

Usa o `catalog_template` compartilhado (migrado + sincronizado com as
fixtures uma vez por sessão), copiado para o `data/` da CLI — mesmo padrão de
`test_cli_scan.py`.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yugioh_scanner.cli.main import app
from yugioh_scanner.config import reset_settings_cache

runner = CliRunner()

BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414


def _seed_ambiguous_pair(db_path: Path) -> None:
    """O catálogo pequeno das fixtures não tem nomes parecidos o bastante
    para gerar ambiguidade de verdade — só "Blue-Eyes White Dragon" contém
    "dragon". Duas cartas sintéticas que só diferem na última palavra
    garantem margem < 0,05 e exercitam o fluxo de desempate de verdade."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO card (id, name, name_normalized, type, desc, has_effect, synced_at) "
            "VALUES (900001, 'Zeta Dragon Alpha', 'zeta dragon alpha', 'Effect Monster', "
            "'', 0, datetime('now'))"
        )
        conn.execute(
            "INSERT INTO card (id, name, name_normalized, type, desc, has_effect, synced_at) "
            "VALUES (900002, 'Zeta Dragon Beta', 'zeta dragon beta', 'Effect Monster', "
            "'', 0, datetime('now'))"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_template: Path
) -> Iterator[None]:
    data_path = tmp_path / "data"
    monkeypatch.setenv("YGS_DATA_PATH", str(data_path))
    monkeypatch.setenv("YGS_LOG_LEVEL", "WARNING")
    reset_settings_cache()

    data_path.mkdir(parents=True, exist_ok=True)
    db_path = data_path / "yugioh.db"
    shutil.copy2(catalog_template, db_path)
    _seed_ambiguous_pair(db_path)
    yield
    reset_settings_cache()


def add(name: str, *extra: str) -> object:
    result = runner.invoke(app, ["collection", "add", name, "--json", *extra])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestHelp:
    """Todo comando precisa de `--help` útil (critério de aceitação)."""

    @pytest.mark.parametrize(
        "args",
        [
            ["collection", "--help"],
            ["collection", "list", "--help"],
            ["collection", "add", "--help"],
            ["collection", "remove", "--help"],
            ["collection", "set-qty", "--help"],
            ["collection", "set-print", "--help"],
            ["collection", "show", "--help"],
            ["collection", "stats", "--help"],
            ["review", "--help"],
            ["scan-status", "--help"],
        ],
    )
    def test_help_exits_cleanly(self, args: list[str]) -> None:
        result = runner.invoke(app, args)
        assert result.exit_code == 0


class TestAdd:
    def test_exact_name(self) -> None:
        item = add("Blue-Eyes White Dragon")
        assert item["card_id"] == BLUE_EYES
        assert item["quantity"] == 1

    def test_partial_name(self) -> None:
        item = add("dark magicia")
        assert item["card_id"] == DARK_MAGICIAN

    def test_with_set_code(self) -> None:
        item = add("Dark Magician", "--set-code", "SDK-001")
        assert item["set_code"] == "SDK-001"

    def test_quantity_and_condition(self) -> None:
        item = add("Pot of Greed", "--qty", "3", "--condition", "Damaged")
        assert item["quantity"] == 3
        assert item["condition"] == "Damaged"

    def test_second_add_sums(self) -> None:
        add("Pot of Greed", "--qty", "2")
        item = add("Pot of Greed", "--qty", "3")
        assert item["quantity"] == 5

    def test_unknown_card_fails_with_exit_code(self) -> None:
        result = runner.invoke(app, ["collection", "add", "xyzzy nao existe carta nenhuma"])
        assert result.exit_code == 2
        assert "Nenhuma carta" in result.output

    def test_wrong_set_code_for_the_card_fails(self) -> None:
        result = runner.invoke(app, ["collection", "add", "Dark Magician", "--set-code", "LOB-001"])
        assert result.exit_code == 2

    def test_ambiguous_name_prompts_and_accepts_a_choice(self) -> None:
        """Critério de aceitação: resolve parcial e pergunta quando ambíguo."""
        result = runner.invoke(app, ["collection", "add", "dragon"], input="1\n")
        assert result.exit_code == 0
        assert "corresponde a mais de uma carta" in result.output

    def test_ambiguous_name_cancelled_with_empty_input(self) -> None:
        result = runner.invoke(app, ["collection", "add", "dragon"], input="\n")
        assert result.exit_code == 1
        assert "Cancelado" in result.output


class TestList:
    def test_lists_added_items(self) -> None:
        add("Blue-Eyes White Dragon")
        add("Dark Magician")
        result = runner.invoke(app, ["collection", "list", "--json"])
        assert len(json.loads(result.stdout)) == 2

    def test_search_filters(self) -> None:
        add("Blue-Eyes White Dragon")
        add("Dark Magician")
        result = runner.invoke(app, ["collection", "list", "--search", "dark", "--json"])
        items = json.loads(result.stdout)
        assert len(items) == 1
        assert items[0]["card_name"] == "Dark Magician"

    def test_no_set_filter(self) -> None:
        add("Blue-Eyes White Dragon")
        add("Dark Magician", "--set-code", "SDK-001")
        result = runner.invoke(app, ["collection", "list", "--no-set", "--json"])
        items = json.loads(result.stdout)
        assert len(items) == 1
        assert items[0]["card_name"] == "Blue-Eyes White Dragon"

    def test_human_table_output(self) -> None:
        add("Blue-Eyes White Dragon")
        result = runner.invoke(app, ["collection", "list"])
        assert result.exit_code == 0
        assert "Blue-Eyes White Dragon" in result.output

    def test_empty_collection(self) -> None:
        result = runner.invoke(app, ["collection", "list"])
        assert result.exit_code == 0


class TestRemove:
    def test_remove_all_with_yes(self) -> None:
        item = add("Pot of Greed", "--qty", "2")
        result = runner.invoke(app, ["collection", "remove", str(item["id"]), "--yes", "--json"])
        assert json.loads(result.stdout)["remaining"] == 0

    def test_remove_partial(self) -> None:
        item = add("Pot of Greed", "--qty", "5")
        result = runner.invoke(
            app,
            ["collection", "remove", str(item["id"]), "--qty", "2", "--yes", "--json"],
        )
        assert json.loads(result.stdout)["remaining"] == 3

    def test_without_yes_asks_for_confirmation(self) -> None:
        item = add("Pot of Greed")
        result = runner.invoke(app, ["collection", "remove", str(item["id"])], input="n\n")
        assert result.exit_code != 0
        assert "Remover" in result.output

    def test_missing_item(self) -> None:
        result = runner.invoke(app, ["collection", "remove", "999999", "--yes"])
        assert result.exit_code == 2


class TestSetQty:
    def test_sets_absolute_value(self) -> None:
        item = add("Pot of Greed", "--qty", "1")
        result = runner.invoke(app, ["collection", "set-qty", str(item["id"]), "10", "--json"])
        assert json.loads(result.stdout)["quantity"] == 10

    def test_zero_removes(self) -> None:
        item = add("Pot of Greed")
        runner.invoke(app, ["collection", "set-qty", str(item["id"]), "0"])
        result = runner.invoke(app, ["collection", "list", "--json"])
        assert json.loads(result.stdout) == []


class TestSetPrint:
    def test_resolves_an_undefined_item(self) -> None:
        item = add("Dark Magician")  # sem set
        result = runner.invoke(
            app, ["collection", "set-print", str(item["id"]), "SDK-001", "--json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["set_code"] == "SDK-001"

    def test_wrong_code_fails(self) -> None:
        item = add("Dark Magician")
        result = runner.invoke(app, ["collection", "set-print", str(item["id"]), "LOB-001"])
        assert result.exit_code == 2


class TestShow:
    def test_shows_the_card_and_known_prints(self) -> None:
        item = add("Dark Magician")
        result = runner.invoke(app, ["collection", "show", str(item["id"]), "--json"])
        payload = json.loads(result.stdout)
        assert payload["card"]["name"] == "Dark Magician"
        assert any(p["set_code"] == "SDK-001" for p in payload["known_prints"])

    def test_human_output(self) -> None:
        item = add("Dark Magician")
        result = runner.invoke(app, ["collection", "show", str(item["id"])])
        assert "Dark Magician" in result.output

    def test_missing_item(self) -> None:
        result = runner.invoke(app, ["collection", "show", "999999"])
        assert result.exit_code == 2


class TestStats:
    def test_reflects_additions(self) -> None:
        add("Blue-Eyes White Dragon", "--qty", "2")
        add("Dark Magician")
        result = runner.invoke(app, ["collection", "stats", "--json"])
        payload = json.loads(result.stdout)
        assert payload["distinct_cards"] == 2
        assert payload["total_copies"] == 3


class TestOutputHygiene:
    def test_json_is_parseable_without_log_noise(self) -> None:
        add("Blue-Eyes White Dragon")
        result = runner.invoke(app, ["collection", "list", "--json"])
        json.loads(result.stdout)
