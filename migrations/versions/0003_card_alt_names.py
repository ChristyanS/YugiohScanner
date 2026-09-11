"""card alt names (matching multilíngue)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11 14:30:00.000000

Nova tabela `card_alt_name`: nomes de cartas em francês/alemão/italiano/
português, vindos de `cardinfo.php?language=...` (plano §7.1/§7.3). Sem ela,
o matching por nome só reconhece cartas fotografadas em inglês — o `id` do
passcode é o mesmo em todo idioma, então a ligação com `card` é direta.

Segue o padrão de `card_fts` em 0001: tabela virtual FTS5 + triggers escritos
à mão, porque o autogenerate não conhece tabelas virtuais. Diferença
importante: `card_alt_fts` **não** é de conteúdo externo (`content='card_alt_name'`)
porque uma carta pode ter várias linhas em `card_alt_name` (uma por idioma) e
FTS5 de conteúdo externo exige exatamente uma linha por `content_rowid` —
aqui o rowid é o autoincrement de `card_alt_fts` mesmo, com `card_id` guardado
como coluna comum (não indexada) para o SELECT de volta.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "card_alt_name",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_normalized", sa.String(length=255), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "language IN ('FR', 'DE', 'IT', 'PT')", name="ck_card_alt_name_language"
        ),
        sa.ForeignKeyConstraint(["card_id"], ["card.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("card_alt_name", schema=None) as batch_op:
        batch_op.create_index(
            "ux_card_alt_name", ["card_id", "language"], unique=True
        )
        batch_op.create_index(
            "ix_card_alt_name_normalized", ["name_normalized"], unique=False
        )

    # Índice de busca textual sobre os nomes alternativos — mesmo tokenizer de
    # `card_fts`, mas conteúdo próprio (ver docstring do módulo).
    op.execute(
        """
        CREATE VIRTUAL TABLE card_alt_fts USING fts5(
            name_normalized,
            card_id UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
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


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_au")
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS card_alt_fts_ai")
    op.execute("DROP TABLE IF EXISTS card_alt_fts")

    with op.batch_alter_table("card_alt_name", schema=None) as batch_op:
        batch_op.drop_index("ix_card_alt_name_normalized")
        batch_op.drop_index("ux_card_alt_name")
    op.drop_table("card_alt_name")
