"""Leitura do catálogo de sets (Fase 8: `GET /api/v1/sets`)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import CardSet


class SetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self, *, limit: int = 1000, offset: int = 0) -> list[CardSet]:
        stmt = select(CardSet).order_by(CardSet.set_name).offset(offset).limit(limit)
        return list(self.session.scalars(stmt))

    def get(self, set_code: str) -> CardSet | None:
        return self.session.get(CardSet, set_code)

    def create(
        self,
        set_code: str,
        set_name: str,
        *,
        num_of_cards: int | None = None,
        tcg_date: dt.date | None = None,
    ) -> CardSet:
        """Insert cego — quem decide se pode inserir (código já existe ou
        não) é o service (`CatalogService.register_set`), mesma divisão de
        responsabilidade do resto dos repositórios deste módulo."""
        row = CardSet(
            set_code=set_code,
            set_name=set_name,
            num_of_cards=num_of_cards,
            tcg_date=tcg_date,
        )
        self.session.add(row)
        self.session.flush()
        return row
