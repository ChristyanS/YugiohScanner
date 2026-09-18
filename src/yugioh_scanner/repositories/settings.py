"""Leitura e escrita de preferências do usuário (`app_setting`).

Mesmo formato de `repositories/sync_state.py`, tabela separada de propósito
— ver docstring de `AppSetting` em `db/tables.py`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import AppSetting, utcnow


class SettingsRepository:
    """Chave-valor simples sobre `app_setting`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> str | None:
        return self.session.scalar(select(AppSetting.value).where(AppSetting.key == key))

    def get_all(self) -> dict[str, str | None]:
        return {row.key: row.value for row in self.session.scalars(select(AppSetting))}

    def set(self, key: str, value: str | None) -> None:
        row = self.session.get(AppSetting, key)
        if row is None:
            self.session.add(AppSetting(key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()
        # `get()` roda um `select()` Core direto (não passa pelo identity map),
        # e a sessão é `autoflush=False` (db/session.py) — sem o flush aqui,
        # ler a preferência logo depois de gravá-la na mesma sessão (como a
        # rota `POST /settings/ui-language` faz) devolveria o valor antigo.
        self.session.flush()
