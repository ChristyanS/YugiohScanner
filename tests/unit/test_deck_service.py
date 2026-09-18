"""`DeckService` — regras de construção de deck (Master Rule): tamanho de
zona, elegibilidade para Extra Deck, limite de cópias por carta/banlist
(somado entre zonas), e o teto extra do modo "só coleção".
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from yugioh_scanner.db.tables import Card, CollectionItem
from yugioh_scanner.errors import DeckValidationError
from yugioh_scanner.services.deck_service import DeckService

NORMAL_CARD = 1
FUSION_CARD = 2
LIMITED_CARD = 3
SKILL_CARD = 4
FORBIDDEN_OCG_ONLY = 5


def _seed_cards(session: Session) -> None:
    session.add_all(
        [
            Card(
                id=NORMAL_CARD,
                name="Normal Monster",
                name_normalized="normal monster",
                type="Normal Monster",
                frame_type="normal",
                desc="",
            ),
            Card(
                id=FUSION_CARD,
                name="Fusion Monster",
                name_normalized="fusion monster",
                type="Fusion Monster",
                frame_type="fusion",
                desc="",
            ),
            Card(
                id=LIMITED_CARD,
                name="Limited Card",
                name_normalized="limited card",
                type="Spell Card",
                frame_type="spell",
                desc="",
                ban_tcg="Limited",
            ),
            Card(
                id=SKILL_CARD,
                name="Some Skill",
                name_normalized="some skill",
                type="Skill Card",
                frame_type="skill",
                desc="",
            ),
            Card(
                id=FORBIDDEN_OCG_ONLY,
                name="OCG Banned Card",
                name_normalized="ocg banned card",
                type="Spell Card",
                frame_type="spell",
                desc="",
                ban_ocg="Forbidden",
            ),
        ]
    )
    session.flush()


@pytest.fixture
def service(session: Session) -> DeckService:
    _seed_cards(session)
    return DeckService(session)


class TestZoneEligibility:
    def test_extra_deck_card_rejected_from_main(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        with pytest.raises(DeckValidationError, match="Extra Deck"):
            service.add_card(deck.id, FUSION_CARD, "main")

    def test_extra_deck_card_accepted_in_extra(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        service.add_card(deck.id, FUSION_CARD, "extra")
        assert service.repo.total_copies_in_deck(deck.id, FUSION_CARD) == 1

    def test_extra_deck_card_accepted_in_side(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        service.add_card(deck.id, FUSION_CARD, "side")
        assert service.repo.total_copies_in_deck(deck.id, FUSION_CARD) == 1

    def test_normal_monster_rejected_from_extra(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        with pytest.raises(DeckValidationError, match="não é uma carta de Fusão"):
            service.add_card(deck.id, NORMAL_CARD, "extra")

    def test_skill_card_rejected_from_any_zone(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        with pytest.raises(DeckValidationError, match="não pode ser incluída"):
            service.add_card(deck.id, SKILL_CARD, "side")


class TestCopyLimits:
    def test_default_limit_is_three(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        service.add_card(deck.id, NORMAL_CARD, "main", quantity=3)
        with pytest.raises(DeckValidationError, match="limite 3"):
            service.add_card(deck.id, NORMAL_CARD, "main", quantity=1)

    def test_limit_reduced_by_banlist(self, service: DeckService) -> None:
        deck = service.create_deck("D1", banlist="TCG")
        service.add_card(deck.id, LIMITED_CARD, "main", quantity=1)
        with pytest.raises(DeckValidationError, match="limite 1"):
            service.add_card(deck.id, LIMITED_CARD, "main", quantity=1)

    def test_limit_counted_across_zones_not_per_zone(self, service: DeckService) -> None:
        """O bug mais fácil de cometer aqui: 2 no main + 1 no side é 3 cópias
        da mesma carta no deck inteiro, não "3 permitidas em cada zona"."""
        deck = service.create_deck("D1")
        service.add_card(deck.id, NORMAL_CARD, "main", quantity=2)
        service.add_card(deck.id, NORMAL_CARD, "side", quantity=1)
        with pytest.raises(DeckValidationError, match="limite 3"):
            service.add_card(deck.id, NORMAL_CARD, "side", quantity=1)

    def test_banlist_choice_is_per_deck(self, service: DeckService) -> None:
        """Mesma carta, banida só em OCG — um deck TCG não é afetado, um
        deck OCG é."""
        tcg_deck = service.create_deck("TCG Deck", banlist="TCG")
        service.add_card(tcg_deck.id, FORBIDDEN_OCG_ONLY, "main", quantity=3)  # ok

        ocg_deck = service.create_deck("OCG Deck", banlist="OCG")
        with pytest.raises(DeckValidationError, match="limite 0"):
            service.add_card(ocg_deck.id, FORBIDDEN_OCG_ONLY, "main", quantity=1)


class TestZoneSizeLimits:
    def test_extra_deck_max_15(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        # 5 cartas diferentes de fusão pra não esbarrar no limite de cópias
        for i in range(5):
            card_id = 100 + i
            service.session.add(
                Card(
                    id=card_id,
                    name=f"Fusion {i}",
                    name_normalized=f"fusion {i}",
                    type="Fusion Monster",
                    frame_type="fusion",
                    desc="",
                )
            )
        service.session.flush()
        for i in range(5):
            service.add_card(deck.id, 100 + i, "extra", quantity=3)
        with pytest.raises(DeckValidationError, match="máximo 15"):
            service.session.add(
                Card(
                    id=200,
                    name="One more fusion",
                    name_normalized="one more fusion",
                    type="Fusion Monster",
                    frame_type="fusion",
                    desc="",
                )
            )
            service.session.flush()
            service.add_card(deck.id, 200, "extra", quantity=1)


class TestCollectionOnlyMode:
    def test_caps_at_owned_quantity(self, service: DeckService) -> None:
        deck = service.create_deck("D1", build_mode="collection_only")
        service.session.add(CollectionItem(card_id=NORMAL_CARD, quantity=2))
        service.session.flush()

        service.add_card(deck.id, NORMAL_CARD, "main", quantity=2)
        with pytest.raises(DeckValidationError, match="Você só possui 2"):
            service.add_card(deck.id, NORMAL_CARD, "main", quantity=1)

    def test_full_db_mode_ignores_ownership(self, service: DeckService) -> None:
        deck = service.create_deck("D1", build_mode="full_db")
        # nenhuma CollectionItem cadastrada — mesmo assim aceita até o limite padrão
        service.add_card(deck.id, NORMAL_CARD, "main", quantity=3)
        assert service.repo.total_copies_in_deck(deck.id, NORMAL_CARD) == 3


class TestValidateDeck:
    def test_incomplete_main_deck_is_not_legal(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        service.add_card(deck.id, NORMAL_CARD, "main", quantity=3)
        result = service.validate_deck(deck.id)
        assert result.legal is False
        assert any("40" in issue for issue in result.issues)

    def test_empty_deck_reports_main_count_zero(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        result = service.validate_deck(deck.id)
        assert result.main_count == 0
        assert result.legal is False


class TestRemoveCard:
    def test_remove_one_copy(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        service.add_card(deck.id, NORMAL_CARD, "main", quantity=3)
        service.remove_card(deck.id, NORMAL_CARD, "main", quantity=1)
        assert service.repo.total_copies_in_deck(deck.id, NORMAL_CARD) == 2

    def test_remove_all_copies(self, service: DeckService) -> None:
        deck = service.create_deck("D1")
        service.add_card(deck.id, NORMAL_CARD, "main", quantity=3)
        service.remove_card(deck.id, NORMAL_CARD, "main")
        assert service.repo.total_copies_in_deck(deck.id, NORMAL_CARD) == 0
