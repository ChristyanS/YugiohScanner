"""`matching/passcode.py::resolve_passcode` — camada 3 (validação contra o
banco) do plano de identidade por Card ID."""

from __future__ import annotations

from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card
from yugioh_scanner.matching.passcode import resolve_passcode

BLUE_EYES = 89631139


def _make_card(session: Session) -> None:
    session.add(
        Card(
            id=BLUE_EYES,
            name="Blue-Eyes White Dragon",
            name_normalized="blue-eyes white dragon",
            type="Normal Monster",
            desc="",
        )
    )
    session.flush()


class TestResolvePasscode:
    def test_real_passcode_resolves_to_the_card(self, session: Session) -> None:
        _make_card(session)
        assert resolve_passcode(session, "89631139") == BLUE_EYES

    def test_ocr_confusion_still_resolves(self, session: Session) -> None:
        """'l' -> "L" -> "1" após correção de `domain/passcode.py`."""
        _make_card(session)
        assert resolve_passcode(session, "8963ll39") == BLUE_EYES

    def test_unknown_passcode_returns_none(self, session: Session) -> None:
        _make_card(session)
        assert resolve_passcode(session, "99999999") is None

    def test_garbage_returns_none_without_querying_a_malformed_id(
        self, session: Session
    ) -> None:
        assert resolve_passcode(session, "BLUE-EYES") is None

    def test_empty_or_none_returns_none(self, session: Session) -> None:
        assert resolve_passcode(session, None) is None
        assert resolve_passcode(session, "") is None
