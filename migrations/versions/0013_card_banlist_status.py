"""card.ban_tcg / card.ban_ocg

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-14 20:00:00.000000

A YGOPRODeck já devolve `banlist_info.ban_tcg`/`.ban_ocg` em `cardinfo.php`
(confirmado ao vivo em 2026-09-14) — só não era gravado. Necessário para o
Deck Builder aplicar o limite de cópias por carta (Forbidden=0, Limited=1,
Semi-Limited=2, default=3). Nullable e sem backfill: cartas existentes só
ganham o dado no próximo `sync --force` — até lá, o Deck Builder as trata
como sem restrição (NULL), nunca como erro.

Achado real ao implementar esta migração: adicionar um `CHECK` via
`batch_alter_table` no SQLite força o modo batch a **recriar a tabela**
inteira (copy-drop-rename), porque `ALTER TABLE ADD CONSTRAINT` não existe
no SQLite. Isso silenciosamente **apaga os triggers** `card_fts_ai/ad/au`
(migração `0001`) — SQLite derruba triggers junto com a tabela a que
pertencem, e o Alembic não sabe recriar DDL escrito à mão (só recria o que
ele mesmo reflete: colunas, índices, constraints). Sem as linhas de
`op.execute(...)` abaixo (idênticas às de `0001`), toda carta inserida
depois desta migração pararia de ser encontrável por nome na busca (FTS)
até um `rebuild_fts()` manual — confirmado reproduzindo o bug antes de
corrigir. `downgrade()` teria o mesmo problema pelo mesmo motivo, por isso
recria os triggers de novo no final.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BAN_VALUES = ("Forbidden", "Limited", "Semi-Limited")


def _recreate_card_fts_triggers() -> None:
    """Idêntico a `0001_initial_schema.py` — recria os 3 triggers que o
    `batch_alter_table("card", ...)` acima apagou junto com a tabela antiga."""
    op.execute(
        """
        CREATE TRIGGER card_fts_ai AFTER INSERT ON card BEGIN
            INSERT INTO card_fts(rowid, name_normalized)
            VALUES (new.id, new.name_normalized);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_fts_ad AFTER DELETE ON card BEGIN
            INSERT INTO card_fts(card_fts, rowid, name_normalized)
            VALUES ('delete', old.id, old.name_normalized);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER card_fts_au AFTER UPDATE ON card BEGIN
            INSERT INTO card_fts(card_fts, rowid, name_normalized)
            VALUES ('delete', old.id, old.name_normalized);
            INSERT INTO card_fts(rowid, name_normalized)
            VALUES (new.id, new.name_normalized);
        END
        """
    )


def upgrade() -> None:
    with op.batch_alter_table("card", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ban_tcg", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("ban_ocg", sa.String(length=16), nullable=True))
        batch_op.create_check_constraint(
            "ck_card_ban_tcg", f"ban_tcg IS NULL OR ban_tcg IN {_BAN_VALUES}"
        )
        batch_op.create_check_constraint(
            "ck_card_ban_ocg", f"ban_ocg IS NULL OR ban_ocg IN {_BAN_VALUES}"
        )
    _recreate_card_fts_triggers()


def downgrade() -> None:
    with op.batch_alter_table("card", schema=None) as batch_op:
        batch_op.drop_constraint("ck_card_ban_ocg", type_="check")
        batch_op.drop_constraint("ck_card_ban_tcg", type_="check")
        batch_op.drop_column("ban_ocg")
        batch_op.drop_column("ban_tcg")
    _recreate_card_fts_triggers()
