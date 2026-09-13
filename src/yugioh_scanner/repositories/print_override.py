"""Leitura e escrita da tabela de overrides de print-por-idioma (ADR 0012)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.tables import CardPrint, CardPrintOverride, utcnow


class PrintOverrideRepository:
    """CRUD simples sobre `card_print_override` — mesma forma de
    `SyncStateRepository`: sem regra de negócio, só tradução para SQL."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_print(self, card_id: int, language: str) -> CardPrint | None:
        return self.session.scalar(
            select(CardPrint)
            .join(CardPrintOverride, CardPrintOverride.card_print_id == CardPrint.id)
            .where(CardPrintOverride.card_id == card_id, CardPrintOverride.language == language)
        )

    def set(self, card_id: int, language: str, card_print_id: int) -> CardPrintOverride:
        """Upsert por `(card_id, language)` — a última confirmação humana vence."""
        existing = self.session.scalar(
            select(CardPrintOverride).where(
                CardPrintOverride.card_id == card_id, CardPrintOverride.language == language
            )
        )
        if existing is None:
            existing = CardPrintOverride(
                card_id=card_id, language=language, card_print_id=card_print_id
            )
            self.session.add(existing)
        else:
            existing.card_print_id = card_print_id
            existing.updated_at = utcnow()
        return existing
