"""scan_result.ocr_passcode_raw + passcode_verified

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-13 21:00:00.000000

Suporte de schema para o passcode ("Card ID", canto inferior-esquerdo) como
fonte primária de identidade da carta — mais forte que nome ou set code
porque é uma chave exata contra o catálogo (`Card.id`), não uma aproximação
fuzzy (ver `domain/passcode.py`, `matching/passcode.py`,
`matching/engine.py::_from_passcode`).

- `ocr_passcode_raw`: texto bruto do OCR, mesmo padrão de `ocr_code_raw` —
  a revisão mostra o que foi lido, não só o resolvido.
- `passcode_verified`: a identidade desta leitura veio do passcode validado
  contra o catálogo, não do caminho normal de nome/código — transparência
  para a revisão saber que este é o sinal mais confiável possível.

Nenhum índice de expressão envolvido aqui (diferente da 0008/0009) — apenas
`add_column` simples via `batch_alter_table`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ocr_passcode_raw", sa.String(16), nullable=True))
        batch_op.add_column(
            sa.Column("passcode_verified", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.drop_column("passcode_verified")
        batch_op.drop_column("ocr_passcode_raw")
