"""Repositório da coleção (plano §4.4 e §13).

Regra que governa este arquivo: **upsert por chave lógica, nunca insert cego**.
A chave é `(card_id, card_print_id, condition, edition, language)` — a mesma do
índice único `ux_collection`. Adicionar cópias de uma combinação que já existe
soma na linha existente; nunca cria uma segunda linha para a mesma combinação.

`card_print_id` pode ser `NULL` ("carta identificada, set desconhecido" — plano
§4.4). O SQLite trata `NULL != NULL` em comparações normais, então a busca por
esse caso usa `.is_(None)` explicitamente; é o mesmo motivo pelo qual o índice
usa `COALESCE(card_print_id, -1)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from ..db.tables import (
    DEFAULT_CONDITION,
    DEFAULT_EDITION,
    DEFAULT_LANGUAGE,
    Card,
    CardAltName,
    CardPrint,
    CollectionItem,
    utcnow,
)
from ..domain.normalization import normalize_strict
from ..errors import CollectionItemNotFoundError


@dataclass(frozen=True, slots=True)
class CollectionKey:
    """A chave lógica de um item de coleção — espelha `ux_collection`."""

    card_id: int
    card_print_id: int | None = None
    #: Set já identificado sem print/raridade resolvidos — só faz sentido
    #: junto de `card_print_id=None` (ver `CollectionItem.set_code_full`).
    set_code_full: str | None = None
    #: Raridade pendente/manual — mesma condição de `set_code_full`.
    rarity: str | None = None
    condition: str = DEFAULT_CONDITION
    edition: str = DEFAULT_EDITION
    language: str = DEFAULT_LANGUAGE


class CollectionRepository:
    """CRUD da coleção, sempre por cima da chave lógica."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ leitura

    def find(self, key: CollectionKey) -> CollectionItem | None:
        """Busca a linha exata da chave lógica, ou `None`."""
        stmt = select(CollectionItem).where(
            CollectionItem.card_id == key.card_id,
            CollectionItem.condition == key.condition,
            CollectionItem.edition == key.edition,
            CollectionItem.language == key.language,
        )
        # `.is_(None)` é obrigatório aqui: `== None` o SQLAlchemy até traduz
        # certo, mas ser explícito documenta a armadilha do NULL do plano §4.3.
        stmt = stmt.where(
            CollectionItem.card_print_id.is_(None)
            if key.card_print_id is None
            else CollectionItem.card_print_id == key.card_print_id
        )
        stmt = stmt.where(
            CollectionItem.set_code_full.is_(None)
            if key.set_code_full is None
            else CollectionItem.set_code_full == key.set_code_full
        )
        stmt = stmt.where(
            CollectionItem.rarity.is_(None)
            if key.rarity is None
            else CollectionItem.rarity == key.rarity
        )
        return self.session.scalars(stmt).first()

    def get(self, item_id: int) -> CollectionItem | None:
        return self.session.get(CollectionItem, item_id)

    def list_all(self) -> list[CollectionItem]:
        return list(self.session.scalars(select(CollectionItem)))

    def list_for_card(self, card_id: int) -> list[CollectionItem]:
        return list(
            self.session.scalars(select(CollectionItem).where(CollectionItem.card_id == card_id))
        )

    def count_items(self) -> int:
        return len(self.session.scalars(select(CollectionItem.id)).all())

    def total_copies(self) -> int:
        return sum(self.session.scalars(select(CollectionItem.quantity)).all())

    # ------------------------------------------------------------------ escrita

    def add_copies(
        self,
        key: CollectionKey,
        quantity: int = 1,
        *,
        source: str = "manual",
        notes: str | None = None,
    ) -> CollectionItem:
        """Soma `quantity` cópias na combinação da chave — cria se não existir.

        `quantity` precisa ser positivo: isto adiciona, nunca subtrai (use
        `remove_copies` para o inverso). O `source` só é gravado na criação —
        uma linha existente mantém a origem da primeira vez que foi adicionada.
        """
        if quantity <= 0:
            raise ValueError(f"quantity deve ser positivo, recebido {quantity}")

        existing = self.find(key)
        if existing is not None:
            existing.quantity += quantity
            existing.updated_at = utcnow()
            if notes and not existing.notes:
                existing.notes = notes
            # A sessão é `autoflush=False`: sem isto, uma consulta SELECT
            # seguinte na mesma sessão (ex.: `find()` de novo, ou uma leitura
            # de estatísticas) não veria esta soma até o próximo commit.
            self.session.flush()
            return existing

        item = CollectionItem(
            card_id=key.card_id,
            card_print_id=key.card_print_id,
            set_code_full=key.set_code_full,
            rarity=key.rarity,
            quantity=quantity,
            condition=key.condition,
            edition=key.edition,
            language=key.language,
            source=source,
            notes=notes,
        )
        self.session.add(item)
        self.session.flush()  # popula item.id para quem for referenciá-lo
        return item

    def remove_copies(self, item_id: int, quantity: int | None = None) -> int:
        """Remove até `quantity` cópias (todas, se `None`).

        Nunca deixa a linha com `quantity <= 0` — o CHECK do banco proíbe isso
        de qualquer forma (plano §4.3: "quantidade zero → a linha some, não
        fica zerada"). Devolve o que restou (0 quando a linha foi apagada).

        Idempotente contra remoção em excesso: pedir mais do que existe apenas
        zera a linha, sem erro — é o comportamento que um "remover" manual
        no CLI/Web precisa ter.
        """
        item = self.get(item_id)
        if item is None:
            raise CollectionItemNotFoundError(item_id)

        to_remove = item.quantity if quantity is None else min(quantity, item.quantity)
        if to_remove <= 0:
            return item.quantity

        remaining = item.quantity - to_remove
        if remaining <= 0:
            self.session.delete(item)
            self.session.flush()
            return 0

        item.quantity = remaining
        item.updated_at = utcnow()
        self.session.flush()
        return remaining

    def set_quantity(self, item_id: int, quantity: int) -> int:
        """Define a quantidade absoluta. `<= 0` remove a linha."""
        item = self.get(item_id)
        if item is None:
            raise CollectionItemNotFoundError(item_id)

        if quantity <= 0:
            self.session.delete(item)
            self.session.flush()
            return 0

        item.quantity = quantity
        item.updated_at = utcnow()
        self.session.flush()
        return quantity

    def set_notes(self, item_id: int, notes: str | None) -> CollectionItem:
        """Substitui a anotação livre (Fase 8: `PATCH /api/v1/collection/{id}`)."""
        item = self.get(item_id)
        if item is None:
            raise CollectionItemNotFoundError(item_id)

        item.notes = notes
        item.updated_at = utcnow()
        self.session.flush()
        return item

    def set_rarity(self, item_id: int, rarity: str | None) -> CollectionItem:
        """Grava a raridade "pendente" (texto livre, sem print resolvido).

        Só se aplica a itens com `card_print_id IS NULL` — uma vez que o
        print está resolvido, a raridade vem dele (`rarity_display`); usar
        `CollectionService.resolve_rarity` quando a raridade escolhida bater
        com uma das catalogadas, para resolver o print de verdade em vez de
        só anotar o texto.
        """
        item = self.get(item_id)
        if item is None:
            raise CollectionItemNotFoundError(item_id)

        item.rarity = rarity
        item.updated_at = utcnow()
        self.session.flush()
        return item

    def set_print(self, item_id: int, card_print_id: int) -> CollectionItem:
        """Resolve manualmente o set de um item que estava `NULL` (plano §11.4).

        Se já existir uma linha para o print de destino com a mesma condição/
        edição/idioma, funde as duas (soma quantidade, apaga a origem) em vez
        de violar `ux_collection` com uma segunda linha.
        """
        item = self.get(item_id)
        if item is None:
            raise CollectionItemNotFoundError(item_id)

        target_key = CollectionKey(
            card_id=item.card_id,
            card_print_id=card_print_id,
            condition=item.condition,
            edition=item.edition,
            language=item.language,
        )
        target = self.find(target_key)
        if target is not None and target.id != item.id:
            target.quantity += item.quantity
            target.updated_at = utcnow()
            self.session.delete(item)
            self.session.flush()
            return target

        item.card_print_id = card_print_id
        # Print resolvido de verdade: os campos "pendente" deixam de fazer
        # sentido (a leitura passa a vir de `card_print` via `*_display`).
        item.set_code_full = None
        item.rarity = None
        item.updated_at = utcnow()
        self.session.flush()
        # Regressão: `flush()` grava a coluna FK, mas a relação `card_print`
        # (lazy="joined") **não** se resincroniza sozinha — ela já tinha sido
        # carregada como None antes deste método rodar. Sem o refresh, quem
        # lê `item.card_print` logo em seguida (mesma sessão) via o cache
        # antigo, mesmo com a FK já correta no banco.
        self.session.refresh(item, attribute_names=["card_print"])
        return item

    # ----------------------------------------------------------------- busca p/ CLI

    def find_by_card_name(self, name_normalized: str) -> list[Card]:
        """Cartas do catálogo cujo nome normalizado contém o termo.

        Usado pela resolução de nome parcial em `collection add` (Fase 6) —
        vive aqui e não em `matching/` porque não envolve OCR nem confiança,
        é busca direta de usuário digitando um nome.
        """
        stmt = select(Card).where(Card.name_normalized.contains(name_normalized))
        return list(self.session.scalars(stmt))

    def prints_for_card(self, card_id: int) -> list[CardPrint]:
        return list(self.session.scalars(select(CardPrint).where(CardPrint.card_id == card_id)))

    #: Colunas aceitas por `--sort` em `collection list` (plano §8).
    SORT_COLUMNS: ClassVar[dict[str, Any]] = {
        "name": Card.name,
        "quantity": CollectionItem.quantity,
        "added": CollectionItem.added_at,
        "set": CardPrint.set_code_full,
    }

    def list_filtered(
        self,
        *,
        search: str | None = None,
        set_prefix: str | None = None,
        no_set: bool = False,
        no_rarity: bool = False,
        sort: str = "name",
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CollectionItem]:
        """Consulta filtrada para `collection list` — a única tela que
        precisa de busca, filtro e ordenação simultâneos (plano §11)."""
        # `.outerjoin(CardPrint)` sozinho é ambíguo assim que `Card` já está na
        # query: o SQLAlchemy pode escolher `CardPrint.card_id == Card.id`
        # (TODOS os prints da carta) em vez de
        # `CollectionItem.card_print_id == CardPrint.id` (o print específico
        # deste item) — e ele escolhe a opção errada aqui, porque `card` foi
        # a tabela mais recentemente unida. O sintoma são duas facetas do
        # mesmo bug: `--limit` cortava linhas duplicadas pela metade, e um
        # filtro `--set` batia se a carta tivesse *qualquer* print naquele
        # set, não o print que o item realmente tem. Usar a relação
        # (`CollectionItem.card_print`) força o caminho de FK certo.
        stmt = select(CollectionItem).join(Card).outerjoin(CollectionItem.card_print)

        if search:
            # Multilíngue desde a Fase 3 do faseamento web: bate tanto no nome
            # canônico (inglês) quanto em qualquer tradução sincronizada
            # (FR/DE/IT/PT) — quem tem a carta física em outro idioma pode
            # digitar exatamente o que está impresso nela. `EXISTS` em vez de
            # `join`: uma carta com 4 traduções não pode virar 4 linhas na
            # coleção só porque a busca casou em mais de um idioma.
            term = normalize_strict(search)
            alt_match = exists(
                select(CardAltName.id).where(
                    CardAltName.card_id == Card.id,
                    CardAltName.name_normalized.contains(term),
                )
            )
            stmt = stmt.where(or_(Card.name_normalized.contains(term), alt_match))
        if set_prefix:
            stmt = stmt.where(CardPrint.set_prefix == set_prefix.strip().upper())
        if no_set:
            stmt = stmt.where(CollectionItem.card_print_id.is_(None))
        if no_rarity:
            # Só faz sentido quando o set já é conhecido: ou o print está
            # resolvido mas sem raridade catalogada, ou o print está
            # pendente (`set_code_full` preenchido) e a raridade também.
            stmt = stmt.where(
                or_(
                    and_(CollectionItem.card_print_id.is_not(None), CardPrint.rarity.is_(None)),
                    and_(
                        CollectionItem.card_print_id.is_(None),
                        CollectionItem.set_code_full.is_not(None),
                        CollectionItem.rarity.is_(None),
                    ),
                )
            )

        column = self.SORT_COLUMNS.get(sort, Card.name)
        stmt = stmt.order_by(column.desc() if descending else column.asc())

        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)

        return list(self.session.scalars(stmt).unique())
