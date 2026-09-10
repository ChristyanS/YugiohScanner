"""Registro de perfis de exportação (plano §15.1).

Mesmo padrão de `ocr/registry.py`: adicionar um perfil é uma fábrica nova
registrada aqui, sem tocar em `services/export_service.py` nem na CLI.

A chave é `(formato, nome)` porque dois formatos podem compartilhar nome —
`ygopocket` existe como CSV **e** como TXT, e são shapes de dado diferentes
(plano §15.5), não o mesmo perfil com extensão trocada.
"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import UnknownExportProfileError
from .base import ExportProfile
from .full import build as build_full
from .text import build_deck, build_list
from .ygopocket import build_csv as build_ygopocket_csv
from .ygopocket import build_txt as build_ygopocket_txt
from .ygoprodeck import build as build_ygoprodeck

_REGISTRY: dict[tuple[str, str], Callable[[], ExportProfile]] = {
    ("csv", "ygoprodeck"): build_ygoprodeck,
    ("csv", "ygopocket"): build_ygopocket_csv,
    ("csv", "full"): build_full,
    ("txt", "list"): build_list,
    ("txt", "deck"): build_deck,
    ("txt", "ygopocket"): build_ygopocket_txt,
}

#: Perfil padrão de cada formato, quando o usuário não escolhe um.
DEFAULT_PROFILE = {"csv": "ygoprodeck", "txt": "list"}


def known_profiles(fmt: str | None = None) -> list[tuple[str, str]]:
    """Todos os `(formato, nome)` conhecidos, opcionalmente filtrados."""
    keys = sorted(_REGISTRY)
    if fmt is None:
        return keys
    return [key for key in keys if key[0] == fmt]


def create_profile(fmt: str, name: str | None = None) -> ExportProfile:
    """Instancia um perfil por `(formato, nome)`.

    `name=None` usa o padrão daquele formato — é o que faz
    `export --format csv` sozinho funcionar sem exigir `--profile`.
    """
    resolved_name = name or DEFAULT_PROFILE.get(fmt, "")
    factory = _REGISTRY.get((fmt, resolved_name))
    if factory is None:
        available = [f"{f}:{n}" for f, n in known_profiles(fmt)]
        raise UnknownExportProfileError(f"{fmt}:{resolved_name}", available)
    return factory()
