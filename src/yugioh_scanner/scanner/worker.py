"""O que roda dentro de cada worker (plano §12.2).

Regras que este módulo existe para respeitar:

* **Worker não toca no banco.** Recebe um caminho, devolve um DTO puro. Toda
  escrita acontece no processo principal, o que elimina de saída a classe de
  bugs `database is locked` (plano §2.3).
* **O modelo carrega uma vez por worker.** No Windows o multiprocessing usa
  `spawn`: cada worker reimporta este módulo do zero. Sem o estado global
  abaixo, o modelo de OCR seria carregado a cada imagem — 1–2 s por foto.
* **Falha de uma imagem é dado, não exceção.** Tudo é capturado aqui e volta
  como `ScanOutcome(status="error")`; o pool nunca vê a exceção.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import Settings
from ..errors import YugiohScannerError
from ..images.grid import detect_grid_cells, is_cell_blank, manual_grid_cells
from ..images.preprocess import BoundingBox, ImageError, PreparedImage, load_image, prepare_regions
from ..ocr.base import (
    REGION_CODE,
    REGION_FULL,
    REGION_NAME,
    REGION_PASSCODE,
    OCRProvider,
    OCRRequest,
    OCRResult,
)
from ..ocr.registry import create_provider

#: Estado por processo. Preenchido por `init_worker` e reutilizado em todas as
#: imagens que aquele worker processar.
_PROVIDER: OCRProvider | None = None
_SETTINGS: Settings | None = None
#: Liga a detecção de grade (`--grid`). Default `False` preserva o caminho de
#: hoje: sem isto, `images/grid.py` (e portanto `cv2`) nunca é importado.
_GRID_MODE: bool = False
#: Quando informado (`--grid-size`), o layout é explícito (linhas, colunas) —
#: pula a detecção automática por contorno de vez, sem precisar de `cv2`.
_GRID_SIZE: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class ScanTask:
    """Uma unidade de trabalho enviada ao worker.

    Leva **caminho**, nunca bytes: serializar 5 MB de imagem entre processos
    custaria mais que o próprio OCR.
    """

    path: Path
    file_hash: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class CropRegion:
    """Uma célula de uma grade de cartas dentro da foto de origem.

    `count == 1` e `bbox is None` é o caso de hoje (uma foto = uma carta) —
    inclusive quando `--grid` está ligado mas a foto não rendeu uma grade
    confiável (`detect_grid_cells` devolveu `[]`).
    """

    index: int
    count: int
    bbox: BoundingBox | None


@dataclass
class CropOutcome:
    """O que o OCR concluiu sobre um recorte (uma célula da grade, ou a foto
    inteira quando não há grade)."""

    region: CropRegion
    status: str = "ok"
    ocr: OCRResult | None = None
    error: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status in {"invalid", "error"}

    @property
    def read_name(self) -> str:
        """Nome lido, montado a partir de **todas** as caixas da região.

        O motor de OCR quebra um nome longo em várias detecções
        (`['BLUE-EYES WHITE', 'DRAGON']`). Pegar só a de maior confiança
        devolveria `'DRAGON'` — por isso aqui as linhas são unidas, e não
        escolhidas. Cai para a imagem inteira quando a ROI não rendeu nada.
        """
        if self.ocr is None:
            return ""
        return self.ocr.joined(REGION_NAME) or self.ocr.joined(REGION_FULL)

    @property
    def read_code(self) -> str:
        """Set code lido. Aqui vale o oposto: é um token só, então a melhor
        caixa é a resposta certa — juntar linhas só somaria ruído."""
        if self.ocr is None:
            return ""
        return self.ocr.best(REGION_CODE)

    @property
    def read_passcode(self) -> str:
        """Passcode ("Card ID") lido. Mesmo caso do set code: um token só,
        a melhor caixa é a resposta certa."""
        if self.ocr is None:
            return ""
        return self.ocr.best(REGION_PASSCODE)


@dataclass
class ScanOutcome:
    """O que volta do worker. Cruza a fronteira de processos, então é puro.

    Uma foto pode render vários recortes (`crops`) — uma grade de cartas
    (plano §22) vira N leituras de OCR independentes, cada uma com sua
    própria decisão de matching rio abaixo. `crops` tem exatamente um item
    (`CropRegion(0, 1, None)`) sempre que `--grid` está desligado ou a foto
    não rendeu uma grade confiável — o caminho de hoje, byte a byte.
    """

    task: ScanTask
    crops: list[CropOutcome] = field(default_factory=list)
    elapsed_ms: int = 0
    #: Células de uma grade `--grid-size` descartadas por estarem vazias
    #: (mesa do scanner sem carta) — nunca rodaram OCR, nunca viraram
    #: `CropOutcome`/`ScanResult`.
    skipped_empty: int = 0
    #: Preenchidos só quando a foto inteira falhou antes de qualquer recorte
    #: (arquivo corrompido, formato inválido) — `crops` fica vazio nesse caso.
    preprocess_status: str | None = None
    preprocess_error: str | None = None

    @property
    def path(self) -> Path:
        return self.task.path


def init_worker(
    provider_name: str,
    settings: Settings,
    *,
    grid_mode: bool = False,
    grid_size: tuple[int, int] | None = None,
) -> None:
    """Inicializador do pool: cria e aquece o provider uma única vez."""
    global _PROVIDER, _SETTINGS, _GRID_MODE, _GRID_SIZE
    _SETTINGS = settings
    _GRID_MODE = grid_mode
    _GRID_SIZE = grid_size
    _PROVIDER = create_provider(provider_name, settings)
    _PROVIDER.warmup()


def get_provider() -> OCRProvider:
    """Provider do worker atual.

    Se `init_worker` não rodou (execução serial, testes), o chamador precisa
    ter feito `set_provider` antes — falhar aqui é melhor do que carregar um
    modelo por engano no meio de um laço.
    """
    if _PROVIDER is None:  # pragma: no cover - erro de programação
        raise RuntimeError("Worker sem provider: chame init_worker() ou set_provider().")
    return _PROVIDER


def set_provider(
    provider: OCRProvider,
    settings: Settings,
    *,
    grid_mode: bool = False,
    grid_size: tuple[int, int] | None = None,
) -> None:
    """Injeta o provider no processo atual (execução serial e testes)."""
    global _PROVIDER, _SETTINGS, _GRID_MODE, _GRID_SIZE
    _PROVIDER = provider
    _SETTINGS = settings
    _GRID_MODE = grid_mode
    _GRID_SIZE = grid_size


def reset_provider() -> None:
    global _PROVIDER, _SETTINGS, _GRID_MODE, _GRID_SIZE
    if _PROVIDER is not None:
        _PROVIDER.close()
    _PROVIDER = None
    _SETTINGS = None
    _GRID_MODE = False
    _GRID_SIZE = None


def _read_with_fallback(provider: OCRProvider, prepared: PreparedImage, source: str) -> OCRResult:
    """Lê as ROIs e só recorre à imagem inteira se elas não renderem nada.

    A região `full` custa tanto quanto as ROIs juntas. Processá-la sempre
    triplicaria o tempo de cada foto para servir a um punhado de casos ruins —
    então ela é a rede de segurança, não o caminho normal (plano §5.2).
    """
    rois = {
        name: image
        for name, image in prepared.regions.items()
        if name in (REGION_NAME, REGION_CODE, REGION_PASSCODE)
    }
    result = provider.read(OCRRequest(regions=rois, source=source))

    if result.joined(REGION_NAME) or result.best(REGION_CODE) or result.best(REGION_PASSCODE):
        return result

    full = prepared.regions.get(REGION_FULL)
    if full is None:
        return result

    fallback = provider.read(OCRRequest(regions={REGION_FULL: full}, source=source))
    return OCRResult(
        texts={**result.texts, **fallback.texts},
        provider=result.provider,
        elapsed_ms=result.elapsed_ms + fallback.elapsed_ms,
        raw={"used_full_fallback": True},
    )


def process_task(task: ScanTask) -> ScanOutcome:
    """Pré-processa e roda OCR em uma imagem. Nunca levanta exceção.

    Com `_GRID_MODE` ligado, tenta primeiro achar uma grade de cartas na foto
    (`detect_grid_cells`); se achar, cada célula vira um `CropOutcome`
    independente. Sem grade detectada (ou com `_GRID_MODE` desligado), o
    resultado é uma lista de um único recorte — a foto inteira, exatamente
    como antes desta função existir.
    """
    started = time.perf_counter()
    provider = get_provider()
    settings = _SETTINGS
    max_pixels = settings.max_image_pixels if settings else 40_000_000

    try:
        image = load_image(task.path, max_pixels=max_pixels)
    except ImageError as exc:
        return ScanOutcome(
            task=task,
            preprocess_status="invalid",
            preprocess_error=exc.user_message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # pragma: no cover - defensivo
        return ScanOutcome(
            task=task,
            preprocess_status="error",
            preprocess_error=f"Falha inesperada ao preparar a imagem: {exc}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    skipped_empty = 0
    try:
        boxes: list[BoundingBox] = []
        if _GRID_MODE:
            # `_GRID_SIZE` (--grid-size) pula a detecção automática por
            # contorno de vez — a pessoa já sabe o layout, e a detecção por
            # contorno não é confiável em fotos reais (achado real: 2 de 9
            # cartas numa digitalização de grade 3x3, plano §22/ADR 0011).
            boxes = (
                manual_grid_cells(image, *_GRID_SIZE)
                if _GRID_SIZE is not None
                else detect_grid_cells(image)
            )
        grid_active = len(boxes) >= 2
        if grid_active and _GRID_SIZE is not None:
            # O layout informado pode ter mais células que cartas reais na
            # página (ex.: 7 cartas numa folha de fichário com disposição
            # 3x3) — sem isto, cada célula vazia vira uma leitura de OCR de
            # ruído. Nunca esvazia a lista inteira: uma página aparentemente
            # toda "vazia" é sinal de limiar errado, não de página em branco.
            non_blank = [box for box in boxes if not is_cell_blank(image, box)]
            skipped_empty = len(boxes) - len(non_blank)
            if non_blank:
                boxes = non_blank
            else:
                skipped_empty = 0
        regions: list[BoundingBox | None] = list(boxes) if grid_active else [None]
        crops = [
            _process_one_crop(provider, image, task, CropRegion(idx, len(regions), box))
            for idx, box in enumerate(regions)
        ]
    finally:
        image.close()

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ScanOutcome(task=task, crops=crops, elapsed_ms=elapsed_ms, skipped_empty=skipped_empty)


def _process_one_crop(
    provider: OCRProvider, image: Image.Image, task: ScanTask, region: CropRegion
) -> CropOutcome:
    """O corpo de sempre (ROIs + OCR) aplicado a um recorte da foto já carregada."""
    try:
        prepared = prepare_regions(image, source=task.path, region=region.bbox)
    except ImageError as exc:
        return CropOutcome(region=region, status="invalid", error=exc.user_message)
    except Exception as exc:  # pragma: no cover - defensivo
        return CropOutcome(region=region, status="error", error=f"{type(exc).__name__}: {exc}")

    try:
        result = _read_with_fallback(provider, prepared, str(task.path))
    except YugiohScannerError as exc:
        return CropOutcome(
            region=region, status="error", error=exc.user_message, notes=prepared.notes
        )
    except Exception as exc:
        # Uma imagem problemática não pode interromper as demais (§16).
        return CropOutcome(
            region=region,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            notes=prepared.notes,
        )
    finally:
        prepared.close()

    return CropOutcome(
        region=region,
        status="ocr_empty" if result.is_empty else "ok",
        ocr=result,
        notes=prepared.notes,
    )
