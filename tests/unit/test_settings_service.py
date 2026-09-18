"""`SettingsService`/`SettingsRepository` — preferências globais (idioma da
UI, idioma padrão de exibição de carta).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from yugioh_scanner.errors import InvalidSettingValueError
from yugioh_scanner.services.settings_service import SettingsService


class TestUiLanguage:
    def test_default_is_pt_br(self, session: Session) -> None:
        assert SettingsService(session).get_ui_language() == "pt-BR"

    def test_set_and_get_same_session(self, session: Session) -> None:
        # Read-after-write na mesma sessão: `SettingsRepository.get()` roda
        # um `select()` Core direto, que não vê mudanças pendentes sem
        # flush explícito (a sessão é `autoflush=False`) — regressão real
        # encontrada ao implementar a rota `POST /settings/ui-language`.
        service = SettingsService(session)
        service.set_ui_language("en")
        assert service.get_ui_language() == "en"

    def test_update_existing_value(self, session: Session) -> None:
        service = SettingsService(session)
        service.set_ui_language("en")
        service.set_ui_language("pt-BR")
        assert service.get_ui_language() == "pt-BR"

    def test_invalid_value_rejected(self, session: Session) -> None:
        with pytest.raises(InvalidSettingValueError):
            SettingsService(session).set_ui_language("xx")


class TestDefaultCardLanguage:
    def test_default_follows_ui_language_pt_br(self, session: Session) -> None:
        # `ui_language` já cai em "pt-BR" por padrão (TestUiLanguage acima).
        assert SettingsService(session).get_default_card_language() == "PT"

    def test_default_follows_ui_language_en(self, session: Session) -> None:
        service = SettingsService(session)
        service.set_ui_language("en")
        assert service.get_default_card_language() == "EN"

    def test_set_and_get_uppercases(self, session: Session) -> None:
        service = SettingsService(session)
        service.set_default_card_language("pt")
        assert service.get_default_card_language() == "PT"

    def test_invalid_value_rejected(self, session: Session) -> None:
        with pytest.raises(InvalidSettingValueError):
            SettingsService(session).set_default_card_language("xx")
