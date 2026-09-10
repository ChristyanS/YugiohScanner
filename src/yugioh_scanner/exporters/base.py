"""Contrato de exportação (plano §15.1).

`CollectionRow` desacopla os perfis do ORM: quem monta as linhas é
`services/export_service.py`, e cada perfil só formata texto a partir de
campos simples. Adicionar um formato novo é uma classe nova aqui + uma
entrada no registry — nenhuma outra camada muda.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, TextIO


@dataclass(frozen=True, slots=True)
class CollectionRow:
    """Uma linha da coleção, já achatada — sem relacionamentos do ORM."""

    item_id: int
    card_id: int
    card_name: str
    quantity: int
    condition: str
    edition: str
    language: str
    notes: str | None
    source: str
    added_at: str  # ISO 8601

    #: `None` quando o item não tem set identificado (plano §4.4).
    card_print_id: int | None = None
    set_code_full: str | None = None
    set_prefix: str | None = None
    region: str | None = None
    set_name: str | None = None
    rarity: str | None = None

    @property
    def has_print(self) -> bool:
        return self.card_print_id is not None


class ExportProfile(Protocol):
    """Um formato de exportação. `render` escreve texto pronto em `out`."""

    #: Identificador usado em `export --profile NOME`.
    name: str
    #: `csv` ou `txt` — decide como o serviço abre o arquivo (§ abaixo).
    format: str
    #: Cabeçalho de colunas, só para documentação/`--list-profiles` (perfis
    #: de texto livre, como `txt`, deixam vazio).
    columns: list[str]
    #: Encoding do arquivo. `ygopocket` precisa de BOM (`utf-8-sig`) para
    #: bater byte a byte com o export real; os demais usam `utf-8` puro.
    encoding: str
    #: Terminador de linha. `\r\n` reproduz o export real do YGOPocket;
    #: `\n` é o padrão dos outros perfis (nada a mais para o Git perturbar).
    newline: str

    def render(self, rows: Iterable[CollectionRow], out: TextIO) -> None: ...


#: Caracteres que abrem fórmula em Excel/LibreOffice/Sheets ao importar CSV.
#: Um `notes` livre digitado pelo usuário é o único campo desta aplicação que
#: pode conter isso por acidente (plano §21) — nomes de carta não começam
#: com nenhum deles.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_cell(value: str) -> str:
    """Neutraliza CSV injection: prefixa `'` se a célula abrir fórmula.

    >>> sanitize_csv_cell("=cmd|'/c calc'!A1")
    "'=cmd|'/c calc'!A1"
    >>> sanitize_csv_cell("Blue-Eyes White Dragon")
    'Blue-Eyes White Dragon'
    >>> sanitize_csv_cell("")
    ''
    """
    if value and value.startswith(_FORMULA_TRIGGERS):
        return f"'{value}"
    return value


#: Sujeira típica de export de outra aplicação: linhas em branco extras,
#: espaços nas pontas. Usado pelos importadores (round-trip do perfil full).
_BLANK_LINE = re.compile(r"^\s*$")


def is_blank(line: str) -> bool:
    return bool(_BLANK_LINE.match(line))
