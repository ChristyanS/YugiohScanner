"""scan_result.job_id

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-14 16:00:00.000000

`GET /scan/{id}` (a grade de resultados do job) filtrava por
`scan_image.job_id` — o job que **descobriu** o arquivo pela primeira vez.
Achado real do usuário: num `--reprocess` (ou em qualquer novo scan de uma
pasta já vista, comum ao reusar fotos de calibração), o `ScanResult` novo
fica "preso" na tela do job original, e o job que de fato o gerou aparece
sem nenhuma linha (scan carta-a-carta) ou com a lista incompleta (grade).

`job_id` grava o job que **produziu** cada leitura, direto na tabela certa.
Backfill best-effort para linhas existentes: `scan_image.job_id` é a única
informação disponível para elas, e é o valor correto para toda leitura que
nunca foi reprocessada (a esmagadora maioria do histórico) — só fica
impreciso para reprocessamentos antigos, exatamente o cenário que este
schema agora resolve para daqui em diante.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.add_column(sa.Column("job_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_scan_result_job", ["job_id"])
        batch_op.create_foreign_key(
            "fk_scan_result_job_id", "scan_job", ["job_id"], ["id"], ondelete="SET NULL"
        )

    op.execute(
        "UPDATE scan_result SET job_id = ("
        "SELECT scan_image.job_id FROM scan_image WHERE scan_image.id = scan_result.scan_image_id"
        ")"
    )


def downgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.drop_constraint("fk_scan_result_job_id", type_="foreignkey")
        batch_op.drop_index("ix_scan_result_job")
        batch_op.drop_column("job_id")
