#!/usr/bin/env python
"""Mede os números-alvo do plano §20.5 contra o código real (Fase 9).

Uso:
    python scripts/benchmark_performance.py            # tudo
    python scripts/benchmark_performance.py --skip-init-sync   # pula o que precisa de rede
    python scripts/benchmark_performance.py --json

Cada benchmark roda contra um banco **escrachado** (cópia temporária,
descartada ao final) — nunca `data/yugioh.db`. O benchmark de scan usa fotos
**sintéticas**: mede *throughput* do pipeline (tempo por imagem), não
acurácia — a acurácia é o que `calibrate_thresholds.py` mede contra fotos
reais (plano §9, ver `docs/adr/0001-limiares-de-confianca.md`).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from sqlalchemy.orm import Session

from yugioh_scanner.config import Settings
from yugioh_scanner.db.engine import engine_from_settings
from yugioh_scanner.db.migrations import upgrade_to_head
from yugioh_scanner.db.session import Database
from yugioh_scanner.db.tables import DEFAULT_EDITION, DEFAULT_LANGUAGE, Card, CardPrint
from yugioh_scanner.matching.candidates import NameIndex
from yugioh_scanner.matching.engine import MatchingEngine
from yugioh_scanner.repositories.collection import CollectionRepository
from yugioh_scanner.scanner.executor import resolve_workers
from yugioh_scanner.scanner.pipeline import run_pipeline
from yugioh_scanner.services.export_service import ExportService
from yugioh_scanner.services.sync_service import SyncService
from yugioh_scanner.ygoprodeck.client import YgoProDeckClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))


@dataclass
class BenchmarkResult:
    name: str
    elapsed_s: float
    target_s: float
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.elapsed_s <= self.target_s

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "elapsed_s": round(self.elapsed_s, 4),
            "target_s": self.target_s,
            "passed": self.passed,
            "note": self.note,
        }


T = TypeVar("T")


def _timed(fn: Callable[[], T]) -> tuple[float, T]:
    started = time.perf_counter()
    result = fn()
    return time.perf_counter() - started, result


def _scratch_settings(tmp_dir: Path) -> Settings:
    data_path = tmp_dir / "data"
    return Settings(
        data_path=data_path,
        database_url=f"sqlite:///{(data_path / 'bench.db').as_posix()}",
        log_level="WARNING",
        http_rate_limit_per_s=100_000,
        http_backoff_base_s=0.0,
    )


def _migrated_scratch(tmp_dir: Path) -> Settings:
    settings = _scratch_settings(tmp_dir)
    settings.ensure_directories()
    upgrade_to_head(settings.effective_database_url)
    return settings


# ------------------------------------------------------------ init / sync


def bench_init(tmp_dir: Path) -> BenchmarkResult:
    """`init` completo: migração + primeiro sync do catálogo real (rede de verdade)."""
    settings = _migrated_scratch(tmp_dir / "init")
    engine = engine_from_settings(settings)
    database = Database(engine)
    client = YgoProDeckClient(settings)
    try:
        elapsed, report = _timed(lambda: SyncService(database, client, settings).sync(force=True))
    finally:
        client.close()
        engine.dispose()
    return BenchmarkResult(
        "init completo (catálogo, sem imagens)",
        elapsed,
        180.0,
        note=f"{report.total_cards} cartas, {report.total_prints} prints",
    )


def bench_sync_no_changes(tmp_dir: Path, seeded_settings: Settings) -> BenchmarkResult:
    """`sync` quando a versão remota não mudou — só o check, sem baixar nada."""
    engine = engine_from_settings(seeded_settings)
    database = Database(engine)
    client = YgoProDeckClient(seeded_settings)
    try:
        elapsed, _ = _timed(lambda: SyncService(database, client, seeded_settings).check())
    finally:
        client.close()
        engine.dispose()
    return BenchmarkResult("sync sem mudanças (checkDBVer)", elapsed, 2.0)


# --------------------------------------------------------------- matching


def bench_matching(seeded_settings: Settings) -> list[BenchmarkResult]:
    engine = engine_from_settings(seeded_settings)
    database = Database(engine)
    results: list[BenchmarkResult] = []
    try:
        with database.session() as session:
            index = NameIndex()
            matcher = MatchingEngine(session, seeded_settings, index=index)
            matcher.match("Blue-Eyes White Dragon", "LOB-001")  # aquece o índice em memória

            elapsed, _ = _timed(lambda: matcher.match("Blue-Eyes White Dragon", "LOB-001"))
            results.append(BenchmarkResult("matching tier 0/1 (nome exato)", elapsed, 0.001))

            # Tier 4 (`NameIndex.search`) só carrega os ~14,5k nomes em memória
            # na PRIMEIRA vez que é alcançado (`ensure_loaded`, cacheado depois
            # por contagem de cartas) — sem aquecer aqui, o benchmark mediria
            # esse custo de carga único junto com a busca de verdade.
            matcher.match("Xyzqzq Warmup Name Not A Real Card", None)
            elapsed, _ = _timed(lambda: matcher.match("Blu3-3y3s Wh1t3 Drag0n XYZQZQ", None))
            results.append(BenchmarkResult("matching tier 4 (fuzzy completo)", elapsed, 0.050))
    finally:
        engine.dispose()
    return results


# ---------------------------------------------------------------- scanner


def bench_scan_throughput(
    tmp_dir: Path,
    seeded_settings: Settings,
    *,
    n_images: int,
    workers: int | None = None,
) -> BenchmarkResult:
    from factories import make_card_image  # tests/factories.py

    folder = tmp_dir / "scan_corpus"
    folder.mkdir(parents=True, exist_ok=True)
    names = ["BLUE-EYES WHITE DRAGON", "DARK MAGICIAN", "POT OF GREED", "MIRROR FORCE"]
    for i in range(n_images):
        make_card_image(
            folder / f"card_{i:04d}.jpg", name=names[i % len(names)], set_code="LOB-001"
        )

    from yugioh_scanner.scanner.discovery import discover_images

    discovered = discover_images(folder, recursive=False)
    elapsed, _outcomes = _timed(
        lambda: list(
            run_pipeline(discovered, seeded_settings, provider_name="rapidocr", workers=workers)
        )
    )
    return BenchmarkResult(
        f"scan de {n_images} fotos, {resolve_workers(seeded_settings, workers)} workers, RapidOCR",
        elapsed,
        360.0,
        note=f"{elapsed / n_images:.2f}s/foto",
    )


# -------------------------------------------------------------- coleção


def _seed_collection(session: Session, n_items: int) -> None:
    """Insere `n_items` linhas **distintas** de coleção direto via ORM.

    Não usa `CollectionRepository.add_copies` (upsert por chave lógica): a
    chave (`ux_collection`) não inclui `notes`, só
    `(card_id, card_print_id, condition, edition, language)` — ciclar só o
    `card_id` colidiria bem antes de 5.000 e viraria "poucas linhas com
    quantidade alta" em vez do que o benchmark do plano §20.5 quer medir
    (a LARGURA da tabela, não a soma). Por isso a condição também varia:
    `len(card_ids) × len(CONDITIONS)` cobre até ~10.000 combinações.
    """
    from yugioh_scanner.db.tables import CONDITIONS, CollectionItem

    card_ids = [row[0] for row in session.query(Card.id).limit(2000).all()]
    if not card_ids:
        raise SystemExit("Catálogo vazio — rode com um banco sincronizado.")

    # Um print por carta (o primeiro encontrado) — não precisa ser "o"
    # print certo, só um id válido para a FK; SQLite não tem `DISTINCT ON`.
    print_by_card: dict[int, int] = {}
    for card_id, print_id in session.query(CardPrint.card_id, CardPrint.id).all():
        print_by_card.setdefault(card_id, print_id)

    conditions = list(CONDITIONS)
    combos_per_card = len(conditions)
    if n_items > len(card_ids) * combos_per_card:
        raise SystemExit(
            f"n_items={n_items} excede as combinações distintas disponíveis "
            f"({len(card_ids)} cartas × {combos_per_card} condições)."
        )

    for i in range(n_items):
        card_id = card_ids[i % len(card_ids)]
        condition = conditions[(i // len(card_ids)) % combos_per_card]
        session.add(
            CollectionItem(
                card_id=card_id,
                card_print_id=print_by_card.get(card_id),
                quantity=1,
                condition=condition,
                edition=DEFAULT_EDITION,
                language=DEFAULT_LANGUAGE,
                source="manual",
            )
        )


def bench_collection_list(seeded_settings: Settings, n_items: int) -> list[BenchmarkResult]:
    engine = engine_from_settings(seeded_settings)
    database = Database(engine)
    results: list[BenchmarkResult] = []
    try:
        with database.session() as session:
            _seed_collection(session, n_items)
            session.commit()

            repo = CollectionRepository(session)
            elapsed, items = _timed(lambda: repo.list_filtered(sort="name", limit=None))
            results.append(
                BenchmarkResult(f"collection list com {len(items)} itens", elapsed, 0.100)
            )

            export = ExportService(session)
            elapsed, _ = _timed(
                lambda: export.export_to_string(fmt="csv", profile_name="ygoprodeck")
            )
            results.append(BenchmarkResult(f"export CSV de {len(items)} itens", elapsed, 1.0))
    finally:
        engine.dispose()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--skip-init-sync", action="store_true", help="Pula os benchmarks que precisam de rede."
    )
    parser.add_argument("--scan-images", type=int, default=500)
    parser.add_argument(
        "--workers", type=int, default=None, help="Padrão: automático (metade dos núcleos)."
    )
    parser.add_argument("--collection-items", type=int, default=5000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results: list[BenchmarkResult] = []
    with tempfile.TemporaryDirectory(prefix="yugioh_bench_") as tmp:
        tmp_dir = Path(tmp)

        if not args.skip_init_sync:
            results.append(bench_init(tmp_dir))
            # A cópia migrada+sincronizada do bench_init vira a base dos
            # demais — evita ressincronizar o catálogo inteiro várias vezes.
            seeded_settings = _scratch_settings(tmp_dir / "init")
        else:
            seeded_settings = _migrated_scratch(tmp_dir / "seed")
            engine = engine_from_settings(seeded_settings)
            database = Database(engine)
            client = YgoProDeckClient(seeded_settings)
            try:
                SyncService(database, client, seeded_settings).sync(force=True)
            finally:
                client.close()
                engine.dispose()

        if not args.skip_init_sync:
            results.append(bench_sync_no_changes(tmp_dir / "sync", seeded_settings))

        results.extend(bench_matching(seeded_settings))
        results.append(
            bench_scan_throughput(
                tmp_dir, seeded_settings, n_images=args.scan_images, workers=args.workers
            )
        )
        results.extend(bench_collection_list(seeded_settings, args.collection_items))

    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print(f"\n{'operação':<50} {'medido':>10} {'alvo':>10}  status")
        for r in results:
            status = "OK" if r.passed else "ACIMA DO ALVO"
            note = f"  ({r.note})" if r.note else ""
            print(f"{r.name:<50} {r.elapsed_s:>9.3f}s {r.target_s:>9.3f}s  {status}{note}")


if __name__ == "__main__":
    main()
