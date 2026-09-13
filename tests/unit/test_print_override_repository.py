"""`PrintOverrideRepository` — CRUD sobre `card_print_override` (ADR 0012)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CardPrint
from yugioh_scanner.repositories.print_override import PrintOverrideRepository

CARD_ID = 1


def _make_card_and_prints(session: Session) -> tuple[CardPrint, CardPrint]:
    session.add(Card(id=CARD_ID, name="X", name_normalized="x", type="Effect Monster", desc=""))
    session.flush()
    first = CardPrint(
        card_id=CARD_ID,
        set_code_full="LOB-EN001",
        set_code_normalized="LOB-EN001",
        set_name="Legend of Blue Eyes White Dragon",
        region="EN",
        number="001",
    )
    second = CardPrint(
        card_id=CARD_ID,
        set_code_full="LOB-DE001",
        set_code_normalized="LOB-DE001",
        set_name="Legend of Blue Eyes White Dragon",
        region="DE",
        number="001",
    )
    session.add_all([first, second])
    session.flush()
    return first, second


class TestPrintOverrideRepository:
    def test_get_print_returns_none_without_an_override(self, session: Session) -> None:
        _make_card_and_prints(session)
        repo = PrintOverrideRepository(session)
        assert repo.get_print(CARD_ID, "DE") is None

    def test_set_then_get_round_trips(self, session: Session) -> None:
        _first, second = _make_card_and_prints(session)
        repo = PrintOverrideRepository(session)

        repo.set(CARD_ID, "DE", second.id)
        session.flush()

        found = repo.get_print(CARD_ID, "DE")
        assert found is not None
        assert found.id == second.id

    def test_set_again_overwrites_the_previous_choice(self, session: Session) -> None:
        """A última confirmação humana vence (upsert por `(card_id, language)`)."""
        first, second = _make_card_and_prints(session)
        repo = PrintOverrideRepository(session)

        repo.set(CARD_ID, "DE", first.id)
        session.flush()
        repo.set(CARD_ID, "DE", second.id)
        session.flush()

        found = repo.get_print(CARD_ID, "DE")
        assert found is not None
        assert found.id == second.id
        # Upsert, não insert duplicado.
        from yugioh_scanner.db.tables import CardPrintOverride

        assert session.query(CardPrintOverride).count() == 1
