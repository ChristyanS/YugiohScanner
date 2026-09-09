"""OCR de verdade, com o motor de verdade (plano §19.4).

**Opt-in.** Marcado com `ocr` e excluído da suíte padrão (`pytest -m ocr` para
rodar). Carrega um modelo real e leva dezenas de segundos — uma suíte principal
que faz isso é uma suíte que ninguém roda.

**Assertivas agregadas, não por imagem.** Testar foto a foto produz uma suíte
frágil que todo mundo acaba desabilitando; medir a *taxa* detecta regressão de
verdade. As barras abaixo estão deliberadamente **abaixo do medido** para que
falhem por regressão, não por variação.

**Limite honesto deste corpus:** as cartas aqui são sintéticas — texto nítido,
sem brilho, sem ângulo, sem sleeve. Elas provam que ROI, upscale e integração
com o motor funcionam; **não** provam acurácia em foto real. Isso é o que o
corpus de fotos reais da Fase 9 vai medir, e é lá que os limiares de confiança
serão calibrados.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from rapidfuzz import fuzz

from tests.factories import make_card_image
from yugioh_scanner.config import Settings
from yugioh_scanner.domain.normalization import normalize_fuzzy
from yugioh_scanner.domain.setcode import normalize_set_code
from yugioh_scanner.ocr.rapidocr_provider import RapidOCRProvider
from yugioh_scanner.scanner.worker import ScanTask, process_task, reset_provider, set_provider

pytestmark = pytest.mark.ocr

#: Cartas variadas em comprimento de nome e formato de código.
#: Os set codes são **reais**, conferidos contra o catálogo do YGOPRODeck — uma
#: primeira versão usava códigos inventados de memória, e três deles pertenciam
#: a outras cartas.
CORPUS: list[tuple[str, str]] = [
    ("BLUE-EYES WHITE DRAGON", "LOB-001"),
    ("DARK MAGICIAN", "LOB-005"),
    ("POT OF GREED", "LOB-119"),
    ("MIRROR FORCE", "MRD-138"),
    ("ELEMENTAL HERO SPARKMAN", "DP1-EN004"),
    ("KURIBOH", "MRD-071"),
    ("MYSTICAL SPACE TYPHOON", "MRL-047"),
    ("MONSTER REBORN", "LOB-118"),
]

#: Barras (medidas em 2026-09-09: média 97.7, mínima 92.7, códigos 8/8).
MIN_MEAN_NAME_SIMILARITY = 90.0
MIN_WORST_NAME_SIMILARITY = 80.0
MIN_CODE_EXACT_RATE = 0.75


@pytest.fixture(scope="module")
def readings(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, object]]:
    """Roda o pipeline real uma vez e compartilha os resultados no módulo.

    Carregar o modelo por teste multiplicaria o custo sem ganho nenhum.
    """
    folder: Path = tmp_path_factory.mktemp("corpus")
    settings = Settings(data_path=folder / "data")
    set_provider(RapidOCRProvider(settings), settings)

    results: list[dict[str, object]] = []
    try:
        started = time.perf_counter()
        for index, (name, code) in enumerate(CORPUS):
            path = make_card_image(folder / f"card_{index:02d}.jpg", name=name, set_code=code)
            outcome = process_task(ScanTask(path=path, file_hash=str(index)))
            results.append(
                {
                    "expected_name": name,
                    "expected_code": code,
                    "read_name": outcome.read_name,
                    "read_code": outcome.read_code,
                    "status": outcome.status,
                    "similarity": fuzz.ratio(
                        normalize_fuzzy(outcome.read_name), normalize_fuzzy(name)
                    ),
                    "code_exact": normalize_set_code(outcome.read_code) == code,
                }
            )
        results.append({"__elapsed__": time.perf_counter() - started})
    finally:
        reset_provider()
    return results


@pytest.fixture(scope="module")
def cards(readings: list[dict[str, object]]) -> list[dict[str, object]]:
    return [row for row in readings if "__elapsed__" not in row]


class TestPipelineIntegration:
    def test_every_card_is_read_without_error(self, cards: list[dict[str, object]]) -> None:
        assert all(row["status"] == "ok" for row in cards)

    def test_no_reading_is_empty(self, cards: list[dict[str, object]]) -> None:
        assert all(str(row["read_name"]).strip() for row in cards)


class TestNameAccuracy:
    def test_mean_similarity(self, cards: list[dict[str, object]]) -> None:
        similarities = [float(row["similarity"]) for row in cards]
        mean = sum(similarities) / len(similarities)
        assert mean >= MIN_MEAN_NAME_SIMILARITY, (
            f"similaridade média caiu para {mean:.1f}: "
            + "; ".join(f"{row['expected_name']}→{row['read_name']!r}" for row in cards)
        )

    def test_worst_case_is_still_matchable(self, cards: list[dict[str, object]]) -> None:
        """Mesmo a pior leitura precisa ficar acima do corte do fuzzy (70)."""
        worst = min(cards, key=lambda row: float(row["similarity"]))
        assert float(worst["similarity"]) >= MIN_WORST_NAME_SIMILARITY, (
            f"pior leitura: {worst['expected_name']} → {worst['read_name']!r}"
        )

    def test_long_names_are_not_truncated_to_a_single_box(
        self, cards: list[dict[str, object]]
    ) -> None:
        """O motor quebra nomes longos em várias caixas.

        Se alguém trocar `joined` por `best` em `read_name`, este teste cai:
        'BLUE-EYES WHITE DRAGON' viraria 'DRAGON'.
        """
        longest = max(cards, key=lambda row: len(str(row["expected_name"])))
        read = str(longest["read_name"])
        assert len(read.split()) >= 2, f"leitura truncada: {read!r}"


class TestSetCodeAccuracy:
    def test_exact_rate(self, cards: list[dict[str, object]]) -> None:
        """O upscale 2× da ROI do código é o que sustenta esta taxa."""
        exact = sum(1 for row in cards if row["code_exact"])
        rate = exact / len(cards)
        assert rate >= MIN_CODE_EXACT_RATE, (
            f"apenas {exact}/{len(cards)} códigos exatos: "
            + "; ".join(
                f"{row['expected_code']}→{row['read_code']!r}"
                for row in cards
                if not row["code_exact"]
            )
        )

    def test_read_codes_survive_the_parser(self, cards: list[dict[str, object]]) -> None:
        """De nada adianta ler o código se o parser o rejeita depois."""
        from yugioh_scanner.domain.setcode import parse_set_code

        parsed = sum(1 for row in cards if parse_set_code(str(row["read_code"])) is not None)
        assert parsed >= len(cards) * MIN_CODE_EXACT_RATE


class TestPerformance:
    def test_per_card_time_is_reasonable(self, readings: list[dict[str, object]]) -> None:
        """Referência, não meta: a otimização séria é a Fase 9.

        Serve para flagrar uma regressão grosseira — por exemplo, alguém
        recarregando o modelo a cada imagem, ou processando a região `full`
        sempre em vez de só no fallback.
        """
        elapsed = float(next(row["__elapsed__"] for row in readings if "__elapsed__" in row))
        per_card = elapsed / len(CORPUS)
        assert per_card < 8.0, f"{per_card:.1f}s por carta é lento demais"
