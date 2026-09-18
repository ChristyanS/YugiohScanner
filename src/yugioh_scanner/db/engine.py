"""Criação do engine e PRAGMAs do SQLite (plano §20.1).

Os PRAGMAs não são detalhe de performance opcional:

* `foreign_keys=ON` precisa ser aplicado **por conexão** — o SQLite ignora FKs
  silenciosamente sem isso, e nossos `ondelete` viram decoração.
* `journal_mode=WAL` permite leitura concorrente durante escrita, que é o que
  torna a UI web utilizável enquanto um scan roda.
* `busy_timeout` é a rede de segurança do escritor único (§2.3).
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.pool import StaticPool

from ..config import Settings
from ..domain.normalization import strip_accents_only
from ..errors import DatabaseCorruptedError

#: Aplicados a toda conexão SQLite. Ordem importa: WAL antes de synchronous.
_CONNECTION_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", "5000"),
    ("cache_size", "-64000"),  # 64 MB, valor negativo = KiB
    ("temp_store", "MEMORY"),
    ("mmap_size", "268435456"),  # 256 MB
)


def _collate_en(a: str, b: str) -> int:
    """Ordenação alfabética case-insensitive simples — o default para
    qualquer idioma que ainda não tem colação dedicada (plano de idioma
    global: só PT-BR ganhou tratamento de acento nesta primeira entrega)."""
    ka, kb = (a.casefold(), a), (b.casefold(), b)
    return -1 if ka < kb else (1 if ka > kb else 0)


def _collate_pt_br(a: str, b: str) -> int:
    """"Á" ordena perto de "A": remove acento (mantendo o resto da string)
    antes de comparar, com a string original como desempate estável."""
    ka = (strip_accents_only(a).casefold(), a)
    kb = (strip_accents_only(b).casefold(), b)
    return -1 if ka < kb else (1 if ka > kb else 0)


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Listener de conexão: aplica os PRAGMAs e registra as colações a cada
    nova conexão.

    As colações são registradas **sempre**, nunca condicionalmente: como o
    pool reaproveita conexões entre requisições, registrar só a "colação
    ativa no momento do connect" deixaria conexões antigas presas ao idioma
    anterior se a preferência mudar em runtime. Registrando as duas sempre,
    quem monta a query escolhe qual **usar** por consulta (`.collate(...)`).
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    dbapi_connection.create_collation("EN", _collate_en)
    dbapi_connection.create_collation("PT_BR", _collate_pt_br)
    cursor = dbapi_connection.cursor()
    try:
        for pragma, value in _CONNECTION_PRAGMAS:
            # WAL não funciona em banco :memory: — ignoramos o erro em vez de
            # criar dois caminhos de código.
            with contextlib.suppress(sqlite3.DatabaseError):  # :memory: recusa WAL
                cursor.execute(f"PRAGMA {pragma}={value}")
    finally:
        cursor.close()


def create_db_engine(
    url: str,
    *,
    echo: bool = False,
    in_memory: bool = False,
) -> Engine:
    """Cria um engine com os PRAGMAs registrados.

    `in_memory=True` usa `StaticPool` para que todas as sessões compartilhem a
    mesma conexão — sem isso, cada sessão veria um banco vazio diferente.
    """
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if in_memory:
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def engine_from_settings(settings: Settings, *, echo: bool = False) -> Engine:
    """Engine apontando para o banco configurado, criando `data/` se preciso."""
    if settings.is_sqlite:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_db_engine(settings.effective_database_url, echo=echo)


def database_exists(settings: Settings) -> bool:
    """O arquivo do banco já existe? (não diz nada sobre o schema)."""
    if not settings.is_sqlite:
        return True
    return settings.database_path.exists()


def check_integrity(engine: Engine) -> None:
    """`PRAGMA integrity_check`. Levanta `DatabaseCorruptedError` se falhar."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA integrity_check")).scalars().all()
    if rows != ["ok"]:
        raise DatabaseCorruptedError(
            "O banco falhou na verificação de integridade:\n  " + "\n  ".join(rows),
            hint="Restaure o backup mais recente em data/backups/ ou recrie com `init --force`.",
        )


def check_foreign_keys(engine: Engine) -> list[tuple[Any, ...]]:
    """Lista violações de chave estrangeira (vazio = tudo certo)."""
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text("PRAGMA foreign_key_check"))]


def vacuum(engine: Engine) -> None:
    """`VACUUM` + `ANALYZE`.

    O `ANALYZE` não é opcional: o planejador do SQLite escolhe índices com base
    nas estatísticas, e sem elas uma busca na coleção pode virar table scan.
    """
    # VACUUM não pode rodar dentro de transação; AUTOCOMMIT é a forma correta
    # de pedir isso ao SQLAlchemy 2.0.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM")
        conn.exec_driver_sql("ANALYZE")


def database_size_bytes(settings: Settings) -> int:
    """Tamanho em disco, somando os arquivos WAL."""
    if not settings.is_sqlite:
        return 0
    base: Path = settings.database_path
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = base.with_name(base.name + suffix)
        if candidate.exists():
            total += candidate.stat().st_size
    return total
