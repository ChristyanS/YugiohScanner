"""Matching contra o catálogo local (Fase 4).

Usa o mesmo catálogo de fixtures do sync: cartas e prints reais, incluindo os
casos difíceis (mesma carta em duas raridades, prefixo desconhecido, código não
parseável).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from yugioh_scanner.config import Settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.domain.confidence import Decision
from yugioh_scanner.matching.candidates import CandidateFinder, NameIndex
from yugioh_scanner.matching.engine import MatchingEngine
from yugioh_scanner.matching.resolver import PrintResolver

BLUE_EYES = 89631139
DARK_MAGICIAN = 46986414
POT_OF_GREED = 55144522


@pytest.fixture
def matcher(catalog: Database, settings: Settings) -> Iterator[MatchingEngine]:
    """Nome deliberadamente diferente de `engine`: aquele já é o Engine do
    SQLAlchemy no conftest, e sombrear geraria dependência recursiva."""
    with catalog.session() as session:
        yield MatchingEngine(session, settings, index=NameIndex())


class TestExactMatching:
    def test_perfect_name_matches_at_tier_zero(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Blue-Eyes White Dragon")
        assert result.card_id == BLUE_EYES
        assert result.tier == 0
        assert result.decision == Decision.AUTO
        assert result.matched_language == "EN"

    def test_case_and_spacing_do_not_matter(self, matcher: MatchingEngine) -> None:
        assert matcher.match("  BLUE-EYES   WHITE DRAGON ").card_id == BLUE_EYES


class TestOcrCorruptedNames:
    """Critério de aceitação: nome corrompido resolve para a carta certa."""

    @pytest.mark.parametrize(
        "ocr_text",
        [
            "BLUE-EYES WH1TE DRAGON",
            "B1ue-Eyes White Dragon",
            "BLUE-EYES WHITE DRAGC",  # truncamento real medido na Fase 3
            "BLUEEYES WHITE DRAGON",
            "BLUE-EYES WHITE DRAG0N",
        ],
    )
    def test_resolves_to_the_right_card(self, matcher: MatchingEngine, ocr_text: str) -> None:
        result = matcher.match(ocr_text)
        assert result.card_id == BLUE_EYES, f"{ocr_text!r} -> {result.card_name!r}"

    def test_partial_name_still_matches(self, matcher: MatchingEngine) -> None:
        assert matcher.match("DARK MAGICIA").card_id == DARK_MAGICIAN

    def test_gibberish_matches_nothing(self, matcher: MatchingEngine) -> None:
        result = matcher.match("XKCD QWERTY ZZZZ")
        assert result.card_id is None
        assert result.decision == Decision.UNMATCHED

    def test_empty_reading_is_unmatched(self, matcher: MatchingEngine) -> None:
        assert matcher.match("").decision == Decision.UNMATCHED


class TestSetCodeResolution:
    def test_valid_code_identifies_the_card(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Dark Magician", "SDK-001")
        assert result.card_id == DARK_MAGICIAN
        assert result.card_print_id is not None
        assert result.decision == Decision.AUTO

    @pytest.mark.parametrize("corrupted", ["SDK-OO1", "SDK-0O1", "5DK-001", "SDK001"])
    def test_corrupted_code_is_corrected(self, matcher: MatchingEngine, corrupted: str) -> None:
        """Critério de aceitação: `LOB-O01` precisa virar `LOB-001`."""
        result = matcher.match("Dark Magician", corrupted)
        assert result.set_code == "SDK-001", f"{corrupted!r} -> {result.set_code!r}"
        assert result.code_score == 1.0

    def test_invented_code_is_never_accepted(self, matcher: MatchingEngine) -> None:
        """Critério de aceitação: código inválido vira NULL, nunca invenção."""
        result = matcher.match("Dark Magician", "ZZZZ-EN999")
        assert result.set_code is None
        assert result.card_print_id is None
        assert result.card_id == DARK_MAGICIAN, "a carta continua identificada pelo nome"

    def test_unreadable_code_does_not_block_the_card(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Pot of Greed", "|||???")
        assert result.card_id == POT_OF_GREED
        assert result.card_print_id is None

    def test_code_alone_identifies_when_the_name_fails(self, matcher: MatchingEngine) -> None:
        result = matcher.match("", "CT13-EN008")
        assert result.card_id == BLUE_EYES
        assert result.decision == Decision.MANUAL, "sem o nome, sempre confirma"


class TestAmbiguousRarity:
    """`LOB-001` existe em Ultra e Secret Rare na fixture (caso real da API)."""

    def test_card_is_identified_but_print_is_not(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Blue-Eyes White Dragon", "LOB-001")
        assert result.card_id == BLUE_EYES
        assert result.card_print_id is None, "escolher uma raridade seria inventar dado"

    def test_the_options_are_offered_for_review(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Blue-Eyes White Dragon", "LOB-001")
        rarities = {option["rarity"] for option in result.print_options}
        assert rarities == {"Ultra Rare", "Secret Rare"}

    def test_unambiguous_code_sets_the_print(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Blue-Eyes White Dragon", "CT13-EN008")
        assert result.card_print_id is not None
        assert not result.print_options


class TestConflict:
    def test_name_and_code_pointing_elsewhere_never_automates(
        self, matcher: MatchingEngine
    ) -> None:
        """Critério de aceitação da Fase 4."""
        result = matcher.match("Blue-Eyes White Dragon", "SDK-001")
        assert result.decision == Decision.MANUAL
        assert "outra carta" in result.reason

    def test_conflict_keeps_the_name_card_as_first_option(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Blue-Eyes White Dragon", "SDK-001")
        assert result.card_id == BLUE_EYES
        assert result.card_print_id is None, "nada é gravado com o print do conflito"


class TestCrossConfirmation:
    """O set code confirma o nome mesmo sem concordar com o 1º candidato.

    Caso real do catálogo completo: o OCR lê `RED-EYES B DRAGON`, a
    similaridade textual põe "Red-Eyes Baby Dragon" na frente de "Red-Eyes
    Black Dragon", e o código `LOB-070` aponta para a segunda. Aqui a fixture
    reproduz a mesma forma com as cartas disponíveis.
    """

    def test_code_promotes_a_lower_ranked_candidate(self, matcher: MatchingEngine) -> None:
        # "POT OF GREE" casa melhor com Pot of Greed, mas o código aponta para
        # Dark Magician — que não está entre os candidatos: isso é conflito.
        conflito = matcher.match("Pot of Greed", "SDK-001")
        assert conflito.decision == Decision.MANUAL

    def test_agreement_on_the_top_candidate_still_works(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Dark Magician", "SDK-001")
        assert result.decision == Decision.AUTO
        assert result.card_id == DARK_MAGICIAN

    def test_promotion_is_refused_when_the_name_was_read_clearly(
        self, matcher: MatchingEngine
    ) -> None:
        """Um nome lido com clareza não é derrubado por um código divergente."""
        result = matcher.match("Blue-Eyes White Dragon", "SDK-001")
        assert result.card_id == BLUE_EYES
        assert result.decision == Decision.MANUAL


class TestAlternateLanguageMatching:
    """Cartas fotografadas em outro idioma (plano §7.1 multilíngue).

    A fixture do catálogo compartilhado (`catalog`) inclui nomes em PT para
    Blue-Eyes ("Dragão Branco de Olhos Azuis") e Dark Magician ("Mago Negro")
    — ver `tests/fixtures/api/cards_page1_pt.json`. Reproduz o caso real: o
    OCR lê o nome certo em português e o catálogo só conhecia inglês.
    """

    def test_exact_alt_name_matches_at_tier_zero(self, matcher: MatchingEngine) -> None:
        result = matcher.match("Dragão Branco de Olhos Azuis")
        assert result.card_id == BLUE_EYES
        assert result.card_name == "Blue-Eyes White Dragon", "exibe sempre o nome canônico"
        assert result.tier == 0
        assert result.decision == Decision.AUTO
        assert result.matched_language == "PT", "idioma detectado pro palpite de idioma da carta"

    def test_alt_name_case_and_accent_do_not_matter(self, matcher: MatchingEngine) -> None:
        assert matcher.match("mago negro").card_id == DARK_MAGICIAN
        assert matcher.match("MAGO NEGRO").card_id == DARK_MAGICIAN

    def test_matched_language_survives_the_rerank_tier(self, matcher: MatchingEngine) -> None:
        """Não só `CandidateFinder`: o `MatchResult` inteiro carrega o idioma
        do candidato vencedor, mesmo quando ele veio do tier 3 (rerank)."""
        result = matcher.match("Mago Negr0")
        assert result.card_id == DARK_MAGICIAN
        assert result.matched_language == "PT"

    def test_code_region_takes_priority_over_name_language(
        self, matcher: MatchingEngine
    ) -> None:
        """Continuação do plano de idiomas (docs/adr/0009): o código já diz
        a região explicitamente — vence o nome, mesmo que o nome tenha
        casado por `card_alt_name` num idioma diferente."""
        result = matcher.match("Dragão Branco de Olhos Azuis", "CT13-EN008")
        assert result.card_id == BLUE_EYES
        assert result.matched_language == "EN"

    @pytest.mark.parametrize(
        "ocr_text",
        [
            "Drag5o Branco de Olhos Azuis",  # 5/S trocado por OCR
            "DRAGAO BRANCO DE OLHOS AZU1S",
            "Dragao Branco de Olhos Azui",  # truncamento
        ],
    )
    def test_corrupted_alt_name_still_resolves(
        self, matcher: MatchingEngine, ocr_text: str
    ) -> None:
        result = matcher.match(ocr_text)
        assert result.card_id == BLUE_EYES, f"{ocr_text!r} -> {result.card_name!r}"

    def test_alt_name_candidate_uses_tiers_two_and_three(
        self, catalog: Database, settings: Settings
    ) -> None:
        """O nome alternativo é achado pelo FTS (tier 2) e sobrevive ao rerank
        (tier 3) — sem o rerank considerar os nomes alternativos, o candidato
        certo seria descartado por comparar só contra o nome em inglês."""
        with catalog.session() as session:
            finder = CandidateFinder(session, index=NameIndex())
            candidates = finder.find("Mago Negr0")
            assert candidates and candidates[0].card_id == DARK_MAGICIAN
            assert candidates[0].language == "PT"
            assert finder.tier_attempts[2] == 1
            assert finder.tier_hits[3] == 1
            assert finder.tier_attempts[4] == 0

    def test_alt_name_reaches_tier_four_when_fts_finds_nothing(
        self, catalog: Database, settings: Settings
    ) -> None:
        """OCR errou o começo de toda palavra, em português: nenhum token do
        FTS (nem `card_fts`, nem `card_alt_fts`) casa, e só o fuzzy sobre o
        catálogo inteiro — que também precisa enxergar os nomes alternativos —
        acha a carta certa."""
        with catalog.session() as session:
            finder = CandidateFinder(session, index=NameIndex())
            candidates = finder.find("ZAGO NEGRZ")
            assert finder.tier_attempts[2] == 1
            assert finder.tier_attempts[3] == 0, "o FTS não devolveu nada para reranquear"
            assert finder.tier_attempts[4] == 1
            assert candidates and candidates[0].card_id == DARK_MAGICIAN
            assert candidates[0].language == "PT"

    def test_name_index_counts_distinct_cards_not_alt_name_rows(self, catalog: Database) -> None:
        """`size` é usado para saber se o catálogo cresceu (§ TestNameIndex);
        contar linhas em vez de cartas distintas o deixaria errado assim que
        uma carta tivesse nome alternativo."""
        index = NameIndex()
        with catalog.session() as session:
            index.ensure_loaded(session)
        assert index.size == 5, "5 cartas na fixture, mesmo com nomes alternativos"


class TestTierEscalation:
    """Critério de aceitação: o tier 4 só roda quando o tier 2 falha."""

    def test_exact_match_uses_tier_zero_only(self, catalog: Database, settings: Settings) -> None:
        with catalog.session() as session:
            finder = CandidateFinder(session, index=NameIndex())
            finder.find("Blue-Eyes White Dragon")
            assert finder.tier_hits[0] == 1
            assert finder.tier_attempts[2] == 0, "nem o FTS precisou rodar"
            assert finder.tier_attempts[4] == 0

    def test_fuzzy_reading_stops_at_tier_three(self, catalog: Database, settings: Settings) -> None:
        with catalog.session() as session:
            finder = CandidateFinder(session, index=NameIndex())
            finder.find("BLUE-EYES WH1TE DRAGON")
            assert finder.tier_attempts[2] == 1
            assert finder.tier_hits[3] == 1
            assert finder.tier_attempts[4] == 0, (
                "com OR o FTS ainda encontra candidatos apesar do caractere errado; "
                "o fuzzy sobre o catálogo inteiro é desnecessário"
            )

    def test_tier_four_runs_when_fts_finds_nothing(
        self, catalog: Database, settings: Settings
    ) -> None:
        """OCR errou o começo de **todas** as palavras: nenhum token casa.

        É o único caso em que vale pagar o fuzzy sobre o catálogo inteiro.
        """
        with catalog.session() as session:
            finder = CandidateFinder(session, index=NameIndex())
            finder.find("ZZLUE ZZYES ZZHITE ZZRAGON")
            assert finder.tier_attempts[2] == 1, "o tier barato foi tentado primeiro"
            assert finder.tier_attempts[3] == 0, "o FTS não devolveu nada para reranquear"
            assert finder.tier_attempts[4] == 1, "só então o fuzzy completo roda"


class TestTierMetrics:
    def test_stats_are_logged_and_returned(self, matcher: MatchingEngine) -> None:
        """Critério de aceitação: o uso dos tiers precisa ser verificável."""
        matcher.match("Dark Magician")
        matcher.match("BLUE-EYES WH1TE DRAGON")
        stats = matcher.log_tier_stats()
        assert stats["tier0_hits"] == 1
        assert stats["tier2_attempts"] == 1
        assert stats["tier4_attempts"] == 0


class TestNameIndex:
    def test_loads_once_and_reuses(self, catalog: Database) -> None:
        index = NameIndex()
        with catalog.session() as session:
            index.ensure_loaded(session)
            first_size = index.size
            index.ensure_loaded(session)
        assert first_size == index.size == 5

    def test_reloads_after_the_catalog_grows(self, catalog: Database) -> None:
        """Depois de um sync que adiciona cartas, o índice não pode envelhecer."""
        from yugioh_scanner.db.tables import Card

        index = NameIndex()
        with catalog.session() as session:
            index.ensure_loaded(session)
            before = index.size
            session.add(
                Card(
                    id=777,
                    name="Carta Nova",
                    name_normalized="carta nova",
                    type="Effect Monster",
                    desc="",
                )
            )
            session.flush()
            index.ensure_loaded(session)
        assert index.size == before + 1


class TestResolverDirectly:
    def test_known_prefix_without_matching_print(self, catalog: Database) -> None:
        """`LOB` existe, `LOB-999` não. Prefixo conhecido ≠ código validado."""
        with catalog.session() as session:
            resolution = PrintResolver(session).resolve("LOB-999")
            assert resolution.prefix_known
            assert not resolution.validated
            assert resolution.score == 0.5, "forma plausível, mas sem confirmação"

    def test_unknown_prefix(self, catalog: Database) -> None:
        with catalog.session() as session:
            resolution = PrintResolver(session).resolve("QQQQ-EN001")
            assert not resolution.prefix_known
            assert not resolution.validated

    def test_empty_input(self, catalog: Database) -> None:
        with catalog.session() as session:
            resolution = PrintResolver(session).resolve(None)
            assert resolution.score == 0.0


class TestResolverLanguageFallback:
    """Continuação do plano de idiomas (docs/adr/0009): quando o código lido
    tem uma região sem print sincronizado (o caso comum — DE/FR/IT, ou PT
    fora de produtos OTS), cai para o equivalente em inglês sem perder o
    idioma detectado. Usa dados sintéticos (`session`, schema vazio) porque
    o catálogo de fixtures não tem um par regional de verdade."""

    def test_falls_back_to_english_when_the_regional_print_does_not_exist(
        self, session: Session
    ) -> None:
        from yugioh_scanner.db.tables import Card, CardPrint, CardSet

        session.add(CardSet(set_code="SR06", set_name="Structure Deck: Dinosmasher's Fury"))
        session.add(
            Card(id=1, name="Ahrima", name_normalized="ahrima", type="Effect Monster", desc="")
        )
        session.add(
            CardPrint(
                card_id=1,
                set_code_full="SR06-EN002",
                set_code_normalized="SR06-EN002",
                set_prefix="SR06",
                set_name="Structure Deck: Dinosmasher's Fury",
                region="EN",
                number="002",
                rarity="Common",
            )
        )
        session.flush()

        resolution = PrintResolver(session).resolve("SR06-PT002")

        assert resolution.detected_region == "PT", "a carta física diz PT, mesmo sem print PT"
        assert resolution.matched_code == "SR06-EN002"
        assert resolution.card_id == 1
        assert resolution.validated

    def test_uses_the_real_regional_print_when_it_exists(self, session: Session) -> None:
        from yugioh_scanner.db.tables import Card, CardPrint, CardSet

        session.add(CardSet(set_code="OP13", set_name="OTS Tournament Pack 13 (POR)"))
        session.add(
            Card(id=2, name="X", name_normalized="x", type="Effect Monster", desc="")
        )
        for region, print_id in (("EN", 10), ("PT", 11)):
            session.add(
                CardPrint(
                    id=print_id,
                    card_id=2,
                    set_code_full=f"OP13-{region}006",
                    set_code_normalized=f"OP13-{region}006",
                    set_prefix="OP13",
                    set_name="OTS Tournament Pack 13 (POR)",
                    region=region,
                    number="006",
                    rarity="Ultra Rare",
                )
            )
        session.flush()

        resolution = PrintResolver(session).resolve("OP13-PT006")

        assert resolution.detected_region == "PT"
        assert resolution.matched_code == "OP13-PT006", "não precisa de fallback: o print existe"
        assert resolution.print_id == 11

    def test_english_code_never_triggers_a_fallback_attempt(self, session: Session) -> None:
        resolution = PrintResolver(session).resolve("LOB-EN001")
        assert resolution.detected_region == "EN"
        assert not resolution.validated  # catálogo vazio: nada deveria casar de propósito

    def test_unrecognized_code_has_no_detected_region(self, session: Session) -> None:
        resolution = PrintResolver(session).resolve("not a set code")
        assert resolution.detected_region is None
