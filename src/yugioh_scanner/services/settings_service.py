"""Preferências do usuário (plano de idioma global): idioma da UI e idioma
padrão de exibição de carta.

Fino de propósito, no mesmo espírito de `catalog_service.py`: valida contra
o vocabulário aceito e cai num default sensato quando a preferência ainda
não foi definida — nunca propaga erro por ausência, só por valor inválido
ao **gravar**.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.tables import (
    APP_SETTING_DEFAULT_CARD_LANGUAGE,
    APP_SETTING_UI_LANGUAGE,
    DEFAULT_UI_LANGUAGE,
    UI_LANGUAGES,
)
from ..errors import InvalidSettingValueError
from ..repositories.settings import SettingsRepository
from .catalog_service import DISPLAY_LANGUAGES


class SettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = SettingsRepository(session)

    def get_ui_language(self) -> str:
        value = self.repo.get(APP_SETTING_UI_LANGUAGE)
        return value if value in UI_LANGUAGES else DEFAULT_UI_LANGUAGE

    def set_ui_language(self, value: str) -> None:
        if value not in UI_LANGUAGES:
            raise InvalidSettingValueError(APP_SETTING_UI_LANGUAGE, value, UI_LANGUAGES)
        self.repo.set(APP_SETTING_UI_LANGUAGE, value)

    def get_default_card_language(self) -> str:
        """Idioma padrão de exibição/ordenação de carta em Coleção/Banco de
        Dados. Sem preferência explícita salva, deriva do idioma global da
        UI (`pt-BR` -> `PT`, `en` -> `EN`) em vez de cair fixo em inglês —
        trocar o idioma do sistema deve mudar o padrão junto (plano de
        idioma global, fase 2)."""
        value = self.repo.get(APP_SETTING_DEFAULT_CARD_LANGUAGE)
        if value in DISPLAY_LANGUAGES:
            return value
        return "PT" if self.get_ui_language() == "pt-BR" else "EN"

    def set_default_card_language(self, value: str) -> None:
        upper = value.upper()
        if upper not in DISPLAY_LANGUAGES:
            raise InvalidSettingValueError(
                APP_SETTING_DEFAULT_CARD_LANGUAGE, value, DISPLAY_LANGUAGES
            )
        self.repo.set(APP_SETTING_DEFAULT_CARD_LANGUAGE, upper)
