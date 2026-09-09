"""Provider padrão: RapidOCR (modelos PP-OCR rodando em ONNXRuntime).

Escolhido para ser o default porque instala com `pip` puro — sem instalador de
sistema, que é o que o Tesseract exigiria no Windows (plano §6.2).

O modelo custa 1–2 s para carregar. `warmup()` existe exatamente para que isso
aconteça **uma vez por worker**, e não uma vez por imagem: sem essa separação,
processar 500 fotos gastaria ~15 minutos só carregando modelo.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import Settings
from ..errors import OcrProviderUnavailableError
from .base import BaseOCRProvider, OCRRequest, OCRResult, TextLine

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image


class RapidOCRProvider(BaseOCRProvider):
    """Motor local baseado em ONNXRuntime."""

    name = "rapidocr"
    is_io_bound = False

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._engine: Any = None

    def warmup(self) -> None:
        """Carrega o modelo. Idempotente."""
        if self._engine is not None:
            return
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - depende do ambiente
            raise OcrProviderUnavailableError("rapidocr", "ocr") from exc
        # -1 = todos os núcleos. Dentro de um pool de processos isso é
        # desastroso: N workers × N threads em N núcleos faz cada carta levar
        # ~8× mais tempo (medido: 3,2s -> 26,7s com 5 workers). Quem decide o
        # valor é o executor, que sabe se está em pool ou não.
        threads = -1 if self._settings is None else (self._settings.ocr_threads_per_worker or -1)
        self._engine = RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=threads)

    def close(self) -> None:
        self._engine = None

    def read(self, request: OCRRequest) -> OCRResult:
        self.warmup()
        started = time.perf_counter()

        texts: dict[str, tuple[TextLine, ...]] = {}
        for region, image in request.regions.items():
            texts[region] = self._read_region(image)

        return OCRResult(
            texts=texts,
            provider=self.name,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _read_region(self, image: Image) -> tuple[TextLine, ...]:
        # O RapidOCR espera ndarray; imagens em L viram (H, W) e ele aceita.
        array = np.asarray(image)
        if array.size == 0:
            return ()

        result, _elapsed = self._engine(array)
        if not result:
            return ()

        lines: list[TextLine] = []
        for entry in result:
            # Cada item é [box, texto, confiança]; toleramos variações de
            # formato entre versões em vez de assumir a tupla exata.
            if len(entry) < 2:
                continue
            text = str(entry[1]).strip()
            if not text:
                continue
            try:
                confidence = float(entry[2]) if len(entry) > 2 else 1.0
            except (TypeError, ValueError):
                confidence = 1.0
            lines.append(TextLine(text=text, confidence=confidence))
        return tuple(lines)


def build(settings: Settings) -> RapidOCRProvider:
    """Fábrica registrada como `rapidocr`."""
    return RapidOCRProvider(settings)
