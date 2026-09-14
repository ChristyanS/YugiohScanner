"""collection_item.set_code_full/rarity + scan_result.matched_set_code

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-13 12:00:00.000000

Desacopla SET de RARIDADE na coleção (pedido real do usuário: um scan de 66
páginas de scanner produziu 207 AUTO, boa parte sem set — porque hoje
`collection_item.card_print_id` só é preenchido quando carta+set+raridade
resolvem juntos numa única linha de `card_print`, e ambiguidade de raridade
(ex.: Dark Magician Girl em RA05 tem Starlight e Ultra Rare) apaga também a
informação de que o set já era conhecido).

1. `collection_item.set_code_full`/`rarity`: só têm sentido quando
   `card_print_id IS NULL` — é o par "sei o set, a raridade fica pendente
   para edição manual depois". Quando `card_print_id` resolve tudo, a leitura
   correta é via `CollectionItem.set_code_full_display`/`rarity_display`
   (`card_print.<campo>` tem prioridade).
2. `scan_result.matched_set_code`: persiste o código já validado contra o
   catálogo (`MatchResult.set_code`) — hoje só existe em memória durante o
   scan; o que fica gravado é `ocr_code_raw`, o texto bruto do OCR.
3. `ux_collection` é um índice de expressão (`COALESCE(card_print_id, -1)`,
   criado na 0001). Diferente de `ux_print` (0008), o Alembic **consegue**
   refletir e recriar sozinho a forma antiga dentro do `batch_alter_table`
   usado para adicionar as colunas — então este módulo dá `DROP INDEX IF
   EXISTS` antes de recriá-lo já com `COALESCE(set_code_full, '')` e
   `COALESCE(rarity, '')`, em vez de assumir que a reflexão automática vai
   falhar como fez para `ux_print`. Alargamento aditivo e seguro: toda linha
   existente tem os dois campos `NULL`, então o comportamento de dedup não
   muda para dados já gravados — só passa a distinguir dois itens do mesmo
   card com `card_print_id NULL` mas sets (ou raridades) pendentes
   diferentes, que hoje colidiriam na mesma linha.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _recreate_ux_collection(*, widened: bool) -> None:
    # Ao contrário de `ux_print` (0008), o SQLAlchemy consegue refletir e
    # recriar sozinho a forma *antiga* de `ux_collection` dentro do
    # `batch_alter_table` acima (a expressão `COALESCE(card_print_id, -1)`
    # não dispara o mesmo aviso de "unsupported reflection" — achado real ao
    # rodar esta migração contra um banco de verdade). `DROP` explícito antes
    # do `CREATE` evita "index already exists" independente do que a
    # reflexão automática tiver feito.
    op.execute("DROP INDEX IF EXISTS ux_collection")
    extra = ", COALESCE(set_code_full, ''), COALESCE(rarity, '')" if widened else ""
    op.execute(
        "CREATE UNIQUE INDEX ux_collection ON collection_item("
        f"card_id, COALESCE(card_print_id, -1){extra}, condition, edition, language)"
    )


def upgrade() -> None:
    with op.batch_alter_table("collection_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("set_code_full", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("rarity", sa.String(64), nullable=True))
    _recreate_ux_collection(widened=True)

    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.add_column(sa.Column("matched_set_code", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scan_result", schema=None) as batch_op:
        batch_op.drop_column("matched_set_code")

    with op.batch_alter_table("collection_item", schema=None) as batch_op:
        batch_op.drop_column("set_code_full")
        batch_op.drop_column("rarity")
    _recreate_ux_collection(widened=False)
