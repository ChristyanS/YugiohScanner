"""Regras de construção de deck do Deck Builder (Master Rule).

Escopo deliberado: só legalidade de **lista de deck** — tamanho de zona,
elegibilidade para Extra Deck, limite de cópias por carta/banlist. Regras de
mesa (zonas Pendulum, marcadores de Link) não afetam se uma lista de deck é
válida e ficam de fora.

`add_card` bloqueia (nunca ajusta quantidade em silêncio); `validate_deck`
nunca levanta exceção — é o painel "esse deck é legal?" que re-audita tudo,
inclusive o mínimo de 40 no Main Deck (que `add_card` não pode exigir, ou
seria impossível começar um deck do zero).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db.tables import Card, Deck, DeckCard
from ..errors import DeckNotFoundError, DeckValidationError
from ..repositories.card_filters import CardAttributeFilters
from ..repositories.collection import CollectionRepository
from ..repositories.decks import DeckRepository
from .catalog_service import CatalogService

MAIN_MIN, MAIN_MAX = 40, 60
EXTRA_MAX = 15
SIDE_MAX = 15

#: `frame_type` confirmados na base sincronizada (2026-09-14): nenhum
#: `link_pendulum` existe (Link nunca foi feito Pendulum) — lista fechada de
#: propósito, não um `endswith("_pendulum")` genérico.
EXTRA_DECK_FRAME_TYPES = frozenset(
    {"fusion", "synchro", "xyz", "link", "fusion_pendulum", "synchro_pendulum", "xyz_pendulum"}
)
#: Cartas que não são de deck de verdade: Skill (Speed Duel) e Token (nunca
#: se compra/inclui, é gerado em jogo) — nenhuma zona aceita.
NOT_DECK_ELIGIBLE_FRAME_TYPES = frozenset({"skill", "token"})

#: `Card.ban_tcg`/`.ban_ocg` -> limite de cópias; ausente (Unlimited) = 3.
BAN_LIMITS: dict[str, int] = {"Forbidden": 0, "Limited": 1, "Semi-Limited": 2}
DEFAULT_COPY_LIMIT = 3

_ZONE_MAX = {"main": MAIN_MAX, "extra": EXTRA_MAX, "side": SIDE_MAX}


@dataclass(frozen=True, slots=True)
class DeckValidationResult:
    legal: bool
    issues: list[str] = field(default_factory=list)
    main_count: int = 0
    extra_count: int = 0
    side_count: int = 0


class DeckService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = DeckRepository(session)
        self.collection_repo = CollectionRepository(session)
        self.catalog = CatalogService(session)

    # ------------------------------------------------------------------ CRUD

    def list_decks(self) -> list[Deck]:
        return self.repo.list_all()

    def get_deck(self, deck_id: int) -> Deck:
        deck = self.repo.get(deck_id)
        if deck is None:
            raise DeckNotFoundError(deck_id)
        return deck

    def create_deck(self, name: str, *, build_mode: str = "full_db", banlist: str = "TCG") -> Deck:
        return self.repo.create(name, build_mode=build_mode, banlist=banlist)

    def rename_deck(self, deck_id: int, name: str) -> Deck:
        deck = self.get_deck(deck_id)
        deck.name = name
        self.repo.touch(deck)
        return deck

    def set_cover(self, deck_id: int, card_id: int | None) -> Deck:
        deck = self.get_deck(deck_id)
        if card_id is not None:
            self.catalog.get_card(card_id)  # levanta CardNotFoundError se inválido
        deck.cover_card_id = card_id
        self.repo.touch(deck)
        return deck

    def delete_deck(self, deck_id: int) -> None:
        self.repo.delete(self.get_deck(deck_id))

    def clear_all_decks(self) -> int:
        """Apaga todos os decks de uma vez. Devolve quantos foram removidos,
        para a UI confirmar o que aconteceu."""
        return self.repo.clear_all()

    # --------------------------------------------------------------- cartas

    def add_card(self, deck_id: int, card_id: int, zone: str, *, quantity: int = 1) -> DeckCard:
        deck = self.get_deck(deck_id)
        card = self.catalog.get_card(card_id)

        if card.frame_type in NOT_DECK_ELIGIBLE_FRAME_TYPES:
            raise DeckValidationError(f"'{card.name}' não pode ser incluída em um deck.")
        is_extra_type = card.frame_type in EXTRA_DECK_FRAME_TYPES
        if zone == "extra" and not is_extra_type:
            raise DeckValidationError(
                f"'{card.name}' não é uma carta de Fusão/Sincro/XYZ/Link — não entra no Extra Deck."
            )
        if zone == "main" and is_extra_type:
            raise DeckValidationError(
                f"'{card.name}' é uma carta de Extra Deck — só entra no Extra Deck ou no Side Deck."
            )

        zone_count = sum(c.quantity for c in self.repo.cards_for_deck(deck_id) if c.zone == zone)
        zone_max = _ZONE_MAX[zone]
        if zone_count + quantity > zone_max:
            raise DeckValidationError(
                f"A zona '{zone}' já tem {zone_count} carta(s) (máximo {zone_max})."
            )

        current_total = self.repo.total_copies_in_deck(deck_id, card_id)
        copy_limit = self._copy_limit(card, deck.banlist)
        if current_total + quantity > copy_limit:
            raise DeckValidationError(
                f"'{card.name}' já tem {current_total} cópia(s) neste deck "
                f"(limite {copy_limit} na banlist {deck.banlist})."
            )

        if deck.build_mode == "collection_only":
            owned = self.collection_repo.total_owned(card_id)
            if current_total + quantity > owned:
                raise DeckValidationError(
                    f"Você só possui {owned} cópia(s) de '{card.name}' na coleção."
                )

        result = self.repo.upsert_card(deck_id, card_id, zone, quantity)
        self.repo.touch(deck)
        return result

    def remove_card(
        self, deck_id: int, card_id: int, zone: str, *, quantity: int | None = None
    ) -> None:
        deck = self.get_deck(deck_id)
        row = self.repo.get_deck_card(deck_id, card_id, zone)
        if row is None:
            return
        new_quantity = 0 if quantity is None else row.quantity - quantity
        self.repo.set_card_quantity(deck_id, card_id, zone, new_quantity)
        self.repo.touch(deck)

    # ------------------------------------------------------------ legalidade

    def validate_deck(self, deck_id: int) -> DeckValidationResult:
        deck = self.get_deck(deck_id)
        cards = self.repo.cards_for_deck(deck_id)

        counts = {"main": 0, "extra": 0, "side": 0}
        totals: dict[int, int] = {}
        for row in cards:
            counts[row.zone] += row.quantity
            totals[row.card_id] = totals.get(row.card_id, 0) + row.quantity

        issues: list[str] = []
        if not (MAIN_MIN <= counts["main"] <= MAIN_MAX):
            issues.append(
                f"Main Deck deve ter entre {MAIN_MIN} e {MAIN_MAX} cartas (tem {counts['main']})."
            )
        if counts["extra"] > EXTRA_MAX:
            issues.append(
                f"Extra Deck excede o máximo de {EXTRA_MAX} cartas (tem {counts['extra']})."
            )
        if counts["side"] > SIDE_MAX:
            issues.append(
                f"Side Deck excede o máximo de {SIDE_MAX} cartas (tem {counts['side']})."
            )

        for card_id, total in totals.items():
            card = self.catalog.get_card(card_id)
            limit = self._copy_limit(card, deck.banlist)
            if total > limit:
                issues.append(
                    f"'{card.name}' tem {total} cópia(s) no deck, limite é {limit} "
                    f"na banlist {deck.banlist}."
                )

        return DeckValidationResult(
            legal=not issues,
            issues=issues,
            main_count=counts["main"],
            extra_count=counts["extra"],
            side_count=counts["side"],
        )

    def searchable_pool(
        self,
        deck: Deck,
        query: str | None,
        *,
        filters: CardAttributeFilters | None = None,
        lang: str = "EN",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Card]:
        """Pool de cartas elegíveis para adicionar ao deck: só a coleção do
        usuário, ou o catálogo inteiro — reaproveita a busca já existente em
        cada caso, não duplica lógica de busca. `filters` são os mesmos
        filtros avançados (Tipo/Atributo/Race/Arquétipo/ATK/DEF/Estrelas/
        Escala/Link) do Banco de Dados e da Coleção
        (`repositories/card_filters.py`); `lang` é o mesmo idioma de busca
        multilíngue (ver docstring de `CollectionRepository.list_filtered`)."""
        if deck.build_mode == "collection_only":
            # `limit`/`offset` aqui contam CARTAS únicas, não linhas de
            # `CollectionItem` — uma carta com 2+ linhas (prints/condições/
            # idiomas diferentes) fazia o corte de linha (LIMIT/OFFSET no
            # nível de `list_filtered`) cair no meio de uma carta e devolver
            # menos cartas únicas que `limit` sem a coleção estar de fato
            # esgotada, e o Deck Builder lia isso como "não há mais páginas"
            # (achado real do usuário: cartas da coleção somem da busca).
            # Por isso busca TODAS as linhas que casam o filtro (sem
            # limit/offset — mesmo padrão de `ExportService.build_rows`),
            # dedupe para cartas únicas, e só então corta a página.
            items = self.collection_repo.list_filtered(search=query, filters=filters, lang=lang)
            seen: set[int] = set()
            cards: list[Card] = []
            for item in items:
                if item.card_id not in seen:
                    seen.add(item.card_id)
                    cards.append(item.card)
            return cards[offset : offset + limit]
        return self.catalog.search_cards(
            query, filters=filters, lang=lang, limit=limit, offset=offset
        )

    def remaining_copies(self, deck: Deck, card: Card) -> int:
        """No modo `collection_only`, quantas cópias possuídas de `card` ainda
        não foram usadas neste deck (plano do Deck Builder §5: selo na
        miniatura da busca). Nunca negativo — `add_card` já bloqueia exceder
        a posse, mas um deck criado antes de perder cópias da coleção (ex.:
        item removido depois) não deve exibir número negativo."""
        owned = self.collection_repo.total_owned(card.id)
        in_deck = self.repo.total_copies_in_deck(deck.id, card.id)
        return max(0, owned - in_deck)

    def _copy_limit(self, card: Card, banlist: str) -> int:
        status = card.ban_tcg if banlist == "TCG" else card.ban_ocg
        return BAN_LIMITS.get(status or "", DEFAULT_COPY_LIMIT)
