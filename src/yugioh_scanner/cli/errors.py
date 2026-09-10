"""Tradução de exceções do domínio em saída de CLI (plano §16).

**O bug que este módulo existe para fechar:** `require_database()` sempre
converteu erro em `fail()` + `typer.Exit()` corretamente, mas qualquer outra
`YugiohScannerError` levantada direto de dentro do corpo de um comando (por
exemplo `resolve_scan_folder()` levantando `InvalidScanPathError`) escapava
**sem tratamento** quando testada via `CliRunner.invoke(app, ...)` — exit
code genérico (1, não o `exit_code` da exceção) e nenhuma mensagem amigável.

Isso não aparecia em produção porque o entry point real (`pyproject.toml` →
`cli.main:run`) tem um `try/except` equivalente — mas os testes chamam o
`app` do Typer diretamente, bypassando `run()`, então exercitavam um caminho
diferente do que o usuário final via. `@handle_errors` fecha os dois: aplica
o mesmo tratamento **dentro** do comando, então `CliRunner` e a CLI real
passam pelo mesmo código.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

import typer

from ..errors import YugiohScannerError
from .render import fail

F = TypeVar("F", bound=Callable[..., Any])


def handle_errors(func: F) -> F:
    """Decorator para comandos Typer: `YugiohScannerError` vira saída limpa.

    Aplicado a **todo** comando que possa tocar banco, matching ou serviços —
    ou seja, a todos exceto os que só formatam texto estático.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except YugiohScannerError as exc:
            fail(str(exc))
            raise typer.Exit(code=exc.exit_code) from None

    return wrapper  # type: ignore[return-value]
