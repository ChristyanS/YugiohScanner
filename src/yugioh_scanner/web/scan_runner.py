"""Execução de scans em segundo plano para a Web, com progresso ao vivo (plano §9, §10.2).

`ScanService.scan()` é síncrono e bloqueante — por baixo ele orquestra um
`ProcessPoolExecutor` (plano §12) — então não dá para chamá-lo direto de um
handler `async` sem travar o loop de eventos por toda a duração do scan. A
solução: cada scan roda numa thread própria; o callback de progresso empurra
eventos para uma `asyncio.Queue` que o endpoint SSE consome.

Deliberadamente **não** usa o `job_id` do `ScanJob` como identificador do scan
em andamento: `ScanService._run()` só cria o `ScanJob` depois de descobrir e
filtrar as imagens por hash (pode não sobrar trabalho nenhum), e mudar essa
ordem só para a Web tocaria código já testado da Fase 5 por uma economia
pequena. Em vez disso, `start()` devolve um `run_id` opaco (UUID) — é por ele
que o cliente abre o SSE e pede cancelamento. O `job_id` de verdade chega no
evento final (`type="done"`), assim que `ScanService.scan()` retorna.

Cancelamento é cooperativo, sem tocar `scan_service.py`: o callback de
progresso já é chamado depois de **cada** imagem processada (é o mesmo gancho
que alimenta a barra de progresso do CLI). Se o run foi marcado para
cancelar, o callback levanta `_ScanCancelledError`; `ScanService.scan()` faz
`session.rollback()` do lote ainda não commitado (no máximo
`COMMIT_BATCH_SIZE` imagens) e propaga — o que já estava commitado fica.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db.session import Database
from ..logging_setup import get_logger
from ..services.scan_service import ImageEvent, ScanRunReport, ScanService

log = get_logger(__name__)

#: Quanto tempo um run concluído fica visível no registro depois de acabar —
#: só o suficiente para um cliente que reconecta (refresh da aba) ainda ver o
#: resultado final, sem acumular runs antigos para sempre num processo longo.
RETENTION_S = 300.0


class _ScanCancelledError(Exception):
    """Sinal interno — nunca escapa de `ScanRunnerRegistry`."""


@dataclass
class ScanRun:
    run_id: str
    queue: asyncio.Queue[dict[str, Any] | None]
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    finished: bool = False
    report: ScanRunReport | None = None
    error: str | None = None
    cancelled: bool = False
    _cancel_requested: threading.Event = field(default_factory=threading.Event)


class ScanRunnerRegistry:
    """Um por processo (`app.state.scan_runner`): registro em memória dos
    scans iniciados pela Web. Não sobrevive a um restart do servidor — um scan
    em andamento é, por natureza, efêmero; o histórico de verdade é o
    `ScanJob` persistido, consultável por `GET /api/v1/scans`."""

    def __init__(self) -> None:
        self._runs: dict[str, ScanRun] = {}
        self._lock = threading.Lock()

    def start(
        self,
        database: Database,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        *,
        folder: Path | str,
        provider_name: str | None,
        workers: int | None,
        recursive: bool,
        no_auto: bool,
        reprocess: bool,
        grid: bool = False,
        grid_size: tuple[int, int] | None = None,
        require_set: bool | None = None,
        require_rarity: bool | None = None,
    ) -> ScanRun:
        run = ScanRun(run_id=uuid.uuid4().hex, queue=asyncio.Queue())
        with self._lock:
            self._evict_finished_locked()
            self._runs[run.run_id] = run

        service = ScanService(database, settings)

        def on_event(event: ImageEvent) -> None:
            if run._cancel_requested.is_set():
                raise _ScanCancelledError
            loop.call_soon_threadsafe(run.queue.put_nowait, {"type": "image", **event.as_dict()})

        def on_heartbeat(pending_paths: list[Path]) -> None:
            # Uma foto de grade lenta (várias cartas numa `Future` só, ver
            # `scanner/pipeline.py::run_pipeline`) fica sem nenhum evento
            # `on_event` até processar todas as células — este pulso avisa o
            # cliente que o scan continua vivo em vez de parecer travado.
            loop.call_soon_threadsafe(
                run.queue.put_nowait,
                {"type": "heartbeat", "files": [path.name for path in pending_paths]},
            )

        def target() -> None:
            payload: dict[str, Any]
            try:
                report = service.scan(
                    folder,
                    provider_name=provider_name,
                    workers=workers,
                    recursive=recursive,
                    apply=True,
                    no_auto=no_auto,
                    reprocess=reprocess,
                    grid=grid,
                    grid_size=grid_size,
                    require_set=require_set,
                    require_rarity=require_rarity,
                    progress=on_event,
                    on_heartbeat=on_heartbeat,
                )
                run.report = report
                payload = {"type": "done", **report.as_dict()}
            except _ScanCancelledError:
                run.cancelled = True
                payload = {"type": "cancelled"}
            except Exception as exc:  # domínio ou infra — o SSE precisa saber de qualquer uma
                run.error = str(exc)
                payload = {"type": "error", "message": str(exc)}
                log.error("web.scan_failed", run_id=run.run_id, error=str(exc))
            finally:
                run.finished = True
                run.finished_at = time.monotonic()
                loop.call_soon_threadsafe(run.queue.put_nowait, payload)
                loop.call_soon_threadsafe(run.queue.put_nowait, None)  # sentinela: fecha o stream

        threading.Thread(target=target, daemon=True, name=f"scan-{run.run_id[:8]}").start()
        return run

    def get(self, run_id: str) -> ScanRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def cancel(self, run_id: str) -> bool:
        """Pede o cancelamento. Devolve `False` se o run não existe ou já terminou."""
        run = self.get(run_id)
        if run is None or run.finished:
            return False
        run._cancel_requested.set()
        return True

    def _evict_finished_locked(self) -> None:
        now = time.monotonic()
        stale = [
            run_id
            for run_id, run in self._runs.items()
            if run.finished_at is not None and (now - run.finished_at) > RETENTION_S
        ]
        for run_id in stale:
            del self._runs[run_id]
