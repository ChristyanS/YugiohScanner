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

from ..config import Settings
from ..errors import YugiohScannerError
from ..images.preprocess import ImageError, PreparedImage, prepare_image
from ..ocr.base import (
    REGION_CODE,
    REGION_FULL,
    REGION_NAME,
    OCRProvider,
    OCRRequest,
    OCRResult,
)
from ..ocr.registry import create_provider

#: Estado por processo. Preenchido por `init_worker` e reutilizado em todas as
#: imagens que aquele worker processar.
_PROVIDER: OCRProvider | None = None
_SETTINGS: Settings | None = None


@dataclass(frozen=True, slots=True)
class ScanTask:
    """Uma unidade de trabalho enviada ao worker.

    Leva **caminho**, nunca bytes: serializar 5 MB de imagem entre processos
    custaria mais que o próprio OCR.
    """

    path: Path
    file_hash: str
    size: int = 0


@dataclass
class ScanOutcome:
    """O que volta do worker. Cruza a fronteira de processos, então é puro."""

    task: ScanTask
    status: str = "ok"
    ocr: OCRResult | None = None
    error: str | None = None
    elapsed_ms: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.task.path

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


def init_worker(provider_name: str, settings: Settings) -> None:
    """Inicializador do pool: cria e aquece o provider uma única vez."""
    global _PROVIDER, _SETTINGS
    _SETTINGS = settings
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


def set_provider(provider: OCRProvider, settings: Settings) -> None:
    """Injeta o provider no processo atual (execução serial e testes)."""
    global _PROVIDER, _SETTINGS
    _PROVIDER = provider
    _SETTINGS = settings


def reset_provider() -> None:
    global _PROVIDER, _SETTINGS
    if _PROVIDER is not None:
        _PROVIDER.close()
    _PROVIDER = None
    _SETTINGS = None


def _read_with_fallback(provider: OCRProvider, prepared: PreparedImage, source: str) -> OCRResult:
    """Lê as ROIs e só recorre à imagem inteira se elas não renderem nada.

    A região `full` custa tanto quanto as duas ROIs juntas. Processá-la sempre
    triplicaria o tempo de cada foto para servir a um punhado de casos ruins —
    então ela é a rede de segurança, não o caminho normal (plano §5.2).
    """
    rois = {
        name: image
        for name, image in prepared.regions.items()
        if name in (REGION_NAME, REGION_CODE)
    }
    result = provider.read(OCRRequest(regions=rois, source=source))

    if result.joined(REGION_NAME) or result.best(REGION_CODE):
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
    """Pré-processa e roda OCR em uma imagem. Nunca levanta exceção."""
    started = time.perf_counter()
    provider = get_provider()
    settings = _SETTINGS

    try:
        prepared = prepare_image(
            task.path,
            max_pixels=settings.max_image_pixels if settings else 40_000_000,
        )
    except ImageError as exc:
        return ScanOutcome(
            task=task,
            status="invalid",
            error=exc.user_message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:  # pragma: no cover - defensivo
        return ScanOutcome(
            task=task,
            status="error",
            error=f"Falha inesperada ao preparar a imagem: {exc}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        result = _read_with_fallback(provider, prepared, str(task.path))
    except YugiohScannerError as exc:
        return ScanOutcome(
            task=task,
            status="error",
            error=exc.user_message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=prepared.notes,
        )
    except Exception as exc:
        # Uma imagem problemática não pode interromper as demais (§16).
        return ScanOutcome(
            task=task,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=prepared.notes,
        )
    finally:
        prepared.close()

    elapsed = int((time.perf_counter() - started) * 1000)
    return ScanOutcome(
        task=task,
        status="ocr_empty" if result.is_empty else "ok",
        ocr=result,
        elapsed_ms=elapsed,
        notes=prepared.notes,
    )
