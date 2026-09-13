#!/usr/bin/env python
"""Gera um rascunho de `expected.json` (plano §19.4, ADR 0001) para um corpus de
fotos reais que ainda não tem gabarito.

Dois jeitos de obter a leitura de cada foto (nome impresso + set code, na
forma que `ocr/claude_provider.py` já usa — plano §6.4):

1. `--readings PATH` (sem custo de API): um JSON pré-gerado por fora — por
   exemplo, o Claude Code lendo as fotos em lote via subagentes — no formato
   `{"<arquivo>": {"name": "...", "set_code": "...", "language": "...",
   "legible": true}}`. Este script só faz a reconciliação contra o catálogo.
2. Sem `--readings`: chama o provider `claude` (Claude Vision via API da
   Anthropic, `ANTHROPIC_API_KEY` obrigatória) uma vez por foto — mesma
   cascata que `services/scan_service.py::_try_fallback` usa.

Em ambos os casos, cada leitura é reconciliada contra o catálogo local pelo
mesmo `MatchingEngine` do app (ADR 0009/0012 incluídos) — uma leitura que não
bate com nenhuma carta real cai em `unresolved`, e uma que bate mas com
confiança baixa fica marcada `needs_review`. Confira ao menos os dois antes de
rodar `calibrate_thresholds.py` para valer.

Uso:
    python scripts/generate_ground_truth.py --corpus DIR --readings leituras.json
    python scripts/generate_ground_truth.py --corpus DIR --readings leituras.json --limit 5
    python scripts/generate_ground_truth.py --corpus DIR                 # via API, exige chave
    python scripts/generate_ground_truth.py --corpus DIR --resume        # retoma de onde parou

Saídas, escritas dentro de `--corpus` (nunca copia as fotos):
    ground_truth_state.json   histórico completo por arquivo (para --resume e auditoria)
    expected.json             {file, name, set_code?} — o que calibrate_thresholds.py lê
    unresolved.json           fotos lidas mas que não bateram com nenhuma carta do
                               catálogo local — precisam de rótulo manual
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from yugioh_scanner.config import Settings, get_settings
from yugioh_scanner.db.engine import engine_from_settings
from yugioh_scanner.db.session import Database
from yugioh_scanner.domain.confidence import Decision
from yugioh_scanner.errors import YugiohScannerError
from yugioh_scanner.images.preprocess import ImageError, load_image, prepare_regions
from yugioh_scanner.matching.candidates import NameIndex
from yugioh_scanner.matching.engine import MatchingEngine
from yugioh_scanner.ocr.base import REGION_FULL, OCRRequest
from yugioh_scanner.ocr.registry import create_provider
from yugioh_scanner.scanner.discovery import discover_images

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {row["file"]: row for row in rows}


def save_state(path: Path, state: dict[str, dict]) -> None:
    rows = list(state.values())
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def read_via_api(path: Path, provider, settings: Settings) -> dict:
    """Chama o provider `claude` (Claude Vision via API) para uma foto."""
    try:
        image = load_image(path, max_pixels=settings.max_image_pixels)
    except ImageError as exc:
        return {"error": f"load: {exc}"}

    try:
        prepared = prepare_regions(image, source=path, region=None)
    except ImageError as exc:
        return {"error": f"prepare: {exc}"}
    finally:
        image.close()

    try:
        full = prepared.regions.get(REGION_FULL)
        request = OCRRequest(regions={REGION_FULL: full}, source=str(path))
        result = provider.read(request)
    except YugiohScannerError as exc:
        return {"error": f"claude: {exc}"}
    finally:
        prepared.close()

    return dict(result.raw or {})


def reconcile(file_name: str, raw: dict, engine: MatchingEngine) -> dict:
    """Casa uma leitura bruta (`name`/`set_code`/`language`/`legible`, o mesmo
    formato de `ocr/claude_provider.py`) contra o catálogo local.
    """
    if "error" in raw:
        return {"file": file_name, "error": raw["error"], "needs_review": True, "resolved": False}

    read_name = str(raw.get("name") or "").strip()
    read_code = str(raw.get("set_code") or "").strip()
    legible = bool(raw.get("legible", False))

    match = engine.match(read_name, read_code or None) if read_name else None
    matched = match is not None and match.card_id is not None

    needs_review = (
        not legible
        or not matched
        or (match is not None and match.decision != Decision.AUTO)
        or bool(raw.get("parse_error"))
        or bool(raw.get("refusal"))
    )

    return {
        "file": file_name,
        "name": (match.card_name if matched else None) or read_name or None,
        "set_code": (match.set_code if matched else None),
        "needs_review": needs_review,
        "resolved": matched,
        "read_name": read_name,
        "read_set_code": read_code,
        "read_language": raw.get("language", ""),
        "read_rarity": raw.get("rarity", ""),
        "read_legible": legible,
        "match_confidence": round(match.confidence, 3) if match else 0.0,
        "match_decision": (match.decision.value if match else "unmatched"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--model", default=None, help="Sobrescreve YGS_LLM_MODEL/llm_model (modo API).")
    parser.add_argument("--resume", action="store_true", help="Pula arquivos já no state.")
    parser.add_argument(
        "--readings",
        type=Path,
        default=None,
        help="JSON {arquivo: {name, set_code, language, legible}} pré-gerado (sem API).",
    )
    args = parser.parse_args()

    corpus = args.corpus.expanduser().resolve(strict=True)
    if not corpus.is_dir():
        raise SystemExit(f"Não é uma pasta: {corpus}")

    settings = get_settings()
    overrides: dict[str, object] = {}
    if args.database:
        overrides["database_url"] = f"sqlite:///{args.database.resolve().as_posix()}"
    if args.model:
        overrides["llm_model"] = args.model
    if overrides:
        settings = settings.model_copy(update=overrides)

    engine_sa = engine_from_settings(settings)
    database = Database(engine_sa)

    images = discover_images(corpus, recursive=False, limit=args.limit)
    if not images:
        raise SystemExit(f"Nenhuma imagem suportada em {corpus} (jpg/jpeg/png).")

    state_path = corpus / "ground_truth_state.json"
    state = load_state(state_path) if args.resume else {}

    readings: dict[str, dict] | None = None
    provider = None
    if args.readings:
        readings = json.loads(args.readings.read_text(encoding="utf-8"))
    else:
        provider = create_provider("claude", settings)
        provider.warmup()  # levanta LlmApiKeyMissingError cedo, com mensagem clara

    index = NameIndex()
    started = time.perf_counter()
    processed = 0
    try:
        with database.session() as session:
            match_engine = MatchingEngine(session, settings, index=index)
            for n, img in enumerate(images, start=1):
                if args.resume and img.name in state and "error" not in state[img.name]:
                    continue
                if readings is not None:
                    raw = readings.get(img.name, {"error": "sem leitura em --readings"})
                else:
                    raw = read_via_api(img.path, provider, settings)
                entry = reconcile(img.name, raw, match_engine)
                state[img.name] = entry
                save_state(state_path, state)
                processed += 1
                flag = "  [REVIEW]" if entry.get("needs_review") else ""
                print(
                    f"[{n}/{len(images)}] {img.name}: "
                    f"{entry.get('name') or entry.get('error')!r}{flag}"
                )
    finally:
        if provider is not None:
            provider.close()
        engine_sa.dispose()

    elapsed = time.perf_counter() - started

    rows = list(state.values())
    resolved = [r for r in rows if r.get("resolved")]
    unresolved = [r for r in rows if not r.get("resolved")]
    needs_review = [r for r in rows if r.get("needs_review")]

    expected = [
        {"file": r["file"], "name": r["name"], **({"set_code": r["set_code"]} if r.get("set_code") else {}), "needs_review": r["needs_review"]}
        for r in resolved
    ]
    (corpus / "expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (corpus / "unresolved.json").write_text(
        json.dumps(unresolved, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n{processed} fotos processadas nesta execução em {elapsed:.1f}s.")
    print(f"Total no state: {len(rows)} | resolvidas: {len(resolved)} | "
          f"não resolvidas: {len(unresolved)} | marcadas p/ revisão: {len(needs_review)}")
    print(f"\nEscrito: {corpus / 'expected.json'}")
    print(f"Escrito: {corpus / 'unresolved.json'}")
    print(f"Estado:  {state_path}")


if __name__ == "__main__":
    main()
