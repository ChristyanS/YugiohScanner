"""`YamlYugiEnrichmentImporter` (ADR 0012, Opção B).

Usa o schema migrado vazio (`session`), com linhas construídas à mão — mesmo
estilo de `test_print_language.py`: o cenário real (enriquecimento sobre um
catálogo já sincronizado da YGOPRODeck) não precisa do catálogo completo das
fixtures, só de um `Card` existente para casar por `password`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from yugioh_scanner.catalog_sources.yaml_yugi.importer import YamlYugiEnrichmentImporter
from yugioh_scanner.catalog_sources.yaml_yugi.schemas import YamlYugiCard
from yugioh_scanner.db.tables import (
    DATA_SOURCE_YAML_YUGI,
    DATA_SOURCE_YGOPRODECK,
    Card,
    CardAltName,
    CardPrint,
    CardSet,
)

RAZEN_ID = 29302858


def _make_razen(session: Session) -> None:
    session.add(
        Card(
            id=RAZEN_ID,
            name="Vanquish Soul Razen",
            name_normalized="vanquish soul razen",
            type="Effect Monster",
            desc="",
        )
    )
    session.add(CardSet(set_code="WISU", set_name="Wild Survivors"))
    session.flush()


def _razen_card(**overrides: object) -> YamlYugiCard:
    payload: dict[str, object] = {
        "password": RAZEN_ID,
        "name": {
            "en": "Vanquish Soul Razen",
            "de": "Bezwingerseele Razen",
            "fr": "Âme du Vainqueur Razen",
            "it": "Sgomina Anima Razen",
            "pt": "Alma Aniquiladora Razen",
            "es": "Derrota Almas Razen",
            "ja": "...",
            "ko": "...",
        },
        "sets": {
            "en": [{"set_number": "WISU-EN016", "set_name": "Wild Survivors", "rarities": ["Ultra Rare"]}],
            "de": [{"set_number": "WISU-DE016", "set_name": "Wild Survivors", "rarities": ["Ultra Rare"]}],
            "ja": [
                {
                    "set_number": "DBWS-JP016",
                    "set_name": "Deck Build Pack: Wild Survivors",
                    "rarities": ["Super Rare"],
                }
            ],
        },
    }
    payload.update(overrides)
    return YamlYugiCard.model_validate(payload)


class TestCardMatching:
    def test_ignores_cards_without_a_matching_card_row(self, session: Session) -> None:
        """Regra inegociável (ADR 0012): nunca cria `Card` nova."""
        importer = YamlYugiEnrichmentImporter(session)
        stats = importer.import_cards([_razen_card()])

        assert stats.cards_unmatched == 1
        assert stats.cards_matched == 0
        assert session.query(Card).count() == 0


class TestAltNameEnrichment:
    def test_inserts_alt_names_for_target_languages(self, session: Session) -> None:
        _make_razen(session)
        importer = YamlYugiEnrichmentImporter(session)

        stats = importer.import_cards([_razen_card()])
        session.flush()

        names = {row.language: row for row in session.scalars(select(CardAltName))}
        assert names["DE"].name == "Bezwingerseele Razen"
        assert names["DE"].data_source == DATA_SOURCE_YAML_YUGI
        assert stats.alt_names_inserted == len(names)  # DE/FR/IT/PT/JA/KO = 6

    def test_never_imports_spanish(self, session: Session) -> None:
        """Achado validado (proposta §3.1.3): 98% das cartas têm nome `es`
        sem nenhum print real por trás, e o Yugipedia nunca sinaliza isso
        como não-oficial — por isso `es` fica fora do mapa de idiomas."""
        _make_razen(session)
        importer = YamlYugiEnrichmentImporter(session)

        importer.import_cards([_razen_card()])
        session.flush()

        languages = set(session.scalars(select(CardAltName.language)))
        assert "ES" not in languages

    def test_never_overwrites_a_ygoprodeck_row(self, session: Session) -> None:
        _make_razen(session)
        session.add(
            CardAltName(
                card_id=RAZEN_ID,
                language="DE",
                name="Nome Oficial YGOPRODeck",
                name_normalized="nome oficial ygoprodeck",
                data_source=DATA_SOURCE_YGOPRODECK,
            )
        )
        session.flush()

        importer = YamlYugiEnrichmentImporter(session)
        stats = importer.import_cards([_razen_card()])
        session.flush()

        de_row = session.scalar(
            select(CardAltName).where(CardAltName.card_id == RAZEN_ID, CardAltName.language == "DE")
        )
        assert de_row is not None
        assert de_row.name == "Nome Oficial YGOPRODeck"
        assert de_row.data_source == DATA_SOURCE_YGOPRODECK
        # DE foi pulado por já existir; os outros 5 idiomas ainda entram.
        assert stats.alt_names_inserted == 5

    def test_skips_names_flagged_as_unofficial_translation(self, session: Session) -> None:
        _make_razen(session)
        importer = YamlYugiEnrichmentImporter(session)

        card = _razen_card(is_translation_unofficial={"name": {"ja": True}})
        stats = importer.import_cards([card])
        session.flush()

        languages = set(session.scalars(select(CardAltName.language)))
        assert "JA" not in languages
        assert stats.alt_names_skipped_unofficial == 1

    def test_re_running_updates_the_yaml_yugi_row_idempotently(self, session: Session) -> None:
        _make_razen(session)
        importer = YamlYugiEnrichmentImporter(session)
        importer.import_cards([_razen_card()])
        session.flush()

        updated_card = _razen_card(name={"en": "Vanquish Soul Razen", "de": "Nome Atualizado"})
        stats = importer.import_cards([updated_card])
        session.flush()

        de_row = session.scalar(
            select(CardAltName).where(CardAltName.card_id == RAZEN_ID, CardAltName.language == "DE")
        )
        assert de_row is not None
        assert de_row.name == "Nome Atualizado"
        assert stats.alt_names_updated >= 1
        assert stats.alt_names_inserted == 0


class TestPrintEnrichment:
    def test_creates_a_new_card_set_for_an_unknown_ocg_prefix(self, session: Session) -> None:
        """`DBWS` (Deck Build Pack: Wild Survivors) não existe na YGOPRODeck
        — o enriquecimento precisa criar o `CardSet`, não só o `CardPrint`."""
        _make_razen(session)
        importer = YamlYugiEnrichmentImporter(session)

        stats = importer.import_cards([_razen_card()])
        session.flush()

        dbws = session.get(CardSet, "DBWS")
        assert dbws is not None
        assert dbws.set_name == "Deck Build Pack: Wild Survivors"
        assert stats.sets_inserted == 1

    def test_creates_the_print_with_correct_region_and_source(self, session: Session) -> None:
        _make_razen(session)
        importer = YamlYugiEnrichmentImporter(session)
        importer.import_cards([_razen_card()])
        session.flush()

        jp_print = session.scalar(
            select(CardPrint).where(CardPrint.set_code_full == "DBWS-JP016")
        )
        assert jp_print is not None
        assert jp_print.region == "JA"
        assert jp_print.rarity == "Super Rare"
        assert jp_print.data_source == DATA_SOURCE_YAML_YUGI
        assert jp_print.set_prefix == "DBWS"

    def test_never_overwrites_a_ygoprodeck_print(self, session: Session) -> None:
        _make_razen(session)
        session.add(
            CardPrint(
                card_id=RAZEN_ID,
                set_code_full="WISU-DE016",
                set_code_normalized="WISU-DE016",
                set_prefix="WISU",
                set_name="Wild Survivors (dado oficial)",
                rarity="Ultra Rare",
                region="DE",
                number="016",
                data_source=DATA_SOURCE_YGOPRODECK,
            )
        )
        session.flush()

        importer = YamlYugiEnrichmentImporter(session)
        stats = importer.import_cards([_razen_card()])
        session.flush()

        de_print = session.scalar(
            select(CardPrint).where(CardPrint.set_code_full == "WISU-DE016")
        )
        assert de_print is not None
        assert de_print.set_name == "Wild Survivors (dado oficial)"
        assert de_print.data_source == DATA_SOURCE_YGOPRODECK
        assert stats.prints_skipped_primary == 1

    def test_reuses_an_existing_set_prefix_without_touching_it(self, session: Session) -> None:
        """`WISU` já existe (veio da YGOPRODeck, versão inglesa) — o
        enriquecimento não deve criar um segundo `CardSet` nem alterá-lo."""
        _make_razen(session)
        importer = YamlYugiEnrichmentImporter(session)

        stats = importer.import_cards([_razen_card()])
        session.flush()

        wisu = session.get(CardSet, "WISU")
        assert wisu is not None
        assert wisu.set_name == "Wild Survivors"
        # Só DBWS é novo; WISU já existia.
        assert stats.sets_inserted == 1
