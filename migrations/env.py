"""Ambiente do Alembic.

A URL do banco vem de `Settings`, não do `alembic.ini` — assim `alembic upgrade`
na mão e `yugioh-scanner db upgrade` mexem exatamente no mesmo arquivo.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from yugioh_scanner.config import get_settings
from yugioh_scanner.db.tables import Base

config = context.config
target_metadata = Base.metadata


def _resolve_url() -> str:
    """URL do banco a migrar.

    Quem chamou já pode ter definido uma (é o que `db.migrations` e os testes
    fazem). Só caímos em `Settings` quando o Alembic foi invocado direto pela
    linha de comando — e nunca sobrescrevemos uma URL explícita, senão os testes
    migrariam o banco de produção.
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured

    settings = get_settings()
    if settings.is_sqlite:
        # A pasta precisa existir antes de o SQLite abrir o arquivo.
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return settings.effective_database_url


config.set_main_option("sqlalchemy.url", _resolve_url())


def _include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    """Mantém o índice FTS5 fora do autogenerate.

    `card_fts` e suas tabelas-sombra (`card_fts_data`, `card_fts_idx`, ...) são
    criadas por SQL explícito na migração inicial. Sem este filtro, o
    autogenerate tentaria removê-las a cada revisão nova.
    """
    return not (type_ == "table" and name is not None and name.startswith("card_fts"))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite não tem ALTER TABLE completo; o modo batch recria a tabela
            # quando preciso. Sem isso, migrações futuras travariam.
            render_as_batch=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
