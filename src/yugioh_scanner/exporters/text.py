"""Perfis de texto simples (plano §15.4).

Dois estilos deliberadamente diferentes do TXT do YGOPocket
(`exporters/ygopocket.py`): este é o formato *nosso*, pensado para leitura
humana (`list`) ou para colar em ferramentas que só quebram carta por cópia
(`deck`) — nenhum dos dois tenta imitar um app externo.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TextIO

from .base import CollectionRow

#: Perfis de texto livre não têm cabeçalho de colunas (plano §15.1). Uma
#: constante compartilhada em vez de um `[]` por classe: satisfaz o `Protocol`
#: (atributo de instância, não `ClassVar`) sem acionar o RUF012 de mutável
#: default — é uma referência, não um literal, igual ao `columns = COLUMNS`
#: dos perfis CSV.
_NO_COLUMNS: list[str] = []


class TextListProfile:
    """`3x Nome [SET-CODE] (Raridade)` — uma linha por item, legível."""

    name = "list"
    format = "txt"
    columns = _NO_COLUMNS
    encoding = "utf-8"
    newline = "\n"

    def render(self, rows: Iterable[CollectionRow], out: TextIO) -> None:
        lines = [self._line(row) for row in rows]
        out.write("\n".join(lines))

    @staticmethod
    def _line(row: CollectionRow) -> str:
        location = f"[{row.set_code_full}]" if row.has_print else "[set desconhecido]"
        rarity = f" ({row.rarity})" if row.rarity else ""
        return f"{row.quantity}x {row.card_name} {location}{rarity}"


class DeckStyleProfile:
    """Uma linha por **cópia** (não por item), sem metadados — cola em
    qualquer lugar que só entenda "uma carta por linha"."""

    name = "deck"
    format = "txt"
    columns = _NO_COLUMNS
    encoding = "utf-8"
    newline = "\n"

    def render(self, rows: Iterable[CollectionRow], out: TextIO) -> None:
        lines = [row.card_name for row in rows for _ in range(row.quantity)]
        out.write("\n".join(lines))


def build_list() -> TextListProfile:
    return TextListProfile()


def build_deck() -> DeckStyleProfile:
    return DeckStyleProfile()
