"""Casos de uso sobre a coleção (plano §11.4).

Fica acima de `repositories/collection.py`: a diferença é que o repositório só
sabe mexer em linhas por chave lógica, enquanto este serviço sabe **resolver**
o que o usuário digitou — nome parcial, código de set — contra o catálogo.
`collection add "blue eyes" --set-code LOB-001` passa por aqui.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db.tables import DEFAULT_CONDITION, DEFAULT_EDITION, DEFAULT_LANGUAGE, Card, CollectionItem
from ..domain.normalization import normalize_strict
from ..errors import AmbiguousCardError, CardNotFoundError, PrintNotFoundForCardError
from ..matching.candidates import CandidateFinder
from ..matching.resolver import PrintResolver
from ..repositories.collection import CollectionKey, CollectionRepository

#: Abaixo disso, um candidato não é bom o bastante para desambiguar sozinho.
#: Mais alto que o cutoff do OCR (plano §7.3) de propósito: aqui é um humano
#: digitando, não uma leitura ruidosa — a régua pode ser mais exigente.
MANUAL_ENTRY_CUTOFF = 85


@dataclass(frozen=True, slots=True)
class CollectionStats:
    """Números do dashboard (plano §10.1) e de `collection stats` (§8)."""

    distinct_cards: int
    total_copies: int
    items_without_print: int

    def as_dict(self) -> dict[str, int]:
        return {
            "distinct_cards": self.distinct_cards,
            "total_copies": self.total_copies,
            "items_without_print": self.items_without_print,
        }


class CollectionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CollectionRepository(session)

    # ------------------------------------------------------------------ leitura

    def stats(self) -> CollectionStats:
        items = self.repo.list_all()
        return CollectionStats(
            distinct_cards=len({item.card_id for item in items}),
            total_copies=sum(item.quantity for item in items),
            items_without_print=sum(1 for item in items if item.card_print_id is None),
        )

    # ------------------------------------------------------------------ escrita

    def add_manual(
        self,
        name_query: str,
        *,
        set_code: str | None = None,
        quantity: int = 1,
        condition: str = DEFAULT_CONDITION,
        edition: str = DEFAULT_EDITION,
        language: str = DEFAULT_LANGUAGE,
        notes: str | None = None,
    ) -> CollectionItem:
        """Adiciona pelo nome digitado (plano §8: `collection add`).

        Resolve o nome contra o catálogo (exato primeiro, fuzzy depois) e,
        se um `set_code` for dado, exige que ele pertença de fato à carta
        resolvida — um código incorreto aqui é erro de digitação do usuário,
        não uma leitura ruidosa de OCR, então não vira `NULL` em silêncio
        (diferente do comportamento do scanner, plano §7.2).
        """
        card = self._resolve_card(name_query)

        card_print_id: int | None = None
        if set_code:
            resolver = PrintResolver(self.session)
            prints = resolver.prints_for_card(card.id, set_code)
            if not prints:
                raise PrintNotFoundForCardError(card.name, set_code)
            if len(prints) > 1:
                raise AmbiguousCardError(
                    f"{card.name} ({set_code})",
                    [f"{p.set_code_full} — {p.rarity or 'raridade desconhecida'}" for p in prints],
                )
            card_print_id = prints[0].print_id

        key = CollectionKey(
            card_id=card.id,
            card_print_id=card_print_id,
            condition=condition,
            edition=edition,
            language=language,
        )
        return self.repo.add_copies(key, quantity, source="manual", notes=notes)

    def remove(self, item_id: int, quantity: int | None = None) -> int:
        return self.repo.remove_copies(item_id, quantity)

    def set_quantity(self, item_id: int, quantity: int) -> int:
        return self.repo.set_quantity(item_id, quantity)

    def set_print(self, item_id: int, card_print_id: int) -> CollectionItem:
        return self.repo.set_print(item_id, card_print_id)

    # ----------------------------------------------------------------- internos

    def _resolve_card(self, name_query: str) -> Card:
        strict = normalize_strict(name_query)
        exact = self.session.query(Card).filter(Card.name_normalized == strict).first()
        if exact is not None:
            return exact

        finder = CandidateFinder(self.session, fuzzy_cutoff=MANUAL_ENTRY_CUTOFF)
        candidates = finder.find(name_query, limit=5)
        if not candidates:
            raise CardNotFoundError(
                f"Nenhuma carta encontrada para '{name_query}'.",
                hint="Confira a grafia ou use o nome completo.",
            )

        # Só o topo é candidato de verdade se estiver claramente à frente do
        # segundo — mesmo raciocínio de margem do matching automático (§7.4),
        # só que aqui não há segunda evidência (set code) para desempatar.
        if len(candidates) > 1 and (candidates[0].score - candidates[1].score) < 0.05:
            raise AmbiguousCardError(
                name_query, [c.name for c in candidates if c.score >= candidates[0].score - 0.05]
            )

        card = self.session.get(Card, candidates[0].card_id)
        assert card is not None  # veio de uma query no próprio Card
        return card
