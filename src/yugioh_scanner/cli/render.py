"""Saída de terminal: tabelas Rich e JSON.

Todo comando de leitura aceita `--json`. A regra é: *humanos leem tabela,
scripts leem JSON* — e nenhum dos dois é montado no meio da lógica de negócio.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from rich.console import Console
from rich.table import Table


def _prepare_stream(stream: TextIO) -> TextIO:
    """Tenta colocar o stream em UTF-8 tolerante a erros.

    O console do Windows ainda usa cp1252 em muitas configurações, e um `✓`
    derrubaria o comando com UnicodeEncodeError — falhar ao *imprimir sucesso*
    é o tipo de bug que só aparece na máquina do usuário.
    """
    with contextlib.suppress(AttributeError, OSError, ValueError):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    return stream


def _encodable(text: str, stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


_prepare_stream(sys.stdout)
_prepare_stream(sys.stderr)

#: Símbolos degradam para ASCII quando o terminal não os suporta.
_UNICODE_OK = _encodable("✓✗→", sys.stdout)
OK = "✓" if _UNICODE_OK else "+"
FAIL = "✗" if _UNICODE_OK else "x"
ARROW = "→" if _UNICODE_OK else "->"

console = Console()
error_console = Console(stderr=True)


def print_json(payload: Any) -> None:
    """JSON estável (chaves ordenadas) na saída padrão."""
    console.print_json(json.dumps(payload, default=str, sort_keys=True, ensure_ascii=False))


def print_key_values(title: str, data: Mapping[str, Any]) -> None:
    """Tabela de duas colunas para status e configuração."""
    table = Table(title=title, show_header=False, box=None, padding=(0, 2))
    table.add_column("chave", style="cyan", no_wrap=True)
    table.add_column("valor", style="white", overflow="fold")
    for key, value in data.items():
        table.add_row(key, "" if value is None else str(value))
    console.print(table)


def print_table(
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    empty_message: str = "Nada para mostrar.",
) -> None:
    if not rows:
        console.print(f"[dim]{empty_message}[/dim]")
        return
    table = Table(title=title)
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*("" if cell is None else str(cell) for cell in row))
    console.print(table)


# Mensagens de status vão para **stderr**, não stdout.
#
# A regra é a mesma do logging (§18): stdout carrega dados, stderr carrega
# tudo o mais. É isso que torna `--json | jq` seguro por construção — sem
# depender de cada comando lembrar de silenciar avisos no modo JSON.
def success(message: str) -> None:
    error_console.print(f"[green]{OK}[/green] {message}")


def warn(message: str) -> None:
    error_console.print(f"[yellow]![/yellow] {message}")


def fail(message: str) -> None:
    error_console.print(f"[red]{FAIL}[/red] {message}")


def hint(message: str) -> None:
    """Orientação para o usuário — nunca é dado, então vai para stderr."""
    error_console.print(f"[dim]{message}[/dim]")


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - inalcançável
