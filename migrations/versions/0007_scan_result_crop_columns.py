"""scan result crop columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-12 11:00:00.000000

Adiciona a `scan_result` as colunas necessárias para uma foto poder conter
várias cartas (grade, plano §22): `crop_index`/`crop_count` identificam a
posição do recorte dentro da foto de origem, e `source_bbox_*` guarda o
retângulo normalizado de onde ele veio (para a revisão poder mostrar o
recorte sem re-detectar a grade). `0`/`1`/`NULL` preservam exatamente o
comportamento de hoje (uma foto = uma carta) sem backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("crop_index", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("crop_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("source_bbox_left", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("source_bbox_top", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("source_bbox_right", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("source_bbox_bottom", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.drop_column("source_bbox_bottom")
        batch_op.drop_column("source_bbox_right")
        batch_op.drop_column("source_bbox_top")
        batch_op.drop_column("source_bbox_left")
        batch_op.drop_column("crop_count")
        batch_op.drop_column("crop_index")
