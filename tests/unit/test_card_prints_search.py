"""`CardRepository.prints_for(..., query=...)` — busca textual sobre os
prints de uma carta.

Motivada por um achado real de uso: o enriquecimento `yaml-yugi` (ADR 0012)
faz uma carta popular passar de 300 prints, e o `<select>` sem filtro nas
telas de Revisão/Coleção virou inutilizável.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CardPrint
from yugioh_scanner.repositories.cards import CardRepository

CARD_ID = 1


def _make_card_with_prints(session: Session) -> None:
    session.add(Card(id=CARD_ID, name="X", name_normalized="x", type="Effect Monster", desc=""))
    session.add_all(
        [
            CardPrint(
                card_id=CARD_ID,
                set_code_full="RA05-EN085",
                set_code_normalized="RA05-EN085",
                set_name="Rarity Collection 5",
                region="EN",
                rarity="Ultra Rare",
            ),
            CardPrint(
                card_id=CARD_ID,
                set_code_full="RA05-PT085",
                set_code_normalized="RA05-PT085",
                set_name="Rarity Collection 5",
                region="PT",
                rarity="Starlight Rare",
            ),
            CardPrint(
                card_id=CARD_ID,
                set_code_full="QCAC-JP021",
                set_code_normalized="QCAC-JP021",
                set_name="Quarter Century Art Collection",
                region="JA",
                rarity="Secret Rare",
            ),
        ]
    )
    session.flush()


class TestPrintsForCardSearch:
    def test_no_query_returns_everything(self, session: Session) -> None:
        _make_card_with_prints(session)
        assert len(CardRepository(session).prints_for(CARD_ID)) == 3

    def test_filters_by_set_code(self, session: Session) -> None:
        _make_card_with_prints(session)
        found = CardRepository(session).prints_for(CARD_ID, query="RA05")
        assert {p.set_code_full for p in found} == {"RA05-EN085", "RA05-PT085"}

    def test_filters_by_region(self, session: Session) -> None:
        _make_card_with_prints(session)
        found = CardRepository(session).prints_for(CARD_ID, query="JA")
        assert [p.set_code_full for p in found] == ["QCAC-JP021"]

    def test_filters_by_rarity(self, session: Session) -> None:
        _make_card_with_prints(session)
        found = CardRepository(session).prints_for(CARD_ID, query="Starlight")
        assert [p.set_code_full for p in found] == ["RA05-PT085"]

    def test_filters_by_set_name(self, session: Session) -> None:
        _make_card_with_prints(session)
        found = CardRepository(session).prints_for(CARD_ID, query="Quarter Century")
        assert [p.set_code_full for p in found] == ["QCAC-JP021"]

    def test_is_case_insensitive(self, session: Session) -> None:
        _make_card_with_prints(session)
        found = CardRepository(session).prints_for(CARD_ID, query="ra05-pt")
        assert [p.set_code_full for p in found] == ["RA05-PT085"]

    def test_no_match_returns_empty(self, session: Session) -> None:
        _make_card_with_prints(session)
        assert CardRepository(session).prints_for(CARD_ID, query="ZZZZ") == []

    def test_blank_query_is_treated_as_no_filter(self, session: Session) -> None:
        _make_card_with_prints(session)
        assert len(CardRepository(session).prints_for(CARD_ID, query="   ")) == 3
