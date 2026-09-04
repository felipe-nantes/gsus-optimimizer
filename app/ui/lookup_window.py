"""Tela de localização manual de paciente por prontuário ("Localizar
Paciente"). REDESENHADA 2026-08-25 (DEC-067, pedido do usuário): antes
navegava ao vivo no GSUS e mostrava histórico bruto de todas as internações
(DEC-031) -- agora busca só no banco LOCAL (nunca toca GSUS) e mostra o
relatório individual JÁ PROCESSADO (mesmo formato da rotina automática),
só para paciente ainda internado. Paciente com alta ou nunca visto pela
rotina automática recebe mensagem direta, sem tentar buscar nada ao vivo.
"""
from __future__ import annotations

import logging
import webbrowser
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Callable

from app import config
from app.reports.html_report import PatientLookupResult, generate_patient_report
from app.storage import database
from app.storage.repository import Repository
from app.ui.icons import line_icon, logo_mark
from app.ui.main_window import (
    BG_COLOR,
    BRAND_COLOR,
    BRAND_SOFT,
    CARD_BG,
    CARD_BORDER,
    FONT_FAMILY,
    OUTER_BG,
    PRIORITY_COLORS,
    SIDEBAR_BG,
    SUCCESS_COLOR,
    TEXT_MUTED,
    TEXT_PRIMARY,
    configure_app_style,
)

logger = logging.getLogger(__name__)

MESSAGE_BY_RESULT = {
    PatientLookupResult.NOT_FOUND: "Prontuário não encontrado nos registros locais -- pode não ter sido processado ainda pela rotina automática (\"Atualizar agora\").",
    PatientLookupResult.DISCHARGED: "Paciente recebeu alta.",
}


class LookupWindow:
    def __init__(self, root: tk.Tk, app_config: config.AppConfig, on_back: Callable[[], None]):
        self.root = root
        self.app_config = app_config
        self.on_back = on_back
        self._report_path = config.get_app_data_dir() / "localizar_relatorio.html"

        root.geometry("760x460")
        # REPORT-004/DEC-099: ver comentário equivalente em setup_window.py
        # -- `MainWindow` tornou a janela raiz redimensionável. `minsize`
        # também precisa ser resetado (achado real de auditoria adversarial,
        # DEC-101) -- sem isso, esta janela herda o mínimo de 1024x700 da
        # dashboard e abre gigante em vez do tamanho compacto pedido acima.
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

        sidebar = tk.Frame(shell, bg=SIDEBAR_BG, width=205)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(2, weight=1)

        brand = tk.Frame(sidebar, bg=SIDEBAR_BG)
        brand.grid(row=0, column=0, sticky="w", padx=23, pady=(26, 38))
        logo_mark(brand, size=22, background=SIDEBAR_BG).pack(side="left")
        tk.Label(
            brand, text="GSUS", font=(FONT_FAMILY, 12, "bold"), bg=SIDEBAR_BG, fg=TEXT_PRIMARY,
        ).pack(side="left", padx=(8, 0))

        menu = tk.Frame(sidebar, bg=SIDEBAR_BG)
        menu.grid(row=1, column=0, sticky="new")
        tk.Label(
            menu, text="MENU PRINCIPAL", font=(FONT_FAMILY, 7, "bold"), bg=SIDEBAR_BG, fg=TEXT_MUTED,
        ).pack(anchor="w", padx=23, pady=(0, 9))

        back_row = tk.Frame(menu, bg=SIDEBAR_BG, cursor="hand2")
        back_row.pack(fill="x", padx=(9, 14), pady=2)
        back_icon = line_icon(back_row, "dashboard", size=16, color=TEXT_MUTED, background=SIDEBAR_BG)
        back_icon.pack(
            side="left", padx=(15, 10), pady=10,
        )
        back_text = tk.Label(
            back_row, text="Dashboard", font=(FONT_FAMILY, 9), bg=SIDEBAR_BG, fg=TEXT_MUTED,
        )
        back_text.pack(side="left")
        for widget in (back_row, back_icon, back_text):
            widget.bind("<Button-1>", lambda _event: self.on_back())

        active_row = tk.Frame(menu, bg=BRAND_SOFT)
        active_row.pack(fill="x", padx=(9, 14), pady=2)
        tk.Frame(active_row, bg=BRAND_COLOR, width=3).pack(side="left", fill="y")
        line_icon(active_row, "search", size=16, color=TEXT_PRIMARY, background=BRAND_SOFT).pack(
            side="left", padx=(12, 10), pady=10,
        )
        tk.Label(
            active_row, text="Localizar paciente", font=(FONT_FAMILY, 9, "bold"),
            bg=BRAND_SOFT, fg=TEXT_PRIMARY,
        ).pack(side="left")

        local_note = tk.Frame(sidebar, bg="#f7f8ef", highlightbackground="#eef0e5", highlightthickness=1)
        local_note.grid(row=3, column=0, sticky="sew", padx=14, pady=14)
        line_icon(local_note, "lock", size=18, color=TEXT_PRIMARY, background="#f7f8ef").pack(
            anchor="w", padx=14, pady=(13, 5),
        )
        tk.Label(
            local_note, text="Consulta segura", font=(FONT_FAMILY, 10, "bold"),
            bg="#f7f8ef", fg=TEXT_PRIMARY,
        ).pack(anchor="w", padx=14)
        tk.Label(
            local_note, text="Esta tela consulta somente a base local.", font=(FONT_FAMILY, 8),
            bg="#f7f8ef", fg=TEXT_MUTED, wraplength=150, justify="left",
        ).pack(anchor="w", padx=14, pady=(3, 13))

        main = tk.Frame(shell, bg=BG_COLOR)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)

        tk.Label(
            main, text="Localizar paciente", font=(FONT_FAMILY, 20, "bold"), bg=BG_COLOR, fg=TEXT_PRIMARY,
        ).grid(row=0, column=0, sticky="w", padx=34, pady=(36, 0))
        tk.Label(
            main, text="Abra o relatório já processado de um paciente ainda internado.",
            font=(FONT_FAMILY, 9), bg=BG_COLOR, fg=TEXT_MUTED,
        ).grid(row=1, column=0, sticky="w", padx=34, pady=(3, 20))

        card = tk.Frame(main, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
        card.grid(row=2, column=0, sticky="ew", padx=34)
        tk.Label(
            card, text="NÚMERO DO PRONTUÁRIO", font=(FONT_FAMILY, 8, "bold"), bg=CARD_BG, fg=TEXT_MUTED,
        ).pack(anchor="w", padx=20, pady=(20, 7))
        form = tk.Frame(card, bg=CARD_BG)
        form.pack(padx=20, fill="x")
        form.grid_columnconfigure(0, weight=1)
        self.record_number_var = tk.StringVar()
        entry = ttk.Entry(form, textvariable=self.record_number_var)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        entry.bind("<Return>", lambda _event: self._on_search())
        ttk.Button(form, text="LOCALIZAR", command=self._on_search, style="Primary.TButton").grid(row=0, column=1)

        self.status_label = tk.Label(
            card, text="A consulta usa somente a base local e não acessa o GSUS ao vivo.",
            font=(FONT_FAMILY, 9), bg=CARD_BG, fg=TEXT_MUTED, wraplength=430, justify="left", anchor="w",
        )
        self.status_label.pack(fill="x", padx=20, pady=(15, 20))
        entry.focus_set()

    def _on_search(self) -> None:
        record_number = self.record_number_var.get().strip()
        if not record_number:
            messagebox.showerror("Número obrigatório", "Digite o número do prontuário.")
            return

        # Busca local (SQLite) -- rápida o bastante pra não precisar de
        # thread/fila como a versão antiga (que dependia do GSUS ao vivo).
        try:
            conn = database.init_db(config.get_db_path())
            repo = Repository(conn)
            result = generate_patient_report(repo, record_number)
            conn.close()
        except Exception:
            logger.exception("Falha ao buscar relatório local do paciente")
            self.status_label.config(
                text="Não foi possível consultar a base local. Tente novamente.",
                fg=PRIORITY_COLORS["ALTA"],
            )
            return

        if result in MESSAGE_BY_RESULT:
            self.status_label.config(text=MESSAGE_BY_RESULT[result], fg=TEXT_PRIMARY)
            return

        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        self._report_path.write_text(result, encoding="utf-8")
        webbrowser.open(self._report_path.as_uri())
        self.status_label.config(text="Relatório aberto no navegador.", fg=SUCCESS_COLOR)
