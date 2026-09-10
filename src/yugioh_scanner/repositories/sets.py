"""Leitura do catálogo de sets (Fase 8: `GET /api/v1/sets`)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import CardSet


class SetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self, *, limit: int = 1000, offset: int = 0) -> list[CardSet]:
        stmt = select(CardSet).order_by(CardSet.set_name).offset(offset).limit(limit)
        return list(self.session.scalars(stmt))
