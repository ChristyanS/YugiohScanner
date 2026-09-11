"""Servidor falso da YGOPRODeck, alimentado pelas fixtures gravadas.

Fica fora de `tests/integration/` porque o `conftest` da raiz também precisa
dele para montar o catálogo compartilhado — importar de dentro de um módulo de
teste criaria dependência entre suítes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import respx

FIXTURES = Path(__file__).parent / "fixtures" / "api"
BASE_URL = "https://db.ygoprodeck.com/api/v7"


def load(name: str) -> Any:
    """Carrega uma resposta gravada da API real."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


#: Idiomas alternativos com fixture gravada (plano §7.1 multilíngue). Um
#: idioma sem fixture aqui devolve `{"data": []}` — igual a uma tradução que a
#: API não tem, e é exatamente o que a maioria dos idiomas faz na fixture: só
#: PT tem cartas traduzidas, propositalmente, para exercitar o caminho.
_ALT_LANGUAGE_PAGES = {"pt": {0: "cards_page1_pt"}}


def install_routes(router: respx.MockRouter, *, page_size: int = 3) -> respx.MockRouter:
    """Registra os endpoints usados pelo sync, com paginação real."""
    router.get("/checkDBVer.php").mock(return_value=httpx.Response(200, json=load("checkdbver")))
    router.get("/cardsets.php").mock(return_value=httpx.Response(200, json=load("cardsets")))

    pages = {0: load("cards_page1"), page_size: load("cards_page2")}
    alt_pages = {
        language: {offset: load(name) for offset, name in offsets.items()}
        for language, offsets in _ALT_LANGUAGE_PAGES.items()
    }

    def card_page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        language = request.url.params.get("language")
        if language:
            return httpx.Response(200, json=alt_pages.get(language, {}).get(offset, {"data": []}))
        return httpx.Response(200, json=pages.get(offset, {"data": []}))

    router.get("/cardinfo.php").mock(side_effect=card_page)
    return router


@contextmanager
def mock_api(*, page_size: int = 3) -> Iterator[respx.MockRouter]:
    """Contexto com a API inteira mockada."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield install_routes(router, page_size=page_size)
