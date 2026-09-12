"""Leitura do catálogo de cartas — busca e detalhe (Fase 8: navegação Web).

Sem regra de negócio: o serviço acima (`services/catalog_service.py`) decide
o que fazer quando nada é encontrado. Isto aqui só traduz consulta em SQL.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.fts import search_alt_card_ids, search_card_ids
from ..db.tables import Card, CardAltName, CardPrint

#: Teto de candidatos considerados para uma busca textual (usado tanto para
#: paginar quanto para contar). Sem teto, uma query genérica ("dragon") teria
#: que varrer o índice FTS inteiro só para saber "quantas páginas existem" —
#: ninguém rola até a página 200 de um resultado de busca, então um teto
#: generoso é mais barato que exatidão que ninguém usa.
_SEARCH_FETCH_CAP = 500


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

    def alt_names_for(self, card_id: int) -> list[CardAltName]:
        """Todas as traduções conhecidas de uma carta (Fase 3: seletor de
        idioma na tela de detalhe)."""
        stmt = select(CardAltName).where(CardAltName.card_id == card_id)
        return list(self.session.scalars(stmt))

    def alt_names_map(self, card_ids: Sequence[int], language: str) -> dict[int, CardAltName]:
        """Tradução num idioma só, para várias cartas de uma vez — usado pela
        listagem do banco de dados para não fazer uma consulta por linha."""
        if not card_ids:
            return {}
        stmt = select(CardAltName).where(
            CardAltName.card_id.in_(card_ids), CardAltName.language == language
        )
        return {row.card_id: row for row in self.session.scalars(stmt)}

    def _matching_ids(self, query: str, *, limit: int) -> list[int]:
        """IDs de carta cujo nome bate com `query` **em qualquer idioma**
        sincronizado (Fase 3: busca multilíngue) — inglês primeiro (é o nome
        canônico, então tende a ser o que mais gente digita), depois FR/DE/
        IT/PT para quem pesquisa pelo nome que está impresso na carta física.
        Nunca duplica: uma carta que bate nos dois só aparece uma vez, na
        posição do primeiro casamento.
        """
        primary = search_card_ids(self.session, query, limit=limit)
        if len(primary) >= limit:
            return primary

        seen = set(primary)
        merged = list(primary)
        for card_id in search_alt_card_ids(self.session, query, limit=limit):
            if card_id not in seen:
                seen.add(card_id)
                merged.append(card_id)
        return merged[:limit]

    def search(
        self,
        query: str | None = None,
        *,
        set_prefix: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Card]:
        """Busca por nome (FTS5, plano §7.3, multilíngue desde a Fase 3 do
        faseamento web) opcionalmente filtrada por set.

        Sem `query`: lista o catálogo em ordem alfabética — é o estado inicial
        da tela `/collection`'s' equivalente de catálogo e do `/api/v1/cards`
        sem `q` (navegação/paginação pura).
        """
        if query and query.strip():
            # Um pouco mais que `limit` para sobrar candidato depois do
            # filtro de set opcional.
            ids = self._matching_ids(query, limit=limit + offset + 50)
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

    def count(self, query: str | None = None, *, set_prefix: str | None = None) -> int:
        """Total de cartas que `search()` encontraria, para paginação.

        Com `query`: conta dentro do mesmo teto de candidatos que `search()`
        usa (`_SEARCH_FETCH_CAP`) — resultado de busca textual não precisa de
        contagem exata além disso (ver docstring do teto). Sem `query`
        (navegação alfabética do catálogo inteiro): conta de verdade, é uma
        soma barata com os índices existentes.
        """
        if query and query.strip():
            ids = self._matching_ids(query, limit=_SEARCH_FETCH_CAP)
            if not ids:
                return 0
            stmt = select(func.count(func.distinct(Card.id))).where(Card.id.in_(ids))
            if set_prefix:
                stmt = stmt.join(CardPrint).where(CardPrint.set_prefix == set_prefix.upper())
            return int(self.session.scalar(stmt) or 0)

        stmt = select(func.count(func.distinct(Card.id)))
        if set_prefix:
            stmt = stmt.join(CardPrint).where(CardPrint.set_prefix == set_prefix.upper())
        return int(self.session.scalar(stmt) or 0)
