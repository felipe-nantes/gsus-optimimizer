"""Tela de configuração inicial (UI-002)."""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from app import config, scheduling
from app.security import credentials
from app.ui.icons import line_icon, logo_mark
from app.ui.main_window import (
    BG_COLOR,
    BRAND_COLOR,
    BRAND_SOFT,
    CARD_BG,
    CARD_BORDER,
    FONT_FAMILY,
    OUTER_BG,
    SIDEBAR_BG,
    TEXT_MUTED,
    TEXT_PRIMARY,
    configure_app_style,
)

logger = logging.getLogger(__name__)


class SetupWindow:
    def __init__(self, root: tk.Tk, app_config: config.AppConfig, on_complete: Callable[[], None] | None = None):
        self.root = root
        self.app_config = app_config
        self.on_complete = on_complete

        root.geometry("820x590")
        # REPORT-004/DEC-099: `MainWindow` (tela única) tornou a janela raiz
        # redimensionável -- esta tela precisa voltar a fixar o tamanho
        # explicitamente, já que `app/main.py::render` só troca os widgets,
        # nunca reseta esse estado sozinho. `minsize` também precisa ser
        # resetado (achado real de auditoria adversarial, DEC-101):
        # `MainWindow` chama `root.minsize(1024, 700)`, que sobrevive à
        # troca de tela e força esta janela pequena a abrir do tamanho
        # mínimo herdado (1024x700) em vez do `geometry` pedido acima.
        root.resizable(False, False)
        root.minsize(1, 1)
        root.configure(bg=OUTER_BG)
        configure_app_style()
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=1)

        shell = tk.Frame(root, bg=OUTER_BG)
        shell.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        sidebar = tk.Frame(shell, bg=SIDEBAR_BG, width=220)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(2, weight=1)

        brand = tk.Frame(sidebar, bg=SIDEBAR_BG)
        brand.grid(row=0, column=0, sticky="w", padx=24, pady=(26, 38))
        logo_mark(brand, size=22, background=SIDEBAR_BG).pack(side="left")
        tk.Label(
            brand, text="GSUS", font=(FONT_FAMILY, 12, "bold"), bg=SIDEBAR_BG, fg=TEXT_PRIMARY,
        ).pack(side="left", padx=(8, 0))

        steps = tk.Frame(sidebar, bg=SIDEBAR_BG)
        steps.grid(row=1, column=0, sticky="new")
        tk.Label(
            steps, text="CONFIGURAÇÃO", font=(FONT_FAMILY, 7, "bold"), bg=SIDEBAR_BG, fg=TEXT_MUTED,
        ).pack(anchor="w", padx=24, pady=(0, 10))
        for icon_name, text, active in (
            ("user", "Credenciais do GSUS", True),
            ("clock", "Rotina automática", False),
            ("lock", "Processamento local", False),
        ):
            row = tk.Frame(steps, bg=BRAND_SOFT if active else SIDEBAR_BG)
            row.pack(fill="x", padx=(9, 14), pady=2)
            tk.Frame(row, bg=BRAND_COLOR if active else row["bg"], width=3).pack(side="left", fill="y")
            line_icon(
                row, icon_name, size=16, color=TEXT_PRIMARY if active else TEXT_MUTED, background=row["bg"],
            ).pack(side="left", padx=(12, 9), pady=10)
            tk.Label(
                row, text=text, font=(FONT_FAMILY, 9, "bold" if active else "normal"),
                bg=row["bg"], fg=TEXT_PRIMARY if active else TEXT_MUTED,
            ).pack(side="left")

        privacy = tk.Frame(sidebar, bg="#f7f8ef", highlightbackground="#eef0e5", highlightthickness=1)
        privacy.grid(row=3, column=0, sticky="sew", padx=14, pady=14)
        line_icon(privacy, "lock", size=18, color=TEXT_PRIMARY, background="#f7f8ef").pack(
            anchor="w", padx=14, pady=(13, 5),
        )
        tk.Label(
            privacy, text="Dados protegidos", font=(FONT_FAMILY, 10, "bold"),
            bg="#f7f8ef", fg=TEXT_PRIMARY,
        ).pack(anchor="w", padx=14)
        tk.Label(
            privacy, text="Credenciais e análises permanecem neste computador.",
            font=(FONT_FAMILY, 8), bg="#f7f8ef", fg=TEXT_MUTED, wraplength=165, justify="left",
        ).pack(anchor="w", padx=14, pady=(3, 13))

        main = tk.Frame(shell, bg=BG_COLOR)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)

        has_saved_credential = credentials.get_credential("gsus") is not None
        title = "Configurações do GSUS" if app_config.configured else "Conectar ao GSUS"
        tk.Label(
            main, text=title, font=(FONT_FAMILY, 20, "bold"), bg=BG_COLOR, fg=TEXT_PRIMARY,
        ).grid(row=0, column=0, sticky="w", padx=34, pady=(30, 0))
        tk.Label(
            main, text="Informe os dados de consulta e defina a atualização automática.",
            font=(FONT_FAMILY, 9), bg=BG_COLOR, fg=TEXT_MUTED,
        ).grid(row=1, column=0, sticky="w", padx=34, pady=(3, 18))

        form = tk.Frame(main, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
        form.grid(row=2, column=0, sticky="ew", padx=34)
        form.grid_columnconfigure(0, weight=1)

        def field_label(row_number: int, icon_name: str, text: str) -> None:
            holder = tk.Frame(form, bg=CARD_BG)
            holder.grid(row=row_number, column=0, sticky="w", padx=20, pady=(13 if row_number else 18, 5))
            line_icon(holder, icon_name, size=15, color=TEXT_MUTED, background=CARD_BG).pack(side="left")
            tk.Label(
                holder, text=text, bg=CARD_BG, fg=TEXT_PRIMARY, font=(FONT_FAMILY, 9, "bold"),
            ).pack(side="left", padx=(7, 0))

        field_label(0, "user", "CPF (login GSUS)")
        self.username_var = tk.StringVar(value=app_config.gsus_username)
        username_entry = ttk.Entry(form, textvariable=self.username_var)
        username_entry.grid(row=1, column=0, sticky="ew", padx=20)

        field_label(2, "lock", "Senha do GSUS")
        self.password_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.password_var, show="*").grid(row=3, column=0, sticky="ew", padx=20)
        if has_saved_credential:
            tk.Label(
                form, text="Deixe em branco para manter a senha já salva.",
                bg=CARD_BG, fg=TEXT_MUTED, font=(FONT_FAMILY, 8),
            ).grid(row=4, column=0, sticky="w", padx=20, pady=(3, 0))

        field_label(5, "unit", "Setor")
        self.unit_var = tk.StringVar(value=app_config.unit)
        ttk.Entry(form, textvariable=self.unit_var).grid(row=6, column=0, sticky="ew", padx=20)

        field_label(7, "clock", "Horário da atualização automática")
        self.schedule_var = tk.StringVar(value=app_config.schedule_time)
        ttk.Entry(form, textvariable=self.schedule_var).grid(row=8, column=0, sticky="ew", padx=20)

        ttk.Button(
            root, text="CONCLUIR", command=self._on_submit, style="Primary.TButton", width=24,
        ).grid(in_=form, row=9, column=0, sticky="ew", padx=20, pady=(20, 12))
        tk.Label(
            form, text="A senha é protegida pelo Windows e não aparece nos relatórios.",
            font=(FONT_FAMILY, 8), bg=CARD_BG, fg=TEXT_MUTED,
        ).grid(row=10, column=0, pady=(0, 18))
        username_entry.focus_set()

    def _on_submit(self) -> None:
        username = self.username_var.get().strip()
        password = self.password_var.get()
        unit = self.unit_var.get().strip()
        schedule_time = self.schedule_var.get().strip()

        has_saved_credential = credentials.get_credential("gsus") is not None

        if not username or not unit or (not password and not has_saved_credential):
            messagebox.showerror("Configuração incompleta", "Preencha usuário, senha e setor.")
            return

        if password:  # em branco + credencial já salva = manter a senha atual
            try:
                credentials.save_credential("gsus", username, password)
            except credentials.CredentialError:
                logger.exception("Falha ao salvar credencial GSUS")
                messagebox.showerror(
                    "Não foi possível salvar",
                    "Não foi possível salvar a senha com segurança. Tente novamente.",
                )
                return

        self.app_config.gsus_username = username
        self.app_config.unit = unit
        self.app_config.schedule_time = schedule_time or self.app_config.schedule_time
        self.app_config.configured = True
        config.save_config(self.app_config)
        self._register_scheduled_task()

        logger.info("Configuração inicial concluída para setor=%s", unit)
        messagebox.showinfo("Configuração concluída", "Configuração salva com sucesso.")
        if self.on_complete is not None:
            self.on_complete()

    def _register_scheduled_task(self) -> None:
        """SCHEDULE-001: o app se agenda sozinho a cada vez que a
        configuração (e o horário) é salva -- prompt mestre seção 12/Fase
        12, usuário nunca mexe no Task Scheduler manualmente. Achado real
        (INSTALL-001, 2026-08-25): `register_daily_task` existia e era
        testado desde SCHEDULE-001, mas nunca era chamado por ninguém --
        mesmo um instalador perfeito nunca criaria a tarefa de verdade.

        Só em modo `frozen` -- em dev, `sys.executable` é o interpretador
        Python, agendar isso rodaria "python.exe" sem argumento nenhum."""
        if not getattr(sys, "frozen", False):
            logger.info("Modo dev -- tarefa agendada não registrada (só roda em build empacotado)")
            return
        try:
            scheduling.register_daily_task(sys.executable, self.app_config.schedule_time, arguments="--auto-update")
        except scheduling.SchedulingError:
            logger.exception("Falha ao registrar tarefa agendada")
            messagebox.showwarning(
                "Atualização automática não agendada",
                "A configuração foi salva, mas não foi possível agendar a atualização diária "
                f"automática ({self.app_config.schedule_time}). Você ainda pode atualizar "
                'manualmente clicando em "ATUALIZAR AGORA".',
            )
