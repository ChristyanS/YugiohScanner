"""Catálogo de textos da UI (chrome — pt-BR/en, plano de idioma global).

Escopo deliberadamente pequeno: só 2 idiomas. Cobre tanto texto renderizado
pelo Jinja (`translate()`) quanto strings usadas em `<script>` inline
(`catalog_json()`, consumida pela ponte `window.I18N`/`t()` em `base.html`)
— mensagens de erro geradas no Python (`errors.py`) ficam de fora, essas são
compartilhadas com a CLI e traduzi-las é um esforço à parte.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

_I18N_DIR = Path(__file__).resolve().parent

#: Nome de arquivo por idioma da UI (`db.tables.UI_LANGUAGES`).
_CATALOG_FILES = {"pt-BR": "pt_BR.json", "en": "en.json"}


@cache
def _load_catalog(lang: str) -> dict[str, str]:
    filename = _CATALOG_FILES.get(lang)
    if filename is None:
        return {}
    with (_I18N_DIR / filename).open(encoding="utf-8") as f:
        data: dict[str, str] = json.load(f)
    return data


def translate(lang: str, key: str) -> str:
    """Texto de `key` no idioma `lang`, ou a própria chave se faltar —
    nunca quebra o render de uma página por causa de uma string ausente."""
    return _load_catalog(lang).get(key, key)


@cache
def catalog_json(lang: str) -> str:
    """JSON do catálogo completo de `lang`, pronto para embutir num
    `<script>` inline — a ponte cliente (`window.I18N`/`t()` em
    `base.html`) espelha o `t()` do Jinja para strings que só existem
    dentro de `<script>`. `</` vira `<\\/` só por precaução: nenhuma
    string do catálogo deve conseguir fechar a tag `<script>` ao redor.
    """
    return json.dumps(_load_catalog(lang), ensure_ascii=False).replace("</", "<\\/")
