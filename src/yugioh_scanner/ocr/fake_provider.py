"""Provider determinístico para testes (plano §19.3).

Existe para que o pipeline inteiro — descoberta, paralelismo, matching,
idempotência, coleção — possa ser testado **sem OCR de verdade**: rápido,
determinístico e sem baixar modelo nenhum.

Cobre ~90% da lógica do scanner. O OCR real tem sua própria suíte, marcada e
opt-in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseOCRProvider, OCRRequest, OCRResult, TextLine

#: Formato do roteiro: nome do arquivo (sem pasta) → {região: texto}.
Script = dict[str, dict[str, str]]


class FakeOCRProvider(BaseOCRProvider):
    """Devolve textos pré-programados por nome de arquivo.

    >>> provider = FakeOCRProvider({"a.jpg": {"name": "Dark Magician"}})
    >>> provider.read(OCRRequest(regions={}, source="a.jpg")).best("name")
    'Dark Magician'
    """

    name = "fake"
    is_io_bound = False

    def __init__(
        self,
        script: Script | None = None,
        *,
        default: dict[str, str] | None = None,
        fail_on: set[str] | None = None,
        confidence: float = 0.95,
    ) -> None:
        self.script = script or {}
        self.default = default or {}
        #: Arquivos para os quais o provider levanta exceção — é assim que se
        #: testa que uma imagem problemática não derruba as demais.
        self.fail_on = fail_on or set()
        self.confidence = confidence
        self.warmup_calls = 0
        self.read_calls = 0

    def warmup(self) -> None:
        self.warmup_calls += 1

    def read(self, request: OCRRequest) -> OCRResult:
        self.read_calls += 1
        key = Path(request.source).name

        if key in self.fail_on:
            raise RuntimeError(f"Falha simulada de OCR em {key}")

        texts = self.script.get(key, self.default)
        return OCRResult(
            texts={
                region: (TextLine(text=value, confidence=self.confidence),)
                for region, value in texts.items()
                if value
            },
            provider=self.name,
            elapsed_ms=1,
        )


def build(settings: Any) -> FakeOCRProvider:
    """Fábrica registrada como `fake`. Sem roteiro, devolve OCR vazio."""
    return FakeOCRProvider()
