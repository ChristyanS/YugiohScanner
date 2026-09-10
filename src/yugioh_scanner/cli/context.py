"""Montagem das dependências para a CLI (injeção simples, sem framework).

A CLI é a "raiz de composição": é aqui que engine, sessão, cliente HTTP e
serviços são criados e amarrados. Os serviços em si recebem tudo pronto pelo
construtor, o que os mantém testáveis sem tocar em rede nem em disco.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import typer

from ..config import Settings, get_settings
from ..db.engine import database_exists, engine_from_settings
from ..db.migrations import is_up_to_date
from ..db.session import Database
from ..services.collection_service import CollectionService
from ..services.scan_service import ScanService
from ..services.sync_service import SyncService
from ..ygoprodeck.client import YgoProDeckClient
from .render import fail, hint


def require_database(settings: Settings | None = None) -> Database:
    """Devolve a conexão, ou falha com instrução acionável.

    Banco ausente e schema atrasado são estados previstos: viram uma linha de
    orientação, nunca um traceback (plano §16).
    """
    settings = settings or get_settings()

    if not database_exists(settings):
        fail(f"Banco não encontrado em {settings.database_path}")
        hint("Execute `yugioh-scanner init` para criá-lo e baixar o catálogo.")
        raise typer.Exit(code=2)

    engine = engine_from_settings(settings)
    if not is_up_to_date(engine):
        engine.dispose()
        fail("O schema do banco está desatualizado.")
        hint("Execute `yugioh-scanner db upgrade`.")
        raise typer.Exit(code=2)

    return Database(engine)


@contextmanager
def sync_service(settings: Settings | None = None) -> Iterator[SyncService]:
    """`SyncService` pronto, com cliente HTTP fechado ao final."""
    settings = settings or get_settings()
    database = require_database(settings)
    client = YgoProDeckClient(settings)
    try:
        yield SyncService(database, client, settings)
    finally:
        client.close()
        database.dispose()


def scan_service(settings: Settings | None = None) -> tuple[ScanService, Database]:
    """`ScanService` pronto. O chamador é responsável por `database.dispose()`.

    Diferente de `sync_service`/`collection_service`, `ScanService` abre e
    fecha suas próprias sessões por chamada (plano §5.1 — precisa orquestrar
    OCR em paralelo entre múltiplas operações de banco), então não há uma
    única sessão para gerir aqui.
    """
    settings = settings or get_settings()
    database = require_database(settings)
    return ScanService(database, settings), database


@contextmanager
def collection_service(settings: Settings | None = None) -> Iterator[CollectionService]:
    """`CollectionService` pronto, com sessão e commit/rollback geridos.

    A sessão fica aberta por toda a duração do bloco `with` de quem chama —
    é por isso que ler `item.card`/`item.card_print` funciona sem
    `DetachedInstanceError` dentro do comando, mas não depois dele.
    """
    settings = settings or get_settings()
    database = require_database(settings)
    try:
        with database.session() as session:
            yield CollectionService(session)
    finally:
        database.dispose()
