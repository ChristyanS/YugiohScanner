"""Comandos `init` e `sync` — popular e atualizar o catálogo local."""

from __future__ import annotations

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
from ..db.engine import database_exists, engine_from_settings
from ..db.migrations import backup_database, current_revision, upgrade_to_head
from ..services.sync_service import SyncReport
from .context import sync_service
from .errors import handle_errors
from .render import ARROW, console, hint, print_json, print_key_values, success, warn


def _run_sync(
    *,
    force: bool,
    sets_only: bool,
    quiet: bool,
) -> SyncReport:
    """Executa o sync mostrando progresso real (páginas, não spinner falso)."""
    settings = get_settings()

    with sync_service(settings) as service:
        if quiet:
            return service.sync(force=force, sets_only=sets_only)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Baixando catálogo", total=None)

            def on_progress(processed: int, total: int) -> None:
                progress.update(task, completed=processed, total=total or None)

            return service.sync(force=force, sets_only=sets_only, progress=on_progress)


def _report(report: SyncReport, as_json: bool) -> None:
    if as_json:
        print_json(report.as_dict())
        return

    if not report.performed:
        success(f"Nada a fazer: {report.reason}.")
    else:
        success(f"Catálogo sincronizado em {report.elapsed_s:.1f}s ({report.reason}).")

    print_key_values(
        "Catálogo local",
        {
            "versão YGOPRODeck": report.remote_version,
            "cartas": report.total_cards,
            "prints": report.total_prints,
            "sets": report.total_sets,
        },
    )

    if report.performed:
        stats = report.stats
        print_key_values(
            "Alterações",
            {
                "cartas novas": stats.cards_inserted,
                "cartas atualizadas": stats.cards_updated,
                "prints novos": stats.prints_inserted,
                "prints atualizados": stats.prints_updated,
                "sets novos": stats.sets_inserted,
                "sets atualizados": stats.sets_updated,
            },
        )
        if stats.prints_stale:
            warn(
                f"{stats.prints_stale} print(s) não vieram mais da API e foram "
                "preservados (apagá-los quebraria referências da sua coleção)."
            )
        if stats.unparsed_set_codes:
            unique = sorted(set(stats.unparsed_set_codes))
            warn(
                f"{len(unique)} código(s) de set fora dos formatos conhecidos, "
                f"guardados como texto: {', '.join(unique[:5])}" + (" …" if len(unique) > 5 else "")
            )


@handle_errors
def init_command(
    force: bool = typer.Option(False, "--force", help="Recria o banco do zero (faz backup antes)."),
    skip_sync: bool = typer.Option(
        False, "--skip-sync", help="Só cria o banco, sem baixar o catálogo."
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Cria o banco, aplica as migrações e baixa o catálogo do YGOPRODeck."""
    settings = get_settings()
    settings.ensure_directories()

    if force and database_exists(settings):
        typer.confirm(
            f"Isso vai apagar {settings.database_path} e sua coleção. Continuar?",
            abort=True,
        )
        backup = backup_database(settings)
        if backup is not None:
            warn(f"Backup salvo em {backup}")
        settings.database_path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = settings.database_path.with_name(settings.database_path.name + suffix)
            extra.unlink(missing_ok=True)

    before = current_revision(engine_from_settings(settings)) if database_exists(settings) else None
    upgrade_to_head(settings.effective_database_url)
    after = current_revision(engine_from_settings(settings))
    if before != after:
        success(f"Schema criado: {before or 'vazio'} {ARROW} {after}")

    if skip_sync:
        warn("Catálogo não baixado (--skip-sync). Rode `yugioh-scanner sync` depois.")
        return

    _report(_run_sync(force=False, sets_only=False, quiet=as_json), as_json)


@handle_errors
def sync_command(
    force: bool = typer.Option(
        False, "--force", help="Sincroniza mesmo que a versão remota não tenha mudado."
    ),
    sets_only: bool = typer.Option(
        False, "--sets-only", help="Atualiza apenas o catálogo de sets."
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Atualiza o catálogo local a partir da API do YGOPRODeck.

    Sem `--force`, consulta a versão remota primeiro e não faz nada se ela não
    mudou — o caso comum custa uma requisição.
    """
    _report(_run_sync(force=force, sets_only=sets_only, quiet=as_json), as_json)


@handle_errors
def check_command(
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Diz se há atualização de catálogo disponível, sem baixar nada."""
    with sync_service() as service:
        decision = service.check()

    payload = {
        "needs_sync": decision.needs_sync,
        "reason": decision.reason,
        "local_version": decision.local_version,
        "remote_version": decision.remote_version,
        "remote_updated_at": decision.remote_updated_at,
    }
    if as_json:
        print_json(payload)
        return

    if decision.needs_sync:
        warn(f"Atualização disponível: {decision.reason}.")
        hint("Execute `yugioh-scanner sync`.")
    else:
        success(decision.reason.capitalize() + ".")
