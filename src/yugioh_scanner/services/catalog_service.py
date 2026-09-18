"""Navegação do catálogo (Fase 8: telas `/cards/{id}` e busca da Web).

Fino de propósito: a única regra é "carta inexistente vira erro do domínio"
— o resto é leitura direta. Fica ao lado de `collection_service.py` porque as
duas telas (coleção e catálogo) frequentemente precisam das duas juntas (uma
carta + quantas cópias você tem dela).

Fase 3 do faseamento web (multilíngue): além de casar nomes traduzidos com o
OCR, agora também **exibimos** nome e descrição no idioma escolhido, com
fallback silencioso para inglês quando a tradução não existe (sync rodado com
`--skip-alt-names`, ou carta nova ainda não sincronizada nesse idioma).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db.tables import ALT_NAME_LANGUAGES, Card, CardAltName, CardPrint, CardSet
from ..errors import CardNotFoundError, InvalidSetDataError, SetAlreadyExistsError
from ..repositories.cards import CardRepository
from ..repositories.sets import SetRepository

#: Idiomas que a tela de detalhe e as listagens podem exibir — inglês
#: (canônico, sempre disponível) + os mesmos idiomas que o sync baixa para
#: matching (plano §7.1). Não é um conjunto maior que isso porque a própria
#: API só traduz para estes quatro (verificado ao vivo em `cardinfo.php`).
DISPLAY_LANGUAGES = ("EN", *ALT_NAME_LANGUAGES)


#: Mapa idioma de exibição -> colação SQLite registrada em `db/engine.py`
#: (plano de idioma global). Só `PT` ganhou tratamento de acento dedicado
#: nesta primeira entrega; todo o resto cai na colação `EN` (case-insensitive
#: simples) em vez de deixar sem colação nenhuma.
_SORT_COLLATION_BY_LANGUAGE = {"PT": "PT_BR"}


def sort_collation_for_language(lang: str) -> str:
    return _SORT_COLLATION_BY_LANGUAGE.get(lang, "EN")


def resolve_display_language(raw: str | None, *, default: str = "EN") -> str:
    """Normaliza um valor vindo de query string para um idioma válido.

    Qualquer coisa fora de `DISPLAY_LANGUAGES` (ausente, minúsculo, idioma
    que a API não traduz) cai em `default` em vez de propagar erro — é um
    parâmetro de exibição, não uma entrada que mereça 400 se vier errada.
    `default` normalmente vem de `SettingsService.get_default_card_language()`
    (plano de idioma global) — quem chama sem passar nada continua caindo em
    `"EN"`, comportamento anterior preservado.
    """
    lang = (raw or default).upper()
    return lang if lang in DISPLAY_LANGUAGES else default


@dataclass(frozen=True, slots=True)
class LocalizedCard:
    """Nome/descrição de uma carta prontos para exibir num idioma."""

    name: str
    desc: str
    language: str
    is_translated: bool


class CatalogService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.cards = CardRepository(session)
        self.sets = SetRepository(session)

    def search_cards(
        self,
        query: str | None = None,
        *,
        set_prefix: str | None = None,
        limit: int = 50,
        offset: int = 0,
        locale: str = "EN",
        lang: str = "EN",
    ) -> list[Card]:
        return self.cards.search(
            query, set_prefix=set_prefix, limit=limit, offset=offset, locale=locale, lang=lang
        )

    def get_card(self, card_id: int) -> Card:
        card = self.cards.get(card_id)
        if card is None:
            raise CardNotFoundError(f"Carta #{card_id} não encontrada no catálogo.")
        return card

    def count_cards(self, query: str | None = None, *, set_prefix: str | None = None) -> int:
        return self.cards.count(query, set_prefix=set_prefix)

    def prints_for_card(self, card_id: int, *, query: str | None = None) -> list[CardPrint]:
        return self.cards.prints_for(card_id, query=query)

    def list_sets(self, *, limit: int = 1000, offset: int = 0) -> list[CardSet]:
        return self.sets.list_all(limit=limit, offset=offset)

    def register_set(
        self,
        set_code: str,
        set_name: str,
        *,
        num_of_cards: int | None = None,
        tcg_date: dt.date | None = None,
    ) -> CardSet:
        """Cadastro manual de um set que o sync ainda não trouxe (tela de
        Revisão: "Set não encontrado? Cadastrar manualmente").

        É CREATE, não upsert: um `set_code` já existente é erro, não
        atualização — um typo digitado à mão nunca deve sobrescrever dado
        que já veio do catálogo sincronizado.
        """
        code = set_code.strip().upper()
        name = set_name.strip()
        if not code or not name:
            raise InvalidSetDataError("Código e nome do set são obrigatórios.")
        if self.sets.get(code) is not None:
            raise SetAlreadyExistsError(code)
        return self.sets.create(code, name, num_of_cards=num_of_cards, tcg_date=tcg_date)

    # --------------------------------------------------------- multilíngue

    def alt_names_for_card(self, card_id: int) -> list[CardAltName]:
        """Todas as traduções conhecidas de uma carta (tela de detalhe)."""
        return self.cards.alt_names_for(card_id)

    def localize(
        self, card: Card, alt_names: Sequence[CardAltName], language: str
    ) -> LocalizedCard:
        """Nome/descrição no idioma pedido, com fallback para inglês.

        `language="EN"` sempre devolve o dado canônico. Para os demais,
        procura a linha de `card_alt_name` correspondente; se não existir,
        cai para inglês em vez de mostrar campo vazio — `is_translated=False`
        avisa o chamador de que o fallback aconteceu.
        """
        if language != "EN":
            for alt in alt_names:
                if alt.language == language:
                    return LocalizedCard(
                        name=alt.name,
                        desc=alt.desc or card.desc,
                        language=language,
                        is_translated=True,
                    )
        return LocalizedCard(name=card.name, desc=card.desc, language="EN", is_translated=False)

    def display_names(self, cards: Sequence[Card], language: str) -> dict[int, str]:
        """`{card_id: nome traduzido}` só para quem tem tradução nesse idioma.

        Usado pelas listagens (Fase 3: idioma de exibição) — o chamador cai
        para `card.name` quando a carta não está no dicionário devolvido.
        Devolve vazio para `language="EN"`: não há o que traduzir.
        """
        if language == "EN" or not cards:
            return {}
        alt_map = self.cards.alt_names_map([card.id for card in cards], language)
        return {card_id: alt.name for card_id, alt in alt_map.items()}
