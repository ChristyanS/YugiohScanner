"""`SiblingPrintLanguageResolver` (plano de idiomas, continuação da Fase 3 —
ver docs/adr/0009): consulta print irmão por (card_id, set_prefix, number,
region), nunca transforma a string do código.

Usa o schema migrado vazio (`session`), não o catálogo de fixtures — o
cenário real que isto modela (par EN/PT do mesmo produto "OP") não existe
nas fixtures gravadas, então as linhas são construídas à mão.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CardPrint, CardSet
from yugioh_scanner.matching.print_language import SiblingPrintLanguageResolver

CARD_ID = 1561110


def _make_card(session: Session) -> None:
    session.add(
        Card(id=CARD_ID, name="Dorover", name_normalized="dorover", type="Effect Monster", desc="")
    )
    session.add(CardSet(set_code="OP13", set_name="OTS Tournament Pack 13 (POR)"))
    session.flush()


def _add_print(
    session: Session, *, set_code_full: str, region: str | None, number: str, rarity: str | None
) -> CardPrint:
    print_row = CardPrint(
        card_id=CARD_ID,
        set_code_full=set_code_full,
        set_code_normalized=set_code_full,
        set_prefix="OP13",
        set_name="OTS Tournament Pack 13 (POR)",
        region=region,
        number=number,
        rarity=rarity,
    )
    session.add(print_row)
    session.flush()
    return print_row


class TestSiblingPrintLanguageResolver:
    def test_finds_the_sibling_print_in_the_target_language(self, session: Session) -> None:
        _make_card(session)
        en_print = _add_print(
            session, set_code_full="OP13-EN006", region="EN", number="006", rarity="Ultra Rare"
        )
        pt_print = _add_print(
            session, set_code_full="OP13-PT006", region="PT", number="006", rarity="Ultra Rare"
        )

        resolver = SiblingPrintLanguageResolver()
        found = resolver.resolve(session, CARD_ID, en_print, "PT")

        assert found is not None
        assert found.id == pt_print.id

    def test_returns_none_when_no_sibling_exists(self, session: Session) -> None:
        """O caso comum: DE/FR/IT não têm print distinto no catálogo real."""
        _make_card(session)
        en_print = _add_print(
            session, set_code_full="OP13-EN006", region="EN", number="006", rarity="Ultra Rare"
        )

        resolver = SiblingPrintLanguageResolver()
        assert resolver.resolve(session, CARD_ID, en_print, "DE") is None

    def test_returns_none_for_english(self, session: Session) -> None:
        """Inglês é o print padrão — nada para converter."""
        _make_card(session)
        en_print = _add_print(
            session, set_code_full="OP13-EN006", region="EN", number="006", rarity="Ultra Rare"
        )

        resolver = SiblingPrintLanguageResolver()
        assert resolver.resolve(session, CARD_ID, en_print, "EN") is None

    def test_returns_none_without_a_current_print(self, session: Session) -> None:
        """Sem set definido, não há âncora (prefixo/número) pra buscar a irmã."""
        resolver = SiblingPrintLanguageResolver()
        assert resolver.resolve(session, CARD_ID, None, "PT") is None

    def test_falls_back_to_any_match_when_rarity_disagrees(self, session: Session) -> None:
        """Nunca visto nos dados reais (25 pares EN/PT conferidos), mas a
        carta certa importa mais que a raridade certa se um dia acontecer."""
        _make_card(session)
        en_print = _add_print(
            session, set_code_full="OP13-EN006", region="EN", number="006", rarity="Ultra Rare"
        )
        pt_print = _add_print(
            session, set_code_full="OP13-PT006", region="PT", number="006", rarity="Secret Rare"
        )

        resolver = SiblingPrintLanguageResolver()
        found = resolver.resolve(session, CARD_ID, en_print, "PT")

        assert found is not None
        assert found.id == pt_print.id
