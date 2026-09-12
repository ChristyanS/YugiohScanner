"""Ponto de entrada do `.exe` desktop — sobe a Web e abre o navegador sozinho.

Diferente de `yugioh-scanner web` (CLI, plano §8), este módulo assume que
quem está rodando não vai digitar `init`/`db upgrade` na mão: detecta banco
ausente ou desatualizado e resolve sozinho, com progresso no console (a
janela do `.exe` continua sendo um terminal — só o clique inicial muda).

Erros ficam na tela (não fecha a janela sozinho) porque um `.exe` clicado no
Explorer não tem um terminal por trás para mostrar o traceback depois que a
janela some.
"""

from __future__ import annotations

import threading
import webbrowser

from .cli.render import fail, hint, success, warn
from .cli.sync_cmd import _report, _run_sync
from .config import get_settings
from .db.engine import database_exists, engine_from_settings
from .db.migrations import current_revision, head_revision, upgrade_to_head
from .errors import YugiohScannerError
from .logging_setup import configure_logging

#: Tempo para o uvicorn começar a aceitar conexões antes de abrir o navegador.
_BROWSER_OPEN_DELAY_S = 1.5


def _ensure_database_ready() -> None:
    """Cria/atualiza o schema e, na primeira execução, baixa o catálogo."""
    settings = get_settings()
    settings.ensure_directories()

    is_new = not database_exists(settings)
    engine = engine_from_settings(settings)
    needs_upgrade = is_new or current_revision(engine) != head_revision()
    engine.dispose()

    if needs_upgrade:
        success("Preparando o banco de dados local…")
        upgrade_to_head(settings.effective_database_url)

    if is_new:
        warn("Primeira execução: baixando o catálogo de cartas do YGOPRODeck.")
        hint("Isso acontece só uma vez e pode levar alguns minutos.")
        report = _run_sync(force=False, sets_only=False, quiet=False)
        _report(report, as_json=False)


def _open_browser_later(url: str) -> None:
    timer = threading.Timer(_BROWSER_OPEN_DELAY_S, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()


def main() -> int:
    settings = get_settings()
    settings.ensure_directories()
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        log_file=settings.logs_path / "app.log",
    )

    try:
        _ensure_database_ready()
    except YugiohScannerError as exc:
        fail(str(exc))
        input("Pressione Enter para sair…")
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - rede/disco fora do nosso controle
        fail(f"Falha inesperada ao preparar o banco: {exc}")
        input("Pressione Enter para sair…")
        return 2

    import uvicorn

    url = f"http://{settings.web_host}:{settings.web_port}"
    success(f"Abrindo {url} no navegador…")
    hint("Feche esta janela para encerrar o servidor.")
    _open_browser_later(url)

    try:
        uvicorn.run(
            "yugioh_scanner.web.app:create_app",
            factory=True,
            host=settings.web_host,
            port=settings.web_port,
            reload=False,
            log_level=settings.log_level.lower(),
        )
    except Exception as exc:  # pragma: no cover - ex.: porta já em uso
        fail(f"Não foi possível subir o servidor: {exc}")
        input("Pressione Enter para sair…")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
