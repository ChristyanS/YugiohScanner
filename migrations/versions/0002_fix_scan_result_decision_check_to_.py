"""fix scan_result decision check to include manual

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09 21:14:46.050478

Bug real (achado por verificação manual contra o catálogo real, Fase 6): o
CHECK de `scan_result.decision` nunca incluiu `'manual'`, um dos quatro
valores do enum `domain.confidence.Decision` desde a Fase 4. Qualquer carta
ambígua ou de baixa confiança — o caso mais comum de precisar revisão humana
— quebrava o `scan` com `IntegrityError` ao tentar gravar o resultado.

O autogenerate do Alembic não detecta mudança de CHECK constraint no SQLite
(saiu vazio) — esta migração é escrita à mão, e por precaução **não** usa
`batch_alter_table` confiando na reflexão automática: `scan_result` tem um
índice parcial (`ix_scan_result_pending`, com `WHERE decision = 'pending'`) e
já vimos o Alembic avisar que não reflete bem índices por expressão neste
schema (`ux_print`/`ux_collection`, em outras tabelas). Para não arriscar
perder esse índice numa reconstrução automática, a tabela é recriada à mão,
copiando coluna por coluna e recriando os três índices explicitamente.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = """
    id INTEGER NOT NULL,
    scan_image_id INTEGER NOT NULL,
    card_id INTEGER,
    card_print_id INTEGER,
    ocr_name_raw VARCHAR(512),
    ocr_code_raw VARCHAR(64),
    name_score FLOAT NOT NULL,
    code_score FLOAT NOT NULL,
    confidence FLOAT NOT NULL,
    margin FLOAT NOT NULL,
    candidates JSON,
    decision VARCHAR(16) NOT NULL,
    applied BOOLEAN NOT NULL,
    collection_item_id INTEGER,
    created_at DATETIME NOT NULL,
    decided_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(card_id) REFERENCES card (id) ON DELETE SET NULL,
    FOREIGN KEY(card_print_id) REFERENCES card_print (id) ON DELETE SET NULL,
    FOREIGN KEY(collection_item_id) REFERENCES collection_item (id) ON DELETE SET NULL,
    FOREIGN KEY(scan_image_id) REFERENCES scan_image (id) ON DELETE CASCADE
"""


def _recreate(check_values: str) -> None:
    op.execute("ALTER TABLE scan_result RENAME TO scan_result_old")
    op.execute(
        f"""
        CREATE TABLE scan_result (
            {_COLUMNS},
            CONSTRAINT ck_scan_result_decision CHECK (decision IN ({check_values}))
        )
        """
    )
    op.execute(
        """
        INSERT INTO scan_result (
            id, scan_image_id, card_id, card_print_id, ocr_name_raw, ocr_code_raw,
            name_score, code_score, confidence, margin, candidates, decision,
            applied, collection_item_id, created_at, decided_at
        )
        SELECT
            id, scan_image_id, card_id, card_print_id, ocr_name_raw, ocr_code_raw,
            name_score, code_score, confidence, margin, candidates, decision,
            applied, collection_item_id, created_at, decided_at
        FROM scan_result_old
        """
    )
    op.execute("DROP TABLE scan_result_old")
    op.execute("CREATE INDEX ix_scan_result_card ON scan_result (card_id)")
    op.execute("CREATE INDEX ix_scan_result_image ON scan_result (scan_image_id)")
    op.execute(
        "CREATE INDEX ix_scan_result_pending ON scan_result (decision) WHERE decision = 'pending'"
    )


def upgrade() -> None:
    _recreate("'auto', 'pending', 'manual', 'confirmed', 'rejected', 'unmatched'")


def downgrade() -> None:
    # Reverter perderia silenciosamente qualquer linha com decision='manual'
    # já gravada sob o schema novo — que o CHECK antigo rejeitaria. Falha
    # alto e explícito em vez de descartar dado do usuário calado.
    # (`RAISE()` do SQLite só funciona dentro de um trigger, não num SELECT
    # solto — por isso a checagem é feita em Python, não em SQL.)
    connection = op.get_bind()
    has_manual = connection.execute(
        text("SELECT 1 FROM scan_result WHERE decision = 'manual' LIMIT 1")
    ).first()
    if has_manual is not None:
        raise RuntimeError(
            "downgrade indisponível: existem scan_result.decision='manual' "
            "que o CHECK antigo (antes da 0002) rejeitaria."
        )
    _recreate("'auto', 'pending', 'confirmed', 'rejected', 'unmatched'")
