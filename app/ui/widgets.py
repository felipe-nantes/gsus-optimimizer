"""Widgets pequenos desenhados pelo proprio Tkinter (UI-006).

Mesmo principio de `app/ui/icons.py`: sem fonte de simbolos, arquivo externo
ou dependencia nova -- o `ttk` (tema `clam`) nao tem interruptor nativo.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable

TRACK_ON = "#ff4f0a"           # laranja da marca (BRAND_COLOR)
TRACK_OFF = "#c9c9cd"
TRACK_DISABLED_ON = "#f3aa8b"
TRACK_DISABLED_OFF = "#e3e3e6"
KNOB = "#ffffff"


class ToggleSwitch(tk.Canvas):
    """Interruptor liga/desliga.

    `command(value)` e chamado a cada clique com o novo valor booleano.
    `set_enabled(False)` bloqueia cliques (usado enquanto uma atualizacao
    esta em andamento -- a escolha so vale a partir da proxima)."""

    def __init__(
        self,
        master: tk.Misc,
        value: bool = False,
        command: Callable[[bool], None] | None = None,
        width: int = 40,
        height: int = 22,
        bg: str = "#ffffff",
    ):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._value = bool(value)
        self._command = command
        self._enabled = True
        # Nunca `self._w`/`self._h`: o Tkinter usa `_w` como o caminho Tcl do
        # proprio widget -- sobrescrever quebra qualquer chamada seguinte.
        self._switch_width = width
        self._switch_height = height
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def get(self) -> bool:
        return self._value

    def set(self, value: bool, notify: bool = False) -> None:
        self._value = bool(value)
        self._draw()
        if notify and self._command is not None:
            self._command(self._value)

    def toggle(self) -> None:
        self.set(not self._value, notify=True)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.config(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def is_enabled(self) -> bool:
        return self._enabled

    def _on_click(self, _event: tk.Event | None = None) -> None:
        if self._enabled:
            self.toggle()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self._switch_width, self._switch_height
        radius = h / 2
        if self._enabled:
            track = TRACK_ON if self._value else TRACK_OFF
        else:
            track = TRACK_DISABLED_ON if self._value else TRACK_DISABLED_OFF
        # Trilho arredondado: dois circulos nas pontas + retangulo no meio.
        self.create_oval(1, 1, h - 1, h - 1, fill=track, outline=track)
        self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=track, outline=track)
        self.create_rectangle(radius, 1, w - radius, h - 1, fill=track, outline=track)
        pad = 3
        knob_x = (w - h + pad) if self._value else pad
        self.create_oval(knob_x, pad, knob_x + h - 2 * pad, h - pad, fill=KNOB, outline=KNOB)
