"""Liga descoberta + execução, sem tocar no banco (plano §5.1).

Fica em `scanner/` (infraestrutura) e não em `services/` porque não conhece
coleção nem matching — só transforma arquivos já descobertos em `ScanOutcome`.
Quem decide quais imagens pular por já terem sido processadas é
`services/scan_service.py`, que tem acesso ao banco; este módulo nunca
consulta o SQLite, mantendo a regra de que workers/infra de scan não tocam o
banco (plano §2.3).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from ..config import Settings
from .discovery import DiscoveredImage
from .executor import DEFAULT_HEARTBEAT_INTERVAL_S, StopCheck, create_executor
from .worker import ScanOutcome, ScanTask

#: Chamado quando nenhuma foto termina dentro de `heartbeat_interval` — só
#: os caminhos ainda em processamento, sem o tipo interno `ScanTask` (que é
#: detalhe de `scanner/`, não precisa vazar para quem chama `run_pipeline`).
HeartbeatCallback = Callable[[list[Path]], None]


def run_pipeline(
    images: list[DiscoveredImage],
    settings: Settings,
    *,
    provider_name: str | None = None,
    workers: int | None = None,
    should_stop: StopCheck | None = None,
    grid_mode: bool = False,
    grid_size: tuple[int, int] | None = None,
    on_heartbeat: HeartbeatCallback | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
) -> Iterator[ScanOutcome]:
    """OCR em paralelo sobre as imagens já filtradas pelo chamador.

    `images` deve conter apenas o que de fato precisa ser processado nesta
    execução — a decisão de pular imagens já vistas é do chamador, que tem
    acesso ao banco. `grid_mode` liga a detecção de grade de cartas (plano
    §22): o fan-out de uma foto em N recortes acontece dentro do worker, não
    aqui — cada foto continua sendo uma `ScanTask` só. `grid_size`, quando
    informado, pula a detecção automática por contorno e divide cada foto em
    linhas×colunas fixas (`--grid-size`, ADR 0011).

    `on_heartbeat`: uma foto de grade processa todas as células numa `Future`
    só (limite do `concurrent.futures` — sem resultado parcial), então numa
    grade lenta o chamador fica sem nenhum evento até a foto inteira acabar.
    `on_heartbeat`, quando informado, é chamado a cada `heartbeat_interval`
    segundos de silêncio com os arquivos ainda em voo, para quem consome
    (Web) poder mostrar "ainda processando" em vez de parecer travado.
    """
    tasks = [
        ScanTask(path=image.path, file_hash=image.file_hash, size=image.size) for image in images
    ]
    executor = create_executor(
        settings,
        provider_name=provider_name,
        workers=workers,
        should_stop=should_stop,
        grid_mode=grid_mode,
        grid_size=grid_size,
    )
    heartbeat_adapter = (
        (lambda pending: on_heartbeat([task.path for task in pending]))
        if on_heartbeat is not None
        else None
    )
    yield from executor.map(
        tasks, on_heartbeat=heartbeat_adapter, heartbeat_interval=heartbeat_interval
    )
