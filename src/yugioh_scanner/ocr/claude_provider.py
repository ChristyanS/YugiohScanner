"""Fallback opcional por LLM Vision (plano §6.4, Fase 10, ADR 0004).

Não é o motor padrão — é acionado só pela cascata de `ScanService` quando o
OCR local deixou a leitura `pending`/`manual` (plano §6.3). Por isso a
imagem enviada é a que o pipeline já preparou (recortada/reduzida a 1600px de
lado maior em `images/preprocess.py`), nunca o arquivo original de 12 MP —
custo de tokens ~10× menor, como o plano exige.

Sem `ANTHROPIC_API_KEY`, `warmup()` levanta `LlmApiKeyMissingError` — quem
chama (a cascata) trata isso como "degradar em silêncio para manual", nunca
como falha do scan inteiro.
"""

from __future__ import annotations

import io
import json
import time
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..errors import LlmApiKeyMissingError, OcrProviderError, OcrProviderUnavailableError
from .base import (
    REGION_CODE,
    REGION_FULL,
    REGION_NAME,
    BaseOCRProvider,
    OCRRequest,
    OCRResult,
    TextLine,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from PIL.Image import Image

#: Ordem de preferência ao montar as imagens da mensagem — `full` primeiro
#: porque é o caso normal da cascata; as ROIs entram quando alguém usa
#: `YGS_OCR_PROVIDER=claude` diretamente (fora da cascata) e o pipeline manda
#: as regiões de nome/código em vez da carta inteira.
_REGION_ORDER = (REGION_FULL, REGION_NAME, REGION_CODE)

_PROMPT = (
    "You are reading a photo of a physical Yu-Gi-Oh! trading card. Extract these "
    "fields and answer ONLY with the JSON object the schema requires:\n"
    "- name: the card's printed name, exactly as shown (any language).\n"
    "- set_code: the collector number printed in the bottom-right corner, in the "
    "form PREFIX-REGIONNNN (e.g. 'LOB-EN001', 'SDK-001').\n"
    "- rarity: the rarity mark if visible (e.g. 'Common', 'Ultra Rare', 'Secret "
    "Rare'), otherwise an empty string.\n"
    "- edition: edition text if printed (e.g. '1st Edition'), otherwise an empty "
    "string.\n"
    "- language: the language the printed text is in, as a short code (EN, PT, "
    "FR, DE, IT, JA, KO, ...).\n"
    "- legible: false if the photo is too blurry, dark, angled, or obstructed to "
    "read the name or set code with confidence; true otherwise.\n"
    "Use an empty string for any field you cannot determine — never invent a "
    "value."
)

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "set_code": {"type": "string"},
        "rarity": {"type": "string"},
        "edition": {"type": "string"},
        "language": {"type": "string"},
        "legible": {"type": "boolean"},
    },
    "required": ["name", "set_code", "rarity", "edition", "language", "legible"],
    "additionalProperties": False,
}

#: JSON pequeno; a carta já chega recortada, então não há razão para gastar
#: mais que isto (plano §6.4 — custo é o ponto central da decisão de Fase 10).
_MAX_TOKENS = 512
_DEFAULT_MODEL = "claude-opus-5"
_DEFAULT_TIMEOUT_S = 60


class ClaudeVisionOCRProvider(BaseOCRProvider):
    """Lê a carta inteira com Claude Vision e devolve nome + set code."""

    name = "claude"
    #: Tempo é de rede, não de CPU — decide o pool de threads em vez de
    #: processos (plano §12, `scanner/executor.py`).
    is_io_bound = True

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._client: Any = None
        self._model = settings.llm_model if settings is not None else _DEFAULT_MODEL
        self._timeout = settings.ocr_timeout_s if settings is not None else _DEFAULT_TIMEOUT_S

    def warmup(self) -> None:
        if self._client is not None:
            return
        try:
            import anthropic
        except ImportError as exc:
            raise OcrProviderUnavailableError(self.name, "llm") from exc

        api_key = None
        if self._settings is not None and self._settings.llm_api_key is not None:
            api_key = self._settings.llm_api_key.get_secret_value()
        if not api_key:
            raise LlmApiKeyMissingError(self.name)

        self._client = anthropic.Anthropic(api_key=api_key)

    def close(self) -> None:
        self._client = None

    def read(self, request: OCRRequest) -> OCRResult:
        self.warmup()
        started = time.perf_counter()

        if not request.regions:
            return OCRResult(texts={}, provider=self.name, elapsed_ms=0)

        content = self._build_content(request.regions)
        try:
            response = self._client.with_options(timeout=self._timeout).messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": content}],
                extra_body={
                    "output_config": {"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}}
                },
            )
        except Exception as exc:  # SDK expõe várias classes de erro (rede, 4xx, 5xx)
            raise OcrProviderError(f"Falha ao consultar Claude Vision: {exc}") from exc

        data = self._parse_response(response)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return OCRResult(
            texts=self._to_texts(data),
            provider=self.name,
            elapsed_ms=elapsed_ms,
            raw=data,
        )

    def _build_content(self, regions: Mapping[str, Image]) -> list[dict[str, Any]]:
        ordered = [name for name in _REGION_ORDER if name in regions]
        ordered.extend(name for name in regions if name not in ordered)

        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": _encode_jpeg(regions[name]),
                },
            }
            for name in ordered
        ]
        content.append({"type": "text", "text": _PROMPT})
        return content

    def _parse_response(self, response: Any) -> dict[str, Any]:
        if getattr(response, "stop_reason", None) == "refusal":
            return {"legible": False, "refusal": True}

        text = ""
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text = block.text
                break

        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return {"legible": False, "parse_error": True, "raw_text": text}

        if not isinstance(data, dict):
            return {"legible": False, "parse_error": True, "raw_text": text}
        return data

    def _to_texts(self, data: dict[str, Any]) -> dict[str, tuple[TextLine, ...]]:
        # `legible=False` não zera a confiança sozinho — o modelo às vezes
        # arrisca uma leitura mesmo incerto. É só um desconto (plano §7.4,
        # `ConfidenceInput.ocr_quality`), não uma exclusão.
        confidence = 1.0 if data.get("legible") else 0.5

        texts: dict[str, tuple[TextLine, ...]] = {}
        name = str(data.get("name") or "").strip()
        code = str(data.get("set_code") or "").strip()
        if name:
            texts[REGION_NAME] = (TextLine(text=name, confidence=confidence),)
        if code:
            texts[REGION_CODE] = (TextLine(text=code, confidence=confidence),)
        return texts


def _encode_jpeg(image: Image) -> str:
    import base64

    buffer = io.BytesIO()
    rgb = image.convert("RGB") if image.mode not in ("RGB", "L") else image
    rgb.save(buffer, format="JPEG", quality=90)
    return base64.standard_b64encode(buffer.getvalue()).decode("ascii")


def build(settings: Settings) -> ClaudeVisionOCRProvider:
    return ClaudeVisionOCRProvider(settings)
