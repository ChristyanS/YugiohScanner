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
        # `lang=EN` explícito: o padrão agora segue o idioma global (pt-BR ->
        # PT), e este teste quer só confirmar que a página renderiza com o
        # item adicionado, não testar qual idioma é o padrão.
        response = client.get("/collection", params={"lang": "EN"})
        assert response.status_code == 200
        assert "Dark Magician" in response.text

    def test_card_detail_page_renders(self, client: TestClient) -> None:
        response = client.get(f"/cards/{BLUE_EYES}", params={"lang": "EN"})
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

    def test_print_options_search_filters_the_list(self, client: TestClient) -> None:
        """Achado real de uso (ADR 0012): sem busca, o `<select>` de sets
        virou grande demais depois do enriquecimento yaml-yugi."""
        item = client.post("/api/v1/collection", json={"name": "Dark Magician"}).json()

        matching = client.get(f"/collection/{item['id']}/print-options", params={"q": "SDK"})
        assert matching.status_code == 200
        assert "SDK-001" in matching.text

        no_match = client.get(f"/collection/{item['id']}/print-options", params={"q": "ZZZZNOPE"})
        assert no_match.status_code == 200
        assert "SDK-001" not in no_match.text
        assert "<select" in no_match.text  # continua um dropdown válido, só vazio

    def test_delete_removes_the_row(self, client: TestClient) -> None:
        item = client.post("/api/v1/collection", json={"name": "Dark Magician"}).json()
        response = client.delete(f"/collection/{item['id']}")
        assert response.status_code == 200
        assert response.text.strip() == ""
        assert not any(i["id"] == item["id"] for i in client.get("/api/v1/collection").json())

    def test_rows_partial_respects_filters(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        client.post("/api/v1/collection", json={"name": "Pot of Greed"})
        response = client.get("/collection/rows", params={"search": "dark", "lang": "EN"})
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
        response = client.get("/cards", params={"lang": "EN"})
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
        response = client.get("/cards", params={"q": "dragon", "lang": "EN"})
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

        # As strings de navegação também aparecem como vocabulário no
        # catálogo `window.I18N` embutido em toda página (ponte de tradução
        # para JS) — checar o link renderizado (`>Anterior</a>`), não a
        # palavra solta, para não dar falso positivo contra o catálogo.
        page1 = client.get("/cards", params={"page": 1})
        assert "página 1 de 3" in page1.text
        assert "Próxima &rarr;</a>" in page1.text
        assert "Anterior</a>" not in page1.text

        page2 = client.get("/cards", params={"page": 2})
        assert "página 2 de 3" in page2.text
        assert "Anterior</a>" in page2.text
        assert "Próxima &rarr;</a>" in page2.text


class TestMultilingualCatalog:
    """Fase 3 do faseamento web: busca e exibição em outros idiomas.

    A fixture compartilhada (`catalog_template`) sincroniza PT de verdade
    (via `tests/fixtures/api/cards_page1_pt.json`) e os demais idiomas
    alternativos com uma resposta vazia — dá pra testar tanto "tem tradução"
    quanto "não tem, cai para inglês" sem massa de dados extra.
    """

    def test_search_by_translated_name_finds_the_card(self, client: TestClient) -> None:
        # "Mago Negro" é como a API PT chama o Dark Magician — a busca em
        # inglês não bateria com isso antes da Fase 3.
        response = client.get("/cards", params={"q": "Mago Negro"})
        assert response.status_code == 200
        assert "Dark Magician" in response.text

    def test_search_by_translated_name_in_manual_search_too(self, client: TestClient) -> None:
        response = client.get("/api/v1/cards", params={"q": "Mago Negro"})
        assert response.status_code == 200
        names = [card["name"] for card in response.json()]
        assert "Dark Magician" in names

    def test_detail_page_shows_translated_name_and_description(self, client: TestClient) -> None:
        response = client.get(f"/cards/{DARK_MAGICIAN}", params={"lang": "PT"})
        assert response.status_code == 200
        assert "Mago Negro" in response.text
        assert "supremo mago" in response.text  # início da descrição em PT
        # A frase completa interpolada ("PT" no lugar de "{lang}") só existe
        # se o aviso de fato renderizou — a versão crua com "{lang}" (não
        # substituído) sempre aparece no catálogo `window.I18N` embutido em
        # toda página, então checar só "não disponível" daria falso positivo.
        assert "Tradução para PT não disponível" not in response.text

    def test_detail_page_falls_back_to_english_when_translation_missing(
        self, client: TestClient
    ) -> None:
        # DE não tem fixture — a resposta mockada é vazia para esse idioma.
        response = client.get(f"/cards/{DARK_MAGICIAN}", params={"lang": "DE"})
        assert response.status_code == 200
        assert "Dark Magician" in response.text
        assert "Tradução para DE não disponível" in response.text

    def test_detail_page_defaults_to_ui_language_without_lang_param(
        self, client: TestClient
    ) -> None:
        # Sem `?lang=`, o padrão agora segue o idioma global da UI (plano de
        # idioma global, fase 2) em vez de cair fixo em inglês.
        pt_br_default = client.get(f"/cards/{DARK_MAGICIAN}")
        assert "Mago Negro" in pt_br_default.text

        client.post("/api/v1/settings/ui-language", json={"language": "en"})
        en_default = client.get(f"/cards/{DARK_MAGICIAN}")
        assert "Dark Magician" in en_default.text
        assert "Mago Negro" not in en_default.text

    def test_unknown_lang_param_is_ignored(self, client: TestClient) -> None:
        response = client.get(f"/cards/{DARK_MAGICIAN}", params={"lang": "XX"})
        assert response.status_code == 200
        assert "Dark Magician" in response.text

    def test_database_listing_shows_names_in_the_chosen_language(
        self, client: TestClient
    ) -> None:
        response = client.get("/cards", params={"lang": "PT"})
        assert response.status_code == 200
        # O link usa o nome traduzido; "Dark Magician" ainda aparece na linha
        # como valor do arquétipo (não traduzido pela API), então a asserção
        # negativa mira o texto do link, não a página inteira.
        assert ">Mago Negro</a>" in response.text
        assert ">Dark Magician</a>" not in response.text

    def test_database_listing_defaults_to_ui_language(self, client: TestClient) -> None:
        pt_br_default = client.get("/cards")
        assert ">Mago Negro</a>" in pt_br_default.text

        client.post("/api/v1/settings/ui-language", json={"language": "en"})
        en_default = client.get("/cards")
        assert "Dark Magician" in en_default.text
        assert "Mago Negro" not in en_default.text


class TestCollectionGalleryView:
    def test_gallery_view_shows_quantity_and_image(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician", "quantity": 3})
        response = client.get("/collection", params={"view": "gallery", "lang": "EN"})
        assert response.status_code == 200
        assert 'class="gallery-grid"' in response.text
        assert "x3" in response.text
        assert "Dark Magician" in response.text

    def test_gallery_rows_partial_respects_filters(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        client.post("/api/v1/collection", json={"name": "Pot of Greed"})
        response = client.get(
            "/collection/rows", params={"search": "dark", "view": "gallery", "lang": "EN"}
        )
        assert "Dark Magician" in response.text
        assert "Pot of Greed" not in response.text


class TestCollectionMultilingual:
    """Fase 3 do faseamento web, extensão para a coleção: buscar e exibir em
    qualquer idioma sincronizado, não só no banco de dados geral."""

    def test_search_by_translated_name_finds_the_item(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        # `lang=EN` isola o que este teste quer verificar (busca em PT acha
        # o item) do idioma de exibição padrão (que agora segue o idioma
        # global, coberto em `test_listing_defaults_to_ui_language`).
        response = client.get("/collection", params={"search": "Mago Negro", "lang": "EN"})
        assert response.status_code == 200
        assert "Dark Magician" in response.text

    def test_rows_partial_search_by_translated_name(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        client.post("/api/v1/collection", json={"name": "Pot of Greed"})
        response = client.get(
            "/collection/rows", params={"search": "Mago Negro", "lang": "EN"}
        )
        assert "Dark Magician" in response.text
        assert "Pot of Greed" not in response.text

    def test_listing_shows_name_in_the_chosen_language(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        response = client.get("/collection", params={"lang": "PT"})
        assert response.status_code == 200
        assert ">Mago Negro</a>" in response.text

    def test_listing_defaults_to_ui_language(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})

        pt_br_default = client.get("/collection")
        assert ">Mago Negro</a>" in pt_br_default.text

        client.post("/api/v1/settings/ui-language", json={"language": "en"})
        en_default = client.get("/collection")
        assert ">Dark Magician</a>" in en_default.text

    def test_gallery_also_respects_the_chosen_language(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        response = client.get("/collection", params={"lang": "PT", "view": "gallery"})
        assert "Mago Negro" in response.text

    def test_inline_quantity_update_keeps_the_chosen_language(self, client: TestClient) -> None:
        item = client.post("/api/v1/collection", json={"name": "Dark Magician"}).json()
        response = client.post(
            f"/collection/{item['id']}/quantity", params={"delta": 1, "lang": "PT"}
        )
        assert response.status_code == 200
        assert "Mago Negro" in response.text
        assert 'href="/cards/46986414?lang=PT"' in response.text


class TestManualAddForm:
    """Formulário "Adicionar carta manualmente" na tela `/collection`
    (Fase 3): a lógica de resolução em si já é coberta por
    `test_web_api.py::TestCollectionCrud` e `test_collection_service.py` —
    aqui só garante que a tela expõe os controles que o JS depende."""

    def test_page_renders_the_manual_add_controls(self, client: TestClient) -> None:
        response = client.get("/collection")
        assert response.status_code == 200
        assert 'id="manual-add"' in response.text
        assert 'id="add-search-input"' in response.text
        assert 'id="add-print-select"' in response.text
        # Os selects de condição/edição/idioma vêm do vocabulário real do
        # banco (plano §4), não hardcoded na tela.
        assert "Near Mint" in response.text
        assert "1st Edition" in response.text


class TestScanDetailPage:
    def test_404_for_unknown_job_is_handled_cleanly(self, client: TestClient) -> None:
        response = client.get("/scan/999999")
        assert response.status_code == 404

    def test_lists_a_thumbnail_and_the_crop_positioned_for_a_grid_result(
        self, client: TestClient, catalog: Database
    ) -> None:
        """Achado real do usuário: a tabela só mostrava texto — sem ver a
        carta, dava pra avaliar um erro de OCR. A miniatura reaproveita a
        mesma técnica de `review.html` (posicionar a foto original via
        `source_bbox`, sem recortar de verdade no servidor)."""
        from yugioh_scanner.images.preprocess import BoundingBox
        from yugioh_scanner.repositories.scans import ScanRepository

        with catalog.session() as session:
            repo = ScanRepository(session)
            job = repo.create_job("/fotos", "fake", 1)
            image = repo.create_image(
                job.id, file_path="/fotos/grade.jpg", file_hash="h1", file_size=1, status="ok"
            )
            repo.create_result(
                image.id,
                job_id=job.id,
                card_id=BLUE_EYES,
                card_print_id=None,
                ocr_name_raw="Blue-Eyes White Dragon",
                ocr_code_raw=None,
                name_score=1.0,
                code_score=0.0,
                confidence=1.0,
                margin=1.0,
                candidates=None,
                decision="auto",
                crop_index=1,
                crop_count=4,
                source_bbox=BoundingBox(left=0.5, top=0.0, right=1.0, bottom=0.5),
            )
            session.commit()
            job_id = job.id

        response = client.get(f"/scan/{job_id}")
        assert response.status_code == 200
        assert f'src="/api/v1/scan-images/{image.id}/file"' in response.text
        assert 'class="photo-wrap thumb cropped"' in response.text
        # width = 100 / (right - left) = 100 / 0.5 = 200%
        assert "width: 200.0%" in response.text
