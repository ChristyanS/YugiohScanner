"""Liga descoberta + execução, sem tocar no banco (plano §5.1).

Fica em `scanner/` (infraestrutura) e não em `services/` porque não conhece
coleção nem matching — só transforma arquivos já descobertos em `ScanOutcome`.
Quem decide quais imagens pular por já terem sido processadas é
`services/scan_service.py`, que tem acesso ao banco; este módulo nunca
consulta o SQLite, mantendo a regra de que workers/infra de scan não tocam o
banco (plano §2.3).
"""

from __future__ import annotations

from collections.abc import Iterator

from ..config import Settings
from .discovery import DiscoveredImage
from .executor import StopCheck, create_executor
from .worker import ScanOutcome, ScanTask


def run_pipeline(
    images: list[DiscoveredImage],
    settings: Settings,
    *,
    provider_name: str | None = None,
    workers: int | None = None,
    should_stop: StopCheck | None = None,
) -> Iterator[ScanOutcome]:
    """OCR em paralelo sobre as imagens já filtradas pelo chamador.

    `images` deve conter apenas o que de fato precisa ser processado nesta
    execução — a decisão de pular imagens já vistas é do chamador, que tem
    acesso ao banco.
    """
    tasks = [
        ScanTask(path=image.path, file_hash=image.file_hash, size=image.size) for image in images
    ]
    executor = create_executor(
        settings,
        provider_name=provider_name,
        workers=workers,
        should_stop=should_stop,
    )
    yield from executor.map(tasks)
