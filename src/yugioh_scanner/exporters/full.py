"""Perfil `full` — backup/restauração da própria aplicação (plano §15.3).

Superset com todos os campos internos, inclusive os IDs (`card_id`,
`card_print_id`). É o único perfil com round-trip garantido: os outros
formatos existem para OUTRAS aplicações lerem, e perdem informação de
propósito (raridade textual em vez de ID, sem condição/idioma, etc.).
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy.orm import Session

from ..repositories.collection import CollectionKey, CollectionRepository
from .base import CollectionRow, is_blank, sanitize_csv_cell

COLUMNS = [
    "item_id",
    "card_id",
    "card_name",
    "card_print_id",
    "set_code_full",
    "set_prefix",
    "region",
    "rarity",
    "quantity",
    "condition",
    "edition",
    "language",
    "source",
    "added_at",
    "notes",
]


class FullCsvProfile:
    name = "full"
    format = "csv"
    columns = COLUMNS
    encoding = "utf-8"
    newline = "\n"

    def render(self, rows: Iterable[CollectionRow], out: TextIO) -> None:
        writer = csv.writer(out, lineterminator=self.newline)
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.item_id,
                    row.card_id,
                    sanitize_csv_cell(row.card_name),
                    row.card_print_id if row.card_print_id is not None else "",
                    row.set_code_full or "",
                    row.set_prefix or "",
                    row.region or "",
                    sanitize_csv_cell(row.rarity or ""),
                    row.quantity,
                    row.condition,
                    row.edition,
                    row.language,
                    row.source,
                    row.added_at,
                    sanitize_csv_cell(row.notes or ""),
                ]
            )


def build() -> FullCsvProfile:
    return FullCsvProfile()


@dataclass(frozen=True, slots=True)
class ImportedRow:
    """Uma linha lida de volta — antes de virar upsert na coleção."""

    card_id: int
    card_print_id: int | None
    quantity: int
    condition: str
    edition: str
    language: str
    source: str
    notes: str | None


@dataclass
class FullImportStats:
    rows_read: int = 0
    items_created: int = 0
    items_updated: int = 0
    copies_added: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "rows_read": self.rows_read,
            "items_created": self.items_created,
            "items_updated": self.items_updated,
            "copies_added": self.copies_added,
        }


def parse_full_csv(text: str) -> list[ImportedRow]:
    """Lê um CSV no formato `full` de volta em linhas estruturadas.

    Tolera linhas em branco (sujeira comum de editor de planilha) e valida o
    cabeçalho — um arquivo de outro perfil não é aceito em silêncio.
    """
    lines = [line for line in text.splitlines() if not is_blank(line)]
    reader = csv.DictReader(lines)
    if reader.fieldnames is None or list(reader.fieldnames) != COLUMNS:
        raise ValueError(
            "Cabeçalho não bate com o perfil 'full'. "
            f"Esperado: {COLUMNS}, encontrado: {reader.fieldnames}"
        )

    rows: list[ImportedRow] = []
    for raw in reader:
        rows.append(
            ImportedRow(
                card_id=int(raw["card_id"]),
                card_print_id=int(raw["card_print_id"]) if raw["card_print_id"] else None,
                quantity=int(raw["quantity"]),
                condition=raw["condition"],
                edition=raw["edition"],
                language=raw["language"],
                source=raw["source"] or "import",
                notes=raw["notes"] or None,
            )
        )
    return rows


def import_full_csv(session: Session, text: str) -> FullImportStats:
    """Restaura uma coleção a partir de um export `full` (plano §15.3/Fase 7).

    Upsert por chave lógica, igual a qualquer outra escrita na coleção
    (`CollectionRepository.add_copies`) — importar duas vezes soma, nunca
    duplica linha.
    """
    stats = FullImportStats()
    repo = CollectionRepository(session)

    for parsed in parse_full_csv(text):
        stats.rows_read += 1
        key = CollectionKey(
            card_id=parsed.card_id,
            card_print_id=parsed.card_print_id,
            condition=parsed.condition,
            edition=parsed.edition,
            language=parsed.language,
        )
        existed = repo.find(key) is not None
        repo.add_copies(key, parsed.quantity, source=parsed.source, notes=parsed.notes)
        stats.copies_added += parsed.quantity
        if existed:
            stats.items_updated += 1
        else:
            stats.items_created += 1

    return stats
