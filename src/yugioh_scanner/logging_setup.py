"""Logging estruturado (plano §18).

structlog montado **sobre o logging da stdlib**, não ao lado dele. Três
consequências que importam:

1. Eventos do structlog e de bibliotecas de terceiros passam pelos mesmos
   handlers — então `data/logs/app.log` recebe tudo, não metade.
2. Log vai para **stderr**, nunca stdout. O stdout é reservado para dados
   (`--json` precisa ser pipeável para o `jq` sem lixo no meio).
3. O stream é resolvido a cada emissão. Um handler que congela `sys.stderr` na
   construção quebra em qualquer contexto que o substitua (testes, runners de
   CLI, processos longos que reabrem descritores) — e falhar *ao logar* é o
   pior lugar possível para falhar.

O que torna o log realmente diagnosticável é o **contexto vinculado**: dentro de
um job todo evento carrega `job_id`; dentro de uma imagem, também `image`.
Filtrar `job_id=42` conta a história completa daquele scan.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

#: Formatos de segredo que nunca podem chegar ao log, mesmo por acidente.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]

#: Chaves cujo valor é redigido inteiro, independente do formato.
_SECRET_KEYS = frozenset({"api_key", "llm_api_key", "authorization", "x-api-key", "token"})


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(lambda m: f"{m.group(0)[:7]}***{m.group(0)[-4:]}", value)
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Redige segredos em qualquer evento antes de renderizar.

    Última linha de defesa: mesmo que alguém logue um objeto de requisição
    inteiro, a chave não vaza para o arquivo.
    """
    for key, value in event_dict.items():
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = "***"
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


class LiveStderrHandler(logging.StreamHandler):
    """StreamHandler que consulta `sys.stderr` a cada emissão."""

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        # Ignorado de propósito: o stream é sempre o `sys.stderr` do momento.
        return


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
    ]


def _formatter(fmt: str, shared: list[Any]) -> structlog.stdlib.ProcessorFormatter:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )


def configure_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_file: Path | None = None,
    **_ignored: Any,
) -> None:
    """(Re)configura o logging. Seguro para chamar mais de uma vez."""
    shared = _shared_processors()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Sem cache: um logger congelado guardaria handlers já removidos.
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            handler.close()

    console_handler = LiveStderrHandler()
    console_handler.setFormatter(_formatter(fmt, shared))
    root.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8", delay=True
        )
        # O arquivo é sempre JSON: é ele que serve para diagnóstico posterior.
        file_handler.setFormatter(_formatter("json", shared))
        root.addHandler(file_handler)

    root.setLevel(logging.getLevelName(level.upper()))

    # Bibliotecas de terceiros: só o que for problema de verdade.
    for noisy in ("httpx", "httpcore", "alembic", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    """Logger nomeado. Use `__name__` do módulo chamador."""
    return structlog.get_logger(name)


@contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Vincula chaves a todos os eventos emitidos dentro do bloco.

    Uso::

        with log_context(job_id=42):
            log.info("scan.start")   # sai com job_id=42
    """
    tokens = structlog.contextvars.bind_contextvars(**kwargs)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
