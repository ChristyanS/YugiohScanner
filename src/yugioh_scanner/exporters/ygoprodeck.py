"""Perfil `ygoprodeck` — formato verificado do importador oficial (plano §15.2).

Confirmado via fórum/suporte oficial da YGOPRODeck (docs/PLAN.md §0.4):
`Card Name, Card Quantity, Card Rarity, Card Condition, Card Edition, Card Set,
Card Set Code` — nessa ordem, sem a coluna `cardid` de exports antigos, e sem
nada com prefixo `Custom_`.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from typing import TextIO

from .base import CollectionRow, sanitize_csv_cell

COLUMNS = [
    "Card Name",
    "Card Quantity",
    "Card Rarity",
    "Card Condition",
    "Card Edition",
    "Card Set",
    "Card Set Code",
]


class YgoProDeckProfile:
    name = "ygoprodeck"
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
                    sanitize_csv_cell(row.card_name),
                    row.quantity,
                    sanitize_csv_cell(row.rarity or ""),
                    sanitize_csv_cell(row.condition),
                    sanitize_csv_cell(row.edition),
                    sanitize_csv_cell(row.set_name or ""),
                    row.set_code_full or "",
                ]
            )


def build() -> YgoProDeckProfile:
    return YgoProDeckProfile()
