"""`SiblingPrintLanguageResolver` (plano de idiomas, continuação da Fase 3 —
ver docs/adr/0009): consulta print irmão por (card_id, set_prefix, number,
region), nunca transforma a string do código.

`OverridePrintLanguageResolver`/`CompositePrintLanguageResolver` (ADR 0012):
correção aprendida do próprio uso, consultada antes da heurística automática.

Usa o schema migrado vazio (`session`), não o catálogo de fixtures — o
cenário real que isto modela (par EN/PT do mesmo produto "OP") não existe
nas fixtures gravadas, então as linhas são construídas à mão.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CardPrint, CardSet
from yugioh_scanner.matching.print_language import (
    CompositePrintLanguageResolver,
    OverridePrintLanguageResolver,
    SiblingPrintLanguageResolver,
)
from yugioh_scanner.repositories.print_override import PrintOverrideRepository

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


class TestOverridePrintLanguageResolver:
    def test_finds_a_print_recorded_by_a_human_confirmation(self, session: Session) -> None:
        _make_card(session)
        jp_print = _add_print(
            session, set_code_full="DBWS-JP016", region="JA", number="016", rarity="Super Rare"
        )
        PrintOverrideRepository(session).set(CARD_ID, "JA", jp_print.id)
        session.flush()

        resolver = OverridePrintLanguageResolver()
        found = resolver.resolve(session, CARD_ID, None, "JA")

        assert found is not None
        assert found.id == jp_print.id

    def test_returns_none_without_a_recorded_override(self, session: Session) -> None:
        _make_card(session)
        resolver = OverridePrintLanguageResolver()
        assert resolver.resolve(session, CARD_ID, None, "JA") is None

    def test_returns_none_for_english(self, session: Session) -> None:
        resolver = OverridePrintLanguageResolver()
        assert resolver.resolve(session, CARD_ID, None, "EN") is None


class TestCompositePrintLanguageResolver:
    def test_override_wins_over_sibling(self, session: Session) -> None:
        """Uma correção humana (Opção D) sempre vence a heurística
        automática de print irmão (Opção B/ADR 0009) — ADR 0012."""
        _make_card(session)
        en_print = _add_print(
            session, set_code_full="OP13-EN006", region="EN", number="006", rarity="Ultra Rare"
        )
        sibling_print = _add_print(
            session, set_code_full="OP13-PT006", region="PT", number="006", rarity="Ultra Rare"
        )
        override_print = _add_print(
            session, set_code_full="OP13-PT006-ALT", region="PT", number="006ALT", rarity="Secret Rare"
        )
        PrintOverrideRepository(session).set(CARD_ID, "PT", override_print.id)
        session.flush()

        resolver = CompositePrintLanguageResolver(
            [OverridePrintLanguageResolver(), SiblingPrintLanguageResolver()]
        )
        found = resolver.resolve(session, CARD_ID, en_print, "PT")

        assert found is not None
        assert found.id == override_print.id
        assert found.id != sibling_print.id

    def test_falls_back_to_sibling_when_no_override_exists(self, session: Session) -> None:
        _make_card(session)
        en_print = _add_print(
            session, set_code_full="OP13-EN006", region="EN", number="006", rarity="Ultra Rare"
        )
        pt_print = _add_print(
            session, set_code_full="OP13-PT006", region="PT", number="006", rarity="Ultra Rare"
        )

        resolver = CompositePrintLanguageResolver(
            [OverridePrintLanguageResolver(), SiblingPrintLanguageResolver()]
        )
        found = resolver.resolve(session, CARD_ID, en_print, "PT")

        assert found is not None
        assert found.id == pt_print.id

    def test_returns_none_when_nothing_resolves(self, session: Session) -> None:
        _make_card(session)
        en_print = _add_print(
            session, set_code_full="OP13-EN006", region="EN", number="006", rarity="Ultra Rare"
        )

        resolver = CompositePrintLanguageResolver(
            [OverridePrintLanguageResolver(), SiblingPrintLanguageResolver()]
        )
        assert resolver.resolve(session, CARD_ID, en_print, "DE") is None
