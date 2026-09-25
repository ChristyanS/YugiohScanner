"""Fixtures compartilhadas.

Princípio (plano §19): o banco de teste é criado pelas **migrações Alembic
reais**, nunca por `Base.metadata.create_all`. Assim as migrações são
exercitadas de graça em cada teste de integração — e um `create_all` que
diverge da migração deixa de ser possível.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

# `COLUMNS`/`LINES` fixos ANTES de qualquer import de `yugioh_scanner` —
# `cli/render.py` cria `Console`/`error_console` como singleton de módulo, e
# o Rich resolve a largura desse singleton na primeira leitura e a mantém
# pelo resto do processo (achado real: falha só em CI, nunca localmente —
# o runner do GitHub Actions no Windows não tem um terminal de verdade
# associado ao processo, então `shutil.get_terminal_size()`/`COLUMNS`
# ambiente resolvem para uma largura minúscula, e `--help`/mensagens de erro
# saem quebrados a ponto de um texto como "--code" nunca aparecer inteiro
# numa linha só). Setar aqui, antes de qualquer `Console` existir, garante
# uma largura previsível em qualquer ambiente (CI ou terminal local) —
# sobrescreve de propósito (não `setdefault`): o valor que já vinha do
# ambiente é justamente a causa do bug, herdar ele não resolveria nada.
os.environ["COLUMNS"] = "200"
os.environ["LINES"] = "50"

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from yugioh_scanner.config import Settings, reset_settings_cache
from yugioh_scanner.db.engine import create_db_engine
from yugioh_scanner.db.migrations import upgrade_to_head
from yugioh_scanner.db.session import Database


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Impede que o `.env` ou o ambiente do desenvolvedor vaze para os testes."""
    for var in list(os_environ_keys()):
        if var.startswith("YGS_") or var == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(var, raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


def os_environ_keys() -> list[str]:
    import os

    return list(os.environ)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Configuração apontando para um `data/` temporário e descartável."""
    return Settings(
        data_path=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        log_level="WARNING",
        # Sem espera artificial: o rate limiter tem teste próprio e não pode
        # transformar a suíte em meio minuto de sleep.
        http_rate_limit_per_s=100_000,
        # O retry continua sendo exercitado; só não dormimos de verdade.
        http_backoff_base_s=0.0,
    )


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Banco migrado uma única vez por sessão, usado como molde.

    Rodar o Alembic em cada teste custava ~0,5s por teste — meio minuto de
    suíte. Migramos uma vez e copiamos o arquivo, que é instantâneo. As
    migrações continuam sendo o único caminho para criar o schema, e
    `test_migrations.py` as exercita do zero.
    """
    path = tmp_path_factory.mktemp("template") / "template.db"
    upgrade_to_head(f"sqlite:///{path.as_posix()}")

    # Consolida o WAL no arquivo principal para que a cópia seja completa.
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    return path


@pytest.fixture
def engine(settings: Settings, migrated_template: Path) -> Iterator[Engine]:
    """Engine sobre uma cópia limpa do banco migrado."""
    settings.ensure_directories()
    shutil.copy2(migrated_template, settings.database_path)

    eng = create_db_engine(settings.effective_database_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="session")
def catalog_template(tmp_path_factory: pytest.TempPathFactory, migrated_template: Path) -> Path:
    """Banco migrado **e** populado com o catálogo das fixtures, uma vez.

    Vários testes (matching, coleção, exportação) precisam de um catálogo
    pronto. Sincronizar em cada um custava ~0,3s por teste e crescia a cada
    fase; aqui o sync roda uma vez e o arquivo é copiado.
    """
    import httpx

    from yugioh_scanner.services.sync_service import SyncService
    from yugioh_scanner.ygoprodeck.client import YgoProDeckClient

    from .fixtures_api import BASE_URL, mock_api

    path = tmp_path_factory.mktemp("catalog") / "catalog.db"
    shutil.copy2(migrated_template, path)

    catalog_settings = Settings(
        data_path=path.parent,
        database_url=f"sqlite:///{path.as_posix()}",
        log_level="WARNING",
        http_rate_limit_per_s=100_000,
        http_backoff_base_s=0.0,
    )
    engine = create_db_engine(catalog_settings.effective_database_url)
    try:
        with mock_api():
            client = YgoProDeckClient(catalog_settings, client=httpx.Client(base_url=BASE_URL))
            SyncService(Database(engine), client, catalog_settings).sync(page_size=3)
            client.close()
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        engine.dispose()

    return path


@pytest.fixture
def catalog(settings: Settings, catalog_template: Path) -> Iterator[Database]:
    """Banco com o catálogo das fixtures, pronto para uso."""
    settings.ensure_directories()
    shutil.copy2(catalog_template, settings.database_path)

    eng = create_db_engine(settings.effective_database_url)
    try:
        yield Database(eng)
    finally:
        eng.dispose()


@pytest.fixture
def database(engine: Engine) -> Database:
    return Database(engine)


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    """Sessão crua, sem commit automático no teardown.

    Vários testes provocam `IntegrityError` de propósito e deixam a sessão em
    estado de rollback pendente; commitar no teardown transformaria isso em
    erro de teardown. Quem precisa de commit chama `session.commit()`.
    O banco é temporário e morre com o teste.
    """
    sess = database.session_factory()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()
