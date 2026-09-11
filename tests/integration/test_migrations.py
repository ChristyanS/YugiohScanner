"""As migrações são o único caminho para criar o schema (plano §19.3)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, inspect, text

from yugioh_scanner.config import Settings
from yugioh_scanner.db.engine import create_db_engine
from yugioh_scanner.db.fts import fts_row_count
from yugioh_scanner.db.migrations import (
    backup_database,
    current_revision,
    downgrade_to,
    head_revision,
    is_up_to_date,
    upgrade_to_head,
)
from yugioh_scanner.db.tables import Base

EXPECTED_TABLES = {
    "card",
    "card_alt_name",
    "card_image",
    "card_set",
    "card_print",
    "collection_item",
    "scan_job",
    "scan_image",
    "scan_result",
    "sync_state",
}


class TestUpgrade:
    def test_creates_every_table(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        assert tables >= EXPECTED_TABLES

    def test_reaches_head(self, engine: Engine) -> None:
        assert current_revision(engine) == head_revision()
        assert is_up_to_date(engine)

    def test_is_idempotent(self, settings: Settings, engine: Engine) -> None:
        upgrade_to_head(settings.effective_database_url)
        assert is_up_to_date(engine)

    def test_schema_matches_the_orm_models(self, engine: Engine) -> None:
        """Guarda contra o clássico: alguém edita `tables.py` e esquece a migração.

        Compara tabelas e colunas do banco migrado com os modelos declarativos.
        """
        inspector = inspect(engine)
        db_tables = set(inspector.get_table_names())

        for table_name, table in Base.metadata.tables.items():
            assert table_name in db_tables, f"tabela {table_name} não existe no banco migrado"
            db_columns = {col["name"] for col in inspector.get_columns(table_name)}
            model_columns = {col.name for col in table.columns}
            assert model_columns == db_columns, (
                f"colunas divergentes em {table_name}: "
                f"só no modelo={model_columns - db_columns}, só no banco={db_columns - model_columns}"
            )


class TestHandWrittenObjects:
    """Objetos que o autogenerate do Alembic não consegue produzir."""

    def test_expression_unique_indexes_exist(self, engine: Engine) -> None:
        with engine.connect() as conn:
            names = set(
                conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars()
            )
        assert "ux_print" in names
        assert "ux_collection" in names

    def test_fts_table_and_triggers_exist(self, engine: Engine) -> None:
        with engine.connect() as conn:
            objects = set(
                conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")
                ).scalars()
            )
        assert "card_fts" in objects
        assert {"card_fts_ai", "card_fts_ad", "card_fts_au"} <= objects

    def test_alt_name_fts_table_and_triggers_exist(self, engine: Engine) -> None:
        with engine.connect() as conn:
            objects = set(
                conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")
                ).scalars()
            )
        assert "card_alt_fts" in objects
        assert {"card_alt_fts_ai", "card_alt_fts_ad", "card_alt_fts_au"} <= objects

    def test_fts_starts_empty(self, engine: Engine) -> None:
        assert fts_row_count(engine) == 0


class TestDowngrade:
    def test_full_downgrade_removes_everything(self, settings: Settings, engine: Engine) -> None:
        engine.dispose()
        downgrade_to(settings.effective_database_url, "base")

        fresh = create_db_engine(settings.effective_database_url)
        remaining = set(inspect(fresh).get_table_names())
        # `alembic_version` sobrevive por design; o resto tem que sumir.
        assert not (EXPECTED_TABLES & remaining)
        assert "card_fts" not in remaining
        assert "card_alt_fts" not in remaining
        fresh.dispose()

    def test_upgrade_after_downgrade_works(self, settings: Settings, engine: Engine) -> None:
        """Ciclo completo: se o downgrade deixar lixo, o upgrade quebra aqui."""
        engine.dispose()
        downgrade_to(settings.effective_database_url, "base")
        upgrade_to_head(settings.effective_database_url)

        fresh = create_db_engine(settings.effective_database_url)
        assert set(inspect(fresh).get_table_names()) >= EXPECTED_TABLES
        assert is_up_to_date(fresh)
        fresh.dispose()


class TestBackup:
    def test_backup_copies_the_file(self, settings: Settings, engine: Engine) -> None:
        backup = backup_database(settings)
        assert backup is not None
        assert backup.exists()
        assert backup.parent == settings.backups_path

    def test_backup_of_missing_database_returns_none(self, tmp_path: Path) -> None:
        settings = Settings(
            data_path=tmp_path,
            database_url=f"sqlite:///{(tmp_path / 'nao-existe.db').as_posix()}",
        )
        assert backup_database(settings) is None
