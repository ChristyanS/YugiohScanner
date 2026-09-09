"""Serviço de coleção — resolução de nome/código digitados (plano §11.4).

Usa o catálogo compartilhado (fixtures reais do YGOPRODeck via conftest), não
um catálogo sintético — a resolução de nome parcial precisa ser exercitada
contra dados de verdade, com todas as ambiguidades que eles trazem.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from yugioh_scanner.db.session import Database
from yugioh_scanner.errors import (
    AmbiguousCardError,
    CardNotFoundError,
    PrintNotFoundForCardError,
)
from yugioh_scanner.services.collection_service import CollectionService

BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414
POT_OF_GREED = 55144522


@pytest.fixture
def service(catalog: Database) -> Iterator[CollectionService]:
    with catalog.session() as session:
        yield CollectionService(session)


class TestAddManual:
    def test_exact_name_resolves(self, service: CollectionService) -> None:
        item = service.add_manual("Blue-Eyes White Dragon")
        assert item.card_id == BLUE_EYES
        assert item.card_print_id is None
        assert item.quantity == 1

    def test_partial_name_resolves(self, service: CollectionService) -> None:
        item = service.add_manual("dark magicia")
        assert item.card_id == DARK_MAGICIAN

    def test_second_call_sums_quantity(self, service: CollectionService) -> None:
        service.add_manual("Pot of Greed", quantity=2)
        item = service.add_manual("Pot of Greed", quantity=3)
        assert item.card_id == POT_OF_GREED
        assert item.quantity == 5

    def test_unknown_name_raises(self, service: CollectionService) -> None:
        with pytest.raises(CardNotFoundError):
            service.add_manual("carta que nao existe de jeito nenhum xyzzy")

    def test_set_code_resolves_the_print(self, service: CollectionService) -> None:
        item = service.add_manual("Dark Magician", set_code="SDK-001")
        assert item.card_print_id is not None

    def test_set_code_not_belonging_to_the_card_raises(self, service: CollectionService) -> None:
        """Diferente do OCR: um código digitado errado avisa, não vira NULL."""
        with pytest.raises(PrintNotFoundForCardError):
            service.add_manual("Dark Magician", set_code="LOB-001")

    def test_ambiguous_rarity_raises_with_options(self, service: CollectionService) -> None:
        """`LOB-001` existe em duas raridades na fixture (plano §0.3.3)."""
        with pytest.raises(AmbiguousCardError) as exc:
            service.add_manual("Blue-Eyes White Dragon", set_code="LOB-001")
        assert len(exc.value.candidates) == 2

    def test_notes_are_stored(self, service: CollectionService) -> None:
        item = service.add_manual("Pot of Greed", notes="comprada na loja X")
        assert item.notes == "comprada na loja X"

    def test_custom_condition_is_a_different_item(self, service: CollectionService) -> None:
        service.add_manual("Pot of Greed", condition="Near Mint")
        service.add_manual("Pot of Greed", condition="Damaged")
        assert service.repo.count_items() == 2


class TestRemoveAndAdjust:
    def test_remove_partial(self, service: CollectionService) -> None:
        item = service.add_manual("Pot of Greed", quantity=5)
        remaining = service.remove(item.id, 2)
        assert remaining == 3

    def test_set_quantity(self, service: CollectionService) -> None:
        item = service.add_manual("Pot of Greed", quantity=1)
        service.set_quantity(item.id, 10)
        assert service.repo.get(item.id).quantity == 10  # type: ignore[union-attr]


class TestStats:
    def test_reflects_the_collection(self, service: CollectionService) -> None:
        service.add_manual("Blue-Eyes White Dragon", quantity=2)
        service.add_manual("Dark Magician", quantity=1, set_code="SDK-001")

        stats = service.stats()
        assert stats.distinct_cards == 2
        assert stats.total_copies == 3
        assert stats.items_without_print == 1

    def test_empty_collection(self, service: CollectionService) -> None:
        stats = service.stats()
        assert stats.as_dict() == {
            "distinct_cards": 0,
            "total_copies": 0,
            "items_without_print": 0,
        }
