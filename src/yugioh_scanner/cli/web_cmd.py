"""Comando `web` — sobe a interface web (plano §8 e Fase 8)."""

from __future__ import annotations

import typer

from ..config import get_settings
from .context import require_database
from .errors import handle_errors
from .render import hint, success, warn


@handle_errors
def web_command(
    host: str = typer.Option(None, "--host", help="Padrão: web_host da config (127.0.0.1)."),
    port: int = typer.Option(None, "--port", help="Padrão: web_port da config (8000)."),
    reload: bool = typer.Option(False, "--reload", help="Recarrega ao editar código (dev)."),
) -> None:
    """Sobe a interface web. Requer as dependências opcionais `[web]`."""
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter(
            'Dependências da interface web não instaladas. Rode: pip install -e ".[web]"'
        ) from exc

    settings = get_settings()
    # Mesma checagem de banco/schema dos outros comandos — falhar aqui, com
    # mensagem acionável, é melhor que a app subir e cada rota 500 sozinha.
    require_database(settings).dispose()

    resolved_host = host or settings.web_host
    resolved_port = port or settings.web_port

    if resolved_host not in ("127.0.0.1", "localhost", "::1"):
        warn(f"Bind em {resolved_host} — esta app não tem autenticação (plano §21).")
        hint("Use 127.0.0.1 a menos que saiba exatamente o que está fazendo.")

    success(f"Subindo em http://{resolved_host}:{resolved_port}")
    uvicorn.run(
        "yugioh_scanner.web.app:create_app",
        factory=True,
        host=resolved_host,
        port=resolved_port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )
