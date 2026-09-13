"""`SyncService.enrich_i18n` (ADR 0012) — orquestração do enriquecimento
opcional via `yaml-yugi`. Nenhum teste toca a rede: `respx` serve o dataset.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from yugioh_scanner.catalog_sources.yaml_yugi.client import YamlYugiClient
from yugioh_scanner.config import Settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.db.tables import Card, CardAltName, CardSet
from yugioh_scanner.repositories.sync_state import SyncStateRepository
from yugioh_scanner.services.sync_service import SYNC_KEY_YAML_YUGI_ETAG, SyncService
from yugioh_scanner.ygoprodeck.client import YgoProDeckClient

DATASET_URL = "https://dawnbrandbots.github.io/yaml-yugi/cards.json"

_DATASET = [
    {
        "password": 46986414,
        "name": {"en": "Dark Magician", "de": "Dunkler Magier"},
        "sets": {"de": [{"set_number": "SDY-G005", "set_name": "Starter Deck: Yugi", "rarities": ["Ultra Rare"]}]},
    }
]


@pytest.fixture
def existing_card(database: Database) -> None:
    with database.session() as session:
        session.add(
            Card(id=46986414, name="Dark Magician", name_normalized="dark magician", type="Normal Monster", desc="")
        )
        session.add(CardSet(set_code="SDY", set_name="Starter Deck: Yugi"))


@pytest.fixture
def yaml_yugi_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(DATASET_URL).mock(
            return_value=httpx.Response(200, json=_DATASET, headers={"ETag": '"v1"'})
        )
        yield router


def _service(settings: Settings, database: Database) -> SyncService:
    dummy = YgoProDeckClient(settings, client=httpx.Client(base_url=settings.ygoprodeck_base_url))
    return SyncService(database, dummy, settings)


class TestEnrichI18n:
    def test_first_run_imports_the_dataset(
        self, settings: Settings, database: Database, existing_card: None, yaml_yugi_route: respx.MockRouter
    ) -> None:
        service = _service(settings, database)
        report = service.enrich_i18n()

        assert report.performed
        assert report.stats.cards_matched == 1
        assert report.stats.alt_names_inserted == 1
        assert report.stats.prints_inserted == 1

        with database.session() as session:
            de_name = session.query(CardAltName).filter_by(card_id=46986414, language="DE").one()
            assert de_name.name == "Dunkler Magier"
            etag = SyncStateRepository(session).get(SYNC_KEY_YAML_YUGI_ETAG)
            assert etag == '"v1"'

    def test_second_run_skips_when_etag_unchanged(
        self, settings: Settings, database: Database, existing_card: None, yaml_yugi_route: respx.MockRouter
    ) -> None:
        service = _service(settings, database)
        service.enrich_i18n()

        yaml_yugi_route.get(DATASET_URL).mock(return_value=httpx.Response(304))
        report = service.enrich_i18n()

        assert not report.performed
        assert "ETag" in report.reason or "mudanças" in report.reason

    def test_force_bypasses_the_etag_cache(
        self, settings: Settings, database: Database, existing_card: None, yaml_yugi_route: respx.MockRouter
    ) -> None:
        service = _service(settings, database)
        service.enrich_i18n()

        report = service.enrich_i18n(force=True)
        assert report.performed

    def test_unreachable_source_is_reported_without_raising(
        self, settings: Settings, database: Database, existing_card: None
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(DATASET_URL).mock(side_effect=httpx.ConnectError("boom"))
            service = _service(settings, database)
            report = service.enrich_i18n()

        assert not report.performed
        assert "YGOPRODeck" in report.reason  # tranquiliza: catálogo primário não foi afetado

    def test_never_creates_a_card_that_does_not_exist_yet(
        self, settings: Settings, database: Database, yaml_yugi_route: respx.MockRouter
    ) -> None:
        """Sem `existing_card`: a carta do dataset não existe no catálogo local."""
        service = _service(settings, database)
        report = service.enrich_i18n()

        assert report.stats.cards_unmatched == 1
        with database.session() as session:
            assert session.query(Card).count() == 0


def test_yaml_yugi_client_reports_unavailable_source_on_http_error(settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(DATASET_URL).mock(return_value=httpx.Response(500))
        client = YamlYugiClient(settings, client=httpx.Client())
        from yugioh_scanner.errors import EnrichmentSourceUnavailableError

        with pytest.raises(EnrichmentSourceUnavailableError):
            client.fetch_cards()
