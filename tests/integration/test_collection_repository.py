"""Repositório de coleção (plano §4.4): upsert por chave lógica."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CardPrint
from yugioh_scanner.errors import CollectionItemNotFoundError
from yugioh_scanner.repositories.collection import CollectionKey, CollectionRepository


def make_card(session: Session, card_id: int = 1, name: str = "Blue-Eyes White Dragon") -> Card:
    card = Card(id=card_id, name=name, name_normalized=name.lower(), type="Normal Monster", desc="")
    session.add(card)
    session.flush()
    return card


def make_print(session: Session, card: Card, code: str = "LOB-001") -> CardPrint:
    card_print = CardPrint(
        card_id=card.id, set_code_full=code, set_code_normalized=code, set_name="Legend"
    )
    session.add(card_print)
    session.flush()
    return card_print


class TestAddCopies:
    def test_creates_a_new_row(self, session: Session) -> None:
        card = make_card(session)
        item = CollectionRepository(session).add_copies(CollectionKey(card_id=card.id), 3)
        assert item.quantity == 3
        assert item.card_print_id is None
        assert item.source == "manual"

    def test_second_add_sums_instead_of_duplicating(self, session: Session) -> None:
        """O núcleo do upsert: mesma chave lógica nunca vira duas linhas."""
        card = make_card(session)
        repo = CollectionRepository(session)
        repo.add_copies(CollectionKey(card_id=card.id), 2)
        repo.add_copies(CollectionKey(card_id=card.id), 3)

        assert repo.count_items() == 1
        item = repo.find(CollectionKey(card_id=card.id))
        assert item is not None
        assert item.quantity == 5

    def test_null_print_and_a_real_print_are_different_items(self, session: Session) -> None:
        card = make_card(session)
        card_print = make_print(session, card)
        repo = CollectionRepository(session)
        repo.add_copies(CollectionKey(card_id=card.id), 1)
        repo.add_copies(CollectionKey(card_id=card.id, card_print_id=card_print.id), 1)
        assert repo.count_items() == 2

    def test_different_condition_is_a_different_item(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        repo.add_copies(CollectionKey(card_id=card.id, condition="Near Mint"), 1)
        repo.add_copies(CollectionKey(card_id=card.id, condition="Damaged"), 1)
        assert repo.count_items() == 2

    def test_zero_or_negative_quantity_rejected(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        with pytest.raises(ValueError, match="positivo"):
            repo.add_copies(CollectionKey(card_id=card.id), 0)
        with pytest.raises(ValueError):
            repo.add_copies(CollectionKey(card_id=card.id), -1)

    def test_sum_is_visible_to_a_fresh_query_in_the_same_session(self, session: Session) -> None:
        """Regressão: a sessão é autoflush=False (plano §20.1).

        Sem `flush()` explícito depois de somar, uma consulta SELECT nova na
        mesma sessão (não apenas ler o objeto já em mãos) não veria a soma.
        """
        card = make_card(session)
        repo = CollectionRepository(session)
        repo.add_copies(CollectionKey(card_id=card.id), 2)
        repo.add_copies(CollectionKey(card_id=card.id), 3)

        # `find()` roda um SELECT de verdade — não é o mesmo objeto Python.
        refetched = repo.find(CollectionKey(card_id=card.id))
        assert refetched is not None
        assert refetched.quantity == 5

    def test_source_is_kept_from_the_first_insert(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        repo.add_copies(CollectionKey(card_id=card.id), 1, source="scan")
        repo.add_copies(CollectionKey(card_id=card.id), 1, source="manual")
        item = repo.find(CollectionKey(card_id=card.id))
        assert item is not None
        assert item.source == "scan"


class TestRemoveCopies:
    def test_partial_removal(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 5)
        remaining = repo.remove_copies(item.id, 2)
        assert remaining == 3
        assert repo.get(item.id) is not None

    def test_removing_everything_deletes_the_row(self, session: Session) -> None:
        """Quantidade zero → a linha some, não fica zerada (plano §4.3)."""
        card = make_card(session)
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 3)
        remaining = repo.remove_copies(item.id, 3)
        assert remaining == 0
        assert repo.get(item.id) is None

    def test_removing_more_than_available_just_clears_it(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 2)
        remaining = repo.remove_copies(item.id, 999)
        assert remaining == 0
        assert repo.get(item.id) is None

    def test_no_quantity_removes_all(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 4)
        assert repo.remove_copies(item.id) == 0
        assert repo.get(item.id) is None

    def test_missing_item_raises(self, session: Session) -> None:
        with pytest.raises(CollectionItemNotFoundError):
            CollectionRepository(session).remove_copies(999999)


class TestSetQuantity:
    def test_sets_an_absolute_value(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 1)
        repo.set_quantity(item.id, 10)
        assert repo.get(item.id).quantity == 10  # type: ignore[union-attr]

    def test_zero_deletes(self, session: Session) -> None:
        card = make_card(session)
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 1)
        repo.set_quantity(item.id, 0)
        assert repo.get(item.id) is None

    def test_missing_item_raises(self, session: Session) -> None:
        with pytest.raises(CollectionItemNotFoundError):
            CollectionRepository(session).set_quantity(999999, 1)


class TestSetPrint:
    def test_resolves_a_null_print(self, session: Session) -> None:
        card = make_card(session)
        card_print = make_print(session, card)
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 1)

        updated = repo.set_print(item.id, card_print.id)
        assert updated.card_print_id == card_print.id

    def test_the_relationship_is_synced_not_just_the_foreign_key(self, session: Session) -> None:
        """Regressão: `card_print_id` sozinho não resincroniza `.card_print`.

        A relação é `lazy="joined"` e já tinha sido carregada como `None`
        (o item nasceu sem print). Sem um refresh explícito, o objeto Python
        continua mostrando `card_print=None` mesmo com a FK já gravada —
        um `list()` novo mascararia isso (consulta fresca), então o teste
        lê exatamente o mesmo objeto que `set_print` devolveu.
        """
        card = make_card(session)
        card_print = make_print(session, card, "LOB-001")
        repo = CollectionRepository(session)
        item = repo.add_copies(CollectionKey(card_id=card.id), 1)
        assert item.card_print is None

        updated = repo.set_print(item.id, card_print.id)

        assert updated.card_print is not None
        assert updated.card_print.set_code_full == "LOB-001"

    def test_merges_into_an_existing_item_with_the_same_print(self, session: Session) -> None:
        """Resolver o set não pode violar a unicidade — funde em vez de duplicar."""
        card = make_card(session)
        card_print = make_print(session, card)
        repo = CollectionRepository(session)

        without_print = repo.add_copies(CollectionKey(card_id=card.id), 2)
        with_print = repo.add_copies(CollectionKey(card_id=card.id, card_print_id=card_print.id), 3)

        merged = repo.set_print(without_print.id, card_print.id)
        assert merged.id == with_print.id
        assert merged.quantity == 5
        assert repo.count_items() == 1

    def test_missing_item_raises(self, session: Session) -> None:
        with pytest.raises(CollectionItemNotFoundError):
            CollectionRepository(session).set_print(999999, 1)


class TestStats:
    def test_totals(self, session: Session) -> None:
        card_a = make_card(session, 1, "Card A")
        card_b = make_card(session, 2, "Card B")
        repo = CollectionRepository(session)
        repo.add_copies(CollectionKey(card_id=card_a.id), 3)
        repo.add_copies(CollectionKey(card_id=card_b.id), 2)
        assert repo.count_items() == 2
        assert repo.total_copies() == 5
