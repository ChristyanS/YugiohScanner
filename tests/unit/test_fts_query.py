"""Sanitização de consultas FTS5 (plano §21 — injeção é um vetor real aqui)."""

from __future__ import annotations

import pytest

from yugioh_scanner.db.fts import sanitize_fts_query


class TestBasicBehaviour:
    def test_simple_name(self) -> None:
        assert sanitize_fts_query("Blue-Eyes White Dragon") == ("blue OR eyes OR white OR dragon*")

    def test_or_is_the_default_because_input_comes_from_ocr(self) -> None:
        """Com AND, um caractere errado em qualquer palavra zera a busca.

        Medido no catálogo real: 5 de 6 leituras típicas de OCR devolviam zero
        candidatos com AND, e 50 com OR.
        """
        assert " OR " in sanitize_fts_query("Blue-Eyes White Dragon")
        assert sanitize_fts_query("Dark Magician", operator="AND") == "dark AND magician*"

    def test_single_token_gets_prefix(self) -> None:
        assert sanitize_fts_query("dragon") == "dragon*"

    def test_prefix_can_be_disabled(self) -> None:
        assert sanitize_fts_query("dragon", prefix_last=False) == "dragon"

    def test_single_letter_last_token_gets_no_prefix(self) -> None:
        """`a*` casaria com meio catálogo — não vale como busca por prefixo."""
        assert sanitize_fts_query("dark a") == "dark OR a"

    def test_digits_are_kept(self) -> None:
        assert sanitize_fts_query("Number 39 Utopia") == "number OR 39 OR utopia*"

    def test_case_is_normalized(self) -> None:
        assert sanitize_fts_query("DARK MAGICIAN") == sanitize_fts_query("dark magician")


class TestInjection:
    """Cada entrada aqui é sintaxe FTS5 que mudaria a semântica da busca."""

    @pytest.mark.parametrize(
        "hostile",
        [
            '" OR name MATCH "x',
            "dragon OR 1=1",
            "dragon NEAR/5 blue",
            '"exact phrase"',
            "-excluded",
            "dragon*)(",
            "^caret",
            "a AND b OR c",
        ],
    )
    def test_operators_are_neutralized(self, hostile: str) -> None:
        result = sanitize_fts_query(hostile)
        # Sobram apenas tokens alfanuméricos unidos pelo nosso próprio AND.
        assert '"' not in result
        assert "(" not in result and ")" not in result
        assert "-" not in result
        assert "^" not in result
        assert "NEAR" not in result

    def test_operator_words_survive_only_as_plain_tokens(self) -> None:
        """ "OR" vira o literal 'or', que o FTS5 trata como termo, não operador."""
        assert sanitize_fts_query("dragon OR blue") == "dragon OR or OR blue*"


class TestEmptyResults:
    @pytest.mark.parametrize("empty", ["", "   ", "!!!", "---", '"""', "\n\t"])
    def test_no_tokens_yields_empty_string(self, empty: str) -> None:
        """Chamador deve tratar string vazia como 'sem candidatos'.

        Executar `MATCH ''` levantaria erro de sintaxe no SQLite.
        """
        assert sanitize_fts_query(empty) == ""


class TestOcrArtifacts:
    def test_ocr_noise_is_tolerated(self) -> None:
        """Texto vindo de OCR chega sujo; a sanitização não pode explodir."""
        assert sanitize_fts_query("BLUE-EYES  WH1TE   DRAG0N|") == (
            "blue OR eyes OR wh1te OR drag0n*"
        )

    def test_accented_text(self) -> None:
        """Bug real de busca (achado do usuário, ver docs/adr e histórico):
        esta função recebe texto de busca **direto do usuário**, não só nome
        já normalizado — o comentário antigo deste teste presumia o
        contrário e documentava o bug como se fosse o comportamento certo.
        Sem passar por `normalize_strict` primeiro, "Dragão" quebrava em
        dois fragmentos curtos e ruidosos ("drag" + "o") em vez do token
        único "dragao*", que é o que bate com o conteúdo de fato indexado
        (`name_normalized` já remove acentos na ingestão)."""
        assert sanitize_fts_query("Dragão") == "dragao*"

    def test_multiword_accented_phrase_produces_clean_tokens(self) -> None:
        """Caso real reportado: buscar o nome em português de Blue-Eyes White
        Dragon precisa gerar tokens limpos o bastante para o motor de busca
        achar a carta — fragmentos como "drag"/"o"/"for"/"a" geravam ruído
        que ou enterrava o resultado certo ou saturava sozinho o teto de
        candidatos antes mesmo de consultar os nomes traduzidos."""
        assert sanitize_fts_query("Dragão Branco de Olhos Azuis") == (
            "dragao OR branco OR de OR olhos OR azuis*"
        )
        assert sanitize_fts_query("Força Celeste") == "forca OR celeste*"
