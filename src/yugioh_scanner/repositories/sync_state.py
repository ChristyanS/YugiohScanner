"""Leitura e escrita do estado de sincronização."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import SyncState, utcnow


class SyncStateRepository:
    """Chave-valor simples sobre `sync_state`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> str | None:
        return self.session.scalar(select(SyncState.value).where(SyncState.key == key))

    def get_all(self) -> dict[str, str | None]:
        return {row.key: row.value for row in self.session.scalars(select(SyncState))}

    def set(self, key: str, value: str | None) -> None:
        row = self.session.get(SyncState, key)
        if row is None:
            self.session.add(SyncState(key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()

    def set_many(self, values: dict[str, str | None]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def get_datetime(self, key: str) -> dt.datetime | None:
        raw = self.get(key)
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
