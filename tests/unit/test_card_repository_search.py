"""`CardRepository._matching_ids`/`search` — busca por Card ID (passcode) e
ordenação alfabética sensível a idioma (plano de idioma global).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CardAltName
from yugioh_scanner.repositories.cards import CardRepository


def _add_card(session: Session, card_id: int, name: str) -> None:
    session.add(
        Card(id=card_id, name=name, name_normalized=name.lower(), type="Spell Card", desc="")
    )


class TestSearchByPasscode:
    def test_exact_passcode_match(self, session: Session) -> None:
        _add_card(session, 55144522, "Pot of Greed")
        _add_card(session, 46986414, "Dark Magician")
        session.flush()

        ids = CardRepository(session)._matching_ids("55144522", limit=10)
        assert ids == [55144522]

    def test_unknown_passcode_returns_empty(self, session: Session) -> None:
        _add_card(session, 46986414, "Dark Magician")
        session.flush()

        assert CardRepository(session)._matching_ids("99999999", limit=10) == []

    def test_name_search_unaffected(self, session: Session) -> None:
        _add_card(session, 46986414, "Dark Magician")
        session.flush()

        assert CardRepository(session)._matching_ids("dark", limit=10) == [46986414]

    def test_search_returns_card_object_for_passcode_query(self, session: Session) -> None:
        _add_card(session, 55144522, "Pot of Greed")
        session.flush()

        found = CardRepository(session).search("55144522")
        assert [c.id for c in found] == [55144522]


class TestLocaleAwareSort:
    def test_pt_br_collation_orders_accented_names_correctly(self, session: Session) -> None:
        # Sem tratamento de acento, "Água" (á fora do intervalo ASCII) cai
        # pro final da lista; com a colação PT_BR, "Agua" (sem acento) fica
        # entre "Astral" e "Azul" — ordem alfabética de dicionário de verdade.
        _add_card(session, 1, "Azul")
        _add_card(session, 2, "Água")
        _add_card(session, 3, "Astral")
        session.flush()

        repo = CardRepository(session)
        en_order = [c.name for c in repo.search(None, locale="EN", limit=10)]
        pt_order = [c.name for c in repo.search(None, locale="PT_BR", limit=10)]

        assert en_order == ["Astral", "Azul", "Água"]
        assert pt_order == ["Água", "Astral", "Azul"]

    def test_default_locale_is_en(self, session: Session) -> None:
        _add_card(session, 1, "Azul")
        _add_card(session, 2, "Água")
        session.flush()

        found = [c.name for c in CardRepository(session).search(None, limit=10)]
        assert found == ["Azul", "Água"]

    def test_sorts_by_translated_name_when_lang_given(self, session: Session) -> None:
        """Achado real do usuário: escolher um idioma de exibição não pode só
        reacentuar o nome em inglês — tem que reordenar pelo nome traduzido."""
        _add_card(session, 1, "A Case for K9")
        _add_card(session, 2, "Book of Moon")
        session.add(
            CardAltName(
                card_id=1,
                language="PT",
                name="Um Caso para K9",
                name_normalized="um caso para k9",
                desc="",
            )
        )
        session.flush()

        repo = CardRepository(session)
        en_order = [c.name for c in repo.search(None, limit=10)]
        pt_order = [c.name for c in repo.search(None, limit=10, lang="PT", locale="PT_BR")]

        assert en_order == ["A Case for K9", "Book of Moon"]
        assert pt_order == ["Book of Moon", "A Case for K9"]

    def test_falls_back_to_english_name_without_translation(self, session: Session) -> None:
        _add_card(session, 1, "Zebra Card")
        _add_card(session, 2, "Apple Card")
        session.flush()

        # Nenhum CardAltName cadastrado para PT — deve continuar ordenando
        # pelo nome em inglês (fallback do COALESCE), sem quebrar nem
        # esconder cartas sem tradução.
        found = [c.name for c in CardRepository(session).search(None, limit=10, lang="PT")]
        assert found == ["Apple Card", "Zebra Card"]
