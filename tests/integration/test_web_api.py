"""API JSON da Web (`/api/v1/*`) pela `TestClient` real (Fase 8).

Mesmo padrão de `test_cli_*`: banco populado pelo `catalog` compartilhado
(cartas reais — Blue-Eyes, Dark Magician, ...), sem tocar rede de verdade.
`YgoProDeckClient.download_image` é substituído por uma escrita local
(`tests/factories.make_card_image`) — o cache em si (locks, layout em
disco) é exercitado de verdade; só o download passa a ser síncrono e local.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.factories import make_card_image
from yugioh_scanner.config import Settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.ocr.fake_provider import FakeOCRProvider
from yugioh_scanner.ocr.registry import register_provider, unregister_provider
from yugioh_scanner.scanner.worker import reset_provider
from yugioh_scanner.web.app import create_app
from yugioh_scanner.ygoprodeck.client import YgoProDeckClient

BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414

SCRIPT = {
    "IMG_001.jpg": {"name": "BLUE-EYES WHITE DRAGON", "code": "LOB-001"},
    "IMG_002.jpg": {"name": "DARK MAGICIAN", "code": "SDY-006"},
    "IMG_003.jpg": {"name": "POT OF GREED", "code": "LOB-119"},
}


@pytest.fixture(autouse=True)
def _no_real_image_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_download(self: YgoProDeckClient, url: str, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        make_card_image(destination)
        return destination.stat().st_size

    monkeypatch.setattr(YgoProDeckClient, "download_image", fake_download)


@pytest.fixture
def client(settings: Settings, catalog: Database) -> Iterator[TestClient]:
    del catalog  # só o efeito colateral (arquivo do banco populado) importa aqui
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def scripted_provider() -> Iterator[None]:
    """OCR determinístico, roteirizado (mesmo padrão de `test_cli_scan.py`)."""
    register_provider("roteiro", lambda _settings: FakeOCRProvider(SCRIPT))
    try:
        yield
    finally:
        unregister_provider("roteiro")
        reset_provider()


def cards_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cards"
    for filename, reading in SCRIPT.items():
        make_card_image(folder / filename, name=reading["name"], set_code=reading["code"])
    return folder


def _collect_sse_events(client: TestClient, run_id: str, *, timeout_s: float = 10.0) -> list[dict]:
    """Lê o stream até `done`/`scan-error`/`cancelled`, ou estoura o timeout."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout_s
    with client.stream("GET", f"/api/v1/scans/live/{run_id}/events") as response:
        assert response.status_code == 200
        current_event = "message"
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                raise TimeoutError(f"SSE não terminou em {timeout_s}s: {events}")
            if not line:
                continue
            if line.startswith("event:"):
                current_event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                payload = json.loads(line.removeprefix("data:").strip())
                events.append({"event": current_event, **payload})
                if current_event in ("done", "scan-error", "cancelled"):
                    return events
    return events


class TestHealthAndStats:
    def test_health_reports_catalog_size(self, client: TestClient) -> None:
        payload = client.get("/api/v1/health").json()
        assert payload["status"] == "ok"
        assert payload["catalog_cards"] > 0

    def test_stats_reflects_collection_and_pending(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Blue-Eyes White Dragon", "quantity": 2})
        payload = client.get("/api/v1/stats").json()
        assert payload["distinct_cards"] == 1
        assert payload["total_copies"] == 2
        assert payload["pending_review"] == 0


class TestCards:
    def test_search_finds_by_partial_name(self, client: TestClient) -> None:
        results = client.get("/api/v1/cards", params={"q": "blue eyes"}).json()
        assert any(c["id"] == BLUE_EYES for c in results)

    def test_search_without_query_lists_alphabetically(self, client: TestClient) -> None:
        results = client.get("/api/v1/cards", params={"limit": 5}).json()
        assert len(results) == 5

    def test_get_card_includes_prints_and_ownership(self, client: TestClient) -> None:
        # CT13-EN008 é print único (sem ambiguidade de raridade) na fixture —
        # LOB-001 tem duas (plano §0.3.3) e dispararia 409 aqui.
        client.post(
            "/api/v1/collection",
            json={"name": "Blue-Eyes White Dragon", "set_code": "CT13-EN008", "quantity": 3},
        )
        payload = client.get(f"/api/v1/cards/{BLUE_EYES}").json()
        assert payload["name"] == "Blue-Eyes White Dragon"
        assert len(payload["prints"]) >= 1
        assert payload["owned"][0]["quantity"] == 3

    def test_unknown_card_is_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/cards/999999999")
        assert response.status_code == 404
        assert response.json()["error"]

    def test_image_is_served_and_cacheable(self, client: TestClient) -> None:
        response = client.get(f"/api/v1/cards/{BLUE_EYES}/image", params={"size": "small"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert "immutable" in response.headers["cache-control"]

    def test_image_for_unknown_card_is_404_not_a_crash(self, client: TestClient) -> None:
        response = client.get("/api/v1/cards/999999999/image")
        assert response.status_code == 404

    def test_second_request_never_touches_the_network(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critério de aceitação da Fase 8: cache quente serve sem rede (plano §14.1).

        A 1ª chamada baixa (via o fake do fixture `_no_real_image_downloads`).
        Aqui o fake é substituído por uma versão que **falha** se chamada de
        novo — se a 2ª requisição ainda tentasse baixar, o teste veria o erro.
        """
        first = client.get(f"/api/v1/cards/{BLUE_EYES}/image", params={"size": "small"})
        assert first.status_code == 200

        def network_disabled(self: YgoProDeckClient, url: str, destination: Path) -> int:
            raise AssertionError("cache quente não deveria tocar a rede")

        monkeypatch.setattr(YgoProDeckClient, "download_image", network_disabled)

        second = client.get(f"/api/v1/cards/{BLUE_EYES}/image", params={"size": "small"})
        assert second.status_code == 200
        assert second.content == first.content


class TestSets:
    def test_list_sets(self, client: TestClient) -> None:
        results = client.get("/api/v1/sets").json()
        assert len(results) > 0
        assert "set_code" in results[0]


class TestCollectionCrud:
    def test_add_list_patch_delete_roundtrip(self, client: TestClient) -> None:
        added = client.post(
            "/api/v1/collection", json={"name": "Dark Magician", "quantity": 1}
        ).json()
        assert added["card_id"] == DARK_MAGICIAN

        listed = client.get("/api/v1/collection").json()
        assert any(i["id"] == added["id"] for i in listed)

        patched = client.patch(f"/api/v1/collection/{added['id']}", json={"quantity": 9}).json()
        assert patched["quantity"] == 9

        deleted = client.delete(f"/api/v1/collection/{added['id']}").json()
        assert deleted["removed"] is True
        assert not any(i["id"] == added["id"] for i in client.get("/api/v1/collection").json())

    def test_patch_set_code_resolves_print(self, client: TestClient) -> None:
        added = client.post("/api/v1/collection", json={"name": "Dark Magician"}).json()
        assert added["card_print_id"] is None
        patched = client.patch(
            f"/api/v1/collection/{added['id']}", json={"set_code": "SDK-001"}
        ).json()
        assert patched["set_code"] == "SDK-001"

    def test_patch_quantity_zero_removes_the_item(self, client: TestClient) -> None:
        added = client.post("/api/v1/collection", json={"name": "Dark Magician"}).json()
        patched = client.patch(f"/api/v1/collection/{added['id']}", json={"quantity": 0}).json()
        assert patched["removed"] is True

    def test_ambiguous_name_is_a_clean_409_not_a_500(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/collection", json={"name": "Blue-Eyes White Dragon", "set_code": "LOB-001"}
        )
        # LOB-001 tem duas raridades na fixture (plano §0.3.3) — ambíguo de propósito.
        assert response.status_code == 409

    def test_add_by_card_print_id(self, client: TestClient) -> None:
        """Fase 3 do faseamento web: a tela `/collection` manda o `id` do
        print escolhido direto, em vez de reabrir a resolução por código."""
        card = client.get(f"/api/v1/cards/{DARK_MAGICIAN}").json()
        sdk_print_id = next(p["id"] for p in card["prints"] if p["set_code"] == "SDK-001")

        added = client.post(
            "/api/v1/collection",
            json={"name": "Dark Magician", "card_print_id": sdk_print_id, "quantity": 2},
        ).json()
        assert added["card_print_id"] == sdk_print_id
        assert added["quantity"] == 2

    def test_add_by_card_print_id_from_another_card_is_rejected(self, client: TestClient) -> None:
        dark_magician = client.get(f"/api/v1/cards/{DARK_MAGICIAN}").json()
        sdk_print_id = next(p["id"] for p in dark_magician["prints"] if p["set_code"] == "SDK-001")

        response = client.post(
            "/api/v1/collection",
            json={"name": "Blue-Eyes White Dragon", "card_print_id": sdk_print_id},
        )
        # `PrintNotFoundForCardError` é 409, não 404: a carta existe, o print
        # existe — a combinação dos dois é que não faz sentido.
        assert response.status_code == 409

    def test_add_by_translated_name(self, client: TestClient) -> None:
        added = client.post("/api/v1/collection", json={"name": "Mago Negro"}).json()
        assert added["card_id"] == DARK_MAGICIAN


class TestExport:
    def test_default_csv_profile(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        response = client.get("/api/v1/export", params={"format": "csv"})
        assert response.status_code == 200
        assert "Card Name" in response.text

    def test_ygopocket_profile_has_bom_and_crlf(self, client: TestClient) -> None:
        client.post("/api/v1/collection", json={"name": "Dark Magician"})
        response = client.get("/api/v1/export", params={"format": "csv", "profile": "ygopocket"})
        assert response.content.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" in response.content

    def test_unknown_profile_is_a_clean_error(self, client: TestClient) -> None:
        response = client.get("/api/v1/export", params={"format": "csv", "profile": "nope"})
        assert response.status_code == 422
        assert "nope" in response.json()["error"]


class TestUploads:
    def test_rejects_more_files_than_the_limit(
        self, client: TestClient, settings: Settings
    ) -> None:
        files = [
            ("files", (f"a{i}.jpg", b"x", "image/jpeg"))
            for i in range(settings.max_upload_files + 1)
        ]
        response = client.post("/api/v1/uploads", files=files)
        assert response.status_code == 422

    def test_rejects_a_file_over_the_size_limit(
        self, client: TestClient, settings: Settings
    ) -> None:
        big = b"x" * ((settings.max_upload_mb + 1) * 1024 * 1024)
        response = client.post("/api/v1/uploads", files={"files": ("big.jpg", big, "image/jpeg")})
        assert response.status_code == 422

    def test_rejects_non_image_content_even_with_jpg_extension(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/uploads", files={"files": ("fake.jpg", b"not a real jpeg", "image/jpeg")}
        )
        payload = response.json()
        assert payload["saved"] == 0
        assert "fake.jpg" in payload["rejected"]

    def test_accepts_real_images_and_the_folder_is_scannable(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        source = tmp_path / "photo.jpg"
        make_card_image(source, name="POT OF GREED", set_code="LOB-119")
        with source.open("rb") as handle:
            response = client.post(
                "/api/v1/uploads", files={"files": ("photo.jpg", handle, "image/jpeg")}
            )
        payload = response.json()
        assert payload["saved"] == 1
        assert payload["rejected"] == []
        assert Path(payload["folder"]).exists()

    def test_uploaded_filename_is_discarded_not_used_as_a_path(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """plano §21: o nome vira `{uuid}.{ext}` — nunca o nome original."""
        source = tmp_path / "photo.jpg"
        make_card_image(source)
        with source.open("rb") as handle:
            response = client.post(
                "/api/v1/uploads",
                files={"files": ("../../evil.jpg", handle, "image/jpeg")},
            )
        payload = response.json()
        assert payload["saved"] == 1
        saved_files = list(Path(payload["folder"]).iterdir())
        assert len(saved_files) == 1
        assert saved_files[0].name != "../../evil.jpg"
        assert ".." not in saved_files[0].name


class TestScanLifecycle:
    def test_bad_folder_is_a_clean_422_not_a_crash(self, client: TestClient) -> None:
        response = client.post("/api/v1/scans", json={"folder": "C:/does/not/exist/anywhere"})
        assert response.status_code == 422

    def test_full_scan_via_sse_creates_a_job_and_results(
        self, client: TestClient, tmp_path: Path, scripted_provider: None
    ) -> None:
        folder = cards_folder(tmp_path)
        response = client.post(
            "/api/v1/scans",
            json={
                "folder": str(folder),
                "provider": "roteiro",
                "workers": 1,
                "auto": False,  # força tudo pendente — determinístico, sem depender de threshold
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        events = _collect_sse_events(client, run_id)
        image_events = [e for e in events if e["event"] == "image"]
        done_events = [e for e in events if e["event"] == "done"]

        assert len(image_events) == 3
        assert len(done_events) == 1
        job_id = done_events[0]["job_id"]
        assert job_id is not None

        job = client.get(f"/api/v1/scans/{job_id}").json()
        assert job["total_images"] == 3
        assert job["pending"] == 3

        results = client.get(f"/api/v1/scans/{job_id}/results").json()
        assert len(results) == 3
        assert all(r["decision"] == "pending" for r in results)

    def test_cancel_of_unknown_run_reports_false(self, client: TestClient) -> None:
        response = client.delete("/api/v1/scans/live/does-not-exist")
        assert response.json() == {"cancelled": False}

    def test_events_of_unknown_run_stream_a_scan_error(self, client: TestClient) -> None:
        with client.stream("GET", "/api/v1/scans/live/does-not-exist/events") as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())
        assert "scan-error" in text


class TestScanResultActions:
    def test_confirm_and_reject_flow(
        self, client: TestClient, tmp_path: Path, scripted_provider: None
    ) -> None:
        folder = cards_folder(tmp_path)
        run_id = client.post(
            "/api/v1/scans",
            json={"folder": str(folder), "provider": "roteiro", "workers": 1, "auto": False},
        ).json()["run_id"]
        events = _collect_sse_events(client, run_id)
        job_id = next(e for e in events if e["event"] == "done")["job_id"]

        pending = client.get(
            f"/api/v1/scans/{job_id}/results", params={"decision": "pending"}
        ).json()
        assert len(pending) == 3

        target = pending[0]
        confirmed = client.post(
            f"/api/v1/scan-results/{target['id']}/confirm",
            json={
                "card_id": target["card_id"],
                "card_print_id": target["card_print_id"],
                "quantity": 1,
            },
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["card_id"] == target["card_id"]

        # já aplicado — confirmar de novo é 409, não 500.
        again = client.post(
            f"/api/v1/scan-results/{target['id']}/confirm",
            json={"card_id": target["card_id"], "quantity": 1},
        )
        assert again.status_code == 409

    def test_confirm_accepts_an_explicit_language(
        self, client: TestClient, tmp_path: Path, scripted_provider: None
    ) -> None:
        """Continuação do plano de idiomas: a tela de revisão manda o
        idioma escolhido no corpo da confirmação."""
        folder = cards_folder(tmp_path)
        run_id = client.post(
            "/api/v1/scans",
            json={"folder": str(folder), "provider": "roteiro", "workers": 1, "auto": False},
        ).json()["run_id"]
        events = _collect_sse_events(client, run_id)
        job_id = next(e for e in events if e["event"] == "done")["job_id"]

        pending = client.get(
            f"/api/v1/scans/{job_id}/results", params={"decision": "pending"}
        ).json()
        target = pending[0]
        assert target["detected_language"] == "EN"

        confirmed = client.post(
            f"/api/v1/scan-results/{target['id']}/confirm",
            json={"card_id": target["card_id"], "quantity": 1, "language": "DE"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["language"] == "DE"

        rejected = client.post(f"/api/v1/scan-results/{pending[1]['id']}/reject")
        assert rejected.json() == {"rejected": True}

        remaining = client.get("/api/v1/scan-results").json()
        remaining_ids = {r["id"] for r in remaining}
        assert target["id"] not in remaining_ids
        assert pending[1]["id"] not in remaining_ids
