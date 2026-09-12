"""Alvo do PyInstaller — nunca é o `desktop_launcher.py` diretamente.

O PyInstaller sempre executa o script apontado em `Analysis([...])` como
`__main__`, sem contexto de pacote — os imports relativos (`from .cli...`)
de `desktop_launcher.py` quebrariam com `ImportError: attempted relative
import with no known parent package`. Este shim importa o pacote de verdade
por caminho absoluto, então `desktop_launcher.py` roda como o submódulo que
é, com seus imports relativos intactos — igual a `python -m
yugioh_scanner.desktop_launcher`, que é exatamente o que isto reproduz.
"""

from __future__ import annotations

from yugioh_scanner.desktop_launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
