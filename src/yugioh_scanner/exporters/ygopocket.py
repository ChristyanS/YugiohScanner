"""Perfis `ygopocket` — CSV e TXT, confirmados contra exports reais (plano §15.5).

Mapeamento verificado byte a byte contra os dois arquivos que o usuário
exportou do próprio app (`export-samples/`, docs/PLAN.md §0.4). Dois formatos
distintos, não um só: o CSV carrega uma linha por combinação (carta, print,
condição, ...) com metadados completos; o TXT é `<quantidade> <nome>`, uma
linha por *tipo* de carta, sem cabeçalho — mais perto do estilo "deck list"
que do CSV.

**O que não está confirmado:** só existia uma carta `NM` na amostra real, então
o resto do vocabulário de `condition` (`LP`/`MP`/`HP`/`DMG`) é assumido por ser
convenção quase universal do hobby (TCGplayer, Cardmarket, ...) — não
verificado contra o app. Documentado aqui e em §0.4/§15.5 do plano; se algum
dia divergir, é uma linha para corrigir, não uma reescrita.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from typing import TextIO

from .base import CollectionRow, sanitize_csv_cell

#: O perfil TXT não tem cabeçalho de colunas (usado só por `YgoPocketTxtProfile`).
_TXT_NO_COLUMNS: list[str] = []

#: Ordem exata das 26 colunas do export real. Não inventar, não reordenar.
COLUMNS = [
    "card_id",
    "card_name",
    "quantity",
    "set_code",
    "rarity",
    "art_variant",
    "variant_label",
    "condition",
    "language",
    "printing_region",
    "notes",
    "storage_location",
    "grading_company",
    "grade",
    "certification_number",
    "subgrade_centering",
    "subgrade_corners",
    "subgrade_edges",
    "subgrade_surface",
    "sealed",
    "signed",
    "altered",
    "source",
    "edition",
    "purchase_price",
    "market_value_override",
]

#: Só `Near Mint` → `NM` veio confirmado no export real; o resto é a
#: convenção universal do hobby (ver docstring do módulo).
_CONDITION_ABBREVIATIONS = {
    "Near Mint": "NM",
    "Lightly Played": "LP",
    "Moderately Played": "MP",
    "Heavily Played": "HP",
    "Damaged": "DMG",
}


def _condition(value: str) -> str:
    return _CONDITION_ABBREVIATIONS.get(value, value)


def _edition(value: str) -> str:
    # A única amostra real tinha o campo vazio para uma carta que não era
    # 1st Edition — não sabemos como o app grava "Unlimited"/"Limited" por
    # extenso, então só emitimos o valor que de fato vimos.
    return "1st Edition" if value == "1st Edition" else ""


class YgoPocketCsvProfile:
    name = "ygopocket"
    format = "csv"
    columns = COLUMNS
    #: BOM + CRLF: bate byte a byte com o export real do app.
    encoding = "utf-8-sig"
    newline = "\r\n"

    def render(self, rows: Iterable[CollectionRow], out: TextIO) -> None:
        writer = csv.writer(out, lineterminator=self.newline)
        writer.writerow(COLUMNS)
        for row in rows:
            language = row.language or "EN"
            writer.writerow(
                [
                    row.card_id,
                    sanitize_csv_cell(row.card_name),
                    row.quantity,
                    row.set_code_full or "",
                    sanitize_csv_cell(row.rarity or ""),
                    "",  # art_variant — sem equivalente
                    "",  # variant_label — sem equivalente
                    _condition(row.condition),
                    language.upper(),
                    language.lower(),
                    sanitize_csv_cell(row.notes or ""),
                    "",  # storage_location
                    "",  # grading_company
                    "",  # grade
                    "",  # certification_number
                    "",  # subgrade_centering
                    "",  # subgrade_corners
                    "",  # subgrade_edges
                    "",  # subgrade_surface
                    "false",  # sealed — não rastreamos
                    "false",  # signed
                    "false",  # altered
                    "",  # source (o campo deles, não o nosso `source` scan/manual)
                    _edition(row.edition),
                    "",  # purchase_price
                    "",  # market_value_override
                ]
            )


class YgoPocketTxtProfile:
    """`<quantidade> <nome>`, uma linha por tipo de carta — formato real do app.

    Diferente do perfil genérico `txt --profile deck` (uma linha por CÓPIA,
    sem quantidade): este é o que o YGOPocket de fato produz, confirmado
    contra a amostra real (`1 Flame Administrator`).
    """

    name = "ygopocket"
    format = "txt"
    #: Sem cabeçalho de colunas — referência, não literal, para não acionar o
    #: RUF012 de mutável default sem precisar de `ClassVar` (que quebraria a
    #: conformidade estrutural com `ExportProfile.columns: list[str]`).
    columns = _TXT_NO_COLUMNS
    encoding = "utf-8"
    newline = "\n"

    def render(self, rows: Iterable[CollectionRow], out: TextIO) -> None:
        lines = [f"{row.quantity} {row.card_name}" for row in rows]
        out.write("\n".join(lines))


def build_csv() -> YgoPocketCsvProfile:
    return YgoPocketCsvProfile()


def build_txt() -> YgoPocketTxtProfile:
    return YgoPocketTxtProfile()
