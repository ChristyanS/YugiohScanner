#!/usr/bin/env python
"""Calibra CONFIDENCE_AUTO/CONFIDENCE_REVIEW contra um corpus real (plano §9, §19.4).

Uso:
    python scripts/calibrate_thresholds.py
    python scripts/calibrate_thresholds.py --corpus tests/fixtures/cards --provider rapidocr
    python scripts/calibrate_thresholds.py --database data/yugioh.db --json

Roda OCR + matching **uma única vez** por foto do corpus (a parte cara — leva
a maior parte do tempo) e depois varre combinações de `auto`/`review` sobre os
scores já computados (barato: é só reavaliar `domain.confidence.decide` para
cada par). Não há motivo para re-rodar OCR a cada limiar testado.

O corpus é `tests/fixtures/cards/expected.json`:

    [{"file": "blue_eyes_lob.jpg", "name": "Blue-Eyes White Dragon", "set_code": "LOB-001"}]

`set_code` é opcional (omita quando a foto não mostra um set code legível).
Fotos são SUAS — o script não gera nem baixa nada, só lê o que já está lá
(plano §19.4: "Fotos são suas, ficam no repositório").
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from yugioh_scanner.config import Settings, get_settings
from yugioh_scanner.db.engine import engine_from_settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.domain.confidence import ConfidenceInput, ConfidenceThresholds, Decision, decide
from yugioh_scanner.domain.normalization import normalize_strict
from yugioh_scanner.matching.candidates import NameIndex
from yugioh_scanner.matching.engine import MatchingEngine, MatchResult
from yugioh_scanner.ocr.registry import create_provider
from yugioh_scanner.scanner.discovery import DiscoveredImage, discover_images
from yugioh_scanner.scanner.worker import ScanTask, process_task, set_provider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT_ROOT / "tests" / "fixtures" / "cards"

#: Varredura padrão — cobre a faixa em que qualquer limiar razoável mora.
AUTO_SWEEP = [round(0.80 + 0.01 * i, 2) for i in range(20)]  # 0.80 .. 0.99
REVIEW_SWEEP = [round(0.50 + 0.02 * i, 2) for i in range(21)]  # 0.50 .. 0.90


@dataclass(frozen=True, slots=True)
class CorpusCase:
    file: str
    name: str
    set_code: str | None = None


@dataclass
class Reading:
    """O que se sabe sobre uma foto do corpus depois de OCR + matching."""

    case: CorpusCase
    match: MatchResult
    ocr_ms: int
    ocr_status: str

    @property
    def name_correct(self) -> bool:
        if self.match.card_name is None:
            return False
        return normalize_strict(self.match.card_name) == normalize_strict(self.case.name)

    @property
    def set_code_correct(self) -> bool:
        if self.case.set_code is None:
            return True  # nada para conferir
        return (self.match.set_code or "").upper() == self.case.set_code.upper()


def load_corpus(corpus_dir: Path) -> list[CorpusCase]:
    expected_path = corpus_dir / "expected.json"
    if not expected_path.exists():
        raise SystemExit(
            f"Corpus não encontrado: {expected_path}\n"
            "Crie a pasta com fotos reais e um expected.json (plano §19.4) — "
            "ver tests/fixtures/cards/README.md para o formato."
        )
    raw = json.loads(expected_path.read_text(encoding="utf-8"))
    return [
        CorpusCase(file=row["file"], name=row["name"], set_code=row.get("set_code")) for row in raw
    ]


def run_pipeline(
    corpus_dir: Path,
    cases: list[CorpusCase],
    provider_name: str,
    settings: Settings,
    database: Database,
) -> list[Reading]:
    images = {img.path.name: img for img in discover_images(corpus_dir, recursive=False)}
    missing = [c.file for c in cases if c.file not in images]
    if missing:
        raise SystemExit(f"expected.json cita arquivos que não existem no corpus: {missing}")

    provider = create_provider(provider_name, settings)
    provider.warmup()
    set_provider(provider, settings)

    index = NameIndex()
    readings: list[Reading] = []
    try:
        with database.session() as session:
            engine = MatchingEngine(session, settings, index=index)
            for case in cases:
                image = images[case.file]
                outcome = process_task(_as_scan_task(image))
                # `--grid` nunca é usado aqui (calibração roda o caso comum: uma
                # foto, uma carta), então `crops` tem exatamente um item — exceto
                # quando o pré-processamento falhou antes de gerar qualquer
                # recorte (arquivo corrompido), caso em que `crops` fica vazio
                # (ADR 0011, `scanner/worker.py::process_task`).
                crop = outcome.crops[0] if outcome.crops else None
                read_name = crop.read_name if crop is not None else ""
                read_code = crop.read_code if crop is not None else ""
                ocr_status = crop.status if crop is not None else (outcome.preprocess_status or "error")
                match = engine.match(read_name, read_code or None)
                readings.append(
                    Reading(case=case, match=match, ocr_ms=outcome.elapsed_ms, ocr_status=ocr_status)
                )
    finally:
        provider.close()

    return readings


def _as_scan_task(image: DiscoveredImage) -> ScanTask:
    return ScanTask(path=image.path, file_hash=image.file_hash, size=image.size)


def top1_accuracy(readings: list[Reading]) -> dict[str, float]:
    with_code = [r for r in readings if r.case.set_code is not None]
    return {
        "name_accuracy": _rate(r.name_correct for r in readings),
        "set_code_accuracy": _rate(r.set_code_correct for r in with_code)
        if with_code
        else float("nan"),
        "n": len(readings),
        "n_with_set_code": len(with_code),
    }


def _rate(flags: Iterable[bool]) -> float:
    values = list(flags)
    return sum(values) / len(values) if values else float("nan")


def sweep(readings: list[Reading], base: ConfidenceThresholds) -> list[dict[str, object]]:
    """Para cada (auto, review): quantos AUTO estariam certos/errados."""
    rows: list[dict[str, object]] = []
    for auto in AUTO_SWEEP:
        for review in REVIEW_SWEEP:
            if review > auto:
                continue
            thresholds = ConfidenceThresholds(
                auto=auto,
                review=review,
                agreement_auto=min(base.agreement_auto, auto),
                min_margin=base.min_margin,
                ambiguous_margin=base.ambiguous_margin,
            )
            autos = 0
            autos_correct = 0
            manual = 0
            for r in readings:
                decision, _reason = decide(
                    _as_confidence_input(r.match), thresholds, confidence=r.match.confidence
                )
                if decision == Decision.AUTO:
                    autos += 1
                    if r.name_correct and r.set_code_correct:
                        autos_correct += 1
                else:
                    manual += 1
            precision = autos_correct / autos if autos else float("nan")
            recall = autos_correct / len(readings) if readings else float("nan")
            rows.append(
                {
                    "auto": auto,
                    "review": review,
                    "auto_count": autos,
                    "auto_correct": autos_correct,
                    "manual_count": manual,
                    "precision": precision,
                    "recall": recall,
                }
            )
    return rows


def _as_confidence_input(match: MatchResult) -> ConfidenceInput:
    return ConfidenceInput(
        name_score=match.name_score,
        code_score=match.code_score,
        margin=match.margin,
        agreement=match.agreement,
        has_candidate=match.matched,
    )


def best_rows(
    rows: list[dict[str, object]], *, min_precision: float = 1.0
) -> list[dict[str, object]]:
    """Melhores limiares: 100% de precisão (nenhum AUTO errado) com o maior recall."""
    perfect = [
        r for r in rows if isinstance(r["precision"], float) and r["precision"] >= min_precision
    ]
    perfect.sort(key=lambda r: (-float(r["recall"]), -float(r["auto"])))  # type: ignore[arg-type]
    return perfect[:5]


def print_report(readings: list[Reading], rows: list[dict[str, object]]) -> None:
    acc = top1_accuracy(readings)
    print(f"\nCorpus: {acc['n']} fotos ({acc['n_with_set_code']} com set code legível)")
    print(f"Taxa de acerto top-1 do nome:      {acc['name_accuracy']:.1%}  (meta §19: >= 80%)")
    if acc["n_with_set_code"]:
        rate = acc["set_code_accuracy"]
        print(f"Taxa de acerto do set code:         {rate:.1%}  (meta §19: >= 60%)")

    print("\nErros (nome ou set code):")
    any_error = False
    for r in readings:
        if not r.name_correct or not r.set_code_correct:
            any_error = True
            print(
                f"  {r.case.file}: esperado {r.case.name!r}/{r.case.set_code!r} "
                f"-> leu {r.match.card_name!r}/{r.match.set_code!r} "
                f"(confiança {r.match.confidence:.0%}, status OCR={r.ocr_status})"
            )
    if not any_error:
        print("  (nenhum)")

    print("\nMelhores limiares (0 falso-positivo em AUTO, maior recall):")
    print(f"  {'auto':>6} {'review':>7} {'auto_n':>7} {'manual_n':>9} {'recall':>7}")
    for row in best_rows(rows):
        print(
            f"  {row['auto']:>6.2f} {row['review']:>7.2f} {row['auto_count']:>7} "
            f"{row['manual_count']:>9} {row['recall']:>6.1%}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--provider", default=None, help="Padrão: OCR_PROVIDER da config.")
    parser.add_argument("--database", type=Path, default=None, help="Padrão: banco da config.")
    parser.add_argument("--json", action="store_true", help="Saída em JSON (para scripts).")
    args = parser.parse_args()

    settings = get_settings()
    if args.database:
        settings = Settings(database_url=f"sqlite:///{args.database.resolve().as_posix()}")
    provider_name = args.provider or settings.ocr_provider

    engine = engine_from_settings(settings)
    database = Database(engine)

    started = time.perf_counter()
    cases = load_corpus(args.corpus)
    readings = run_pipeline(args.corpus, cases, provider_name, settings, database)
    elapsed = time.perf_counter() - started

    thresholds = ConfidenceThresholds.from_settings(settings)
    rows = sweep(readings, thresholds)

    if args.json:
        print(
            json.dumps(
                {
                    "accuracy": top1_accuracy(readings),
                    "elapsed_s": round(elapsed, 1),
                    "sweep": rows,
                    "best": best_rows(rows),
                },
                default=str,
            )
        )
    else:
        print_report(readings, rows)
        print(f"\n{len(readings)} fotos processadas em {elapsed:.1f}s ({provider_name}).")

    engine.dispose()


if __name__ == "__main__":
    main()
