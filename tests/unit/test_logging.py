"""Redação de segredos no log (plano §21 — a chave nunca pode vazar)."""

from __future__ import annotations

from yugioh_scanner.logging_setup import redact_secrets


def redact(**event: object) -> dict[str, object]:
    return dict(redact_secrets(None, "info", dict(event)))  # type: ignore[arg-type]


class TestKeyBasedRedaction:
    def test_known_secret_keys_are_wiped(self) -> None:
        result = redact(api_key="qualquer-coisa", token="abc", authorization="Bearer x")
        assert result == {"api_key": "***", "token": "***", "authorization": "***"}

    def test_key_matching_is_case_insensitive(self) -> None:
        assert redact(API_KEY="segredo")["API_KEY"] == "***"


class TestPatternBasedRedaction:
    def test_anthropic_key_in_free_text_is_masked(self) -> None:
        """Mesmo quem loga um objeto inteiro por engano não vaza a chave."""
        event = redact(msg="falhou com sk-ant-api03-abcdefghijklmnopqrstuv ao chamar")
        text = str(event["msg"])
        assert "abcdefghijklmnop" not in text
        assert "sk-ant-" in text, "o prefixo fica, para dar para identificar a chave"

    def test_url_with_embedded_key(self) -> None:
        event = redact(url="https://api.exemplo.com/x?key=sk-ant-api03-vazamento12345")
        assert "vazamento12345" not in str(event["url"])


class TestPassthrough:
    def test_normal_fields_are_untouched(self) -> None:
        event = redact(image="IMG_001.jpg", confidence=0.98, job_id=42)
        assert event == {"image": "IMG_001.jpg", "confidence": 0.98, "job_id": 42}

    def test_card_names_are_not_mangled(self) -> None:
        assert redact(card="Blue-Eyes White Dragon")["card"] == "Blue-Eyes White Dragon"
