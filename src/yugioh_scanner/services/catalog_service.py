"""Navegação do catálogo (Fase 8: telas `/cards/{id}` e busca da Web).

Fino de propósito: a única regra é "carta inexistente vira erro do domínio"
— o resto é leitura direta. Fica ao lado de `collection_service.py` porque as
duas telas (coleção e catálogo) frequentemente precisam das duas juntas (uma
carta + quantas cópias você tem dela).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.tables import Card, CardPrint, CardSet
from ..errors import CardNotFoundError
from ..repositories.cards import CardRepository
from ..repositories.sets import SetRepository


class CatalogService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.cards = CardRepository(session)
        self.sets = SetRepository(session)

    def search_cards(
        self,
        query: str | None = None,
        *,
        set_prefix: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Card]:
        return self.cards.search(query, set_prefix=set_prefix, limit=limit, offset=offset)

    def get_card(self, card_id: int) -> Card:
        card = self.cards.get(card_id)
        if card is None:
            raise CardNotFoundError(f"Carta #{card_id} não encontrada no catálogo.")
        return card

    def prints_for_card(self, card_id: int) -> list[CardPrint]:
        return self.cards.prints_for(card_id)

    def list_sets(self, *, limit: int = 1000, offset: int = 0) -> list[CardSet]:
        return self.sets.list_all(limit=limit, offset=offset)
