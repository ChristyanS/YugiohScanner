"""Comando `export` (plano §8, §15 e Fase 7).

Cada perfil sabe seu próprio encoding/newline (ver `exporters/base.py`) — por
isso o arquivo é sempre aberto com esses valores exatos, e a saída padrão
(sem `-o`) escreve os mesmos bytes direto no stream binário do stdout, sem
deixar o modo texto do Windows reescrever `\\n`/`\\r\\n` no meio do caminho.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from ..exporters import create_profile, known_profiles
from .context import export_service
from .errors import handle_errors
from .render import fail, print_table, success


@handle_errors
def export_command(
    fmt: str = typer.Option(
        None, "--format", "-f", help="csv ou txt. Obrigatório, salvo com --list-profiles."
    ),
    profile: str = typer.Option(
        None, "--profile", "-p", help="Nome do perfil. Padrão: o perfil principal do formato."
    ),
    output: Path = typer.Option(
        None, "-o", "--output", help="Arquivo de destino. Sem isso, escreve no stdout."
    ),
    skip_unresolved: bool = typer.Option(
        False,
        "--skip-unresolved",
        help="Omite itens sem print identificado, em vez de exportar com set vazio.",
    ),
    list_profiles: bool = typer.Option(
        False, "--list-profiles", help="Lista os perfis disponíveis e sai."
    ),
) -> None:
    """Exporta a coleção. Perfis verificados contra exports reais (plano §0.4)."""
    if list_profiles:
        _print_profiles()
        return

    if fmt is None:
        fail("--format é obrigatório (csv ou txt). Ou use --list-profiles.")
        raise typer.Exit(code=1)

    with export_service() as service:
        if output is not None:
            report = service.export_to_path(
                output, fmt=fmt, profile_name=profile, skip_unresolved=skip_unresolved
            )
            success(
                f"{report.rows_written} linha(s) escrita(s) em {report.path} "
                f"(perfil {report.profile}, {report.rows_skipped} omitida(s))"
            )
            return

        text, resolved_profile = service.export_to_string(
            fmt=fmt, profile_name=profile, skip_unresolved=skip_unresolved
        )

    sys.stdout.buffer.write(text.encode(resolved_profile.encoding))
    sys.stdout.buffer.flush()


def _print_profiles() -> None:
    rows = [
        [f"{f}:{name}", ", ".join(_columns_for(f, name)) or "(texto livre)"]
        for f, name in known_profiles()
    ]
    print_table("Perfis de exportação", ["Perfil", "Colunas"], rows)


def _columns_for(fmt: str, name: str) -> list[str]:
    return create_profile(fmt, name).columns
