"""Repositório do Deck Builder — CRUD simples sobre `deck`/`deck_card`.

Sem regra de negócio: legalidade de deck (tamanhos de zona, limite de cópias
por carta/banlist, teto de posse no modo "só coleção") mora em
`services/deck_service.py`. Aqui é só tradução de operação em SQL, mesmo
espírito de `repositories/collection.py`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import Deck, DeckCard, utcnow


class DeckRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self) -> list[Deck]:
        return list(self.session.scalars(select(Deck).order_by(Deck.name)))

    def get(self, deck_id: int) -> Deck | None:
        return self.session.get(Deck, deck_id)

    def create(self, name: str, *, build_mode: str, banlist: str) -> Deck:
        deck = Deck(name=name, build_mode=build_mode, banlist=banlist)
        self.session.add(deck)
        self.session.flush()
        return deck

    def delete(self, deck: Deck) -> None:
        self.session.delete(deck)

    def cards_for_deck(self, deck_id: int) -> list[DeckCard]:
        return list(self.session.scalars(select(DeckCard).where(DeckCard.deck_id == deck_id)))

    def get_deck_card(self, deck_id: int, card_id: int, zone: str) -> DeckCard | None:
        stmt = select(DeckCard).where(
            DeckCard.deck_id == deck_id, DeckCard.card_id == card_id, DeckCard.zone == zone
        )
        return self.session.scalars(stmt).first()

    def upsert_card(self, deck_id: int, card_id: int, zone: str, quantity: int) -> DeckCard:
        """Soma `quantity` numa linha existente de `(deck_id, card_id, zone)`,
        ou cria uma nova — mesmo idioma de `CollectionRepository.add_copies`."""
        row = self.get_deck_card(deck_id, card_id, zone)
        if row is None:
            row = DeckCard(deck_id=deck_id, card_id=card_id, zone=zone, quantity=quantity)
            self.session.add(row)
        else:
            row.quantity += quantity
        self.session.flush()
        return row

    def set_card_quantity(self, deck_id: int, card_id: int, zone: str, quantity: int) -> None:
        row = self.get_deck_card(deck_id, card_id, zone)
        if row is None:
            return
        if quantity <= 0:
            self.session.delete(row)
        else:
            row.quantity = quantity
        self.session.flush()

    def total_copies_in_deck(self, deck_id: int, card_id: int) -> int:
        """Soma de cópias de uma carta **em todas as zonas** do deck — o
        limite de 3/banlist conta o deck inteiro, não zona isolada."""
        return sum(
            row.quantity
            for row in self.session.scalars(
                select(DeckCard).where(DeckCard.deck_id == deck_id, DeckCard.card_id == card_id)
            )
        )

    def touch(self, deck: Deck) -> None:
        deck.updated_at = utcnow()
