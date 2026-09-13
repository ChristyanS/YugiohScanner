"""Comando `scan-device` (plano §22, ADR 0011) — captura via scanner/impressora.

Captura só resolve "como um arquivo chega no disco"; todo o resto (grade,
OCR, matching, coleção) é o mesmo `ScanService.scan()` de sempre, chamado
através de `_run_scan_and_render()` — o mesmo caminho que `scan_cmd.py` usa.
Nenhuma lógica de identificação é duplicada aqui.
"""

from __future__ import annotations

import datetime as dt

import typer
from rich.console import Console
from rich.prompt import Prompt

from ..capture.registry import create_backend
from ..config import get_settings
from ..images.grid import parse_grid_size
from ..services.scan_service import ScanService
from .context import require_database
from .errors import handle_errors
from .render import console
from .scan_cmd import _run_scan_and_render

#: O prompt entre páginas é interação, não dado — vai para stderr (mesma
#: regra de `review_cmd.py`: stdout é reservado para saída de máquina).
_prompt_console = Console(stderr=True)


@handle_errors
def scan_device_command(
    device: str = typer.Option(
        None, "--device", help="ID do dispositivo. Padrão: o primeiro encontrado."
    ),
    list_devices: bool = typer.Option(
        False, "--list-devices", help="Lista os scanners disponíveis e sai."
    ),
    dpi: int = typer.Option(300, "--dpi", help="Resolução da captura."),
    color: bool = typer.Option(True, "--color/--gray", help="Cor ou tons de cinza."),
    pages: int = typer.Option(
        1,
        "--pages",
        "-n",
        help=(
            "Quantas páginas capturar. Mais de uma página pausa e pede "
            "confirmação antes de cada captura seguinte — dá tempo de trocar "
            "a folha na mesa do scanner (não é ADF com alimentador automático)."
        ),
    ),
    workers: int = typer.Option(
        None, "--workers", "-w", help="Processos de OCR. Padrão: metade dos núcleos (teto 8)."
    ),
    provider: str = typer.Option(
        None, "--provider", "-p", help="Motor de OCR. Padrão: o de YGS_OCR_PROVIDER."
    ),
    grid: bool = typer.Option(
        True,
        "--grid/--no-grid",
        help="Detecta várias cartas por página (ligado por padrão: é o caso comum do scanner).",
    ),
    grid_size: str = typer.Option(
        None,
        "--grid-size",
        help=(
            "Layout exato da grade (LINHASxCOLUNAS, ex.: 3x3), em vez de "
            "detectar por contorno — recomendado: a detecção automática erra "
            "fácil com arte de carta de verdade (ao contrário de imagens "
            "sintéticas de teste)."
        ),
    ),
    apply: bool = typer.Option(
        True,
        "--apply/--dry-run",
        help="Grava na coleção (padrão) ou só mostra o que aconteceria.",
    ),
    no_auto: bool = typer.Option(
        False, "--no-auto", help="Nada é adicionado sozinho; tudo vira pendente."
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Ao terminar, revisa as pendências na hora (mesmo laço de `review`).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Captura página(s) de um scanner Windows (WIA) e identifica na hora.

    Só a captura é nova: a partir do momento em que a página vira um arquivo
    em disco, o processamento é idêntico ao de `scan PASTA --grid`.
    """
    parsed_grid_size = parse_grid_size(grid_size) if grid_size else None
    backend = create_backend("wia")

    if list_devices:
        devices = backend.list_devices()
        if not devices:
            console.print("Nenhum scanner/impressora WIA encontrado.")
            return
        for info in devices:
            console.print(f"{info.id}\t{info.name}")
        return

    settings = get_settings()
    database = require_database(settings)
    try:
        capture_dir = settings.uploads_path / "captures" / _timestamp()
        for page_num in range(1, pages + 1):
            if page_num > 1:
                # Sem isto, a próxima captura dispara na hora — não dá tempo
                # de tirar a folha da mesa do scanner e colocar a próxima
                # (mesa plana, não alimentador automático).
                _prompt_console.print(f"\nColoque a página {page_num} de {pages} no scanner.")
                Prompt.ask(
                    "Pressione Enter para capturar", console=_prompt_console, default=""
                )
            backend.capture(
                capture_dir,
                device_id=device,
                dpi=dpi,
                color_mode="color" if color else "gray",
            )
            if pages > 1:
                _prompt_console.print(f"Página {page_num}/{pages} capturada.")

        service = ScanService(database, settings)
        _run_scan_and_render(
            service,
            capture_dir,
            provider_name=provider,
            workers=workers,
            apply=apply,
            no_auto=no_auto,
            grid=grid,
            grid_size=parsed_grid_size,
            interactive=interactive,
            as_json=as_json,
        )
    finally:
        database.dispose()


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")
