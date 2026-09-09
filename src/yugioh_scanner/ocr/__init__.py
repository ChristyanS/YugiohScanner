"""Motores de OCR por trás de um único Protocol (plano §6)."""

from .base import (
    REGION_CODE,
    REGION_FULL,
    REGION_NAME,
    BaseOCRProvider,
    OCRProvider,
    OCRRequest,
    OCRResult,
    TextLine,
)
from .registry import (
    create_provider,
    known_providers,
    provider_from_settings,
    register_provider,
    unregister_provider,
)

__all__ = [
    "REGION_CODE",
    "REGION_FULL",
    "REGION_NAME",
    "BaseOCRProvider",
    "OCRProvider",
    "OCRRequest",
    "OCRResult",
    "TextLine",
    "create_provider",
    "known_providers",
    "provider_from_settings",
    "register_provider",
    "unregister_provider",
]
