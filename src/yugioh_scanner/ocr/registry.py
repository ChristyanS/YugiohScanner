"""Registro de providers de OCR (plano §6.1).

Os imports são **preguiçosos**: `import yugioh_scanner.ocr` não pode exigir que
PaddleOCR, PyTorch ou o SDK da Anthropic estejam instalados. Cada fábrica só
importa sua biblioteca quando alguém pede aquele provider — e, se faltar, o
erro diz qual extra instalar em vez de estourar um `ImportError` cru.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from ..config import Settings
from ..errors import OcrProviderError
from .base import OCRProvider

#: nome → (módulo da fábrica, extra do pip que o instala)
_REGISTRY: dict[str, tuple[str, str]] = {
    "rapidocr": ("yugioh_scanner.ocr.rapidocr_provider", "ocr"),
    "tesseract": ("yugioh_scanner.ocr.tesseract_provider", "tesseract"),
    "claude": ("yugioh_scanner.ocr.claude_provider", "llm"),
    "fake": ("yugioh_scanner.ocr.fake_provider", ""),
}

#: Fábricas registradas em tempo de execução (usado por testes e plugins).
_OVERRIDES: dict[str, Callable[[Settings], OCRProvider]] = {}


def register_provider(name: str, factory: Callable[[Settings], OCRProvider]) -> None:
    """Registra (ou substitui) uma fábrica sob um nome."""
    _OVERRIDES[name] = factory


def unregister_provider(name: str) -> None:
    _OVERRIDES.pop(name, None)


def known_providers() -> list[str]:
    """Todos os nomes conhecidos, instalados ou não."""
    return sorted(set(_REGISTRY) | set(_OVERRIDES))


def create_provider(name: str, settings: Settings) -> OCRProvider:
    """Instancia um provider pelo nome.

    Não faz `warmup()`: quem decide quando carregar o modelo é o executor, que
    o faz uma vez por worker (plano §12.2).
    """
    if name in _OVERRIDES:
        return _OVERRIDES[name](settings)

    entry = _REGISTRY.get(name)
    if entry is None:
        raise OcrProviderError(
            f"Provider de OCR desconhecido: {name!r}.",
            hint=f"Disponíveis: {', '.join(known_providers())}",
        )

    module_path, _extra = entry
    module = import_module(module_path)
    factory: Callable[[Settings], OCRProvider] = module.build
    return factory(settings)


def provider_from_settings(settings: Settings, override: str | None = None) -> OCRProvider:
    """Provider configurado, com possibilidade de sobrescrita pela CLI."""
    return create_provider(override or settings.ocr_provider, settings)
