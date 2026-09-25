"""Estratégias de paralelismo do scanner (plano §12).

O ponto central: **"OCR" não é um tipo de carga só.**

* OCR local é *CPU-bound*. Threads não ajudam (o GIL as serializa); é preciso
  processo separado.
* OCR por LLM é *I/O-bound*. Processos seriam desperdício — o tempo é passado
  esperando rede.

Usar a mesma estratégia para os dois é o erro clássico, e é por isso que
`ScanExecutor` tem implementações distintas escolhidas automaticamente a partir
de `provider.is_io_bound`.

Desvio consciente do plano: para a carga I/O-bound o plano previa `asyncio` +
`httpx.AsyncClient`. Como o `OCRProvider` é uma interface **síncrona**, um pool
de threads entrega o mesmo paralelismo de espera sem exigir uma segunda versão
assíncrona de cada provider. Se algum dia um provider for nativamente async, a
troca fica confinada a este arquivo.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from typing import Protocol

from ..config import MAX_WORKERS, Settings
from ..logging_setup import get_logger
from ..ocr.base import OCRProvider
from ..ocr.registry import create_provider
from .worker import ScanOutcome, ScanTask, init_worker, process_task, set_provider

log = get_logger(__name__)

#: Chamado para saber se o job foi cancelado (a UI web usa isso).
StopCheck = Callable[[], bool]

#: Chamado quando nenhuma tarefa terminou dentro de `heartbeat_interval` —
#: recebe as tarefas ainda em voo. Existe só para a Web mostrar "ainda
#: processando" numa foto de grade lenta (várias cartas por foto, plano
#: §22): o pool só devolve resultado quando a `Future` inteira termina (nunca
#: parcial, é limite do `concurrent.futures`), então sem isto o log fica em
#: silêncio total enquanto uma foto de 9 células é lida.
HeartbeatCallback = Callable[[list[ScanTask]], None]

#: Intervalo padrão entre pulsos — curto o bastante para não parecer travado
#: numa foto de grade lenta, longo o bastante para não virar spam numa foto
#: rápida de carta única (que quase nunca dispara heartbeat nenhum).
DEFAULT_HEARTBEAT_INTERVAL_S = 2.0


class ScanExecutor(Protocol):
    """Recebe tarefas, devolve resultados conforme ficam prontos."""

    workers: int

    def map(
        self,
        tasks: Sequence[ScanTask],
        *,
        on_heartbeat: HeartbeatCallback | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ) -> Iterator[ScanOutcome]: ...

    def shutdown(self) -> None: ...


class SerialScanExecutor:
    """Sem paralelismo. Usado com `--workers 1`, em debug e nos testes.

    É também a referência de comportamento: se o resultado do pool divergir do
    serial, o bug está no paralelismo.
    """

    workers = 1

    def __init__(
        self,
        provider: OCRProvider,
        settings: Settings,
        *,
        should_stop: StopCheck | None = None,
        grid_mode: bool = False,
        grid_size: tuple[int, int] | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._should_stop = should_stop
        self._grid_mode = grid_mode
        self._grid_size = grid_size

    def map(
        self,
        tasks: Sequence[ScanTask],
        *,
        on_heartbeat: HeartbeatCallback | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ) -> Iterator[ScanOutcome]:
        # `on_heartbeat`/`heartbeat_interval` só existem para o protocolo
        # bater com `PoolScanExecutor` — sem paralelismo não há como avisar
        # "ainda processando" no meio de uma tarefa (é a mesma limitação do
        # pool, só que sem outras tarefas concorrentes para dar timeout
        # contra elas). `--workers 1` já é o caminho de debug/teste (ver
        # docstring da classe); aceitar e não usar é o comportamento certo.
        del on_heartbeat, heartbeat_interval
        set_provider(
            self._provider, self._settings, grid_mode=self._grid_mode, grid_size=self._grid_size
        )
        self._provider.warmup()
        for task in tasks:
            if self._should_stop is not None and self._should_stop():
                return
            yield process_task(task)

    def shutdown(self) -> None:
        self._provider.close()


class PoolScanExecutor:
    """Base dos executores com pool, com janela deslizante de submissão.

    Enfileirar 5.000 futures de uma vez faria a memória crescer com o tamanho
    da pasta. A janela (`workers × 2`) mantém o consumo constante e ainda deixa
    todos os workers ocupados.
    """

    def __init__(
        self,
        workers: int,
        *,
        should_stop: StopCheck | None = None,
        grid_mode: bool = False,
        grid_size: tuple[int, int] | None = None,
    ) -> None:
        self.workers = max(1, workers)
        self._should_stop = should_stop
        self._grid_mode = grid_mode
        self._grid_size = grid_size
        self._executor: Executor | None = None

    def _create_executor(self) -> Executor:  # pragma: no cover - abstrato
        raise NotImplementedError

    def map(
        self,
        tasks: Sequence[ScanTask],
        *,
        on_heartbeat: HeartbeatCallback | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ) -> Iterator[ScanOutcome]:
        if not tasks:
            return

        self._executor = self._create_executor()
        pending: set[Future[ScanOutcome]] = set()
        # Só para o heartbeat saber QUAIS arquivos estão em voo quando o
        # timeout bate sem nenhuma `Future` pronta — `wait()` devolve só as
        # próprias `Future`s, sem essa associação.
        task_by_future: dict[Future[ScanOutcome], ScanTask] = {}
        queue = iter(tasks)
        window = self.workers * 2
        # Sem `on_heartbeat`, mantém o `wait()` bloqueando para sempre (como
        # antes desta mudança) — só entra em modo "acorda de N em N segundos
        # pra checar" quando alguém pediu o pulso; ninguém observa diferença
        # nenhuma se não passar `on_heartbeat`.
        wait_timeout = heartbeat_interval if on_heartbeat is not None else None

        try:
            while True:
                while len(pending) < window:
                    task = next(queue, None)
                    if task is None:
                        break
                    future = self._executor.submit(process_task, task)
                    pending.add(future)
                    task_by_future[future] = task

                if not pending:
                    return

                done, pending = wait(pending, timeout=wait_timeout, return_when=FIRST_COMPLETED)
                if not done:
                    # Só acontece com `wait_timeout` setado, ou seja, só
                    # quando `on_heartbeat` não é `None` — o pool inteiro
                    # ainda está processando a(s) foto(s) em voo (grade
                    # lenta, várias células numa `Future` só) sem nenhuma
                    # terminar ainda; avisa quais arquivos são essas.
                    if on_heartbeat is not None:
                        on_heartbeat([task_by_future[f] for f in pending])
                    continue
                for future in done:
                    task_by_future.pop(future, None)
                    yield future.result()

                if self._should_stop is not None and self._should_stop():
                    log.info("scan.cancelled", pending=len(pending))
                    for future in pending:
                        future.cancel()
                    return
        except KeyboardInterrupt:
            # Ctrl+C: encerra limpo preservando o que já foi processado.
            log.warning("scan.interrupted")
            for future in pending:
                future.cancel()
            raise
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(cancel_futures=True)
            self._executor = None


class ProcessPoolScanExecutor(PoolScanExecutor):
    """Para OCR local (CPU-bound).

    O `initializer` é o que garante que o modelo seja carregado **uma vez por
    worker**. No Windows (`spawn`) cada worker reimporta tudo do zero, então
    sem isso o custo de 1–2 s do modelo apareceria em cada imagem.
    """

    def __init__(
        self,
        provider_name: str,
        settings: Settings,
        workers: int,
        *,
        should_stop: StopCheck | None = None,
        grid_mode: bool = False,
        grid_size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(workers, should_stop=should_stop, grid_mode=grid_mode, grid_size=grid_size)
        self._provider_name = provider_name
        # Cada worker fica com uma thread: o paralelismo vem dos processos.
        # Sem isso os workers disputam os mesmos núcleos e o scan em paralelo
        # fica MAIS LENTO que o serial (medido antes da correção).
        self._settings = settings.model_copy(
            update={"ocr_threads_per_worker": settings.ocr_threads_per_worker or 1}
        )

    def _create_executor(self) -> Executor:
        log.info(
            "scan.pool_started",
            kind="process",
            workers=self.workers,
            provider=self._provider_name,
        )
        return ProcessPoolExecutor(
            max_workers=self.workers,
            initializer=_process_initializer,
            initargs=(self._provider_name, self._settings, self._grid_mode, self._grid_size),
        )


class ThreadPoolScanExecutor(PoolScanExecutor):
    """Para OCR remoto (I/O-bound): o tempo é espera de rede, não CPU."""

    def __init__(
        self,
        provider: OCRProvider,
        settings: Settings,
        workers: int,
        *,
        should_stop: StopCheck | None = None,
        grid_mode: bool = False,
        grid_size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(workers, should_stop=should_stop, grid_mode=grid_mode, grid_size=grid_size)
        self._provider = provider
        self._settings = settings

    def _create_executor(self) -> Executor:
        # Threads compartilham o processo, então basta um provider — e ele
        # precisa ser thread-safe, o que providers de rede naturalmente são.
        set_provider(
            self._provider, self._settings, grid_mode=self._grid_mode, grid_size=self._grid_size
        )
        self._provider.warmup()
        log.info(
            "scan.pool_started",
            kind="thread",
            workers=self.workers,
            provider=self._provider.name,
        )
        return ThreadPoolExecutor(max_workers=self.workers)

    def shutdown(self) -> None:
        super().shutdown()
        self._provider.close()


def _process_initializer(
    provider_name: str,
    settings: Settings,
    grid_mode: bool = False,
    grid_size: tuple[int, int] | None = None,
) -> None:
    """Roda uma vez em cada subprocesso, antes da primeira tarefa."""
    # Complementa o `intra_op_num_threads` do provider: se alguma dependência
    # usar OpenMP (numpy, BLAS), ela também fica com uma thread só.
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    init_worker(provider_name, settings, grid_mode=grid_mode, grid_size=grid_size)


def resolve_workers(settings: Settings, requested: int | None = None) -> int:
    """Quantos workers usar: pedido explícito, ou automático com teto."""
    if requested is not None:
        return max(1, min(requested, MAX_WORKERS))
    return settings.effective_workers


def create_executor(
    settings: Settings,
    *,
    provider_name: str | None = None,
    workers: int | None = None,
    should_stop: StopCheck | None = None,
    provider: OCRProvider | None = None,
    grid_mode: bool = False,
    grid_size: tuple[int, int] | None = None,
) -> ScanExecutor:
    """Escolhe a estratégia certa a partir do tipo de carga do provider.

    Quem chama nunca precisa saber qual usar — passar `--workers 1` força o
    serial, e o resto é decidido por `provider.is_io_bound`.
    """
    name = provider_name or settings.ocr_provider
    resolved_workers = resolve_workers(settings, workers)

    # Precisamos de uma instância para consultar `is_io_bound`. Em pool de
    # processos ela é descartada: cada worker cria a sua.
    probe = provider or create_provider(name, settings)

    if resolved_workers == 1:
        return SerialScanExecutor(
            probe, settings, should_stop=should_stop, grid_mode=grid_mode, grid_size=grid_size
        )

    if probe.is_io_bound:
        return ThreadPoolScanExecutor(
            probe,
            settings,
            resolved_workers,
            should_stop=should_stop,
            grid_mode=grid_mode,
            grid_size=grid_size,
        )

    if provider is not None:
        # Um provider injetado (testes, fake) não sobrevive ao `spawn`: o
        # subprocesso não teria como recriá-lo. Rodar em threads mantém o
        # comportamento observável sem mentir sobre o paralelismo.
        return ThreadPoolScanExecutor(
            probe,
            settings,
            resolved_workers,
            should_stop=should_stop,
            grid_mode=grid_mode,
            grid_size=grid_size,
        )

    probe.close()
    return ProcessPoolScanExecutor(
        name,
        settings,
        resolved_workers,
        should_stop=should_stop,
        grid_mode=grid_mode,
        grid_size=grid_size,
    )


def run_tasks(
    tasks: Iterable[ScanTask],
    settings: Settings,
    **kwargs: object,
) -> Iterator[ScanOutcome]:
    """Atalho: cria o executor, processa e encerra."""
    executor = create_executor(settings, **kwargs)  # type: ignore[arg-type]
    yield from executor.map(list(tasks))
