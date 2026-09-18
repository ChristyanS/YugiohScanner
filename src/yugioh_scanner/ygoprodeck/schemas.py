"""Schemas Pydantic das respostas da YGOPRODeck (plano §0.1).

Regra que governa este arquivo: **a API omite campos vazios**
(*"If a piece of response info is empty or null then it will NOT show up"*).
Portanto quase tudo é opcional, e o parser precisa sobreviver a uma carta que
não tem `atk`, nem `card_sets`, nem `misc_info`.

Os validadores convertem os tipos que a API entrega como string (`set_price`
vem como `"74.49"`, datas como `"2002-03-08"` ou `""`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiModel(BaseModel):
    """Base tolerante: campos novos da API não quebram o parsing."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)


def _empty_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class ApiCardSet(ApiModel):
    """Uma entrada de `card_sets[]`.

    Atenção: aqui `set_code` é o **código completo do print** (`CT13-EN008`),
    não o prefixo do set — o duplo sentido documentado no plano §0.3.1.
    """

    set_name: str
    set_code: str
    set_rarity: str | None = None
    set_rarity_code: str | None = None
    set_price: float | None = None

    @field_validator("set_rarity", "set_rarity_code", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return _empty_to_none(value)

    @field_validator("set_price", mode="before")
    @classmethod
    def _price_to_float(cls, value: Any) -> Any:
        value = _empty_to_none(value)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class ApiCardImage(ApiModel):
    """Uma entrada de `card_images[]` — uma arte da carta."""

    id: int
    image_url: str
    image_url_small: str | None = None
    image_url_cropped: str | None = None


class ApiMiscInfo(ApiModel):
    """Conteúdo de `misc_info[]` (só vem com `misc=yes`)."""

    tcg_date: dt.date | None = None
    ocg_date: dt.date | None = None
    konami_id: int | None = None
    has_effect: int | None = None
    beta_name: str | None = None
    md_rarity: str | None = None
    treated_as: str | None = None

    @field_validator("tcg_date", "ocg_date", mode="before")
    @classmethod
    def _blank_date(cls, value: Any) -> Any:
        return _empty_to_none(value)


class ApiBanlistInfo(ApiModel):
    """Conteúdo de `banlist_info` — status na banlist TCG/OCG/Goat (Deck
    Builder: `Forbidden`/`Limited`/`Semi-Limited`, ausente = sem restrição).

    Confirmado ao vivo em `cardinfo.php?name=Pot%20of%20Greed` (2026-09-14):
    o campo existe mesmo sem pedir nada especial na consulta. `ban_goat` é
    capturado mas intencionalmente não usado (fora de escopo do Deck Builder).
    """

    ban_tcg: str | None = None
    ban_ocg: str | None = None
    ban_goat: str | None = None


class ApiCard(ApiModel):
    """Uma carta de `cardinfo.php`."""

    id: int
    name: str
    type: str
    frame_type: str | None = Field(default=None, alias="frameType")
    human_readable_type: str | None = Field(default=None, alias="humanReadableCardType")
    desc: str = ""
    banlist_info: ApiBanlistInfo | None = None

    atk: int | None = None
    # `def` é palavra reservada em Python; o alias mantém o nome da API.
    defense: int | None = Field(default=None, alias="def")
    level: int | None = None
    attribute: str | None = None
    race: str | None = None
    archetype: str | None = None
    scale: int | None = None
    linkval: int | None = None

    typeline: list[str] | None = None
    linkmarkers: list[str] | None = None
    ygoprodeck_url: str | None = None
    #: Só vem preenchido quando a consulta pediu `language=...`: o nome em
    #: inglês, de graça, junto do nome traduzido. Usado como conferência de
    #: integridade da tradução (docs/proposta-i18n-cartas-e-sets.md §1.2).
    name_en: str | None = None

    card_sets: list[ApiCardSet] = Field(default_factory=list)
    card_images: list[ApiCardImage] = Field(default_factory=list)
    misc_info: list[ApiMiscInfo] = Field(default_factory=list)

    @property
    def misc(self) -> ApiMiscInfo | None:
        """`misc_info` vem como lista de um elemento — ou não vem."""
        return self.misc_info[0] if self.misc_info else None


class ApiPageMeta(ApiModel):
    """`meta` da resposta paginada de `cardinfo.php`."""

    current_rows: int = 0
    total_rows: int = 0
    rows_remaining: int = 0
    total_pages: int = 0
    pages_remaining: int = 0
    next_page_offset: int | None = None
    generated: str | None = None


class ApiCardPage(ApiModel):
    """Resposta de `cardinfo.php`. `meta` só existe quando paginado."""

    data: list[ApiCard] = Field(default_factory=list)
    meta: ApiPageMeta | None = None


class ApiSet(ApiModel):
    """Uma entrada de `cardsets.php`.

    Aqui `set_code` é o **prefixo** (`LOB`, `MP24`) — o outro lado do duplo
    sentido descrito em `ApiCardSet`.
    """

    set_name: str
    set_code: str
    num_of_cards: int | None = None
    tcg_date: dt.date | None = None
    set_image: str | None = None

    @field_validator("tcg_date", mode="before")
    @classmethod
    def _blank_date(cls, value: Any) -> Any:
        return _empty_to_none(value)


class ApiDbVersion(ApiModel):
    """Resposta de `checkDBVer.php`.

    O guia da API diz `date`, mas a resposta real traz `last_update` (verificado
    em 2026-09-09). Aceitamos os dois para não quebrar se eles mudarem.
    """

    database_version: str
    last_update: str | None = Field(default=None, validation_alias="last_update")
    date: str | None = None

    @property
    def updated_at(self) -> str | None:
        return self.last_update or self.date
