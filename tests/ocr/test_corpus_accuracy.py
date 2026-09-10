"""Acurácia contra o corpus de fotos REAIS (plano §9 e §19.4).

Diferente de `test_real_ocr.py` (cartas sintéticas, prova só que o pipeline
funciona): este teste lê `tests/fixtures/cards/expected.json` e roda o motor
de OCR de verdade contra fotos de verdade — é a única fonte honesta para os
critérios de aceitação da Fase 9 ("taxa de acerto top-1 do nome ≥ 80%").

**Ainda sem corpus.** Como o plano deixa explícito (§19.4: "fotos são
SUAS"), este teste não pode fabricar as fotos — ele **pula** (não falha)
enquanto `tests/fixtures/cards/expected.json` não existir. Ver
`tests/fixtures/cards/README.md` para como adicionar o corpus.

Roda contra o banco **real e completo** (`data/yugioh.db` por padrão,
substituível via `YGS_CALIBRATION_DB`) — não a fixture pequena que o resto
da suíte usa. Um corpus de fotos reais suas quase certamente inclui cartas
fora do punhado que a fixture conhece; testar contra ela mediria "a carta
existe na fixture", não "o OCR leu certo".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ocr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "tests" / "fixtures" / "cards"
EXPECTED_JSON = CORPUS_DIR / "expected.json"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

#: Metas do plano §19.4 — as mesmas que `scripts/calibrate_thresholds.py` imprime.
MIN_NAME_ACCURACY = 0.80
MIN_SET_CODE_ACCURACY = 0.60


def _default_database_path() -> Path:
    override = os.environ.get("YGS_CALIBRATION_DB")
    return Path(override) if override else PROJECT_ROOT / "data" / "yugioh.db"


@pytest.fixture(scope="module")
def corpus_readings() -> list[object]:
    if not EXPECTED_JSON.exists():
        pytest.skip(
            f"Sem corpus de fotos reais em {EXPECTED_JSON}. "
            "Veja tests/fixtures/cards/README.md — este teste é o critério "
            "de aceitação da Fase 9 e só roda quando o corpus existir."
        )

    database_path = _default_database_path()
    if not database_path.exists():
        pytest.skip(
            f"Banco real não encontrado em {database_path}. Rode `yugioh-scanner "
            "init` primeiro, ou aponte YGS_CALIBRATION_DB para um catálogo completo "
            "— a fixture pequena da suíte não serve (cartas do corpus provavelmente "
            "não estão nela)."
        )

    import calibrate_thresholds as calib  # type: ignore[import-not-found]

    from yugioh_scanner.config import Settings
    from yugioh_scanner.db.engine import engine_from_settings
    from yugioh_scanner.db.session import Database

    settings = Settings(database_url=f"sqlite:///{database_path.resolve().as_posix()}")
    engine = engine_from_settings(settings)
    database = Database(engine)
    try:
        cases = calib.load_corpus(CORPUS_DIR)
        return calib.run_pipeline(CORPUS_DIR, cases, settings.ocr_provider, settings, database)
    finally:
        engine.dispose()


class TestAggregateAccuracy:
    """Assertiva agregada, não por imagem — mesma razão de `test_real_ocr.py`:
    testar foto a foto produz uma suíte frágil; testar a taxa detecta
    regressão de verdade."""

    def test_name_top1_accuracy(self, corpus_readings: list[object]) -> None:
        import calibrate_thresholds as calib  # type: ignore[import-not-found]

        acc = calib.top1_accuracy(corpus_readings)  # type: ignore[arg-type]
        assert acc["name_accuracy"] >= MIN_NAME_ACCURACY, (
            f"acerto do nome caiu para {acc['name_accuracy']:.1%} "
            f"(meta: {MIN_NAME_ACCURACY:.0%}) — rode "
            "`python scripts/calibrate_thresholds.py` para ver quais fotos erraram"
        )

    def test_set_code_accuracy(self, corpus_readings: list[object]) -> None:
        import calibrate_thresholds as calib  # type: ignore[import-not-found]

        acc = calib.top1_accuracy(corpus_readings)  # type: ignore[arg-type]
        if not acc["n_with_set_code"]:
            pytest.skip("Nenhum caso do corpus tem set_code em expected.json.")
        assert acc["set_code_accuracy"] >= MIN_SET_CODE_ACCURACY, (
            f"acerto do set code caiu para {acc['set_code_accuracy']:.1%} "
            f"(meta: {MIN_SET_CODE_ACCURACY:.0%})"
        )
