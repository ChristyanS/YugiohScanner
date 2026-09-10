"""Ponto de entrada da CLI (plano §8).

A CLI é uma camada fina: traduz argumentos, chama serviços e formata a saída.
Nenhuma regra de negócio mora aqui.

Convenção de exit codes:
  0 — sucesso
  1 — erro de uso/configuração
  2 — erro de execução
"""

from __future__ import annotations

import typer

from .. import __version__
from ..config import get_settings
from ..errors import YugiohScannerError
from ..logging_setup import configure_logging
from . import collection_cmd, db_cmd, export_cmd, review_cmd, scan_cmd, sync_cmd
from .errors import handle_errors
from .render import fail, print_json, print_key_values

app = typer.Typer(
    name="yugioh-scanner",
    help="Scanner e gerenciador de coleção de cartas de Yu-Gi-Oh!.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)

app.add_typer(db_cmd.app, name="db")
app.add_typer(collection_cmd.app, name="collection")

# Comandos de topo: são o fluxo de uso normal, não manutenção.
app.command("init")(sync_cmd.init_command)
app.command("sync")(sync_cmd.sync_command)
app.command("check-updates")(sync_cmd.check_command)
app.command("scan")(scan_cmd.scan_command)
app.command("scan-status")(scan_cmd.scan_status_command)
app.command("review")(review_cmd.review_command)
app.command("export")(export_cmd.export_command)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"yugioh-scanner {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    log_level: str = typer.Option(
        None, "--log-level", help="DEBUG, INFO, WARNING, ERROR. Sobrescreve a config."
    ),
    log_format: str = typer.Option(
        None, "--log-format", help="console ou json. Sobrescreve a config."
    ),
    _version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Mostra a versão."
    ),
) -> None:
    """Configura logging antes de qualquer subcomando rodar."""
    settings = get_settings()
    configure_logging(
        level=log_level or settings.log_level,
        fmt=log_format or settings.log_format,
        log_file=settings.logs_path / "app.log" if settings.logs_path.exists() else None,
    )


@app.command("config")
@handle_errors
def config_cmd(
    action: str = typer.Argument("show", help="Apenas 'show' por enquanto."),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON."),
) -> None:
    """Mostra a configuração efetiva, com segredos mascarados."""
    if action != "show":
        fail(f"Ação desconhecida: {action!r}. Use 'show'.")
        raise typer.Exit(code=1)

    data = get_settings().masked_dump()
    if as_json:
        print_json(data)
    else:
        print_key_values("Configuração efetiva", data)


def run() -> int:
    """Wrapper que traduz exceções do domínio em mensagens e exit codes.

    Existe para que um erro previsto (banco ausente, configuração inválida)
    apareça como uma linha legível em vez de um traceback.
    """
    try:
        app()
    except YugiohScannerError as exc:
        fail(str(exc))
        return exc.exit_code
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
