"""Schemas Pydantic do dataset agregado do `yaml-yugi` (ADR 0012).

Formato confirmado ao vivo em 2026-09-12 (docs/proposta-fontes-dados-catalogo.md):
`aggregate/cards.json` é uma lista de ~13 mil objetos, um por carta, com nome
e prints por idioma (chave ISO minúscula: `en`, `de`, `es`, `fr`, `it`, `pt`,
`ja`, `ko`, `zh-TW`, `zh-CN`, mais `ja_romaji`/`ko_rr` que não são idiomas de
verdade). Tudo tolerante a campos ausentes: o mesmo princípio de
`ygoprodeck/schemas.py` — um fornecedor de terceiros nunca deve quebrar o
parser por um campo novo ou ausente.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class YamlYugiModel(BaseModel):
    """Base tolerante: campos novos do dataset não quebram o parsing."""

    model_config = ConfigDict(extra="ignore")


class YamlYugiPrint(YamlYugiModel):
    """Uma entrada de `sets.<idioma>[]` — um print real documentado no wiki."""

    set_number: str
    set_name: str
    rarities: list[str] = Field(default_factory=list)

    @field_validator("rarities", mode="before")
    @classmethod
    def _none_to_empty_list(cls, value: Any) -> Any:
        # Confirmado ao vivo contra o dataset real: alguns prints (ex.
        # `sets.ja` de certas cartas) trazem `"rarities": null` em vez de
        # ausente/`[]` — o mesmo princípio tolerante de `ygoprodeck/schemas.py`.
        return [] if value is None else value


class YamlYugiUnofficialFlags(YamlYugiModel):
    """`is_translation_unofficial` — sinalização formal de tradução de wiki,
    não de produto oficial (proposta §3.1). Só `name`/`text` importam aqui;
    `text` (lore) não é usado por este projeto.
    """

    name: dict[str, bool] = Field(default_factory=dict)
    text: dict[str, bool] = Field(default_factory=dict)


class YamlYugiCard(YamlYugiModel):
    """Uma carta do dataset agregado.

    `password` é o mesmo passcode que `Card.id` já usa (YGOPRODeck) —
    confirmado ao vivo (proposta §1.2): é a chave de junção entre as duas
    fontes, sem ambiguidade.
    """

    password: int | None = None
    name: dict[str, str | None] = Field(default_factory=dict)
    sets: dict[str, list[YamlYugiPrint]] = Field(default_factory=dict)
    is_translation_unofficial: YamlYugiUnofficialFlags | None = None

    def is_name_unofficial(self, language_lower: str) -> bool:
        if self.is_translation_unofficial is None:
            return False
        return self.is_translation_unofficial.name.get(language_lower, False)
