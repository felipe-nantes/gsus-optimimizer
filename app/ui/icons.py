"""Icones vetoriais simples desenhados pelo proprio Tkinter.

Nao dependem de fonte de simbolos, arquivo externo ou internet. Isso mantem
os icones consistentes no executavel empacotado e em qualquer Windows.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path

from app import config

# Mesmo arquivo usado no recurso de icone do .exe (PyInstaller `icon=`) e no
# instalador (Inno Setup `SetupIconFile`) -- gerado por scripts/generate_app_icon.py.
WINDOW_ICON_RELATIVE_PATH = "assets/gsus-auditoria.ico"


def window_icon_path() -> Path:
    """Caminho do `.ico` multirresolucao da marca.

    Resolvido contra a raiz do app (`config.get_app_root`, DEC-069): em dev e a
    pasta do projeto; no executavel empacotado e a pasta do proprio `.exe`, para
    onde o spec do PyInstaller copia `assets/gsus-auditoria.ico` (`datas`)."""
    return config.resolve_app_path(WINDOW_ICON_RELATIVE_PATH)


def apply_window_icon(root: tk.Tk) -> bool:
    """Aplica a marca no titulo da janela e na barra de tarefas.

    Prefere o mesmo `.ico` embutido no executavel e no instalador (cantos
    arredondados, fundo transparente, todas as resolucoes de 16 a 256 px), para
    que janela, barra de tarefas, atalho e Explorer mostrem o mesmo simbolo.
    Se o arquivo nao existir ou o Tk desta plataforma nao aceitar `.ico`, cai no
    desenho de 32 px em PhotoImage, que nao depende de arquivo externo.
    Devolve True quando o `.ico` foi aplicado."""
    icon_path = window_icon_path()
    if icon_path.is_file():
        try:
            root.iconbitmap(default=str(icon_path))
            return True
        except tk.TclError:
            pass
    image = tk.PhotoImage(width=32, height=32)
    image.put("#ffffff", to=(0, 0, 32, 32))
    image.put("#111315", to=(4, 4, 14, 14))
    image.put("#111315", to=(18, 4, 28, 14))
    image.put("#111315", to=(4, 18, 14, 28))
    image.put("#ff4f0a", to=(18, 18, 28, 28))
    root.iconphoto(True, image)
    root._gsus_icon_image = image  # type: ignore[attr-defined] -- Tk exige referencia viva
    return False


def line_icon(
    master: tk.Misc,
    name: str,
    *,
    size: int = 18,
    color: str = "#111315",
    background: str = "#ffffff",
) -> tk.Canvas:
    canvas = tk.Canvas(
        master,
        width=size,
        height=size,
        bg=background,
        highlightthickness=0,
        bd=0,
    )
    _draw(canvas, name, size, color)
    return canvas


def logo_mark(master: tk.Misc, *, size: int = 24, background: str = "#ffffff") -> tk.Canvas:
    canvas = tk.Canvas(
        master,
        width=size,
        height=size,
        bg=background,
        highlightthickness=0,
        bd=0,
    )
    gap = max(2, size // 10)
    block = (size - gap) // 2
    radius = max(2, size // 8)
    canvas.create_rectangle(0, 0, block, block, fill="#111315", outline="", width=0)
    canvas.create_rectangle(block + gap, 0, size, block, fill="#111315", outline="", width=0)
    canvas.create_rectangle(0, block + gap, block, size, fill="#111315", outline="", width=0)
    canvas.create_rectangle(block + gap, block + gap, size, size, fill="#ff4f0a", outline="", width=0)
    # Pequenos recortes brancos deixam a marca menos pesada em tamanhos maiores.
    if size >= 24:
        canvas.create_oval(block - radius, block - radius, block + radius, block + radius, fill=background, outline="")
    return canvas


def icon_badge(
    master: tk.Misc,
    name: str,
    *,
    size: int = 30,
    color: str = "#111315",
    background: str = "#ffffff",
    badge_background: str = "#f5f6f7",
) -> tk.Canvas:
    canvas = tk.Canvas(
        master,
        width=size,
        height=size,
        bg=background,
        highlightthickness=0,
        bd=0,
    )
    canvas.create_oval(1, 1, size - 1, size - 1, fill=badge_background, outline="")
    icon_size = max(12, round(size * 0.5))
    _draw(canvas, name, icon_size, color)
    offset = (size - icon_size) / 2
    # O circulo e o primeiro item; move apenas os tracos do icone.
    for item in canvas.find_all()[1:]:
        canvas.move(item, offset, offset)
    return canvas


def _draw(canvas: tk.Canvas, name: str, size: int, color: str) -> None:
    scale = size / 18

    def p(value: float) -> float:
        return value * scale

    width = max(1.2, 1.35 * scale)
    common = {"fill": color, "width": width}

    if name == "dashboard":
        for x1, y1, x2, y2 in ((2, 2, 8, 8), (10, 2, 16, 6), (2, 10, 8, 16), (10, 8, 16, 16)):
            canvas.create_rectangle(p(x1), p(y1), p(x2), p(y2), outline=color, width=width)
    elif name == "report":
        canvas.create_rectangle(p(3), p(2), p(15), p(16), outline=color, width=width)
        canvas.create_line(p(6), p(6), p(12), p(6), **common)
        canvas.create_line(p(6), p(9), p(12), p(9), **common)
        canvas.create_line(p(6), p(12), p(10), p(12), **common)
    elif name == "search":
        canvas.create_oval(p(2), p(2), p(12), p(12), outline=color, width=width)
        canvas.create_line(p(11), p(11), p(16), p(16), **common)
    elif name == "settings":
        canvas.create_oval(p(5), p(5), p(13), p(13), outline=color, width=width)
        canvas.create_oval(p(8), p(8), p(10), p(10), outline=color, width=width)
        for x1, y1, x2, y2 in ((9, 1, 9, 4), (9, 14, 9, 17), (1, 9, 4, 9), (14, 9, 17, 9),
                               (3, 3, 5, 5), (13, 13, 15, 15), (13, 5, 15, 3), (3, 15, 5, 13)):
            canvas.create_line(p(x1), p(y1), p(x2), p(y2), **common)
    elif name == "refresh":
        canvas.create_arc(p(2), p(2), p(16), p(16), start=35, extent=280, style=tk.ARC, outline=color, width=width)
        canvas.create_polygon(p(14), p(2), p(17), p(3), p(15), p(6), fill=color, outline="")
    elif name == "patient" or name == "user":
        canvas.create_oval(p(6), p(2), p(12), p(8), outline=color, width=width)
        canvas.create_arc(p(3), p(8), p(15), p(17), start=15, extent=150, style=tk.ARC, outline=color, width=width)
    elif name == "lock":
        canvas.create_rectangle(p(4), p(8), p(14), p(16), outline=color, width=width)
        canvas.create_arc(p(5), p(2), p(13), p(12), start=0, extent=180, style=tk.ARC, outline=color, width=width)
    elif name == "clock":
        canvas.create_oval(p(2), p(2), p(16), p(16), outline=color, width=width)
        canvas.create_line(p(9), p(5), p(9), p(9), p(12), p(11), **common)
    elif name == "unit":
        canvas.create_rectangle(p(3), p(3), p(15), p(16), outline=color, width=width)
        canvas.create_line(p(9), p(6), p(9), p(12), **common)
        canvas.create_line(p(6), p(9), p(12), p(9), **common)
    elif name == "back":
        canvas.create_line(p(15), p(9), p(3), p(9), **common)
        canvas.create_line(p(3), p(9), p(8), p(4), **common)
        canvas.create_line(p(3), p(9), p(8), p(14), **common)
    elif name == "arrow":
        canvas.create_line(p(4), p(14), p(14), p(4), **common)
        canvas.create_line(p(8), p(4), p(14), p(4), p(14), p(10), **common)
    else:
        canvas.create_oval(p(3), p(3), p(15), p(15), outline=color, width=width)
