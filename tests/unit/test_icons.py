"""Ícone da janela (DEC-115): a janela Tk deve usar o MESMO `.ico` embutido
no `.exe` e no instalador, resolvido contra a raiz do app (dev: projeto;
frozen: pasta do `.exe`), com fallback pro desenho em PhotoImage quando o
arquivo não existe.

Cria no máximo UM tk.Tk() por teste (várias instâncias no mesmo processo
Python são conhecidas por serem instáveis nesta versão de Tcl/Tk no Windows).
"""
import os
import sys
import tkinter as tk
from pathlib import Path

import pytest

from app import config
from app.ui import icons


def _tk_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def test_window_icon_path_resolves_against_project_root_in_dev():
    path = icons.window_icon_path()
    assert path == config.get_app_root() / "assets" / "gsus-auditoria.ico"
    # O arquivo versionado precisa existir: é o mesmo usado pelo spec/iss.
    assert path.is_file(), f"assets/gsus-auditoria.ico ausente em {path}"


def test_window_icon_path_resolves_against_exe_dir_when_frozen(tmp_path, monkeypatch):
    fake_exe = tmp_path / "GSUS Auditoria" / "gsus-auditoria.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert icons.window_icon_path() == fake_exe.parent / "assets" / "gsus-auditoria.ico"


@pytest.mark.skipif(not _tk_available(), reason="Sem display Tk disponível neste ambiente")
def test_apply_window_icon_prefers_ico_and_falls_back_when_missing(tmp_path, monkeypatch):
    # Um único tk.Tk() cobre os dois caminhos: dois interpretadores Tk no mesmo
    # processo são instáveis neste Windows/Tcl (mesma regra dos outros testes de UI).
    root = tk.Tk()
    try:
        applied = icons.apply_window_icon(root)
        if sys.platform == "win32":
            # No Windows o Tk aceita .ico direto -- é o caminho de produção.
            assert applied is True
            assert not hasattr(root, "_gsus_icon_image")
        else:
            # Fora do Windows, iconbitmap pode recusar .ico -> fallback aceito.
            assert applied in (True, False)

        monkeypatch.setattr(icons, "WINDOW_ICON_RELATIVE_PATH", str(tmp_path / "nao-existe.ico"))
        assert not Path(icons.window_icon_path()).exists()
        applied = icons.apply_window_icon(root)
        assert applied is False
        assert isinstance(root._gsus_icon_image, tk.PhotoImage)
    finally:
        root.destroy()
