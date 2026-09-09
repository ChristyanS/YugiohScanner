"""Importação do catálogo da API para o banco local (plano §0.3 e Fase 2).

Duas propriedades inegociáveis:

**Idempotência.** Rodar `sync --force` duas vezes não pode duplicar nada. Tudo
é diff explícito (o que existe → update, o que não existe → insert), nunca
delete-and-recreate.

**Estabilidade dos IDs de print.** `collection_item.card_print_id` aponta para
`card_print.id`, que é uma chave sintética nossa (a API não fornece ID de print
— §0.3.2). Se um sync recriasse as linhas, os IDs mudariam e a coleção do
usuário perderia a referência do set. Por isso prints que sumiram da API são
**mantidos** e apenas registrados, nunca apagados.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from ..db.tables import Card, CardImage, CardPrint, CardSet, utcnow
from ..domain.normalization import normalize_strict
from ..domain.setcode import parse_set_code
from ..logging_setup import get_logger
from .schemas import ApiCard, ApiSet

log = get_logger(__name__)

#: Tamanho dos lotes de escrita. Grande o bastante para amortizar o overhead,
#: pequeno o bastante para não montar um INSERT gigante.
BATCH_SIZE = 500


@dataclass
class ImportStats:
    """Contagens de um import, para o relatório de sync."""

    sets_inserted: int = 0
    sets_updated: int = 0
    cards_inserted: int = 0
    cards_updated: int = 0
    images_inserted: int = 0
    images_updated: int = 0
    prints_inserted: int = 0
    prints_updated: int = 0
    #: Prints que existem no banco mas sumiram da API. Preservados de propósito.
    prints_stale: int = 0
    unparsed_set_codes: list[str] = field(default_factory=list)

    def merge(self, other: ImportStats) -> None:
        self.sets_inserted += other.sets_inserted
        self.sets_updated += other.sets_updated
        self.cards_inserted += other.cards_inserted
        self.cards_updated += other.cards_updated
        self.images_inserted += other.images_inserted
        self.images_updated += other.images_updated
        self.prints_inserted += other.prints_inserted
        self.prints_updated += other.prints_updated
        self.prints_stale += other.prints_stale
        self.unparsed_set_codes.extend(other.unparsed_set_codes)

    def as_dict(self) -> dict[str, int]:
        return {
            "sets_inserted": self.sets_inserted,
            "sets_updated": self.sets_updated,
            "cards_inserted": self.cards_inserted,
            "cards_updated": self.cards_updated,
            "images_inserted": self.images_inserted,
            "images_updated": self.images_updated,
            "prints_inserted": self.prints_inserted,
            "prints_updated": self.prints_updated,
            "prints_stale": self.prints_stale,
        }


# ----------------------------------------------------------------- mapeamento


def set_row(api_set: ApiSet, now: Any) -> dict[str, Any]:
    return {
        "set_code": api_set.set_code.strip().upper(),
        "set_name": api_set.set_name,
        "num_of_cards": api_set.num_of_cards,
        "tcg_date": api_set.tcg_date,
        "set_image": api_set.set_image,
        "synced_at": now,
    }


def card_row(api_card: ApiCard, now: Any) -> dict[str, Any]:
    misc = api_card.misc
    return {
        "id": api_card.id,
        "name": api_card.name,
        # A normalização acontece **na ingestão** para que a busca compare
        # sempre o mesmo formato dos dois lados (plano §7.1).
        "name_normalized": normalize_strict(api_card.name),
        "type": api_card.type,
        "frame_type": api_card.frame_type,
        "human_readable_type": api_card.human_readable_type,
        "desc": api_card.desc or "",
        "atk": api_card.atk,
        "defense": api_card.defense,
        "level": api_card.level,
        "attribute": api_card.attribute,
        "race": api_card.race,
        "archetype": api_card.archetype,
        "scale": api_card.scale,
        "linkval": api_card.linkval,
        "typeline": api_card.typeline,
        "linkmarkers": api_card.linkmarkers,
        "tcg_date": misc.tcg_date if misc else None,
        "ocg_date": misc.ocg_date if misc else None,
        "konami_id": misc.konami_id if misc else None,
        "has_effect": bool(misc.has_effect) if misc and misc.has_effect is not None else False,
        "ygoprodeck_url": api_card.ygoprodeck_url,
        "synced_at": now,
    }


def image_rows(api_card: ApiCard) -> list[dict[str, Any]]:
    rows = []
    for index, image in enumerate(api_card.card_images):
        rows.append(
            {
                "id": image.id,
                "card_id": api_card.id,
                "image_url": image.image_url,
                "image_url_small": image.image_url_small,
                "image_url_cropped": image.image_url_cropped,
                # A primeira arte é a canônica (a API a lista primeiro, e o id
                # dela coincide com o passcode).
                "is_primary": index == 0,
            }
        )
    return rows


def print_key(card_id: int, set_code_full: str, rarity: str | None) -> tuple[int, str, str]:
    """Chave lógica do print — a mesma do índice único `ux_print`."""
    return (card_id, set_code_full, rarity or "")


def print_rows(
    api_card: ApiCard, known_prefixes: set[str], unparsed: list[str], now: Any
) -> list[dict[str, Any]]:
    """Converte `card_sets[]` em linhas de `card_print`.

    Quando o prefixo não casa com nenhum set do catálogo, `set_prefix` fica
    NULL e o print é preservado com o `set_name` textual — perder o print seria
    pior do que não saber a qual set ele pertence (plano §4.4).
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()

    for entry in api_card.card_sets:
        code = entry.set_code.strip().upper()
        if not code:
            continue

        key = print_key(api_card.id, code, entry.set_rarity)
        if key in seen:
            # A API às vezes repete a mesma linha; o índice único rejeitaria.
            continue
        seen.add(key)

        parsed = parse_set_code(code)
        if parsed is None:
            unparsed.append(code)
            prefix, region, number, normalized = None, None, None, code
        else:
            normalized = parsed.normalized
            region, number = parsed.region, parsed.number
            prefix = parsed.prefix if parsed.prefix in known_prefixes else None

        rows.append(
            {
                "card_id": api_card.id,
                "set_code_full": code,
                "set_code_normalized": normalized,
                "set_prefix": prefix,
                "set_name": entry.set_name,
                "rarity": entry.set_rarity,
                "rarity_code": entry.set_rarity_code,
                "region": region,
                "number": number,
                "set_price": entry.set_price,
                "synced_at": now,
            }
        )
    return rows


# ------------------------------------------------------------------ importador


def _chunks(rows: Sequence[dict[str, Any]], size: int = BATCH_SIZE) -> Iterable[list[dict]]:
    for start in range(0, len(rows), size):
        yield list(rows[start : start + size])


class CatalogImporter:
    """Aplica os dados da API ao banco, em lotes e sem duplicar."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ sets

    def import_sets(self, api_sets: Sequence[ApiSet]) -> ImportStats:
        stats = ImportStats()
        if not api_sets:
            return stats

        now = utcnow()
        rows = [set_row(item, now) for item in api_sets]
        # A API já trouxe o mesmo prefixo duas vezes no passado; o último vence.
        deduped = {row["set_code"]: row for row in rows}

        existing = set(
            self.session.scalars(
                select(CardSet.set_code).where(CardSet.set_code.in_(deduped))
            ).all()
        )

        to_insert = [row for code, row in deduped.items() if code not in existing]
        to_update = [row for code, row in deduped.items() if code in existing]

        for batch in _chunks(to_insert):
            self.session.execute(insert(CardSet), batch)
        for batch in _chunks(to_update):
            self.session.execute(update(CardSet), batch)

        stats.sets_inserted = len(to_insert)
        stats.sets_updated = len(to_update)
        return stats

    def known_set_prefixes(self) -> set[str]:
        """Prefixos válidos — a base da camada 3 da validação de set code."""
        return set(self.session.scalars(select(CardSet.set_code)).all())

    # ----------------------------------------------------------------- cartas

    def import_cards(self, api_cards: Sequence[ApiCard], known_prefixes: set[str]) -> ImportStats:
        """Importa um lote de cartas com suas artes e prints."""
        stats = ImportStats()
        if not api_cards:
            return stats

        now = utcnow()
        card_ids = [card.id for card in api_cards]

        self._import_card_rows(api_cards, card_ids, now, stats)
        self._import_images(api_cards, card_ids, stats)
        self._import_prints(api_cards, card_ids, known_prefixes, now, stats)
        return stats

    def _import_card_rows(
        self,
        api_cards: Sequence[ApiCard],
        card_ids: Sequence[int],
        now: Any,
        stats: ImportStats,
    ) -> None:
        existing = set(self.session.scalars(select(Card.id).where(Card.id.in_(card_ids))).all())
        rows = [card_row(card, now) for card in api_cards]
        to_insert = [row for row in rows if row["id"] not in existing]
        to_update = [row for row in rows if row["id"] in existing]

        for batch in _chunks(to_insert):
            self.session.execute(insert(Card), batch)
        for batch in _chunks(to_update):
            self.session.execute(update(Card), batch)

        stats.cards_inserted = len(to_insert)
        stats.cards_updated = len(to_update)

    def _import_images(
        self, api_cards: Sequence[ApiCard], card_ids: Sequence[int], stats: ImportStats
    ) -> None:
        rows: list[dict[str, Any]] = []
        for card in api_cards:
            rows.extend(image_rows(card))
        if not rows:
            return

        existing = set(
            self.session.scalars(select(CardImage.id).where(CardImage.card_id.in_(card_ids))).all()
        )
        to_insert = [row for row in rows if row["id"] not in existing]
        to_update = [row for row in rows if row["id"] in existing]

        for batch in _chunks(to_insert):
            self.session.execute(insert(CardImage), batch)
        for batch in _chunks(to_update):
            # `cached_path` fica de fora: é estado local nosso, não da API.
            self.session.execute(update(CardImage), batch)

        stats.images_inserted = len(to_insert)
        stats.images_updated = len(to_update)

    def _import_prints(
        self,
        api_cards: Sequence[ApiCard],
        card_ids: Sequence[int],
        known_prefixes: set[str],
        now: Any,
        stats: ImportStats,
    ) -> None:
        rows: list[dict[str, Any]] = []
        for card in api_cards:
            rows.extend(print_rows(card, known_prefixes, stats.unparsed_set_codes, now))

        # Índice do que já existe, pela mesma chave lógica do `ux_print`.
        existing: dict[tuple[int, str, str], int] = {}
        for existing_row in self.session.execute(
            select(
                CardPrint.id, CardPrint.card_id, CardPrint.set_code_full, CardPrint.rarity
            ).where(CardPrint.card_id.in_(card_ids))
        ):
            existing[
                print_key(existing_row.card_id, existing_row.set_code_full, existing_row.rarity)
            ] = existing_row.id

        to_insert: list[dict[str, Any]] = []
        to_update: list[dict[str, Any]] = []
        seen_keys: set[tuple[int, str, str]] = set()

        for row in rows:
            key = print_key(row["card_id"], row["set_code_full"], row["rarity"])
            seen_keys.add(key)
            existing_id = existing.get(key)
            if existing_id is None:
                to_insert.append(row)
            else:
                # O `id` é o que mantém a referência da coleção intacta.
                to_update.append({**row, "id": existing_id})

        for batch in _chunks(to_insert):
            self.session.execute(insert(CardPrint), batch)
        for batch in _chunks(to_update):
            self.session.execute(update(CardPrint), batch)

        stats.prints_inserted = len(to_insert)
        stats.prints_updated = len(to_update)
        # Preservados de propósito: apagar quebraria a coleção do usuário.
        stats.prints_stale = len(set(existing) - seen_keys)
