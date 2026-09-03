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
from app.ui.main_window import BG_COLOR, BRAND_COLOR, BRAND_COLOR_DARK, FONT_FAMILY, TEXT_MUTED, configure_app_style

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

        root.geometry("420x220")
        # REPORT-004/DEC-099: ver comentário equivalente em setup_window.py
        # -- `MainWindow` tornou a janela raiz redimensionável. `minsize`
        # também precisa ser resetado (achado real de auditoria adversarial,
        # DEC-101) -- sem isso, esta janela herda o mínimo de 1024x700 da
        # dashboard e abre gigante em vez do tamanho compacto pedido acima.
        root.resizable(False, False)
        root.minsize(1, 1)
        root.configure(bg=BG_COLOR)
        configure_app_style()

        tk.Label(
            root, text="LOCALIZAR PACIENTE", font=(FONT_FAMILY, 14, "bold"), bg=BG_COLOR, fg=BRAND_COLOR_DARK,
        ).pack(pady=(18, 4))
        tk.Label(
            root,
            text="Busca o relatório já processado desse prontuário (só pacientes "
            "ainda internados) -- não acessa o GSUS ao vivo.",
            font=(FONT_FAMILY, 9),
            bg=BG_COLOR,
            fg=TEXT_MUTED,
            wraplength=380,
            justify="center",
        ).pack(pady=(0, 14))

        form = tk.Frame(root, bg=BG_COLOR)
        form.pack(padx=16, fill="x")
        tk.Label(form, text="Nº Prontuário:", font=(FONT_FAMILY, 9), bg=BG_COLOR).pack(side="left")
        self.record_number_var = tk.StringVar()
        entry = ttk.Entry(form, textvariable=self.record_number_var, width=20)
        entry.pack(side="left", padx=(8, 8))
        entry.bind("<Return>", lambda _event: self._on_search())
        ttk.Button(form, text="Localizar", command=self._on_search, style="Primary.TButton").pack(side="left")

        self.status_label = tk.Label(
            root, text="", font=(FONT_FAMILY, 9), bg=BG_COLOR, fg=TEXT_MUTED, wraplength=380, justify="center",
        )
        self.status_label.pack(pady=(18, 4))

        back_label = tk.Label(root, text="Voltar", font=(FONT_FAMILY, 9, "underline"), bg=BG_COLOR, fg=BRAND_COLOR, cursor="hand2")
        back_label.pack(pady=(16, 14))
        back_label.bind("<Button-1>", lambda _event: self.on_back())

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
            self.status_label.config(text="Não foi possível consultar o banco local. Veja o log para detalhes.")
            return

        if result in MESSAGE_BY_RESULT:
            self.status_label.config(text=MESSAGE_BY_RESULT[result])
            return

        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        self._report_path.write_text(result, encoding="utf-8")
        webbrowser.open(self._report_path.as_uri())
        self.status_label.config(text="Relatório aberto no navegador.")
