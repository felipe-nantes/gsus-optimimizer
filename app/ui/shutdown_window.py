"""Janela de aviso antes do desligamento automático (SCHEDULE-002, DEC-129).

Aparece só ao fim da atualização agendada, com a opção ligada nas
Configurações e ninguém usando o computador (ver `app/shutdown.py`). Nada foi
pedido ao Windows ainda enquanto ela está aberta: o desligamento só é enviado
quando a contagem chega a zero sem cancelamento. Fechar a janela pelo X
também cancela -- quem fecha está na frente da máquina.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from app.ui.main_window import (
    BG_COLOR,
    BRAND_COLOR_DARK,
    FONT_FAMILY,
    TEXT_MUTED,
    TEXT_PRIMARY,
    configure_app_style,
)


def format_remaining(seconds: int) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{secs:02d}"


class ShutdownCountdown:
    """Conteúdo da janela, separado de `show_countdown` pra ser testável com
    um `tk.Tk()` próprio do teste (sem depender do `mainloop`)."""

    def __init__(self, root: tk.Tk, delay_seconds: int):
        self.root = root
        self.remaining = int(delay_seconds)
        self.cancelled = False
        self._job: str | None = None

        root.configure(bg=BG_COLOR)
        configure_app_style()
        tk.Label(
            root, text="A atualização da madrugada terminou", font=(FONT_FAMILY, 13, "bold"),
            bg=BG_COLOR, fg=TEXT_PRIMARY,
        ).pack(anchor="w", padx=24, pady=(20, 4))
        tk.Label(
            root, font=(FONT_FAMILY, 9), bg=BG_COLOR, fg=TEXT_MUTED, wraplength=400, justify="left",
            text=(
                "O computador será desligado ao fim da contagem, como definido nas "
                "Configurações. Se alguém ainda vai usá-lo, cancele abaixo."
            ),
        ).pack(anchor="w", padx=24)
        self.time_label = tk.Label(
            root, text=format_remaining(self.remaining), font=(FONT_FAMILY, 28, "bold"),
            bg=BG_COLOR, fg=BRAND_COLOR_DARK,
        )
        self.time_label.pack(anchor="w", padx=24, pady=(10, 6))
        self.cancel_button = ttk.Button(
            root, text="CANCELAR DESLIGAMENTO", command=self.cancel, style="Primary.TButton",
        )
        self.cancel_button.pack(anchor="w", padx=24, pady=(8, 22))
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        self._job = root.after(1000, self.tick)

    def tick(self) -> None:
        self.remaining -= 1
        if self.remaining <= 0:
            self._close()
            return
        self.time_label.config(text=format_remaining(self.remaining))
        self._job = self.root.after(1000, self.tick)

    def cancel(self) -> None:
        self.cancelled = True
        self._close()

    def _close(self) -> None:
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def show_countdown(delay_seconds: int) -> bool:
    """Abre a janela e bloqueia até o fim da contagem ou o cancelamento.
    Devolve True se o usuário cancelou."""
    from app.ui.icons import apply_window_icon

    root = tk.Tk()
    root.title("GSUS Auditoria — desligamento automático")
    apply_window_icon(root)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    countdown = ShutdownCountdown(root, delay_seconds)
    root.mainloop()
    return countdown.cancelled
