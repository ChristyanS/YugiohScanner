"""card alt name desc (exibição multilíngue)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11 22:00:00.000000

Adiciona `card_alt_name.desc`: a Fase 3 do faseamento web pede para exibir
(não só casar via OCR) o nome e a descrição da carta em qualquer idioma
sincronizado. A resposta de `cardinfo.php?language=...` já trazia o texto
traduzido — só não era guardado porque `card_alt_name` existia apenas para o
matching (plano §7.1). Bancos já sincronizados ficam com `desc=''` até o
próximo `sync`, que preenche a coluna.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("card_alt_name", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("desc", sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("card_alt_name", schema=None) as batch_op:
        batch_op.drop_column("desc")
