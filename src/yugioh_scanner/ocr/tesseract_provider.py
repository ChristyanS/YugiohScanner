"""Provider alternativo: Tesseract via `pytesseract`.

Só faz sentido para quem já tem o Tesseract instalado no sistema, e é forte
justamente onde o pipeline mais sofre: o **set code**. Com `--psm 7` (uma única
linha) e whitelist de `A-Z0-9-`, ele erra bem menos naquele texto minúsculo do
que um modelo genérico (plano §6.2).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..errors import OcrProviderUnavailableError
from .base import REGION_CODE, BaseOCRProvider, OCRRequest, OCRResult, TextLine

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

#: Uma linha só, alfabeto restrito: é o que o set code é.
CODE_CONFIG = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
#: Bloco de texto uniforme, alfabeto livre (nomes têm apóstrofo, dois-pontos…).
TEXT_CONFIG = "--psm 6"


class TesseractProvider(BaseOCRProvider):
    name = "tesseract"
    is_io_bound = False

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._pytesseract: Any = None

    def warmup(self) -> None:
        if self._pytesseract is not None:
            return
        try:
            import pytesseract
        except ImportError as exc:
            raise OcrProviderUnavailableError("tesseract", "tesseract") from exc

        if self._settings is not None and self._settings.tesseract_cmd is not None:
            pytesseract.pytesseract.tesseract_cmd = str(self._settings.tesseract_cmd)

        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # pragma: no cover - depende do ambiente
            raise OcrProviderUnavailableError("tesseract", "tesseract") from exc

        self._pytesseract = pytesseract

    def read(self, request: OCRRequest) -> OCRResult:
        self.warmup()
        started = time.perf_counter()

        texts: dict[str, tuple[TextLine, ...]] = {}
        for region, image in request.regions.items():
            config = CODE_CONFIG if region == REGION_CODE else TEXT_CONFIG
            texts[region] = self._read_region(image, config)

        return OCRResult(
            texts=texts,
            provider=self.name,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _read_region(self, image: Image, config: str) -> tuple[TextLine, ...]:
        data = self._pytesseract.image_to_data(
            image, config=config, output_type=self._pytesseract.Output.DICT
        )
        lines: list[TextLine] = []
        for text, raw_confidence, left in zip(
            data["text"], data["conf"], data["left"], strict=False
        ):
            cleaned = str(text).strip()
            if not cleaned:
                continue
            try:
                # O Tesseract usa -1 para "sem confiança" e a escala 0..100.
                confidence = max(0.0, float(raw_confidence)) / 100.0
            except (TypeError, ValueError):
                confidence = 0.0
            try:
                x = float(left)
            except (TypeError, ValueError):
                x = 0.0
            lines.append(TextLine(text=cleaned, confidence=confidence, x=x))
        return tuple(lines)


def build(settings: Settings) -> TesseractProvider:
    return TesseractProvider(settings)
