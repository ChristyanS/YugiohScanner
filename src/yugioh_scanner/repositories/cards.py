"""Leitura do catálogo de cartas — busca e detalhe (Fase 8: navegação Web).

Sem regra de negócio: o serviço acima (`services/catalog_service.py`) decide
o que fazer quando nada é encontrado. Isto aqui só traduz consulta em SQL.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.fts import search_card_ids
from ..db.tables import Card, CardPrint


class CardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, card_id: int) -> Card | None:
        return self.session.get(Card, card_id)

    def prints_for(self, card_id: int) -> list[CardPrint]:
        stmt = (
            select(CardPrint).where(CardPrint.card_id == card_id).order_by(CardPrint.set_code_full)
        )
        return list(self.session.scalars(stmt))

    def search(
        self,
        query: str | None = None,
        *,
        set_prefix: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Card]:
        """Busca por nome (FTS5, plano §7.3) opcionalmente filtrada por set.

        Sem `query`: lista o catálogo em ordem alfabética — é o estado inicial
        da tela `/collection`'s' equivalente de catálogo e do `/api/v1/cards`
        sem `q` (navegação/paginação pura).
        """
        if query and query.strip():
            # A FTS já ordena por relevância; buscamos um pouco mais que
            # `limit` para sobrar candidato depois do filtro de set opcional.
            ids = search_card_ids(self.session, query, limit=limit + offset + 50)
            if not ids:
                return []
            stmt = select(Card).where(Card.id.in_(ids))
            if set_prefix:
                stmt = stmt.join(CardPrint).where(CardPrint.set_prefix == set_prefix.upper())
            found = {card.id: card for card in self.session.scalars(stmt.distinct())}
            ordered = [found[card_id] for card_id in ids if card_id in found]
            return ordered[offset : offset + limit]

        stmt = select(Card).order_by(Card.name)
        if set_prefix:
            stmt = stmt.join(CardPrint).where(CardPrint.set_prefix == set_prefix.upper())
        stmt = stmt.distinct().offset(offset).limit(limit)
        return list(self.session.scalars(stmt))
