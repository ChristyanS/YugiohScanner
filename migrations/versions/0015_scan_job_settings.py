"""scan_job.auto_enabled/require_set/require_rarity

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-20 00:00:00.000000

`ScanJob` gravava as contagens (`auto_added`/`pending`/`failed`), mas não a
política que produziu essas contagens. Achado real do usuário: a mesma pasta
escaneada em momentos diferentes (ou com a política mudada entre uma
execução e outra) rende `auto_added` bem diferentes, e a tela de detalhe do
scan não tinha como mostrar *por que* — `Settings.auto_requires_print`/
`auto_requires_rarity` no momento de hoje não é necessariamente o que valia
quando aquele job rodou.

Estas três colunas congelam a política efetiva (já resolvida do `None` do
override por scan contra o default de `Settings`) no momento da criação do
job. Backfill best-effort para jobs existentes: os defaults de `Settings`
(`auto_requires_print=True`, `auto_requires_rarity=False`) e `auto=True` do
formulário são a política mais provável para todo histórico anterior a esta
migração — imprecisa só para quem já rodava com override não-default, que é
exatamente o cenário que este schema passa a registrar corretamente daqui
para frente.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scan_job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_enabled", sa.Boolean(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("require_set", sa.Boolean(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("require_rarity", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("scan_job", schema=None) as batch_op:
        batch_op.drop_column("require_rarity")
        batch_op.drop_column("require_set")
        batch_op.drop_column("auto_enabled")
