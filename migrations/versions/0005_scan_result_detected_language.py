"""scan result detected language

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12 10:00:00.000000

Adiciona `scan_result.detected_language`: o motor de matching já reconhece
nomes em FR/DE/IT/PT via `card_alt_name` (plano §7.1), mas descartava qual
idioma casou assim que resolvia o `card_id`. Esta coluna guarda esse
palpite para a tela de revisão pré-selecionar o idioma da carta física —
sem CHECK, mesmo padrão permissivo de `collection_item.language`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.add_column(sa.Column("detected_language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.drop_column("detected_language")
