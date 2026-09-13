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
    ALT_NAME_LANGUAGES,
    SYNC_KEY_DATABASE_VERSION,
    SYNC_KEY_LAST_FULL_SYNC,
)
from ..ygoprodeck.client import YgoProDeckClient
from .context import sync_service
from .errors import handle_errors
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
@handle_errors
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
@handle_errors
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
@handle_errors
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
@handle_errors
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


@app.command("probe-languages")
@handle_errors
def probe_languages_cmd(
    languages: str = typer.Option(
        None,
        "--languages",
        help=(
            "Lista separada por vírgula (ex. 'fr,de,es'). Default: os idiomas "
            "de db.tables.ALT_NAME_LANGUAGES."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Testa ao vivo quais códigos de `language=` a API aceita hoje.

    Não usa nem confia no guia oficial: ele (e a própria mensagem de erro que
    a API devolve para um valor inválido) lista só fr/de/it/pt, mas ja/ko
    também funcionam na prática (docs/proposta-i18n-cartas-e-sets.md). Como
    esse comportamento não é documentado, revalidar contra a API de verdade é
    a única forma confiável de saber se ele ainda vale — não requer banco.
    """
    settings = get_settings()
    candidates = (
        [code.strip().upper() for code in languages.split(",") if code.strip()]
        if languages
        else list(ALT_NAME_LANGUAGES)
    )

    client = YgoProDeckClient(settings)
    try:
        results = client.probe_languages(candidates)
    finally:
        client.close()

    if as_json:
        print_json(results)
        return

    print_key_values(
        "Idiomas aceitos pela API (ao vivo)",
        {code: ("sim" if accepted else "não") for code, accepted in results.items()},
    )
    rejected = [code for code, accepted in results.items() if not accepted]
    if rejected:
        hint(f"Rejeitados agora: {', '.join(rejected)}.")


@app.command("vacuum")
@handle_errors
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


@app.command("enrich-i18n")
@handle_errors
def enrich_i18n_cmd(
    force: bool = typer.Option(
        False, "--force", help="Baixa o dataset de novo mesmo se o ETag não mudou."
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Preenche nomes e prints regionais/OCG que a YGOPRODeck não tem (ADR 0012).

    Fonte opcional e separada do `sync` principal: baixa o dataset agregado
    do `yaml-yugi` (~94 MB, cacheado por ETag) e só acrescenta o que a
    YGOPRODeck não cobre — nunca sobrescreve um dado que já veio de lá. Uma
    falha aqui nunca deixa o catálogo primário inconsistente
    (docs/proposta-fontes-dados-catalogo.md).
    """
    with sync_service() as service:
        report = service.enrich_i18n(force=force)

    if as_json:
        print_json(report.as_dict())
        return

    if not report.performed:
        success(f"Nada a fazer: {report.reason}.")
        return

    success(f"Enriquecimento concluído em {report.elapsed_s:.1f}s.")
    stats = report.stats
    print_key_values(
        "Cartas",
        {
            "casadas com o catálogo local": stats.cards_matched,
            "sem correspondência": stats.cards_unmatched,
        },
    )
    print_key_values(
        "Nomes alternativos",
        {
            "novos": stats.alt_names_inserted,
            "atualizados": stats.alt_names_updated,
            "pulados (tradução não-oficial)": stats.alt_names_skipped_unofficial,
        },
    )
    print_key_values(
        "Prints/sets",
        {
            "sets novos": stats.sets_inserted,
            "prints novos": stats.prints_inserted,
            "prints atualizados": stats.prints_updated,
            "pulados (já vêm da YGOPRODeck)": stats.prints_skipped_primary,
        },
    )
    if stats.unparsed_set_codes:
        unique = sorted(set(stats.unparsed_set_codes))
        warn(
            f"{len(unique)} código(s) de set fora dos formatos conhecidos: "
            f"{', '.join(unique[:5])}" + (" …" if len(unique) > 5 else "")
        )
