"""Execução programática das migrações do Alembic.

A CLI (`yugioh-scanner db upgrade`) e os testes usam exatamente as mesmas
migrações que `alembic upgrade head` na mão — não existe um segundo caminho
para criar o schema. É por isso que os testes de integração exercitam as
migrações de graça (plano §19.3).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from ..config import PROJECT_ROOT, Settings
from ..errors import SchemaOutdatedError

ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def make_alembic_config(database_url: str) -> Config:
    """Config do Alembic apontando para uma URL específica."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to_head(database_url: str, *, revision: str = "head") -> None:
    """Aplica as migrações pendentes."""
    if database_url.startswith("sqlite:///"):
        Path(database_url[len("sqlite:///") :]).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(make_alembic_config(database_url), revision)


def downgrade_to(database_url: str, revision: str) -> None:
    """Reverte até a revisão informada (`base` para desfazer tudo)."""
    command.downgrade(make_alembic_config(database_url), revision)


def head_revision() -> str:
    """Última revisão disponível no diretório de migrações."""
    script = ScriptDirectory.from_config(make_alembic_config("sqlite://"))
    return script.get_current_head() or ""


def current_revision(engine: Engine) -> str | None:
    """Revisão aplicada no banco (None se ele nunca foi migrado)."""
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def is_up_to_date(engine: Engine) -> bool:
    return current_revision(engine) == head_revision()


def require_up_to_date(engine: Engine) -> None:
    """Falha com mensagem acionável se o schema estiver atrasado."""
    current = current_revision(engine)
    head = head_revision()
    if current != head:
        raise SchemaOutdatedError(current, head)


def backup_database(settings: Settings) -> Path | None:
    """Copia o arquivo do banco para `data/backups/` antes de algo destrutivo.

    Devolve o caminho do backup, ou None se não havia banco para copiar.
    """
    if not settings.is_sqlite:
        return None
    source = settings.database_path
    if not source.exists():
        return None

    import shutil
    from datetime import datetime

    settings.backups_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = settings.backups_path / f"{source.stem}-{stamp}.db"
    shutil.copy2(source, target)
    return target
