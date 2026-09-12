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

import multiprocessing

if __name__ == "__main__":
    # Precisa vir antes de qualquer outra coisa: no `.exe` (PyInstaller,
    # onefile) o `spawn` do multiprocessing relança este mesmo executável
    # como processo filho. Sem isto, o filho não reconhece que deveria
    # rodar como worker — ele executa `main()` de novo (sobe outro servidor
    # Web, tenta abrir a mesma porta) e morre, e o pool então quebra com
    # "A process in the process pool was terminated abruptly...".
    multiprocessing.freeze_support()

    from yugioh_scanner.desktop_launcher import main

    raise SystemExit(main())
