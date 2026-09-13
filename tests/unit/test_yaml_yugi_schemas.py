"""Parsing tolerante do dataset agregado do `yaml-yugi` (ADR 0012).

Casos reais confirmados ao vivo em 2026-09-12
(docs/proposta-fontes-dados-catalogo.md §3.1): "Vanquish Soul Razen" tem
nome/print reais em vários idiomas mas nenhum print `es`; algumas cartas
(ex. "Fiendsmith Requiem") têm o nome inglês sinalizado como tradução
não-oficial do japonês.
"""

from __future__ import annotations

from yugioh_scanner.catalog_sources.yaml_yugi.schemas import YamlYugiCard


class TestYamlYugiCardParsing:
    def test_parses_name_and_sets_per_language(self) -> None:
        payload = {
            "password": 29302858,
            "name": {"en": "Vanquish Soul Razen", "de": "Bezwingerseele Razen", "es": "Derrota Almas Razen"},
            "sets": {
                "en": [{"set_number": "WISU-EN016", "set_name": "Wild Survivors", "rarities": ["Ultra Rare"]}],
                "de": [{"set_number": "WISU-DE016", "set_name": "Wild Survivors", "rarities": ["Ultra Rare"]}],
            },
        }
        card = YamlYugiCard.model_validate(payload)

        assert card.password == 29302858
        assert card.name["de"] == "Bezwingerseele Razen"
        assert "es" not in card.sets  # confirmado ao vivo: 0 prints `es` no dataset inteiro
        assert card.sets["de"][0].set_number == "WISU-DE016"
        assert card.sets["de"][0].rarities == ["Ultra Rare"]

    def test_missing_password_is_none_not_an_error(self) -> None:
        """Carta sem `password` (ex. prerelease) não deve quebrar o parser —
        o importador é quem decide ignorá-la (não tem `Card` correspondente)."""
        card = YamlYugiCard.model_validate({"name": {"en": "Virtue Stream"}})
        assert card.password is None

    def test_unofficial_translation_flag(self) -> None:
        """Caso real medido: nome inglês de carta ainda não lançada em TCG,
        marcado pelo próprio Yugipedia como tradução de trabalho."""
        payload = {
            "password": 2463794,
            "name": {"en": "Fiendsmith Requiem", "ja": "..."},
            "is_translation_unofficial": {"name": {"en": True}, "text": {"en": True}},
        }
        card = YamlYugiCard.model_validate(payload)

        assert card.is_name_unofficial("en") is True
        assert card.is_name_unofficial("ja") is False
        assert card.is_name_unofficial("pt") is False  # idioma nem presente no flag

    def test_no_unofficial_flag_defaults_to_official(self) -> None:
        card = YamlYugiCard.model_validate({"password": 1, "name": {"en": "X"}})
        assert card.is_name_unofficial("en") is False

    def test_rarities_null_is_treated_as_empty(self) -> None:
        """Achado real ao validar contra o dataset ao vivo (não uma
        suposição): alguns prints (ex. `sets.ja` de certas cartas) trazem
        `"rarities": null` em vez de ausente ou `[]` — o parser tolerante
        precisa sobreviver a isso, mesmo princípio de `ygoprodeck/schemas.py`
        para campos que a fonte às vezes omite ou zera."""
        payload = {
            "password": 1,
            "sets": {"ja": [{"set_number": "ABC-JP001", "set_name": "X", "rarities": None}]},
        }
        card = YamlYugiCard.model_validate(payload)
        assert card.sets["ja"][0].rarities == []

    def test_unknown_fields_are_ignored(self) -> None:
        """O dataset pode ganhar campos novos sem quebrar o parser (mesmo
        princípio tolerante de `ygoprodeck/schemas.py`)."""
        card = YamlYugiCard.model_validate(
            {"password": 1, "name": {"en": "X"}, "konami_id": 999, "limit_regulation": {"tcg": "Unlimited"}}
        )
        assert card.password == 1
