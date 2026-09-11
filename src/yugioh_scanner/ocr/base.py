"""Contrato do OCR (plano §6.1) — o ponto de desacoplamento.

Nenhum módulo fora de `ocr/` importa uma biblioteca de OCR. Trocar de motor é
mudar `YGS_OCR_PROVIDER`; nada mais no código muda.

O provider recebe **imagens já recortadas e realçadas**, não o caminho do
arquivo. Quem lê e prepara é `images/preprocess.py`: assim o arquivo é lido uma
vez só, e o mesmo recorte serve para qualquer motor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from PIL.Image import Image

#: Nomes das regiões que o pipeline pede. Strings livres, mas estas duas são o
#: contrato de fato entre `preprocess`, os providers e o matching.
REGION_NAME = "name"
REGION_CODE = "code"
REGION_FULL = "full"


@dataclass(frozen=True, slots=True)
class TextLine:
    """Uma linha de texto reconhecida, com a confiança que o motor reportou.

    `x`: posição horizontal (borda esquerda, em pixels da região) — existe só
    para `OCRResult.joined()` conseguir concatenar em ordem de leitura.
    Nenhum provider garante que devolve as caixas já em ordem (o RapidOCR, em
    particular, devolve na ordem interna de detecção, não da esquerda para a
    direita — verificado numa foto real onde o nome saiu embaralhado sem
    isto: plano §9, Fase 9).
    """

    text: str
    confidence: float = 1.0
    x: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "confidence": round(self.confidence, 4)}


@dataclass(frozen=True)
class OCRRequest:
    """O que se pede a um provider.

    `regions` mapeia nome → imagem já preparada. `source` existe só para
    diagnóstico (nome do arquivo original nos logs).
    """

    regions: Mapping[str, Image]
    source: str = ""
    hints: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OCRResult:
    """O que um provider devolve.

    É um DTO puro (sem imagens, sem handles) de propósito: ele atravessa a
    fronteira de processos entre o worker de OCR e o processo principal.
    """

    texts: Mapping[str, tuple[TextLine, ...]]
    provider: str
    elapsed_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def best(self, region: str) -> str:
        """Linha de maior confiança de uma região ('' se não houver)."""
        lines = self.texts.get(region) or ()
        if not lines:
            return ""
        return max(lines, key=lambda line: line.confidence).text

    def joined(self, region: str, separator: str = " ") -> str:
        """Todas as linhas da região, em ordem de leitura (esquerda→direita).

        Reordena por `TextLine.x` — nenhum provider garante devolver as
        caixas já nessa ordem (achado real da Fase 9: o RapidOCR embaralhava
        nomes com mais de uma caixa detectada). `sorted` é estável, então
        providers que não preenchem `x` (fica 0.0) mantêm a ordem original.
        """
        lines = sorted(self.texts.get(region, ()), key=lambda line: line.x)
        return separator.join(line.text for line in lines if line.text)

    def confidence(self, region: str) -> float:
        """Confiança média da região — entra no score final (plano §7.4)."""
        lines = self.texts.get(region) or ()
        if not lines:
            return 0.0
        return sum(line.confidence for line in lines) / len(lines)

    @property
    def is_empty(self) -> bool:
        return not any(line.text.strip() for lines in self.texts.values() for line in lines)

    def as_dict(self) -> dict[str, Any]:
        """Forma serializável, gravada em `scan_image.ocr_raw`.

        É o que a tela de revisão mostra como "texto bruto do OCR" sem precisar
        reprocessar a imagem.
        """
        return {
            "provider": self.provider,
            "elapsed_ms": self.elapsed_ms,
            "texts": {
                region: [line.as_dict() for line in lines] for region, lines in self.texts.items()
            },
            **({"raw": self.raw} if self.raw else {}),
        }


@runtime_checkable
class OCRProvider(Protocol):
    """Interface que todo motor de OCR implementa."""

    #: Identificador usado em `YGS_OCR_PROVIDER` e gravado em `scan_job`.
    name: str

    #: True para motores que gastam o tempo esperando rede (LLM), False para os
    #: que gastam CPU local. É isto que escolhe entre pool de processos e de
    #: threads (plano §12.1) — sem essa distinção, uma das duas cargas fica com
    #: a estratégia errada.
    is_io_bound: bool

    def warmup(self) -> None:
        """Carrega o modelo. Chamado uma vez por worker, nunca por imagem."""
        ...

    def read(self, request: OCRRequest) -> OCRResult:
        """Extrai texto das regiões pedidas."""
        ...

    def close(self) -> None:
        """Libera recursos."""
        ...


class BaseOCRProvider:
    """Implementação parcial com o que todo provider repetiria.

    Herdar daqui é opcional — o que vale é satisfazer o `Protocol`.
    """

    name = "base"
    is_io_bound = False

    def warmup(self) -> None:
        return

    def close(self) -> None:
        return

    def read(self, request: OCRRequest) -> OCRResult:  # pragma: no cover - abstrato
        raise NotImplementedError
