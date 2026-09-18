"""app_setting

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-14 19:00:00.000000

Nova tabela chave-valor para preferências do usuário (idioma global da UI,
idioma padrão de exibição de carta) — deliberadamente separada de
`sync_state`, que é bookkeeping de sincronização, não preferência editável
pelo usuário (plano de idioma global).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_setting")
