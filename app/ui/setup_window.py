"""Tela de configuração inicial (UI-002)."""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from app import config, scheduling
from app.security import credentials
from app.ui.main_window import BG_COLOR, BRAND_COLOR_DARK, FONT_FAMILY, TEXT_PRIMARY, configure_app_style

logger = logging.getLogger(__name__)


class SetupWindow:
    def __init__(self, root: tk.Tk, app_config: config.AppConfig, on_complete: Callable[[], None] | None = None):
        self.root = root
        self.app_config = app_config
        self.on_complete = on_complete

        root.geometry("380x300")
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
        root.configure(bg=BG_COLOR)
        configure_app_style()

        tk.Label(
            root, text="CONFIGURAÇÃO INICIAL", font=(FONT_FAMILY, 13, "bold"), bg=BG_COLOR, fg=BRAND_COLOR_DARK,
        ).pack(pady=(20, 16))

        form = tk.Frame(root, bg=BG_COLOR)
        form.pack(padx=28, fill="x")

        label_kwargs = {"bg": BG_COLOR, "fg": TEXT_PRIMARY, "font": (FONT_FAMILY, 9)}

        tk.Label(form, text="CPF (login GSUS):", **label_kwargs).grid(row=0, column=0, sticky="w", pady=5)
        self.username_var = tk.StringVar(value=app_config.gsus_username)
        ttk.Entry(form, textvariable=self.username_var).grid(row=0, column=1, sticky="ew", pady=5)

        has_saved_credential = credentials.get_credential("gsus") is not None
        password_label = "Senha (deixe em branco para manter a atual):" if has_saved_credential else "Senha:"
        tk.Label(form, text=password_label, **label_kwargs).grid(row=1, column=0, sticky="w", pady=5)
        self.password_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.password_var, show="*").grid(row=1, column=1, sticky="ew", pady=5)

        tk.Label(form, text="Setor:", **label_kwargs).grid(row=2, column=0, sticky="w", pady=5)
        self.unit_var = tk.StringVar(value=app_config.unit)
        ttk.Entry(form, textvariable=self.unit_var).grid(row=2, column=1, sticky="ew", pady=5)

        tk.Label(form, text="Horário da atualização:", **label_kwargs).grid(row=3, column=0, sticky="w", pady=5)
        self.schedule_var = tk.StringVar(value=app_config.schedule_time)
        ttk.Entry(form, textvariable=self.schedule_var).grid(row=3, column=1, sticky="ew", pady=5)

        form.columnconfigure(1, weight=1)

        ttk.Button(root, text="CONCLUIR", command=self._on_submit, style="Primary.TButton").pack(pady=24)

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
