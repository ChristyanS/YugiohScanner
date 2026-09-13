"""`ClaudeVisionOCRProvider` (plano §6.4, Fase 10).

O SDK da Anthropic é substituído por um módulo falso injetado em
`sys.modules` — o import é preguiçoso (`ocr/registry.py`), então isolar o
teste do pacote real (instalado ou não) é só interceptar `import anthropic`
antes de chamar `warmup()`/`read()`.
"""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from pydantic import SecretStr

from yugioh_scanner.config import Settings
from yugioh_scanner.errors import LlmApiKeyMissingError, OcrProviderError
from yugioh_scanner.ocr.base import REGION_CODE, REGION_FULL, REGION_NAME, OCRRequest
from yugioh_scanner.ocr.claude_provider import ClaudeVisionOCRProvider


def _card_image() -> Image.Image:
    return Image.new("RGB", (16, 16), color="white")


def _with_key(settings: Settings) -> Settings:
    return settings.model_copy(update={"llm_api_key": SecretStr("sk-ant-test")})


class _FakeMessages:
    def __init__(self, response: Any, capture: dict[str, Any]) -> None:
        self._response = response
        self._capture = capture

    def create(self, **kwargs: Any) -> Any:
        self._capture["kwargs"] = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response: Any, capture: dict[str, Any]) -> None:
        self.messages = _FakeMessages(response, capture)

    def with_options(self, **_kwargs: Any) -> _FakeClient:
        return self


def _install_fake_anthropic(
    monkeypatch: pytest.MonkeyPatch, response: Any, capture: dict[str, Any]
) -> None:
    module = types.ModuleType("anthropic")
    module.Anthropic = lambda api_key=None: _FakeClient(response, capture)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)


def _response(payload: dict[str, Any], stop_reason: str = "end_turn") -> SimpleNamespace:
    text_block = SimpleNamespace(type="text", text=json.dumps(payload))
    return SimpleNamespace(content=[text_block], stop_reason=stop_reason)


class TestWarmup:
    def test_raises_without_api_key(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_anthropic(monkeypatch, response=_response({}), capture={})
        provider = ClaudeVisionOCRProvider(settings)

        with pytest.raises(LlmApiKeyMissingError):
            provider.warmup()

    def test_succeeds_with_api_key(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_anthropic(monkeypatch, response=_response({}), capture={})
        provider = ClaudeVisionOCRProvider(_with_key(settings))

        provider.warmup()  # não levanta

    def test_is_idempotent(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}
        module = types.ModuleType("anthropic")

        def factory(api_key: str | None = None) -> _FakeClient:
            calls["n"] += 1
            return _FakeClient(_response({}), {})

        module.Anthropic = factory  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", module)

        provider = ClaudeVisionOCRProvider(_with_key(settings))
        provider.warmup()
        provider.warmup()

        assert calls["n"] == 1


class TestRead:
    def test_no_regions_short_circuits(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture: dict[str, Any] = {}
        _install_fake_anthropic(monkeypatch, response=_response({}), capture=capture)
        provider = ClaudeVisionOCRProvider(_with_key(settings))

        result = provider.read(OCRRequest(regions={}, source="x.jpg"))

        assert result.texts == {}
        assert capture == {}  # a API nunca foi chamada

    def test_parses_name_and_code(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture: dict[str, Any] = {}
        payload = {
            "name": "Dark Magician",
            "set_code": "SDK-001",
            "rarity": "Ultra Rare",
            "edition": "1st Edition",
            "language": "EN",
            "legible": True,
        }
        _install_fake_anthropic(monkeypatch, response=_response(payload), capture=capture)
        provider = ClaudeVisionOCRProvider(_with_key(settings))

        result = provider.read(
            OCRRequest(regions={REGION_FULL: _card_image()}, source="card.jpg")
        )

        assert result.best(REGION_NAME) == "Dark Magician"
        assert result.best(REGION_CODE) == "SDK-001"
        assert result.provider == "claude"
        assert result.raw["rarity"] == "Ultra Rare"

        sent = capture["kwargs"]
        assert sent["model"] == settings.llm_model
        assert sent["extra_body"]["output_config"]["format"]["type"] == "json_schema"
        images_sent = [
            block for block in sent["messages"][0]["content"] if block["type"] == "image"
        ]
        assert len(images_sent) == 1

    def test_low_confidence_when_illegible(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "name": "Dark Magician",
            "set_code": "",
            "rarity": "",
            "edition": "",
            "language": "EN",
            "legible": False,
        }
        _install_fake_anthropic(monkeypatch, response=_response(payload), capture={})
        provider = ClaudeVisionOCRProvider(_with_key(settings))

        result = provider.read(
            OCRRequest(regions={REGION_FULL: _card_image()}, source="card.jpg")
        )

        assert result.confidence(REGION_NAME) == pytest.approx(0.5)

    def test_refusal_returns_empty(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_anthropic(
            monkeypatch, response=_response({}, stop_reason="refusal"), capture={}
        )
        provider = ClaudeVisionOCRProvider(_with_key(settings))

        result = provider.read(
            OCRRequest(regions={REGION_FULL: _card_image()}, source="card.jpg")
        )

        assert result.is_empty

    def test_malformed_json_degrades_to_empty(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="not json")], stop_reason="end_turn"
        )
        _install_fake_anthropic(monkeypatch, response=bad_response, capture={})
        provider = ClaudeVisionOCRProvider(_with_key(settings))

        result = provider.read(
            OCRRequest(regions={REGION_FULL: _card_image()}, source="card.jpg")
        )

        assert result.is_empty

    def test_api_error_raises_ocr_provider_error(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_anthropic(monkeypatch, response=RuntimeError("network down"), capture={})
        provider = ClaudeVisionOCRProvider(_with_key(settings))

        with pytest.raises(OcrProviderError):
            provider.read(OCRRequest(regions={REGION_FULL: _card_image()}, source="card.jpg"))
