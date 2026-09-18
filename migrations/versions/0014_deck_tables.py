"""deck, deck_card

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-14 20:30:00.000000

Deck Builder (feature nova): monta decks a partir da coleção do usuário ou
do catálogo inteiro, respeitando banlist TCG/OCG e as regras de construção
de deck da Master Rule (tamanhos de zona, limite de cópias por carta).

`deck_card` é única por `(deck_id, card_id, zone)`, não `(deck_id, card_id)`:
uma carta pode estar dividida entre main e side ao mesmo tempo.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deck",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("cover_card_id", sa.Integer(), nullable=True),
        sa.Column("build_mode", sa.String(length=16), nullable=False),
        sa.Column("banlist", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cover_card_id"], ["card.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "build_mode IN ('collection_only', 'full_db')", name="ck_deck_build_mode"
        ),
        sa.CheckConstraint("banlist IN ('TCG', 'OCG')", name="ck_deck_banlist"),
    )
    op.create_table(
        "deck_card",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("zone", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["deck.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["card_id"], ["card.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("quantity > 0", name="ck_deck_card_quantity_positive"),
        sa.CheckConstraint("zone IN ('main', 'extra', 'side')", name="ck_deck_card_zone"),
    )
    op.create_index("ux_deck_card", "deck_card", ["deck_id", "card_id", "zone"], unique=True)
    op.create_index("ix_deck_card_deck", "deck_card", ["deck_id"])


def downgrade() -> None:
    op.drop_index("ix_deck_card_deck", table_name="deck_card")
    op.drop_index("ux_deck_card", table_name="deck_card")
    op.drop_table("deck_card")
    op.drop_table("deck")
