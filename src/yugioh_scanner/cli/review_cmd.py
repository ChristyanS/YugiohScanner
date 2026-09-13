"""Comando `review` — fila interativa de pendências (plano §8 e §12).

Chamado de `yugioh-scanner review` na CLI. O plano nomeia isso `scan review`,
mas `scan` já é um comando que recebe uma pasta como argumento posicional
(`scan PASTA`) — no Typer/Click, um comando não pode ser ao mesmo tempo um
grupo com subcomandos (`scan review`, `scan status`) e um comando posicional,
sem quebrar `scan ./pasta` (o Click tentaria interpretar `./pasta` como nome
de subcomando). Por isso `review` e `scan-status` são comandos de topo,
paralelos a `scan` — mesma função, nome de invocação diferente do plano.

O laço interativo (`run_interactive_review`) é compartilhado com
`scan --interactive` (plano §10.2: confirmar pendências sem abrir o
navegador, logo após o scan terminar) — uma função só, dois pontos de entrada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console
from rich.prompt import Prompt

from ..config import get_settings
from ..db.tables import ScanResult
from ..services.scan_service import ScanService
from .context import require_database
from .errors import handle_errors
from .render import console, print_json, print_table, success, warn

#: O prompt é interação, não dado — vai para stderr (mesma regra do resto
#: da CLI: stdout é reservado para saída de máquina).
_prompt_console = Console(stderr=True)


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    confirmed: int
    rejected: int
    skipped: int

    @property
    def total(self) -> int:
        return self.confirmed + self.rejected + self.skipped


def _candidates_of(result: ScanResult) -> list[dict[str, Any]]:
    return list(result.candidates or [])


def _label(result: ScanResult) -> str:
    """Nome do arquivo, com `[i/N]` quando a foto rendeu mais de um recorte
    (grade de cartas, plano §22) — senão duas pendências da mesma foto
    apareceriam idênticas na fila, sem como saber qual carta é qual."""
    path = result.scan_image.file_path
    if result.crop_count > 1:
        return f"{path} [{result.crop_index + 1}/{result.crop_count}]"
    return path


def _render_result(result: ScanResult) -> None:
    console.print(f"\n[bold]{_label(result)}[/bold]")
    console.print(f"  OCR nome: [dim]{result.ocr_name_raw or '(vazio)'}[/dim]")
    console.print(f"  OCR set:  [dim]{result.ocr_code_raw or '(vazio)'}[/dim]")
    console.print(
        f"  decisão: {result.decision}  confiança: {result.confidence:.0%}  "
        f"margem: {result.margin:.0%}"
    )
    candidates = _candidates_of(result)
    if not candidates:
        warn("  Nenhum candidato — só dá para pular ou rejeitar.")
        return
    for index, candidate in enumerate(candidates, start=1):
        marker = " ←" if candidate.get("card_id") == result.card_id else ""
        console.print(f"  {index}. {candidate['name']}  ({candidate['score']:.0%}){marker}")


def _ask_action(has_candidates: bool) -> str:
    options = "1-5 escolhe" if has_candidates else ""
    hint = ", ".join(part for part in (options, "s pula", "x rejeita", "q sai") if part)
    return Prompt.ask(f"  [{hint}]", console=_prompt_console, default="s")


def run_interactive_review(service: ScanService, results: list[ScanResult]) -> ReviewOutcome:
    """O laço de revisão em si — usado por `review` e por `scan --interactive`.

    Recebe a lista já buscada (não busca sozinho) para que quem chama decida
    o escopo: `review` pega tudo que está pendente no banco; `scan
    --interactive` pode restringir ao que acabou de sair deste job.
    """
    confirmed = rejected = skipped = 0
    for result in results:
        _render_result(result)
        candidates = _candidates_of(result)
        action = _ask_action(bool(candidates)).strip().lower()

        if action in ("q", "quit"):
            break
        if action in ("x", "reject", "rejeitar"):
            service.reject_result(result.id)
            rejected += 1
            continue
        if action in ("", "s", "skip", "pular"):
            skipped += 1
            continue
        if action.isdigit() and candidates:
            position = int(action)
            if 1 <= position <= len(candidates):
                chosen = candidates[position - 1]
                # Só sabemos o print quando a escolha é a mesma carta que a
                # matching engine já havia resolvido — outro candidato não
                # carrega informação de print (`NameCandidate` não inclui
                # prints, plano §7). Fica indefinido, resolvível depois com
                # `collection set-print`.
                print_id = result.card_print_id if chosen["card_id"] == result.card_id else None
                service.confirm_result(result.id, card_id=chosen["card_id"], card_print_id=print_id)
                confirmed += 1
                continue
        warn("  Opção inválida — pulando.")
        skipped += 1

    return ReviewOutcome(confirmed=confirmed, rejected=rejected, skipped=skipped)


def print_review_outcome(outcome: ReviewOutcome) -> None:
    print_table(
        "Revisão",
        ["confirmadas", "rejeitadas", "puladas"],
        [[outcome.confirmed, outcome.rejected, outcome.skipped]],
    )


@handle_errors
def review_command(
    limit: int = typer.Option(50, "--limit", "-n", help="Máximo de pendências na fila."),
    as_json: bool = typer.Option(False, "--json", help="Só lista as pendências (sem interação)."),
) -> None:
    """Revisa leituras que ficaram `pending`/`manual` — confirma ou rejeita.

    Sem `--json`, entra num laço interativo: mostra a foto, o texto bruto do
    OCR e os candidatos, e espera você escolher. Com `--json`, só lista a
    fila (para inspeção ou para uma UI futura consumir).
    """
    settings = get_settings()
    database = require_database(settings)
    try:
        service = ScanService(database, settings)
        results = service.pending_results(limit=limit)

        if as_json:
            print_json(
                [
                    {
                        "result_id": result.id,
                        "file": result.scan_image.file_path,
                        "crop_index": result.crop_index,
                        "crop_count": result.crop_count,
                        "ocr_name": result.ocr_name_raw,
                        "ocr_code": result.ocr_code_raw,
                        "decision": result.decision,
                        "confidence": round(result.confidence, 4),
                        "margin": round(result.margin, 4),
                        "candidates": _candidates_of(result),
                    }
                    for result in results
                ]
            )
            return

        if not results:
            success("Nada para revisar.")
            return

        print_review_outcome(run_interactive_review(service, results))
    finally:
        database.dispose()
