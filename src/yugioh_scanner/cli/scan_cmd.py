"""Comando `scan` (plano §8 e Fase 5).

Fecha o ciclo completo: descoberta → OCR → matching → coleção, com
idempotência (§13) e a política de confiança (§7.5) decidindo o que entra
sozinho, o que fica pendente e o que precisa de revisão manual.

`--apply` é o padrão (como o plano especifica: `scan PASTA` sozinho já grava).
`--dry-run` é o modo de conferência — roda tudo, não escreve nada no banco.
"""

from __future__ import annotations

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
from ..services.scan_service import ImageEvent, ScanRunReport, ScanService
from .context import require_database
from .render import console, print_json, print_key_values, print_table, success, warn

log = get_logger(__name__)


def _log_event(event: ImageEvent) -> None:
    with log_context(image=event.file_name):
        if event.skipped:
            log.debug("scan.skip", reason="already_processed")
        elif event.status != "ok":
            log.error("scan.image_failed", status=event.status, error=event.error)
        else:
            log.info(
                "match.resolved",
                decision=event.decision,
                card=event.card_name,
                set_code=event.set_code,
                confidence=event.confidence,
                applied=event.applied,
            )


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
    apply: bool = typer.Option(
        True,
        "--apply/--dry-run",
        help="Grava na coleção (padrão) ou só mostra o que aconteceria.",
    ),
    no_auto: bool = typer.Option(
        False, "--no-auto", help="Nada é adicionado sozinho; tudo vira pendente."
    ),
    reprocess: bool = typer.Option(
        False,
        "--reprocess",
        help="Roda o OCR de novo em imagens já vistas (não reaplica na coleção).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Processa as imagens de uma pasta e atualiza a coleção.

    Rodar duas vezes na mesma pasta não duplica nada: imagens já processadas
    são puladas, a menos que `--reprocess` seja pedido.
    """
    settings = get_settings()
    # Mesmo em --dry-run o matching precisa ler o catálogo local — sem banco,
    # a mensagem de `init` é mais útil do que um erro de SQL no meio do scan.
    database = require_database(settings)

    try:
        service = ScanService(database, settings)

        if as_json:
            report = service.scan(
                folder,
                provider_name=provider,
                workers=workers,
                recursive=recursive,
                limit=limit,
                apply=apply,
                no_auto=no_auto,
                reprocess=reprocess,
            )
        else:
            report = _scan_with_progress(
                service,
                folder,
                provider_name=provider,
                workers=workers,
                recursive=recursive,
                limit=limit,
                apply=apply,
                no_auto=no_auto,
                reprocess=reprocess,
            )
    finally:
        database.dispose()

    _render(report, as_json=as_json, apply=apply)


def _scan_with_progress(
    service: ScanService,
    folder: Path,
    **kwargs: object,
) -> ScanRunReport:
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        bar = progress.add_task("Escaneando cartas", total=None)

        def on_event(event: ImageEvent) -> None:
            _log_event(event)
            progress.update(bar, advance=1)

        return service.scan(folder, progress=on_event, **kwargs)  # type: ignore[arg-type]


def _render(report: ScanRunReport, *, as_json: bool, apply: bool) -> None:
    if as_json:
        print_json(report.as_dict())
        return

    if report.total_images == 0:
        warn(f"Nenhuma imagem .jpg/.jpeg/.png encontrada em {report.folder}")
        return

    rows = []
    for event in report.events:
        if event.skipped:
            rows.append([event.file_name, "(já processada)", "", "", "pulada"])
            continue
        if event.status != "ok":
            rows.append([event.file_name, event.error or event.status, "", "", event.status])
            continue
        confidence = f"{event.confidence:.0%}" if event.confidence is not None else ""
        applied_mark = "✓" if event.applied else ""
        rows.append(
            [
                event.file_name,
                event.card_name or "",
                event.set_code or "",
                confidence,
                f"{event.decision} {applied_mark}".strip(),
            ]
        )

    print_table(
        f"Scan — {report.folder}",
        ["Arquivo", "Carta", "Set", "Confiança", "Decisão"],
        rows,
    )

    summary = {
        "imagens encontradas": report.total_images,
        "já processadas (puladas)": report.skipped,
        "processadas agora": report.processed,
        "adicionadas automaticamente": report.auto_added,
        "aguardando revisão": report.pending,
        "com falha": report.failed,
        "tempo": f"{report.elapsed_s:.1f}s",
    }
    if report.job_id is not None:
        summary["job"] = report.job_id
    print_key_values("Resumo", summary)

    if not apply:
        warn("Modo --dry-run: nada foi gravado no banco.")
    elif report.auto_added:
        success(f"{report.auto_added} carta(s) adicionada(s) à coleção.")
    if report.pending:
        warn(f"{report.pending} leitura(s) aguardando revisão (scan review chega na Fase 6).")
