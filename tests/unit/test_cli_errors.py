"""`handle_errors` — a tradução de `YugiohScannerError` em saída de CLI.

Existe porque o bug que este decorator fecha já escapou uma vez: uma
`YugiohScannerError` levantada direto do corpo de um comando ficava sem
tratamento quando testada via `CliRunner.invoke(app, ...)`, mesmo com o
entry point real (`cli.main:run`) parecendo "funcionar" — os dois caminhos
divergiam. Ver `cli/errors.py` para o relato completo.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from yugioh_scanner.cli.errors import handle_errors
from yugioh_scanner.errors import CollectionError, YugiohScannerError

runner = CliRunner()


def _build_app() -> typer.Typer:
    app = typer.Typer()

    @app.command()
    @handle_errors
    def boom(code: int = typer.Option(2, "--code")) -> None:
        error = CollectionError("algo deu errado")
        error.exit_code = code
        raise error

    @app.command()
    @handle_errors
    def fine() -> None:
        typer.echo("tudo bem")

    return app


class TestHandleErrors:
    def test_domain_error_is_translated_not_raised(self) -> None:
        result = runner.invoke(_build_app(), ["boom"])
        assert not isinstance(result.exception, YugiohScannerError)

    def test_exit_code_matches_the_exception(self) -> None:
        result = runner.invoke(_build_app(), ["boom", "--code", "3"])
        assert result.exit_code == 3

    def test_message_is_printed(self) -> None:
        result = runner.invoke(_build_app(), ["boom"])
        assert "algo deu errado" in result.output

    def test_success_path_is_untouched(self) -> None:
        result = runner.invoke(_build_app(), ["fine"])
        assert result.exit_code == 0
        assert "tudo bem" in result.output

    def test_signature_survives_for_typer_introspection(self) -> None:
        """Se o decorator escondesse a assinatura, `--help` perderia as opções."""
        result = runner.invoke(_build_app(), ["boom", "--help"])
        assert result.exit_code == 0
        assert "--code" in result.output
