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
from ..db.tables import ScanJob
from ..images.grid import parse_grid_size
from ..logging_setup import get_logger, log_context
from ..services.scan_service import ImageEvent, ScanRunReport, ScanService
from .context import require_database
from .errors import handle_errors
from .render import console, print_json, print_key_values, print_table, success, warn
from .review_cmd import print_review_outcome, run_interactive_review

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


@handle_errors
def scan_command(
    folder: Path = typer.Argument(..., help="Pasta com as fotos das cartas."),
    workers: int = typer.Option(
        None, "--workers", "-w", help="Processos de OCR. Padrão: metade dos núcleos (teto 8)."
    ),
    provider: str = typer.Option(
        None, "--provider", "-p", help="Motor de OCR. Padrão: o de YGS_OCR_PROVIDER."
    ),
    fallback_provider: str = typer.Option(
        None,
        "--fallback-provider",
        help=(
            "Reprocessa leituras pending/manual com este provider (ex.: claude). "
            "Padrão: o de YGS_OCR_FALLBACK_PROVIDER (plano §6.3, Fase 10)."
        ),
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
    grid: bool = typer.Option(
        False,
        "--grid/--no-grid",
        help=(
            "Detecta várias cartas numa mesma foto (grade 2x2, 3x3, etc.). "
            'Precisa do extra `cv` (pip install -e ".[cv]").'
        ),
    ),
    grid_size: str = typer.Option(
        None,
        "--grid-size",
        help=(
            "Layout exato da grade (LINHASxCOLUNAS, ex.: 3x3, 2x4), informado "
            "por você em vez de detectado por contorno — mais confiável em "
            "fotos reais (a detecção automática por contorno erra fácil com "
            "arte de carta de verdade). Liga o modo grade sozinho, sem "
            "precisar de `--grid` nem do extra `cv`."
        ),
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Ao terminar, revisa as pendências na hora (mesmo laço de `review`).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Processa as imagens de uma pasta e atualiza a coleção.

    Rodar duas vezes na mesma pasta não duplica nada: imagens já processadas
    são puladas, a menos que `--reprocess` seja pedido.
    """
    settings = get_settings()
    if fallback_provider is not None:
        settings = settings.model_copy(update={"ocr_fallback_provider": fallback_provider})
    parsed_grid_size = parse_grid_size(grid_size) if grid_size else None
    # Mesmo em --dry-run o matching precisa ler o catálogo local — sem banco,
    # a mensagem de `init` é mais útil do que um erro de SQL no meio do scan.
    database = require_database(settings)

    try:
        service = ScanService(database, settings)
        _run_scan_and_render(
            service,
            folder,
            provider_name=provider,
            workers=workers,
            recursive=recursive,
            limit=limit,
            apply=apply,
            no_auto=no_auto,
            reprocess=reprocess,
            grid=grid,
            grid_size=parsed_grid_size,
            interactive=interactive,
            as_json=as_json,
        )
    finally:
        database.dispose()


def _run_scan_and_render(
    service: ScanService,
    folder: Path,
    *,
    provider_name: str | None,
    workers: int | None,
    recursive: bool = False,
    limit: int | None = None,
    apply: bool = True,
    no_auto: bool = False,
    reprocess: bool = False,
    grid: bool = False,
    grid_size: tuple[int, int] | None = None,
    interactive: bool = False,
    as_json: bool = False,
) -> ScanRunReport:
    """Corpo comum de `scan` e `scan-device` (plano §22 — captura reusa 100%
    do pipeline de identificação, só muda como as fotos chegam na pasta)."""
    scan_kwargs: dict[str, object] = {
        "provider_name": provider_name,
        "workers": workers,
        "recursive": recursive,
        "limit": limit,
        "apply": apply,
        "no_auto": no_auto,
        "reprocess": reprocess,
        "grid": grid,
        "grid_size": grid_size,
    }

    if as_json:
        report = service.scan(folder, **scan_kwargs)  # type: ignore[arg-type]
    else:
        report = _scan_with_progress(service, folder, **scan_kwargs)

    _render(report, as_json=as_json, apply=apply)

    # `--interactive` só faz sentido depois de gravar (dry-run não deixa
    # nada pendente para confirmar) e fora do modo --json (é um prompt de
    # terminal). Revisa a fila inteira, não só o que este job deixou
    # pendente — um só código para `review` e para este atalho.
    if interactive and apply and not as_json and report.pending:
        pending = service.pending_results()
        if pending:
            print_review_outcome(run_interactive_review(service, pending))

    return report


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
        # Uma foto com grade (plano §22, `--grid`) rende um `ImageEvent` por
        # carta detectada, todos com o mesmo `file_name` — o sufixo distingue
        # as linhas sem exigir uma coluna nova.
        label = event.file_name
        if event.crop_count > 1:
            label = f"{event.file_name} [{event.crop_index + 1}/{event.crop_count}]"

        if event.skipped:
            rows.append([label, "(já processada)", "", "", "pulada"])
            continue
        if event.status != "ok":
            rows.append([label, event.error or event.status, "", "", event.status])
            continue
        confidence = f"{event.confidence:.0%}" if event.confidence is not None else ""
        applied_mark = "✓" if event.applied else ""
        rows.append(
            [
                label,
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
        warn(f"{report.pending} leitura(s) aguardando revisão — rode `yugioh-scanner review`.")


@handle_errors
def scan_status_command(
    job_id: int = typer.Argument(None, help="Job específico. Omitido: lista os recentes."),
    limit: int = typer.Option(10, "--limit", "-n", help="Quantos jobs listar (sem JOB_ID)."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Mostra o progresso/estatísticas de um job, ou lista os últimos."""
    settings = get_settings()
    database = require_database(settings)
    try:
        service = ScanService(database, settings)

        if job_id is not None:
            job = service.job_detail(job_id)
            payload = _job_dict(job)
            if as_json:
                print_json(payload)
            else:
                print_key_values(f"Job #{job.id}", payload)
            return

        jobs = service.recent_jobs(limit=limit)
        if as_json:
            print_json([_job_dict(job) for job in jobs])
            return
        print_table(
            "Últimos scans",
            ["ID", "Pasta", "Status", "Processadas", "Auto", "Pendentes", "Falhas", "Início"],
            [
                [
                    job.id,
                    job.folder_path,
                    job.status,
                    job.processed,
                    job.auto_added,
                    job.pending,
                    job.failed,
                    job.started_at.strftime("%Y-%m-%d %H:%M"),
                ]
                for job in jobs
            ],
            empty_message="Nenhum scan executado ainda.",
        )
    finally:
        database.dispose()


def _job_dict(job: ScanJob) -> dict[str, object]:
    return {
        "id": job.id,
        "folder_path": job.folder_path,
        "status": job.status,
        "ocr_provider": job.ocr_provider,
        "workers": job.workers,
        "total_images": job.total_images,
        "processed": job.processed,
        "skipped": job.skipped,
        "auto_added": job.auto_added,
        "pending": job.pending,
        "failed": job.failed,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
    }
