"""Calibração de código/passcode contra fotos de celular reais (achado do
usuário: `CODE_ROI`/`PASSCODE_ROI` levaram duas rodadas de correção — a
calibração inicial, medida em só 2 fotos, ainda perdia metade das 10 fotos
deste corpus por variação de `detect_card_bounds` entre fotos: a mesma
margem física não gera a mesma porcentagem de altura em toda foto).

**Opt-in** (`pytest -m ocr`) e depende de fotos reais versionadas em
`tests/fixtures/calibration_cellphone/`, com o gabarito codificado no
próprio nome de cada arquivo: `{passcode}-{setcode}.jpg`. Pula (não falha)
se a pasta não existir/estiver vazia, mesma convenção de
`tests/fixtures/cards` (`tests/ocr/test_corpus_accuracy.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yugioh_scanner.config import Settings
from yugioh_scanner.domain.passcode import clean_passcode
from yugioh_scanner.domain.setcode import correction_variants, normalize_set_code
from yugioh_scanner.ocr.rapidocr_provider import RapidOCRProvider
from yugioh_scanner.scanner.worker import ScanTask, process_task, reset_provider, set_provider

pytestmark = pytest.mark.ocr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "tests" / "fixtures" / "calibration_cellphone"


def _cases() -> list[tuple[Path, str, str]]:
    cases = []
    for path in sorted(CORPUS_DIR.glob("*.jpg")):
        expected_passcode, _, expected_code = path.stem.partition("-")
        cases.append((path, expected_passcode, expected_code))
    return cases


@pytest.fixture(scope="module")
def readings() -> list[dict[str, str]]:
    if not CORPUS_DIR.exists():
        pytest.skip(
            f"Sem corpus de fotos de calibração em {CORPUS_DIR}. Corpus real "
            "fornecido pelo usuário — gabarito no nome do arquivo "
            "({passcode}-{setcode}.jpg), não num expected.json separado."
        )
    cases = _cases()
    if not cases:
        pytest.skip(f"{CORPUS_DIR} existe mas está vazia.")

    settings = Settings(data_path=CORPUS_DIR.parent / "_calibration_scratch")
    set_provider(RapidOCRProvider(settings), settings)
    try:
        results: list[dict[str, str]] = []
        for index, (path, expected_passcode, expected_code) in enumerate(cases):
            outcome = process_task(ScanTask(path=path, file_hash=str(index)))
            crop = outcome.crops[0]
            results.append(
                {
                    "file": path.name,
                    "expected_passcode": expected_passcode,
                    "expected_code": normalize_set_code(expected_code),
                    "read_passcode": clean_passcode(crop.read_passcode) or "",
                    "read_code_raw": crop.read_code,
                }
            )
        return results
    finally:
        reset_provider()


class TestPasscodeCalibration:
    """O passcode é puramente numérico — a fonte de identidade mais forte
    que existe (`matching/engine.py::_from_passcode`). Meta: 100%, não uma
    taxa — errar um só já significa identificar a carta errada com
    confiança máxima se nome/código também derem errado."""

    def test_every_passcode_is_read_correctly(self, readings: list[dict[str, str]]) -> None:
        failures = [r for r in readings if r["read_passcode"] != r["expected_passcode"]]
        assert not failures, "; ".join(
            f"{r['file']}: esperado {r['expected_passcode']!r}, leu {r['read_passcode']!r}"
            for r in failures
        )


class TestSetCodeCalibration:
    """Compara contra `correction_variants` (mesma cascata que
    `matching/resolver.py::PrintResolver.resolve` tenta antes de validar
    contra o banco), não a leitura bruta — confusões como "PTO55" em vez de
    "PT055" (letra "O" por dígito "0") são exatamente o que a camada 2 do
    plano §7.2 existe para corrigir antes da validação real."""

    def test_every_code_is_read_correctly(self, readings: list[dict[str, str]]) -> None:
        failures = []
        for r in readings:
            variants = {normalize_set_code(v) for v in correction_variants(r["read_code_raw"])}
            if r["expected_code"] not in variants:
                msg = f"{r['file']}: esperado {r['expected_code']!r}, leu {r['read_code_raw']!r}"
                failures.append(msg)
        assert not failures, "; ".join(failures)
