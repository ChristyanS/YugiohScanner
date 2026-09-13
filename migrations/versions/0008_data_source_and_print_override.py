"""data_source em card_print/card_alt_name + tabela card_print_override

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-12 20:00:00.000000

Suporte de schema para a Opção E (ADR 0012, docs/proposta-fontes-dados-
catalogo.md): enriquecimento via `yaml-yugi` + overrides aprendidos do uso.

1. `card_print.data_source` / `card_alt_name.data_source`: marca se a linha
   veio da YGOPRODeck (sync principal) ou do enriquecimento `yaml-yugi`
   (`db enrich-i18n`). Todas as linhas existentes recebem `'ygoprodeck'` via
   `server_default` — correto por construção, já que a coluna não existia
   antes deste enriquecimento existir.
2. `card_print_override`: tabela nova, uma correção aprendida por
   `(card_id, language)` — grava qual `card_print_id` um humano confirmou
   manualmente para aquela carta naquele idioma (`ScanService.confirm_result`).

`card_print` não tem tabela-sombra FTS5, mas tem outro objeto que o Alembic
não sabe reconstruir sozinho: `ux_print` é um índice **de expressão**
(`COALESCE(rarity, '')`, criado na 0001), e o SQLite reflection do Alembic
não sabe ler índices de expressão (aviso real observado ao rodar esta
migração: "Skipped unsupported reflection of expression-based index
ux_print"). Isso significa que `batch_alter_table` recria a tabela **sem**
esse índice — perder a unicidade de print em silêncio seria um bug sério
(duas cópias do mesmo print entrariam sem erro). Por isso `ux_print` é
recriado à mão logo depois do `batch_alter_table`, dos dois lados
(upgrade/downgrade). `card_alt_name` tem um problema irmão, mas por FTS5
(`card_alt_fts`, criada na 0003, com triggers amarrados ao nome da tabela) —
mesmo cuidado da 0006: tabela recriada à mão, nunca via reflexão automática.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DATA_SOURCES = "'ygoprodeck', 'yaml_yugi'"


def _drop_fts_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_au")
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_ai")


def _create_fts_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER card_alt_fts_ai AFTER INSERT ON card_alt_name BEGIN
            INSERT INTO card_alt_fts(rowid, name_normalized, card_id)
            VALUES (new.id, new.name_normalized, new.card_id);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_alt_fts_ad AFTER DELETE ON card_alt_name BEGIN
            DELETE FROM card_alt_fts WHERE rowid = old.id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_alt_fts_au AFTER UPDATE ON card_alt_name BEGIN
            UPDATE card_alt_fts SET name_normalized = new.name_normalized, card_id = new.card_id
            WHERE rowid = old.id;
        END
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_card_alt_name ON card_alt_name (card_id, language)")
    op.execute("CREATE INDEX ix_card_alt_name_normalized ON card_alt_name (name_normalized)")


def _recreate_ux_print() -> None:
    op.execute(
        "CREATE UNIQUE INDEX ux_print ON card_print(card_id, set_code_full, COALESCE(rarity, ''))"
    )


def upgrade() -> None:
    # 1. card_print.data_source. `batch_alter_table` não sabe reconstruir o
    # índice de expressão `ux_print` (ver docstring do módulo) — recriado à
    # mão logo em seguida.
    with op.batch_alter_table("card_print", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "data_source",
                sa.String(16),
                nullable=False,
                server_default="ygoprodeck",
            )
        )
        batch_op.create_check_constraint(
            "ck_card_print_data_source", f"data_source IN ({_DATA_SOURCES})"
        )
    _recreate_ux_print()

    # 2. card_alt_name.data_source — recriação manual (FTS5 + triggers).
    _drop_fts_triggers()
    op.execute("DROP INDEX IF EXISTS ux_card_alt_name")
    op.execute("DROP INDEX IF EXISTS ix_card_alt_name_normalized")

    op.execute("ALTER TABLE card_alt_name RENAME TO card_alt_name_old")
    op.execute(
        f"""
        CREATE TABLE card_alt_name (
            id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            language VARCHAR(8) NOT NULL,
            name VARCHAR(255) NOT NULL,
            name_normalized VARCHAR(255) NOT NULL,
            synced_at DATETIME NOT NULL,
            desc TEXT NOT NULL DEFAULT '',
            name_en VARCHAR(255),
            data_source VARCHAR(16) NOT NULL DEFAULT 'ygoprodeck',
            PRIMARY KEY (id),
            FOREIGN KEY(card_id) REFERENCES card (id) ON DELETE CASCADE,
            CONSTRAINT ck_card_alt_name_language_format
                CHECK (length(language) BETWEEN 2 AND 3 AND language = upper(language)),
            CONSTRAINT ck_card_alt_name_data_source
                CHECK (data_source IN ({_DATA_SOURCES}))
        )
        """
    )
    op.execute(
        """
        INSERT INTO card_alt_name (
            id, card_id, language, name, name_normalized, synced_at, desc, name_en
        )
        SELECT id, card_id, language, name, name_normalized, synced_at, desc, name_en
        FROM card_alt_name_old
        """
    )
    op.execute("DROP TABLE card_alt_name_old")
    _create_fts_triggers()

    # 3. card_print_override — tabela nova (Opção D).
    op.create_table(
        "card_print_override",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("card_print_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["card_id"], ["card.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["card_print_id"], ["card_print.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "length(language) BETWEEN 2 AND 3 AND language = upper(language)",
            name="ck_card_print_override_language_format",
        ),
    )
    op.create_index(
        "ux_card_print_override", "card_print_override", ["card_id", "language"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_card_print_override", table_name="card_print_override")
    op.drop_table("card_print_override")

    connection = op.get_bind()
    non_ygoprodeck = connection.execute(
        sa.text("SELECT 1 FROM card_alt_name WHERE data_source != 'ygoprodeck' LIMIT 1")
    ).first()
    if non_ygoprodeck is not None:
        raise RuntimeError(
            "downgrade indisponível: existem card_alt_name enriquecidas via "
            "yaml_yugi que seriam perdidas silenciosamente. Rode "
            "`db enrich-i18n` novamente depois do downgrade se prosseguir manualmente."
        )
    non_ygoprodeck_print = connection.execute(
        sa.text("SELECT 1 FROM card_print WHERE data_source != 'ygoprodeck' LIMIT 1")
    ).first()
    if non_ygoprodeck_print is not None:
        raise RuntimeError(
            "downgrade indisponível: existem card_print enriquecidos via "
            "yaml_yugi que seriam perdidos silenciosamente."
        )

    _drop_fts_triggers()
    op.execute("DROP INDEX IF EXISTS ux_card_alt_name")
    op.execute("DROP INDEX IF EXISTS ix_card_alt_name_normalized")

    op.execute("ALTER TABLE card_alt_name RENAME TO card_alt_name_old")
    op.execute(
        """
        CREATE TABLE card_alt_name (
            id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            language VARCHAR(8) NOT NULL,
            name VARCHAR(255) NOT NULL,
            name_normalized VARCHAR(255) NOT NULL,
            synced_at DATETIME NOT NULL,
            desc TEXT NOT NULL DEFAULT '',
            name_en VARCHAR(255),
            PRIMARY KEY (id),
            FOREIGN KEY(card_id) REFERENCES card (id) ON DELETE CASCADE,
            CONSTRAINT ck_card_alt_name_language_format
                CHECK (length(language) BETWEEN 2 AND 3 AND language = upper(language))
        )
        """
    )
    op.execute(
        """
        INSERT INTO card_alt_name (
            id, card_id, language, name, name_normalized, synced_at, desc, name_en
        )
        SELECT id, card_id, language, name, name_normalized, synced_at, desc, name_en
        FROM card_alt_name_old
        """
    )
    op.execute("DROP TABLE card_alt_name_old")
    _create_fts_triggers()

    with op.batch_alter_table("card_print", schema=None) as batch_op:
        batch_op.drop_constraint("ck_card_print_data_source", type_="check")
        batch_op.drop_column("data_source")
    _recreate_ux_print()
