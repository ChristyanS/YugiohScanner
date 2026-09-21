#!/usr/bin/env python
"""Calibra a leitura de grade de cartas (deskew + ROIs) contra um corpus real
com gabarito por célula.

Uso:
    python scripts/calibrate_grid.py
    python scripts/calibrate_grid.py --corpus tests/fixtures/grid_scans --provider rapidocr

Roda a grade completa por foto (`manual_grid_cells` → `worker._prepare_crop`,
que tenta `images/grid.py::locate_and_deskew_card` antes de cair no
`detect_card_bounds` de sempre) + OCR + matching, uma vez por célula, e
reporta acerto por campo — nome resolvido pelo matching (identidade real,
não string crua de OCR), set code e passcode. Mesmo espírito de
`scripts/calibrate_thresholds.py`, mas para o caminho de grade em vez de
carta única.

O corpus é `tests/fixtures/grid_scans/expected.json`:

    [{"file": "scan_01.png", "grid_size": "3x3",
      "cells": [{"name": "Ansatsu", "set_code": "SDY-016", "passcode": "48365709"}, ...]}]

Fotos são do usuário (digitalizações reais de páginas de fichário 3x3,
mapeadas por ele nesta sessão) — o script só lê o que já está no repo.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from yugioh_scanner.config import Settings, get_settings
from yugioh_scanner.db.engine import engine_from_settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.domain.confidence import Decision
from yugioh_scanner.domain.normalization import normalize_strict
from yugioh_scanner.images.grid import manual_grid_cells, parse_grid_size
from yugioh_scanner.matching.candidates import NameIndex
from yugioh_scanner.matching.engine import MatchingEngine, MatchResult
from yugioh_scanner.ocr.registry import create_provider
from yugioh_scanner.scanner.worker import (
    CropOutcome,
    CropRegion,
    ScanTask,
    _prepare_crop,
    _read_with_fallback,
    set_provider,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT_ROOT / "tests" / "fixtures" / "grid_scans"


@dataclass(frozen=True, slots=True)
class CellCase:
    #: Fundo de carta (achado real, Scan #11: 7/9 células de uma foto eram o
    #: verso de cartas fora do slot, rejeitadas manualmente na revisão — não
    #: existe carta nenhuma pra conferir nome/set/passcode aqui, só o
    #: cuidado de o matching **não** confiar demais num OCR de fundo).
    card_back: bool = False
    name: str | None = None
    set_code: str | None = None
    passcode: str | None = None


@dataclass(frozen=True, slots=True)
class FileCase:
    file: str
    grid_size: str
    cells: list[CellCase]


@dataclass
class Reading:
    file: str
    index: int
    case: CellCase
    match: MatchResult
    read_name: str
    read_code: str
    read_passcode: str

    @property
    def name_correct(self) -> bool:
        if self.match.card_name is None:
            return False
        assert self.case.name is not None
        return normalize_strict(self.match.card_name) == normalize_strict(self.case.name)

    @property
    def set_code_correct(self) -> bool:
        if self.case.set_code is None:
            return True  # nada para conferir
        return (self.match.set_code or "").upper() == self.case.set_code.upper()

    @property
    def passcode_correct(self) -> bool:
        assert self.case.passcode is not None
        return self.case.passcode in self.read_passcode.replace(" ", "")

    @property
    def card_back_rejected(self) -> bool:
        """Para uma célula de fundo de carta: o matching não confiou o
        bastante pra entrar sozinho na coleção (`Decision.AUTO`). Não existe
        detecção de fundo dedicada hoje — o piso aceitável é o matching ficar
        manual/pendente/sem candidato, nunca autoconfirmar um fundo como se
        fosse uma carta de verdade."""
        return self.match.decision != Decision.AUTO


def load_corpus(corpus_dir: Path) -> list[FileCase]:
    expected_path = corpus_dir / "expected.json"
    if not expected_path.exists():
        raise SystemExit(
            f"Corpus não encontrado: {expected_path}\n"
            "Ver tests/fixtures/grid_scans/expected.json para o formato."
        )
    raw = json.loads(expected_path.read_text(encoding="utf-8"))
    return [
        FileCase(
            file=row["file"],
            grid_size=row.get("grid_size", "3x3"),
            cells=[
                CellCase(card_back=True)
                if c.get("card_back")
                else CellCase(name=c["name"], set_code=c.get("set_code"), passcode=c["passcode"])
                for c in row["cells"]
            ],
        )
        for row in raw
    ]


def run_pipeline(
    corpus_dir: Path,
    cases: list[FileCase],
    provider_name: str,
    settings: Settings,
    database: Database,
) -> list[Reading]:
    provider = create_provider(provider_name, settings)
    provider.warmup()
    set_provider(provider, settings, grid_mode=True)

    index = NameIndex()
    readings: list[Reading] = []
    try:
        with database.session() as session:
            engine = MatchingEngine(session, settings, index=index)
            for file_case in cases:
                path = corpus_dir / file_case.file
                if not path.exists():
                    raise SystemExit(f"expected.json cita um arquivo que não existe: {path}")
                image = Image.open(path).convert("RGB")
                try:
                    rows, cols = parse_grid_size(file_case.grid_size)
                    boxes = manual_grid_cells(image, rows, cols)
                    if len(boxes) != len(file_case.cells):
                        raise SystemExit(
                            f"{file_case.file}: manual_grid_cells achou {len(boxes)} "
                            f"células, gabarito tem {len(file_case.cells)}"
                        )
                    task = ScanTask(path=path, file_hash="calibration", size=0)
                    for idx, (box, case) in enumerate(zip(boxes, file_case.cells, strict=True)):
                        region = CropRegion(idx, len(boxes), box)
                        prepared = _prepare_crop(image, task, region)
                        try:
                            result = _read_with_fallback(provider, prepared, str(path))
                        finally:
                            prepared.close()
                        outcome = CropOutcome(region=region, ocr=result)
                        read_name, read_code, read_passcode = (
                            outcome.read_name,
                            outcome.read_code,
                            outcome.read_passcode,
                        )
                        match = engine.match(read_name, read_code or None, read_passcode or None)
                        readings.append(
                            Reading(
                                file=file_case.file,
                                index=idx,
                                case=case,
                                match=match,
                                read_name=read_name,
                                read_code=read_code,
                                read_passcode=read_passcode,
                            )
                        )
                finally:
                    image.close()
    finally:
        provider.close()

    return readings


def _rate(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else float("nan")


def print_report(readings: list[Reading]) -> None:
    cards = [r for r in readings if not r.case.card_back]
    backs = [r for r in readings if r.case.card_back]

    with_code = [r for r in cards if r.case.set_code is not None]
    name_rate = _rate([r.name_correct for r in cards])
    code_rate = _rate([r.set_code_correct for r in with_code]) if with_code else float("nan")
    passcode_rate = _rate([r.passcode_correct for r in cards])

    print(f"\nCorpus: {len(cards)} células de carta ({len(with_code)} com set code no gabarito)")
    print(f"Nome identificado corretamente (matching):  {name_rate:.1%}")
    if with_code:
        print(f"Set code correto:                           {code_rate:.1%}")
    print(f"Passcode correto:                           {passcode_rate:.1%}")

    if backs:
        back_rate = _rate([r.card_back_rejected for r in backs])
        print(f"\nFundos de carta: {len(backs)} células")
        print(f"Corretamente não autoconfirmados:            {back_rate:.1%}")

    print("\nErros:")
    any_error = False
    for r in cards:
        if not (r.name_correct and r.set_code_correct and r.passcode_correct):
            any_error = True
            print(
                f"  {r.file}[{r.index}]: esperado name={r.case.name!r} "
                f"set={r.case.set_code!r} pass={r.case.passcode!r}"
            )
            print(
                f"      leu   name={r.read_name!r} code={r.read_code!r} "
                f"passcode={r.read_passcode!r}"
            )
            print(
                f"      match name={r.match.card_name!r} set={r.match.set_code!r} "
                f"(confiança {r.match.confidence:.0%})"
            )
    for r in backs:
        if not r.card_back_rejected:
            any_error = True
            print(
                f"  {r.file}[{r.index}]: fundo de carta autoconfirmado como "
                f"{r.match.card_name!r} (confiança {r.match.confidence:.0%}) — falso positivo"
            )
    if not any_error:
        print("  (nenhum)")


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

    cases = load_corpus(args.corpus)
    readings = run_pipeline(args.corpus, cases, provider_name, settings, database)

    if args.json:
        cards = [r for r in readings if not r.case.card_back]
        backs = [r for r in readings if r.case.card_back]
        with_code = [r for r in cards if r.case.set_code is not None]
        print(
            json.dumps(
                {
                    "n": len(cards),
                    "name_accuracy": _rate([r.name_correct for r in cards]),
                    "set_code_accuracy": _rate([r.set_code_correct for r in with_code])
                    if with_code
                    else None,
                    "passcode_accuracy": _rate([r.passcode_correct for r in cards]),
                    "card_back_n": len(backs),
                    "card_back_rejected_rate": _rate([r.card_back_rejected for r in backs])
                    if backs
                    else None,
                },
                indent=2,
            )
        )
    else:
        print_report(readings)


if __name__ == "__main__":
    main()
