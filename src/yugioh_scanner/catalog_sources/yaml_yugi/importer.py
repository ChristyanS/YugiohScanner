"""Aplica o dataset `yaml-yugi` ao catálogo local (ADR 0012, Opção B).

Três regras inegociáveis, todas derivadas da validação de risco em
docs/proposta-fontes-dados-catalogo.md §3.1:

1. **Nunca cria `Card` nova.** Casa só por `password == Card.id`; carta que
   não existe no catálogo primário (YGOPRODeck) é ignorada — a identidade da
   carta continua vindo de uma fonte só.
2. **Nunca sobrescreve uma linha `data_source='ygoprodeck'`.** Enriquecimento
   só preenche o que a fonte primária não tem; se um sync futuro da
   YGOPRODeck passar a trazer o mesmo dado, ele automaticamente volta a ser
   a autoridade (o importador primário já teria atualizado a linha antes
   deste rodar).
3. **Só os idiomas de `ALT_NAME_LANGUAGES`.** Exclui `es` de propósito —
   medido 0 prints `es` reais em todo o dataset apesar de 98% das cartas
   terem nome em espanhol — e nunca importa nome sinalizado
   `is_translation_unofficial`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from ...db.tables import (
    ALT_NAME_LANGUAGES,
    DATA_SOURCE_YAML_YUGI,
    DATA_SOURCE_YGOPRODECK,
    Card,
    CardAltName,
    CardPrint,
    CardSet,
    utcnow,
)
from ...domain.normalization import normalize_strict
from ...domain.setcode import parse_set_code
from ...logging_setup import get_logger
from .schemas import YamlYugiCard

log = get_logger(__name__)

BATCH_SIZE = 500

#: Idioma yaml-yugi (chave ISO minúscula) -> código que o app usa (maiúsculo).
#: Só os que `ALT_NAME_LANGUAGES` já reconhece — `es`/`ja_romaji`/`ko_rr`/
#: `zh-TW`/`zh-CN` ficam de fora de propósito (proposta §3.1.3 e §3.2).
LANGUAGE_MAP: dict[str, str] = {
    "de": "DE",
    "fr": "FR",
    "it": "IT",
    "pt": "PT",
    "ja": "JA",
    "ko": "KO",
}
assert set(LANGUAGE_MAP.values()) <= set(ALT_NAME_LANGUAGES)


@dataclass
class EnrichmentStats:
    cards_matched: int = 0
    cards_unmatched: int = 0
    alt_names_inserted: int = 0
    alt_names_updated: int = 0
    alt_names_skipped_unofficial: int = 0
    sets_inserted: int = 0
    prints_inserted: int = 0
    prints_updated: int = 0
    prints_skipped_primary: int = 0
    unparsed_set_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "cards_matched": self.cards_matched,
            "cards_unmatched": self.cards_unmatched,
            "alt_names_inserted": self.alt_names_inserted,
            "alt_names_updated": self.alt_names_updated,
            "alt_names_skipped_unofficial": self.alt_names_skipped_unofficial,
            "sets_inserted": self.sets_inserted,
            "prints_inserted": self.prints_inserted,
            "prints_updated": self.prints_updated,
            "prints_skipped_primary": self.prints_skipped_primary,
        }


def _chunks(rows: Sequence[dict[str, Any]], size: int = BATCH_SIZE) -> list[list[dict]]:
    return [list(rows[start : start + size]) for start in range(0, len(rows), size)]


def _print_key(card_id: int, set_code_full: str, rarity: str | None) -> tuple[int, str, str]:
    """Mesma chave lógica de `ygoprodeck.importer.print_key` / `ux_print`."""
    return (card_id, set_code_full, rarity or "")


class YamlYugiEnrichmentImporter:
    """Aplica um lote de `YamlYugiCard` ao banco, respeitando as três regras."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def import_cards(self, cards: Sequence[YamlYugiCard]) -> EnrichmentStats:
        stats = EnrichmentStats()
        if not cards:
            return stats

        known_card_ids = set(self.session.scalars(select(Card.id)).all())
        matched = [card for card in cards if card.password in known_card_ids]
        stats.cards_matched = len(matched)
        stats.cards_unmatched = len(cards) - len(matched)
        if not matched:
            return stats

        self._import_alt_names(matched, stats)
        self._import_sets_and_prints(matched, stats)
        return stats

    # ---------------------------------------------------------------- nomes

    def _import_alt_names(self, cards: Sequence[YamlYugiCard], stats: EnrichmentStats) -> None:
        now = utcnow()
        card_ids = [card.password for card in cards if card.password is not None]

        existing: dict[tuple[int, str], tuple[int, str]] = {
            (row.card_id, row.language): (row.id, row.data_source)
            for row in self.session.execute(
                select(
                    CardAltName.id,
                    CardAltName.card_id,
                    CardAltName.language,
                    CardAltName.data_source,
                ).where(CardAltName.card_id.in_(card_ids))
            )
        }

        to_insert: list[dict[str, Any]] = []
        to_update: list[dict[str, Any]] = []

        for card in cards:
            if card.password is None:
                continue
            for lang_lower, our_lang in LANGUAGE_MAP.items():
                name = card.name.get(lang_lower)
                if not name:
                    continue
                if card.is_name_unofficial(lang_lower):
                    stats.alt_names_skipped_unofficial += 1
                    continue

                key = (card.password, our_lang)
                found = existing.get(key)
                if found is not None and found[1] == DATA_SOURCE_YGOPRODECK:
                    # A fonte primária já tem esse idioma — nunca sobrescreve.
                    continue

                row = {
                    "card_id": card.password,
                    "language": our_lang,
                    "name": name,
                    "name_normalized": normalize_strict(name),
                    "desc": "",
                    "name_en": card.name.get("en"),
                    "data_source": DATA_SOURCE_YAML_YUGI,
                    "synced_at": now,
                }
                if found is None:
                    to_insert.append(row)
                else:
                    to_update.append({**row, "id": found[0]})

        for batch in _chunks(to_insert):
            self.session.execute(insert(CardAltName), batch)
        for batch in _chunks(to_update):
            self.session.execute(update(CardAltName), batch)

        stats.alt_names_inserted = len(to_insert)
        stats.alt_names_updated = len(to_update)

    # ------------------------------------------------------------ sets/prints

    def _import_sets_and_prints(
        self, cards: Sequence[YamlYugiCard], stats: EnrichmentStats
    ) -> None:
        now = utcnow()
        card_ids = [card.password for card in cards if card.password is not None]

        known_prefixes = set(self.session.scalars(select(CardSet.set_code)).all())
        staged_sets: dict[str, dict[str, Any]] = {}

        existing_prints: dict[tuple[int, str, str], tuple[int, str]] = {
            _print_key(row.card_id, row.set_code_full, row.rarity): (row.id, row.data_source)
            for row in self.session.execute(
                select(
                    CardPrint.id, CardPrint.card_id, CardPrint.set_code_full,
                    CardPrint.rarity, CardPrint.data_source,
                ).where(CardPrint.card_id.in_(card_ids))
            )
        }

        to_insert: list[dict[str, Any]] = []
        to_update: list[dict[str, Any]] = []
        #: Guarda contra o dataset repetir a mesma linha (mesma cautela de
        #: `ygoprodeck.importer.print_rows` para a fonte primária) — sem
        #: isso, um duplicado no JSON colidiria com `ux_print` no insert.
        seen_this_run: set[tuple[int, str, str]] = set()

        for card in cards:
            if card.password is None:
                continue
            for lang_lower, our_lang in LANGUAGE_MAP.items():
                for entry in card.sets.get(lang_lower, []):
                    code = entry.set_number.strip().upper()
                    if not code:
                        continue

                    parsed = parse_set_code(code)
                    if parsed is None:
                        stats.unparsed_set_codes.append(code)
                        prefix, normalized = None, code
                        number = None
                    else:
                        normalized = parsed.normalized
                        number = parsed.number
                        prefix = parsed.prefix
                        if prefix not in known_prefixes and prefix not in staged_sets:
                            staged_sets[prefix] = {
                                "set_code": prefix,
                                "set_name": entry.set_name,
                                "synced_at": now,
                            }

                    rarities: list[str | None] = list(entry.rarities) or [None]
                    for rarity in rarities:
                        key = _print_key(card.password, code, rarity)
                        if key in seen_this_run:
                            continue
                        seen_this_run.add(key)

                        found = existing_prints.get(key)
                        if found is not None and found[1] == DATA_SOURCE_YGOPRODECK:
                            stats.prints_skipped_primary += 1
                            continue

                        row = {
                            "card_id": card.password,
                            "set_code_full": code,
                            "set_code_normalized": normalized,
                            "set_prefix": prefix,
                            "set_name": entry.set_name,
                            "rarity": rarity,
                            "rarity_code": None,
                            "region": our_lang,
                            "number": number,
                            "set_price": None,
                            "data_source": DATA_SOURCE_YAML_YUGI,
                            "synced_at": now,
                        }
                        if found is None:
                            to_insert.append(row)
                        else:
                            to_update.append({**row, "id": found[0]})

        if staged_sets:
            for batch in _chunks(list(staged_sets.values())):
                self.session.execute(insert(CardSet), batch)
            self.session.flush()
            stats.sets_inserted = len(staged_sets)

        for batch in _chunks(to_insert):
            self.session.execute(insert(CardPrint), batch)
        for batch in _chunks(to_update):
            self.session.execute(update(CardPrint), batch)

        stats.prints_inserted = len(to_insert)
        stats.prints_updated = len(to_update)
