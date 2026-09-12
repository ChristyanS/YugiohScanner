# Spec do PyInstaller para o `.exe` desktop da interface Web (Fase 1 do
# faseamento web). Gera um único arquivo (onefile) que sobe o servidor e
# abre o navegador — ver `src/yugioh_scanner/desktop_launcher.py`.
#
# Uso (na raiz do repo, com o venv ativado e `pyinstaller` instalado):
#   pyinstaller packaging/yugioh_scanner_web.spec --noconfirm
#
# Saída: dist/YugiohScanner.exe
#
# Por que onefile: facilidade de distribuir um arquivo só, aceitando o custo
# de extrair para uma pasta temporária a cada execução (escolha do usuário,
# fase 1 do plano de web). Dados do usuário (banco, imagens, logs) NÃO vão
# para essa pasta temporária — `config.py` resolve `%APPDATA%/YugiohScanner`
# quando `sys.frozen` é verdadeiro, então sobrevivem entre execuções.

import os
import sys

block_cipher = None

# SPECPATH (injetado pelo PyInstaller) é a pasta deste .spec — usar isso em
# vez de caminhos relativos "../" evita que o build dependa de onde o
# comando `pyinstaller` foi chamado (raiz do repo vs. dentro de packaging/).
ROOT = os.path.dirname(SPECPATH)

# `collect_submodules()` abaixo importa `yugioh_scanner` de verdade (via
# `importlib`) para listar seus submódulos — não basta o `pathex` do
# `Analysis`, que só vale para a resolução *interna* do modulegraph. Sem
# isto, um venv com o instalável editável quebrado (ex.: instalação
# interrompida) faz `collect_submodules` devolver uma lista vazia **sem
# erro** — o build "funciona" e o .exe quebra com `ModuleNotFoundError` no
# primeiro clique. Inserir `src/` aqui garante que funciona mesmo sem
# depender do estado do `pip install -e .`.
sys.path.insert(0, os.path.join(ROOT, "src"))

from PyInstaller.utils.hooks import collect_data_files, collect_submodules  # noqa: E402

# Alembic não tem hook em pyinstaller-hooks-contrib: sem isto, `command.upgrade`
# falha em runtime tentando importar um `alembic.op`/`alembic.ddl.*` que o
# analisador estático não viu ser usado.
#
# `yugioh_scanner` inteiro também precisa entrar à força: o analisador
# estático só enxerga o que `desktop_launcher.py` importa de verdade, e o
# resto da app é alcançado por import **dinâmico** — `uvicorn.run("yugioh_
# scanner.web.app:create_app", factory=True)` resolve essa string em tempo de
# execução, e `ocr/registry.py` faz o mesmo por provider. Sem isto, o .exe
# builda "com sucesso" e quebra no primeiro clique, com `ModuleNotFoundError`
# vindo de dentro do bootloader — o tipo de falha que só aparece rodando de
# verdade, nunca durante o build.
hidden_imports = [
    *collect_submodules("alembic"),
    *collect_submodules("yugioh_scanner"),
]

datas = [
    (os.path.join(ROOT, "src", "yugioh_scanner", "web", "templates"), "yugioh_scanner/web/templates"),
    (os.path.join(ROOT, "src", "yugioh_scanner", "web", "static"), "yugioh_scanner/web/static"),
    (os.path.join(ROOT, "alembic.ini"), "."),
    # Modelos .onnx + config.yaml do RapidOCR: são dados do pacote, não código
    # Python, então `collect_submodules` (que só enxerga módulos importáveis)
    # não os pega. Sem isto, `RapidOCR()` falha ao abrir os arquivos de
    # modelo no primeiro scan — o servidor sobe normal, só o scan quebra.
    *collect_data_files("rapidocr_onnxruntime"),
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "entrypoint.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

# `migrations/` via Tree (não datas=[...]) para poder excluir __pycache__ —
# um .pyc de outra versão do Python bundlado ali dentro seria só lixo morto,
# nunca executado (o Alembic lê os .py como fonte via `ScriptDirectory`).
migrations_tree = Tree(
    os.path.join(ROOT, "migrations"),
    prefix="migrations",
    excludes=["__pycache__", "*.pyc"],
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    migrations_tree,
    [],
    name="YugiohScanner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
