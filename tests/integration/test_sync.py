"""Sincronização com a YGOPRODeck (Fase 2).

Nenhum teste aqui toca a rede: as respostas são **fixtures gravadas com as
formas reais da API**, servidas por `respx`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from yugioh_scanner.config import Settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.db.tables import (
    SYNC_KEY_DATABASE_VERSION,
    SYNC_KEY_LAST_FULL_SYNC,
    Card,
    CardImage,
    CardPrint,
    CardSet,
    CollectionItem,
)
from yugioh_scanner.errors import ApiRateLimitedError, ApiResponseError, ApiUnavailableError
from yugioh_scanner.repositories.sync_state import SyncStateRepository
from yugioh_scanner.services.sync_service import SyncService
from yugioh_scanner.ygoprodeck.client import YgoProDeckClient

from ..fixtures_api import BASE_URL, mock_api


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    """Roteador com o catálogo completo das fixtures (5 cartas, 2 páginas)."""
    with mock_api() as router:
        yield router


@pytest.fixture
def service(settings: Settings, database: Database, api: respx.MockRouter) -> SyncService:
    client = YgoProDeckClient(
        settings,
        client=httpx.Client(base_url=settings.ygoprodeck_base_url),
    )
    return SyncService(database, client, settings)


def run_sync(service: SyncService, **kwargs: Any) -> Any:
    return service.sync(page_size=3, **kwargs)


class TestFirstSync:
    def test_populates_the_catalog(self, service: SyncService, database: Database) -> None:
        report = run_sync(service)
        assert report.performed
        with database.session() as session:
            assert session.query(Card).count() == 5
            assert session.query(CardSet).count() == 5

    def test_paginates_through_every_page(self, service: SyncService, database: Database) -> None:
        """5 cartas em páginas de 3 => a segunda página precisa ser buscada."""
        run_sync(service)
        with database.session() as session:
            names = set(session.scalars(session.query(Card.name).statement))
        assert "Blue-Eyes White Dragon" in names
        assert "Ojama Token" in names, "a segunda página não foi importada"

    def test_normalizes_names_at_ingestion(self, service: SyncService, database: Database) -> None:
        """A busca compara formas normalizadas; elas nascem aqui (§7.1)."""
        run_sync(service)
        with database.session() as session:
            card = session.get(Card, 89631139)
            assert card is not None
            assert card.name_normalized == "blue-eyes white dragon"

    def test_records_version_and_timestamp(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        with database.session() as session:
            state = SyncStateRepository(session)
            assert state.get(SYNC_KEY_DATABASE_VERSION) == "146.68"
            assert state.get(SYNC_KEY_LAST_FULL_SYNC)

    def test_populates_the_fts_index_through_triggers(
        self, service: SyncService, database: Database
    ) -> None:
        """Os triggers do FTS5 precisam disparar na carga em massa."""
        from yugioh_scanner.db.fts import search_card_ids

        run_sync(service)
        with database.session() as session:
            assert search_card_ids(session, "blue eyes white dragon") == [89631139]


class TestCardMapping:
    def test_maps_every_field(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        with database.session() as session:
            card = session.get(Card, 89631139)
            assert card is not None
            assert card.name == "Blue-Eyes White Dragon"
            assert card.atk == 3000
            assert card.defense == 2500, "o alias de `def` precisa funcionar"
            assert card.level == 8
            assert card.attribute == "LIGHT"
            assert card.archetype == "Blue-Eyes"
            assert card.typeline == ["Dragon", "Normal"]
            assert card.konami_id == 4007
            assert card.tcg_date is not None and card.tcg_date.year == 2002
            assert card.has_effect is False

    def test_card_without_optional_fields_is_imported(
        self, service: SyncService, database: Database
    ) -> None:
        """A API omite campos vazios — isso não pode quebrar o parser (§0.1)."""
        run_sync(service)
        with database.session() as session:
            token = session.get(Card, 10000000)
            assert token is not None
            assert token.archetype is None
            assert token.konami_id is None
            assert token.prints == [], "o Token não tem card_sets"

    def test_empty_date_string_becomes_null(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        with database.session() as session:
            card = session.get(Card, 12345678)
            assert card is not None
            assert card.tcg_date is None

    def test_multiple_artworks_are_kept_with_a_primary(
        self, service: SyncService, database: Database
    ) -> None:
        run_sync(service)
        with database.session() as session:
            images = session.query(CardImage).filter(CardImage.card_id == 89631139).all()
            assert len(images) == 2
            assert sum(1 for image in images if image.is_primary) == 1


class TestPrintMapping:
    def test_same_card_same_set_two_rarities_becomes_two_prints(
        self, service: SyncService, database: Database
    ) -> None:
        """Caso real da API (§0.3.3) que a chave lógica precisa acomodar."""
        run_sync(service)
        with database.session() as session:
            prints = (
                session.query(CardPrint)
                .filter(CardPrint.card_id == 89631139, CardPrint.set_code_full == "LOB-001")
                .all()
            )
            assert {p.rarity for p in prints} == {"Ultra Rare", "Secret Rare"}

    def test_prefix_links_to_known_set(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        with database.session() as session:
            print_row = (
                session.query(CardPrint).filter(CardPrint.set_code_full == "CT13-EN008").one()
            )
            assert print_row.set_prefix == "CT13"
            assert print_row.region == "EN"
            assert print_row.number == "008"

    def test_unknown_prefix_is_preserved_with_null_link(
        self, service: SyncService, database: Database
    ) -> None:
        """`MVP1` não está no catálogo de sets da fixture.

        Perder o print seria pior do que não saber o set (§4.4).
        """
        run_sync(service)
        with database.session() as session:
            print_row = (
                session.query(CardPrint).filter(CardPrint.set_code_full == "MVP1-ENV04").one()
            )
            assert print_row.set_prefix is None
            assert print_row.set_name == "Milênio Promo"

    def test_unparsable_set_code_is_kept_as_text(
        self, service: SyncService, database: Database
    ) -> None:
        """`JUMP` não tem número; guardamos como texto, sem inventar estrutura."""
        report = run_sync(service)
        with database.session() as session:
            print_row = session.query(CardPrint).filter(CardPrint.set_code_full == "JUMP").one()
            assert print_row.set_code_normalized == "JUMP"
            assert print_row.number is None
        assert "JUMP" in report.stats.unparsed_set_codes

    def test_price_string_becomes_float(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        with database.session() as session:
            print_row = (
                session.query(CardPrint).filter(CardPrint.set_code_full == "CT13-EN008").one()
            )
            assert print_row.set_price == pytest.approx(74.49)

    def test_empty_price_becomes_null(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        with database.session() as session:
            print_row = (
                session.query(CardPrint).filter(CardPrint.set_code_full == "MP24-EN999").one()
            )
            assert print_row.set_price is None


class TestIdempotency:
    """O teste mais importante da fase: sincronizar duas vezes não duplica nada."""

    def test_second_sync_changes_no_counts(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        with database.session() as session:
            before = (
                session.query(Card).count(),
                session.query(CardPrint).count(),
                session.query(CardSet).count(),
                session.query(CardImage).count(),
            )

        run_sync(service, force=True)
        with database.session() as session:
            after = (
                session.query(Card).count(),
                session.query(CardPrint).count(),
                session.query(CardSet).count(),
                session.query(CardImage).count(),
            )
        assert before == after

    def test_second_sync_only_updates(self, service: SyncService, database: Database) -> None:
        run_sync(service)
        report = run_sync(service, force=True)
        assert report.stats.cards_inserted == 0
        assert report.stats.prints_inserted == 0
        assert report.stats.cards_updated == 5

    def test_print_ids_are_stable_across_syncs(
        self, service: SyncService, database: Database
    ) -> None:
        """Se os IDs mudassem, a coleção do usuário perderia o set (§importer)."""
        run_sync(service)
        with database.session() as session:
            before = {
                (p.card_id, p.set_code_full, p.rarity): p.id for p in session.query(CardPrint).all()
            }

        run_sync(service, force=True)
        with database.session() as session:
            after = {
                (p.card_id, p.set_code_full, p.rarity): p.id for p in session.query(CardPrint).all()
            }
        assert before == after

    def test_collection_survives_a_resync(self, service: SyncService, database: Database) -> None:
        """A prova real: uma carta na coleção continua ligada ao seu print."""
        run_sync(service)
        with database.session() as session:
            print_row = (
                session.query(CardPrint).filter(CardPrint.set_code_full == "CT13-EN008").one()
            )
            session.add(
                CollectionItem(card_id=print_row.card_id, card_print_id=print_row.id, quantity=2)
            )
            kept_id = print_row.id

        run_sync(service, force=True)
        with database.session() as session:
            item = session.query(CollectionItem).one()
            assert item.card_print_id == kept_id
            assert item.quantity == 2
            assert item.card_print is not None
            assert item.card_print.set_code_full == "CT13-EN008"


class TestSkipLogic:
    def test_second_sync_without_force_is_a_noop(self, service: SyncService) -> None:
        run_sync(service)
        report = run_sync(service)
        assert not report.performed
        assert "146.68" in report.reason

    def test_check_detects_new_version(self, service: SyncService, api: respx.MockRouter) -> None:
        run_sync(service)
        api.get("/checkDBVer.php").mock(
            return_value=httpx.Response(
                200, json=[{"database_version": "147.00", "last_update": "2026-09-01 00:00:00"}]
            )
        )
        decision = service.check()
        assert decision.needs_sync
        assert "146.68" in decision.reason and "147.00" in decision.reason

    def test_empty_catalog_always_needs_sync(self, service: SyncService) -> None:
        decision = service.check()
        assert decision.needs_sync
        assert "vazio" in decision.reason

    def test_sets_only_skips_cards(self, service: SyncService, database: Database) -> None:
        run_sync(service, sets_only=True)
        with database.session() as session:
            assert session.query(CardSet).count() == 5
            assert session.query(Card).count() == 0


class TestErrorHandling:
    def test_server_error_is_retried_then_reported(
        self, settings: Settings, database: Database, api: respx.MockRouter
    ) -> None:
        settings.http_max_retries = 1
        api.get("/checkDBVer.php").mock(return_value=httpx.Response(503))

        service = SyncService(
            database,
            YgoProDeckClient(settings, client=httpx.Client(base_url=BASE_URL)),
            settings,
        )
        with pytest.raises(ApiUnavailableError):
            service.check()

    def test_rate_limit_is_reported_with_a_hint(
        self, settings: Settings, database: Database, api: respx.MockRouter
    ) -> None:
        settings.http_max_retries = 0
        api.get("/checkDBVer.php").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0"})
        )

        client = YgoProDeckClient(settings, client=httpx.Client(base_url=BASE_URL))
        with pytest.raises(ApiRateLimitedError) as exc:
            client.check_db_version()
        assert exc.value.hint is not None

    def test_client_error_is_not_retried(self, settings: Settings, api: respx.MockRouter) -> None:
        """4xx significa pedido errado: retentar só gasta o rate limit deles."""
        settings.http_max_retries = 3
        route = api.get("/cardsets.php").mock(return_value=httpx.Response(400, text="nope"))

        client = YgoProDeckClient(settings, client=httpx.Client(base_url=BASE_URL))
        with pytest.raises(ApiResponseError):
            client.fetch_sets()
        assert route.call_count == 1

    def test_failed_sync_leaves_the_old_catalog_intact(
        self, service: SyncService, database: Database, api: respx.MockRouter
    ) -> None:
        """Transação única: um erro no meio não deixa catálogo pela metade."""
        run_sync(service)
        with database.session() as session:
            before = session.query(Card).count()

        api.get("/cardinfo.php").mock(return_value=httpx.Response(500))
        with pytest.raises(ApiUnavailableError):
            run_sync(service, force=True)

        with database.session() as session:
            assert session.query(Card).count() == before
