"""Deck Builder — rotas HTML (`/decks`, `/decks/{id}`) e JSON
(`/api/v1/decks/*`). Regras de legalidade (limite de cópias, zonas, banlist)
já são cobertas em `tests/unit/test_deck_service.py`; aqui é só a superfície
HTTP: status codes, formato de resposta, mapeamento de erro.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from yugioh_scanner.config import Settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.web.app import create_app

BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414


@pytest.fixture
def client(settings: Settings, catalog: Database) -> Iterator[TestClient]:
    del catalog
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


class TestDeckPages:
    def test_decks_list_page_renders(self, client: TestClient) -> None:
        response = client.get("/decks")
        assert response.status_code == 200
        assert "Criar deck" in response.text

    def test_deck_editor_page_renders(self, client: TestClient) -> None:
        created = client.post("/api/v1/decks", json={"name": "Meu Deck"})
        deck_id = created.json()["id"]
        response = client.get(f"/decks/{deck_id}")
        assert response.status_code == 200
        assert "Main Deck" in response.text


class TestDeckCrudApi:
    def test_create_and_list(self, client: TestClient) -> None:
        res = client.post(
            "/api/v1/decks", json={"name": "Deck A", "build_mode": "full_db", "banlist": "TCG"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["name"] == "Deck A"
        assert body["build_mode"] == "full_db"

        listed = client.get("/api/v1/decks")
        assert listed.status_code == 200
        assert any(d["name"] == "Deck A" for d in listed.json())

    def test_get_missing_deck_is_404(self, client: TestClient) -> None:
        res = client.get("/api/v1/decks/99999")
        assert res.status_code == 404

    def test_rename_deck(self, client: TestClient) -> None:
        deck_id = client.post("/api/v1/decks", json={"name": "Old Name"}).json()["id"]
        res = client.put(f"/api/v1/decks/{deck_id}", json={"name": "New Name"})
        assert res.status_code == 200
        assert res.json()["name"] == "New Name"

    def test_delete_deck(self, client: TestClient) -> None:
        deck_id = client.post("/api/v1/decks", json={"name": "To Delete"}).json()["id"]
        res = client.delete(f"/api/v1/decks/{deck_id}")
        assert res.status_code == 200
        assert client.get(f"/api/v1/decks/{deck_id}").status_code == 404


class TestDeckCardsApi:
    def test_add_and_remove_card(self, client: TestClient) -> None:
        deck_id = client.post("/api/v1/decks", json={"name": "Deck"}).json()["id"]

        added = client.post(
            f"/api/v1/decks/{deck_id}/cards",
            json={"card_id": DARK_MAGICIAN, "zone": "main", "quantity": 2},
        )
        assert added.status_code == 200
        cards = added.json()["cards"]
        assert cards == [{"card_id": DARK_MAGICIAN, "card_name": "Dark Magician", "zone": "main", "quantity": 2}]

        removed = client.delete(f"/api/v1/decks/{deck_id}/cards/{DARK_MAGICIAN}?zone=main&quantity=1")
        assert removed.status_code == 200
        assert removed.json()["cards"][0]["quantity"] == 1

    def test_exceeding_copy_limit_is_422(self, client: TestClient) -> None:
        deck_id = client.post("/api/v1/decks", json={"name": "Deck"}).json()["id"]
        client.post(
            f"/api/v1/decks/{deck_id}/cards",
            json={"card_id": BLUE_EYES, "zone": "main", "quantity": 3},
        )
        res = client.post(
            f"/api/v1/decks/{deck_id}/cards",
            json={"card_id": BLUE_EYES, "zone": "main", "quantity": 1},
        )
        assert res.status_code == 422
        assert "error" in res.json()

    def test_add_card_to_unknown_deck_is_404(self, client: TestClient) -> None:
        res = client.post(
            "/api/v1/decks/99999/cards", json={"card_id": DARK_MAGICIAN, "zone": "main"}
        )
        assert res.status_code == 404


class TestDeckValidateAndSearch:
    def test_validate_reports_issues(self, client: TestClient) -> None:
        deck_id = client.post("/api/v1/decks", json={"name": "Deck"}).json()["id"]
        res = client.get(f"/api/v1/decks/{deck_id}/validate")
        assert res.status_code == 200
        body = res.json()
        assert body["legal"] is False
        assert body["main_count"] == 0

    def test_search_full_db_mode(self, client: TestClient) -> None:
        deck_id = client.post(
            "/api/v1/decks", json={"name": "Deck", "build_mode": "full_db"}
        ).json()["id"]
        res = client.get(f"/api/v1/decks/{deck_id}/search?q=dark")
        assert res.status_code == 200
        assert any(c["id"] == DARK_MAGICIAN for c in res.json())

    def test_search_collection_only_mode_excludes_unowned_cards(self, client: TestClient) -> None:
        deck_id = client.post(
            "/api/v1/decks", json={"name": "Deck", "build_mode": "collection_only"}
        ).json()["id"]
        res = client.get(f"/api/v1/decks/{deck_id}/search?q=dark")
        assert res.status_code == 200
        assert res.json() == []

        client.post("/api/v1/collection", json={"name": "Dark Magician", "quantity": 1})
        res2 = client.get(f"/api/v1/decks/{deck_id}/search?q=dark")
        assert any(c["id"] == DARK_MAGICIAN for c in res2.json())

    def test_search_offset_paginates(self, client: TestClient) -> None:
        """Painel de navegação do Deck Builder (grid 6x5) pagina com
        `offset` — `DeckService.searchable_pool` já aceitava o parâmetro,
        só a rota não expunha."""
        deck_id = client.post(
            "/api/v1/decks", json={"name": "Deck", "build_mode": "full_db"}
        ).json()["id"]
        full = client.get(f"/api/v1/decks/{deck_id}/search?limit=200").json()
        assert len(full) >= 2

        first_page = client.get(f"/api/v1/decks/{deck_id}/search?limit=1&offset=0").json()
        second_page = client.get(f"/api/v1/decks/{deck_id}/search?limit=1&offset=1").json()
        assert first_page == [full[0]]
        assert second_page == [full[1]]
