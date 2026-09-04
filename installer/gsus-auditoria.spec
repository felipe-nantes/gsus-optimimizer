# -*- mode: python ; coding: utf-8 -*-
#
# BUILD-001 (DEC-072): o modelo GGUF (~4,92GB) NUNCA é empacotado aqui --
# baixado sozinho na primeira execução (app/analysis/model_downloader.py,
# decisão do usuário 2026-08-25). `runtime/` (llama-server.exe + DLLs, só
# ~20-30MB) já é pequeno o bastante pra ir dentro do instalador -- evita
# mais um download na primeira execução por algo que não pesa nada.
#
# BUILD-001 (DEC-073): Firefox (engine real do GSUS, DEC-010) também vai
# dentro do instalador -- decisão do usuário 2026-08-25 de priorizar
# funcionar offline desde a 1ª execução (só o modelo de IA baixa depois).
# `playwright-browsers/` só tem Firefox (nunca Chromium -- instalado
# direto nessa pasta dedicada, sem herdar o cache global de máquinas de
# dev que também têm Chromium de quando o projeto começou -- DEC-006).

import os

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "..")
RUNTIME_DIR = os.path.join(PROJECT_ROOT, "runtime")
PLAYWRIGHT_BROWSERS_DIR = os.path.join(PROJECT_ROOT, "playwright-browsers")
ICON_PATH = os.path.join(PROJECT_ROOT, "assets", "gsus-auditoria.ico")

# REPORT-004/DEC-099 (matplotlib, 1ª dependência "pesada" do projeto):
# achado real de build -- o hook oficial do PyInstaller pra numpy não
# coletou `numpy._core._exceptions` (nem outros submódulos C de
# `numpy._core`) nesta combinação de versões, e o app quebrava ANTES de
# qualquer log aparecer ("ImportError: No module named
# 'numpy._core._exceptions'"), assim que a janela principal tentava abrir
# os gráficos (`app/ui/main_window.py::_build_charts`, import tardio de
# matplotlib). `collect_submodules('numpy._core')` garante que todo
# submódulo dessa subpasta entra no bundle, independente do hook.
a = Analysis(
    [os.path.join(PROJECT_ROOT, 'app', 'main.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    # O mesmo .ico do recurso do .exe vai também como arquivo ao lado do
    # executável (contents_directory='.' -> <pasta do .exe>\assets\...), porque a
    # janela Tk aplica esse arquivo em runtime (app/ui/icons.py::apply_window_icon,
    # via config.resolve_app_path) -- assim título, barra de tarefas, atalho e
    # Explorer mostram exatamente o mesmo símbolo.
    datas=[(ICON_PATH, "assets")],
    hiddenimports=collect_submodules('numpy._core'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Tree() (não `datas` com glob) -- precisa recursar em subpastas de verdade
# (a instalação do Firefox tem estrutura profunda; `runtime/` é plana, mas
# Tree() funciona igual pros dois, então usa o mesmo mecanismo pra ambos).
runtime_tree = Tree(RUNTIME_DIR, prefix="runtime")
playwright_browsers_tree = Tree(PLAYWRIGHT_BROWSERS_DIR, prefix="playwright-browsers")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='gsus-auditoria',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=ICON_PATH,
    codesign_identity=None,
    entitlements_file=None,
    # PyInstaller 6.x por padrão põe tudo (exceto o .exe) numa subpasta
    # `_internal/` -- `config.get_app_root()` (DEC-059/069) sempre assumiu
    # layout plano (runtime/models/playwright-browsers direto ao lado do
    # .exe). `contents_directory='.'` restaura esse layout -- achado real
    # (build de teste, DEC-073): sem isso, `runtime/`/`playwright-browsers/`
    # ficavam em `_internal/`, e `resolve_app_path` apontava pro lugar errado.
    # Pertence ao EXE(), não ao COLLECT() (2ª tentativa errada, também achado
    # de teste real -- a API do PyInstaller só documenta isso no docstring
    # da classe EXE).
    contents_directory='.',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    runtime_tree,
    playwright_browsers_tree,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gsus-auditoria',
)
