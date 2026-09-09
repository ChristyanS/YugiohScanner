"""Comandos `yugioh-scanner db …` — manutenção do banco local."""

from __future__ import annotations

import typer
from sqlalchemy import func, select

from ..config import get_settings
from ..db import (
    Card,
    CardPrint,
    CardSet,
    CollectionItem,
    ScanJob,
    SyncState,
    check_foreign_keys,
    check_integrity,
    database_exists,
    database_size_bytes,
    engine_from_settings,
)
from ..db.fts import fts_row_count
from ..db.migrations import (
    backup_database,
    current_revision,
    downgrade_to,
    head_revision,
    upgrade_to_head,
)
from ..db.session import Database
from ..db.tables import (
    SYNC_KEY_DATABASE_VERSION,
    SYNC_KEY_LAST_FULL_SYNC,
)
from .render import (
    ARROW,
    fail,
    hint,
    human_bytes,
    print_json,
    print_key_values,
    success,
    warn,
)

app = typer.Typer(help="Manutenção do banco de dados local.", no_args_is_help=True)


@app.command("upgrade")
def upgrade(
    revision: str = typer.Option("head", help="Revisão alvo."),
) -> None:
    """Cria o banco (se não existir) e aplica as migrações pendentes."""
    settings = get_settings()
    settings.ensure_directories()

    before = current_revision(engine_from_settings(settings)) if database_exists(settings) else None
    upgrade_to_head(settings.effective_database_url, revision=revision)
    after = current_revision(engine_from_settings(settings))

    if before == after:
        success(f"O banco já estava na revisão {after}. Nada a fazer.")
    else:
        success(f"Schema atualizado: {before or 'vazio'} {ARROW} {after}")


@app.command("downgrade")
def downgrade(
    revision: str = typer.Argument(..., help="Revisão alvo ('base' desfaz tudo)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Não perguntar."),
) -> None:
    """Reverte migrações. Faz backup do arquivo antes."""
    settings = get_settings()
    if not database_exists(settings):
        fail("Não há banco para reverter.")
        raise typer.Exit(code=2)

    if not yes:
        typer.confirm(
            f"Reverter o schema até '{revision}'? Isso pode apagar dados.",
            abort=True,
        )

    backup = backup_database(settings)
    if backup is not None:
        warn(f"Backup salvo em {backup}")

    downgrade_to(settings.effective_database_url, revision)
    success(f"Schema revertido para {current_revision(engine_from_settings(settings)) or 'base'}")


@app.command("status")
def status(
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Mostra versão do schema, contagens e tamanho do banco."""
    settings = get_settings()

    if not database_exists(settings):
        payload: dict[str, object] = {
            "database_path": str(settings.database_path),
            "exists": False,
            "schema_revision": None,
            "schema_head": head_revision(),
        }
        if as_json:
            print_json(payload)
        else:
            warn(f"Banco não encontrado em {settings.database_path}")
            hint("Execute `yugioh-scanner db upgrade` para criá-lo.")
        return

    engine = engine_from_settings(settings)
    revision = current_revision(engine)
    head = head_revision()

    payload = {
        "database_path": str(settings.database_path),
        "exists": True,
        "size": database_size_bytes(settings),
        "schema_revision": revision,
        "schema_head": head,
        "schema_up_to_date": revision == head,
    }

    if revision is not None:
        db = Database(engine)
        with db.session() as session:
            payload["cards"] = session.scalar(select(func.count()).select_from(Card)) or 0
            payload["prints"] = session.scalar(select(func.count()).select_from(CardPrint)) or 0
            payload["sets"] = session.scalar(select(func.count()).select_from(CardSet)) or 0
            payload["collection_items"] = (
                session.scalar(select(func.count()).select_from(CollectionItem)) or 0
            )
            payload["collection_copies"] = (
                session.scalar(select(func.coalesce(func.sum(CollectionItem.quantity), 0))) or 0
            )
            payload["scan_jobs"] = session.scalar(select(func.count()).select_from(ScanJob)) or 0
            state = {
                row.key: row.value
                for row in session.scalars(
                    select(SyncState).where(
                        SyncState.key.in_([SYNC_KEY_DATABASE_VERSION, SYNC_KEY_LAST_FULL_SYNC])
                    )
                )
            }
            payload["ygoprodeck_database_version"] = state.get(SYNC_KEY_DATABASE_VERSION)
            payload["last_full_sync"] = state.get(SYNC_KEY_LAST_FULL_SYNC)
        payload["fts_rows"] = fts_row_count(engine)

    if as_json:
        print_json(payload)
        return

    display = dict(payload)
    display["size"] = human_bytes(database_size_bytes(settings))
    print_key_values("Banco de dados", display)

    if revision != head:
        warn(f"Schema desatualizado ({revision} {ARROW} {head}). Execute `db upgrade`.")
    if not payload.get("cards"):
        warn("Catálogo vazio. Execute `yugioh-scanner sync` para populá-lo.")


@app.command("check")
def check() -> None:
    """Verifica integridade do arquivo e chaves estrangeiras."""
    settings = get_settings()
    if not database_exists(settings):
        fail(f"Banco não encontrado em {settings.database_path}")
        raise typer.Exit(code=2)

    engine = engine_from_settings(settings)
    check_integrity(engine)
    success("integrity_check: ok")

    violations = check_foreign_keys(engine)
    if violations:
        fail(f"{len(violations)} violação(ões) de chave estrangeira:")
        for row in violations[:20]:
            typer.echo(f"  {row}")
        raise typer.Exit(code=2)
    success("foreign_key_check: ok")


@app.command("vacuum")
def vacuum_cmd() -> None:
    """Compacta o banco e recalcula estatísticas do planejador."""
    from ..db import vacuum

    settings = get_settings()
    if not database_exists(settings):
        fail(f"Banco não encontrado em {settings.database_path}")
        raise typer.Exit(code=2)

    before = database_size_bytes(settings)
    vacuum(engine_from_settings(settings))
    after = database_size_bytes(settings)
    success(f"VACUUM + ANALYZE concluídos: {human_bytes(before)} {ARROW} {human_bytes(after)}")
