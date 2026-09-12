"""Rotas HTML (`/`, `/scan`, `/review`, `/collection`, `/cards/{id}`) — smoke
tests de renderização (Fase 8, critério de aceitação: "as cinco telas
funcionando"). Não testa JS de navegador (fora do alcance do `TestClient`);
testa que cada template renderiza sem erro e contém os marcadores certos.
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


class TestFiveScreens:
    def test_dashboard_renders(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Dashboard" in response.text

    def test_scan_page_renders_with_provider_options(self, client: TestClient) -> None:
        response = client.get("/scan")
        assert response.status_code == 200
        assert "rapidocr" in response.text
        assert 'name="folder"' in response.text

    def test_review_page_renders(self, client: TestClient) -> None:
        response = client.get("/review")
        assert response.status_code == 200
        assert "Revisão" in response.text

    def test_collection_page_renders(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician", "quantity": 2})
        response = client.get("/collection")
        assert response.status_code == 200
        assert "Dark Magician" in response.text

    def test_card_detail_page_renders(self, client: TestClient) -> None:
        response = client.get(f"/cards/{BLUE_EYES}")
        assert response.status_code == 200
        assert "Blue-Eyes White Dragon" in response.text

    def test_card_detail_shows_ownership(self, client: TestClient) -> None:
        client.post(
            "/api/v1/collection",
            json={"name": "Blue-Eyes White Dragon", "set_code": "CT13-EN008", "quantity": 4},
        )
        response = client.get(f"/cards/{BLUE_EYES}")
        assert "4 cópia" in response.text


class TestDashboardNumbers:
    def test_shows_pending_badge_when_nonzero(self, client: TestClient) -> None:
        response = client.get("/")
        assert "pendências de revisão" in response.text


class TestCollectionInlineActions:
    def test_quantity_increment_and_decrement(self, client: TestClient) -> None:
        item = client.post(
            "/api/v1/collection", json={"name": "Dark Magician", "quantity": 1}
        ).json()
        up = client.post(f"/collection/{item['id']}/quantity", params={"delta": 1})
        assert up.status_code == 200
        assert "2" in up.text

        down_to_zero = client.post(f"/collection/{item['id']}/quantity", params={"delta": -2})
        assert down_to_zero.status_code == 200
        assert down_to_zero.text.strip() == ""  # linha removida (quantidade zerou)

    def test_print_options_and_set_print(self, client: TestClient) -> None:
        item = client.post("/api/v1/collection", json={"name": "Dark Magician"}).json()
        assert item["card_print_id"] is None

        options = client.get(f"/collection/{item['id']}/print-options")
        assert options.status_code == 200
        assert "<select" in options.text

        # A carta tem um print SDK-001 na fixture (sem ambiguidade de raridade).
        row = client.post(
            f"/collection/{item['id']}/set-print", data={"card_print_id": _sdk_print_id(client)}
        )
        assert row.status_code == 200
        assert "SDK-001" in row.text

    def test_delete_removes_the_row(self, client: TestClient) -> None:
        item = client.post("/api/v1/collection", json={"name": "Dark Magician"}).json()
        response = client.delete(f"/collection/{item['id']}")
        assert response.status_code == 200
        assert response.text.strip() == ""
        assert not any(i["id"] == item["id"] for i in client.get("/api/v1/collection").json())

    def test_rows_partial_respects_filters(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        client.post("/api/v1/collection", json={"name": "Pot of Greed"})
        response = client.get("/collection/rows", params={"search": "dark"})
        assert "Dark Magician" in response.text
        assert "Pot of Greed" not in response.text


def _sdk_print_id(client: TestClient) -> int:
    card = client.get(f"/api/v1/cards/{DARK_MAGICIAN}").json()
    return next(p["id"] for p in card["prints"] if p["set_code"] == "SDK-001")


class TestDatabaseScreen:
    """`/cards` — banco de dados completo (Fase 2 do faseamento web)."""

    def test_page_lists_the_whole_catalog_not_just_the_collection(
        self, client: TestClient
    ) -> None:
        # Nada foi adicionado à coleção — a tela de banco de dados mostra o
        # catálogo mesmo assim, ao contrário de `/collection`.
        response = client.get("/cards")
        assert response.status_code == 200
        assert "Dark Magician" in response.text
        assert "Blue-Eyes White Dragon" in response.text

    def test_table_view_is_the_default(self, client: TestClient) -> None:
        response = client.get("/cards")
        assert "<table>" in response.text
        assert 'class="gallery-grid"' not in response.text

    def test_gallery_view_renders_images(self, client: TestClient) -> None:
        response = client.get("/cards", params={"view": "gallery"})
        assert response.status_code == 200
        assert 'class="gallery-grid"' in response.text
        assert "/image?size=small" in response.text

    def test_search_filters_by_name(self, client: TestClient) -> None:
        response = client.get("/cards", params={"q": "dragon"})
        assert "Blue-Eyes White Dragon" in response.text
        assert "Pot of Greed" not in response.text

    def test_set_filter(self, client: TestClient) -> None:
        response = client.get("/cards", params={"set": "SDK"})
        assert "Dark Magician" in response.text  # tem print SDK-001
        assert "Pot of Greed" not in response.text  # sem print nesse set

    def test_rows_partial_used_by_htmx(self, client: TestClient) -> None:
        response = client.get("/cards/rows", params={"q": "magician"})
        assert response.status_code == 200
        assert "Dark Magician" in response.text
        # É um fragmento — não a página inteira com <nav> do menu.
        assert "<html" not in response.text

    def test_pagination_splits_results_across_pages(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from yugioh_scanner.web.routes import cards as cards_module

        monkeypatch.setattr(cards_module, "_PAGE_SIZE_TABLE", 2)

        page1 = client.get("/cards", params={"page": 1})
        assert "página 1 de 3" in page1.text
        assert "Próxima" in page1.text
        assert "Anterior" not in page1.text

        page2 = client.get("/cards", params={"page": 2})
        assert "página 2 de 3" in page2.text
        assert "Anterior" in page2.text
        assert "Próxima" in page2.text


class TestCollectionGalleryView:
    def test_gallery_view_shows_quantity_and_image(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician", "quantity": 3})
        response = client.get("/collection", params={"view": "gallery"})
        assert response.status_code == 200
        assert 'class="gallery-grid"' in response.text
        assert "x3" in response.text
        assert "Dark Magician" in response.text

    def test_gallery_rows_partial_respects_filters(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        client.post("/api/v1/collection", json={"name": "Pot of Greed"})
        response = client.get("/collection/rows", params={"search": "dark", "view": "gallery"})
        assert "Dark Magician" in response.text
        assert "Pot of Greed" not in response.text


class TestScanDetailPage:
    def test_404_for_unknown_job_is_handled_cleanly(self, client: TestClient) -> None:
        response = client.get("/scan/999999")
        assert response.status_code == 404
