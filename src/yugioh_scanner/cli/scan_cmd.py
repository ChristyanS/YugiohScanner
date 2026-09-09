"""Comando `scan` (plano §8).

Nesta fase o scanner vai até o OCR: descobre as imagens, pré-processa, lê o
texto e mostra o resultado. O casamento com o catálogo e a inclusão na coleção
entram nas Fases 4 e 5 — as flags já existem para que a interface do comando
não mude quando isso acontecer.
"""

from __future__ import annotations

import time
from pathlib import Path

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from ..config import get_settings
from ..logging_setup import get_logger, log_context
from ..ocr.base import REGION_NAME
from ..scanner.discovery import discover_images, resolve_scan_folder
from ..scanner.executor import create_executor, resolve_workers
from ..scanner.worker import ScanOutcome, ScanTask
from .render import console, print_json, print_key_values, print_table, success, warn

log = get_logger(__name__)


def _summarize(outcome: ScanOutcome) -> dict[str, object]:
    """Resumo de uma imagem. Nome e código saem de `ScanOutcome`, que é a
    fonte única de "como se lê uma carta" — a Fase 4 usa exatamente o mesmo."""
    ocr = outcome.ocr
    return {
        "file": outcome.path.name,
        "status": outcome.status,
        "name": outcome.read_name,
        "code": outcome.read_code,
        "confidence": round(ocr.confidence(REGION_NAME), 3) if ocr else 0.0,
        "ocr_ms": ocr.elapsed_ms if ocr else 0,
        "elapsed_ms": outcome.elapsed_ms,
        "error": outcome.error,
    }


def scan_command(
    folder: Path = typer.Argument(..., help="Pasta com as fotos das cartas."),
    workers: int = typer.Option(
        None, "--workers", "-w", help="Processos de OCR. Padrão: metade dos núcleos (teto 8)."
    ),
    provider: str = typer.Option(
        None, "--provider", "-p", help="Motor de OCR. Padrão: o de YGS_OCR_PROVIDER."
    ),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Inclui subpastas."),
    limit: int = typer.Option(None, "--limit", "-n", help="Processa só as N primeiras."),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Só mostra o resultado do OCR. (`--apply` chega na Fase 5.)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Processa as imagens de uma pasta e mostra o que o OCR leu."""
    settings = get_settings()
    resolved_folder = resolve_scan_folder(folder, settings)

    if not dry_run:
        warn("A inclusão automática na coleção chega na Fase 5. Processando em modo de leitura.")

    started = time.perf_counter()
    images = discover_images(resolved_folder, recursive=recursive, limit=limit)

    if not images:
        warn(f"Nenhuma imagem .jpg/.jpeg/.png encontrada em {resolved_folder}")
        if as_json:
            print_json({"folder": str(resolved_folder), "images": 0, "results": []})
        return

    provider_name = provider or settings.ocr_provider
    worker_count = resolve_workers(settings, workers)

    log.info(
        "scan.start",
        folder=str(resolved_folder),
        images=len(images),
        provider=provider_name,
        workers=worker_count,
    )

    tasks = [
        ScanTask(path=image.path, file_hash=image.file_hash, size=image.size) for image in images
    ]
    executor = create_executor(settings, provider_name=provider_name, workers=worker_count)

    results: list[dict[str, object]] = []
    counters = {"ok": 0, "ocr_empty": 0, "invalid": 0, "error": 0}

    def consume(outcome: ScanOutcome) -> None:
        counters[outcome.status] = counters.get(outcome.status, 0) + 1
        summary = _summarize(outcome)
        results.append(summary)
        with log_context(image=outcome.path.name):
            if outcome.failed:
                log.error("scan.image_failed", status=outcome.status, error=outcome.error)
            else:
                log.info(
                    "ocr.done",
                    status=outcome.status,
                    name=summary["name"],
                    code=summary["code"],
                    ms=summary["ocr_ms"],
                )

    if as_json:
        for outcome in executor.map(tasks):
            consume(outcome)
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            bar = progress.add_task("Lendo cartas", total=len(tasks))
            for outcome in executor.map(tasks):
                consume(outcome)
                progress.advance(bar)

    elapsed = time.perf_counter() - started
    log.info("scan.done", elapsed_s=round(elapsed, 1), **counters)

    if as_json:
        print_json(
            {
                "folder": str(resolved_folder),
                "images": len(images),
                "provider": provider_name,
                "workers": worker_count,
                "elapsed_s": round(elapsed, 2),
                "counters": counters,
                "results": results,
            }
        )
        return

    print_table(
        f"OCR — {resolved_folder}",
        ["Arquivo", "Nome lido", "Set code", "Status", "ms"],
        [
            [
                row["file"],
                row["name"] or (row["error"] or ""),
                row["code"],
                row["status"],
                row["ocr_ms"],
            ]
            for row in results
        ],
    )
    print_key_values(
        "Resumo",
        {
            "imagens": len(images),
            "lidas": counters.get("ok", 0),
            "sem texto": counters.get("ocr_empty", 0),
            "inválidas": counters.get("invalid", 0),
            "com erro": counters.get("error", 0),
            "tempo": f"{elapsed:.1f}s",
            "workers": worker_count,
            "provider": provider_name,
        },
    )
    if counters.get("ok"):
        success(f"{counters['ok']} imagem(ns) lida(s) com sucesso.")
