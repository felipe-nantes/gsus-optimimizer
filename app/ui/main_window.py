"""Tela principal (UI-001) -- tela única unificando dashboard + painel de
gerenciamento (REPORT-004, DEC-099), com "Atualizar agora" ligado ao
pipeline real (orchestrator + GSUS adapter) e mensagens de erro amigáveis
(UI-003).

O painel de gerenciamento (barra de controle, "Atualizar agora"/"Abrir
Relatório"/"Localizar Paciente") é o que já existia -- comportamento e
atributos usados pelos testes (`status_label`, `_updating`, `_report_path`,
`_on_update`) continuam intactos. O que muda é tudo em volta: cartões de
KPI, 4 gráficos (matplotlib) e uma tabela de censo por paciente, clicável,
todos alimentados por `app/reports/dashboard_metrics.py` (Fases 1/2 do
REPORT-003) através de uma conexão SQLite própria e de curta duração (mesmo
padrão de `app/ui/lookup_window.py` -- nunca abre conexão de vida longa
aqui).

A dashboard NUNCA toca GSUS/IA -- só reconsulta o banco local. Ela se
atualiza sozinha ao fim de toda execução (manual ou agendada, via
`_finish_update`) e periodicamente enquanto a janela está aberta
(`_periodic_refresh`), para refletir uma execução automática que tenha
terminado com a janela já aberta."""
from __future__ import annotations

import logging
import queue
import threading
import webbrowser
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Callable

from app import config
from app.reports import dashboard_metrics
from app.reports.dashboard_metrics import PatientCensusRow
from app.reports.html_report import PatientLookupResult, generate_patient_report
from app.storage import database
from app.storage.repository import Repository

logger = logging.getLogger(__name__)

# Mesmas cores do relatório HTML (app/reports/html_report.py::CSS,
# .prio-ALTA/.prio-MEDIA/.prio-MONITORAMENTO) -- dashboard e relatório
# precisam comunicar gravidade com a MESMA linguagem visual.
PRIORITY_COLORS = {"ALTA": "#b3261e", "MEDIA": "#b8860b", "MONITORAMENTO": "#2e7d32"}
ORIGIN_LABELS = {"INTERNA": "Interna", "EXTERNA": "Externa", "NAO_DEFINIDA": "Não definida"}

# Paleta única da tela (design/UX, 2026-09-03 -- pedido explícito do usuário
# de incluir o polimento visual nesta entrega, depois de meses adiado). Só
# cor/tipografia/espaçamento -- nenhum widget muda de identidade (mesmos
# atributos que os testes já esperam: `_kpi_labels`, `_census_tree`,
# `status_label`, `update_button`). Vermelho/âmbar/verde continuam os MESMOS
# de PRIORITY_COLORS -- mudar essas cores quebraria a linguagem visual
# compartilhada com o relatório HTML.
BG_COLOR = "#f2f4f7"
CARD_BG = "#ffffff"
CARD_BORDER = "#dde1e6"
BRAND_COLOR = "#1f5f8b"
BRAND_COLOR_DARK = "#15486b"
TEXT_PRIMARY = "#1f2937"
TEXT_MUTED = "#6b7280"
FONT_FAMILY = "Segoe UI"

# Cor de destaque no topo de cada cartão de KPI -- neutra (BRAND_COLOR) pra
# indicadores informativos, vermelha pros dois números que pedem atenção
# imediata do auditor (EDD vencida, dia vermelho hoje), verde pro indicador
# positivo (dia verde). Nunca reinterpreta severidade clínica além do que já
# está em PRIORITY_COLORS -- só reaproveita a mesma linguagem.
KPI_ACCENTS = {
    "total_active": BRAND_COLOR,
    "with_pending": BRAND_COLOR,
    "without_edd": PRIORITY_COLORS["MEDIA"],
    "edd_overdue": PRIORITY_COLORS["ALTA"],
    "dia_vermelho": PRIORITY_COLORS["ALTA"],
    "dia_verde": PRIORITY_COLORS["MONITORAMENTO"],
    "median_resolution": BRAND_COLOR,
}

# Achado de auditoria de certificação pré-entrega (2026-09-01, RF-29): a
# tabela não tinha DIH/Contexto/Pendência principal (descrição real, não só
# categoria) -- o requisito pede as 7 colunas explicitamente (leito, DIH,
# contexto, pendência principal, tempo, categoria, prioridade), mesmas já
# presentes no relatório HTML (html_report.py::_render_census_row).
CENSUS_COLUMNS = [
    ("bed", "Leito", 60),
    ("record_number", "Prontuário", 90),
    ("unit", "Unidade", 90),
    ("dih", "DIH", 50),
    ("context", "Contexto", 220),
    ("pending_desc", "Pendência principal", 220),
    ("category", "Categoria", 150),
    ("priority", "Prioridade", 100),
    ("pending_count", "Pendências", 80),
    ("time", "Tempo", 80),
    ("edd", "EDD", 110),
    ("dia", "Dia", 80),
]

CONTEXT_MAX_CHARS = 80  # mesmo corte de html_report.py::_render_census_row


def _format_hours(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 24:
        return f"~{round(hours)}h"
    days, remainder_hours = divmod(hours, 24)
    return f"~{int(days)}d {round(remainder_hours)}h"


def _format_edd_cell(edd_status: str | None, edd_overdue: bool) -> str:
    if edd_overdue:
        return "VENCIDA"
    if edd_status is None or edd_status == "NAO_REGISTRADA":
        return "não documentada"
    return "em dia"


def configure_app_style() -> None:
    """Estilo ttk único da aplicação (design/UX, 2026-09-03) -- função de
    módulo, não método, porque `SetupWindow`/`LookupWindow` (telas menores,
    fora deste arquivo) também precisam dele e podem abrir ANTES de
    `MainWindow` alguma vez existir (ex.: primeira execução, tela de
    configuração inicial). `ttk.Style()` é compartilhado por todo o
    processo Tk -- chamar de novo depois é seguro e barato (apenas
    reconfigura os mesmos nomes de estilo).

    `clam` é o único tema ttk que respeita cor de fundo/primeiro-plano
    custom de forma consistente no Windows -- os temas nativos
    (`vista`/`winnative`) ignoram `background`/`foreground` em `ttk.Button`
    na prática, então não dava pra aplicar a paleta sem trocar de tema."""
    style = ttk.Style()
    style.theme_use("clam")

    style.configure(
        "Primary.TButton", background=BRAND_COLOR, foreground="white",
        font=(FONT_FAMILY, 10, "bold"), padding=(16, 9), borderwidth=0,
    )
    style.map(
        "Primary.TButton",
        background=[("disabled", "#9db8c8"), ("active", BRAND_COLOR_DARK)],
        foreground=[("disabled", "#eef3f7")],
    )
    style.configure(
        "Secondary.TButton", background=CARD_BG, foreground=BRAND_COLOR,
        font=(FONT_FAMILY, 10), padding=(14, 9), borderwidth=1, relief="solid",
    )
    style.map("Secondary.TButton", background=[("active", "#eaf1f6")])

    style.configure(
        "Treeview", background=CARD_BG, fieldbackground=CARD_BG, foreground=TEXT_PRIMARY,
        rowheight=26, font=(FONT_FAMILY, 9), borderwidth=0,
    )
    style.configure(
        "Treeview.Heading", background=BRAND_COLOR, foreground="white",
        font=(FONT_FAMILY, 9, "bold"), relief="flat", padding=(6, 6),
    )
    style.map("Treeview.Heading", background=[("active", BRAND_COLOR_DARK)])
    style.map("Treeview", background=[("selected", BRAND_COLOR)], foreground=[("selected", "white")])

    style.configure("TEntry", padding=(6, 5))


class MainWindow:
    DASHBOARD_REFRESH_INTERVAL_MS = 60_000

    def __init__(
        self,
        root: tk.Tk,
        app_config: config.AppConfig,
        on_settings: Callable[[], None] | None = None,
        on_lookup: Callable[[], None] | None = None,
    ):
        self.root = root
        self.app_config = app_config
        self.on_settings = on_settings
        self.on_lookup = on_lookup
        self._update_queue: queue.Queue = queue.Queue()
        self._updating = False
        self._report_path = config.get_app_data_dir() / "relatorio.html"
        self._census_report_path = config.get_app_data_dir() / "censo_relatorio.html"

        self._census_rows_all: list[PatientCensusRow] = []
        self._census_rows_visible: list[PatientCensusRow] = []
        self._census_sort_state: dict = {"column": None, "reverse": False}
        self._kpi_labels: dict[str, tk.Label] = {}
        self._refresh_job_id: str | None = None

        root.geometry("1200x820")
        root.minsize(1024, 700)
        root.resizable(True, True)
        root.configure(bg=BG_COLOR)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(3, weight=3)  # gráficos
        root.grid_rowconfigure(5, weight=2)  # tabela de censo

        self._build_style()
        self._build_control_bar(root)
        self._build_kpi_cards(root)
        self._build_charts(root)
        self._build_unit_strip(root)
        self._build_census_table(root)

        self._refresh_dashboard()
        self._refresh_job_id = self.root.after(self.DASHBOARD_REFRESH_INTERVAL_MS, self._periodic_refresh)

    def _initial_status_text(self) -> str:
        return "Relatório disponível" if self._report_path.exists() else "Nenhuma atualização ainda"

    def _on_settings(self) -> None:
        if self.on_settings is not None:
            self.on_settings()

    def _on_lookup(self) -> None:
        if self.on_lookup is not None:
            self.on_lookup()

    # ------------------------------------------------------------- layout
    def _build_style(self) -> None:
        configure_app_style()

    def _build_control_bar(self, root: tk.Tk) -> None:
        bar = tk.Frame(root, bg=BG_COLOR)
        bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        bar.grid_columnconfigure(4, weight=1)
        self._control_bar = bar
        bar.bind("<Destroy>", self._on_control_bar_destroyed)

        tk.Label(
            bar, text="AUDITORIA GSUS", font=(FONT_FAMILY, 16, "bold"), bg=BG_COLOR, fg=BRAND_COLOR_DARK,
        ).grid(row=0, column=0, sticky="w")

        self.update_button = ttk.Button(
            bar, text="ATUALIZAR AGORA", command=self._on_update, style="Primary.TButton",
        )
        self.update_button.grid(row=0, column=1, padx=(20, 6))
        ttk.Button(
            bar, text="ABRIR RELATÓRIO", command=self._on_open_report, style="Secondary.TButton",
        ).grid(row=0, column=2, padx=6)
        ttk.Button(
            bar, text="LOCALIZAR PACIENTE", command=self._on_lookup, style="Secondary.TButton",
        ).grid(row=0, column=3, padx=6)

        info = tk.Frame(bar, bg=BG_COLOR)
        info.grid(row=0, column=5, sticky="e")
        tk.Label(info, text=f"Setor: {self.app_config.unit}", font=(FONT_FAMILY, 9), bg=BG_COLOR, fg=TEXT_MUTED).pack(anchor="e")
        tk.Label(
            info, text=f"Próxima atualização: {self.app_config.schedule_time}",
            font=(FONT_FAMILY, 9), bg=BG_COLOR, fg=TEXT_MUTED,
        ).pack(anchor="e")

        settings_label = tk.Label(bar, text="Configurações", font=(FONT_FAMILY, 9, "underline"), bg=BG_COLOR, fg=BRAND_COLOR, cursor="hand2")
        settings_label.grid(row=0, column=6, padx=(16, 0))
        settings_label.bind("<Button-1>", lambda _event: self._on_settings())

        self.status_label = tk.Label(
            bar, text=self._initial_status_text(), font=(FONT_FAMILY, 9), bg=BG_COLOR, fg=TEXT_MUTED, justify="left",
        )
        self.status_label.grid(row=1, column=0, columnspan=7, sticky="w", pady=(10, 0))

    def _build_kpi_cards(self, root: tk.Tk) -> None:
        frame = tk.Frame(root, bg=BG_COLOR)
        frame.grid(row=1, column=0, sticky="ew", padx=16, pady=8)

        cards = [
            ("total_active", "Pacientes ativos"),
            ("with_pending", "Com pendência ativa"),
            ("without_edd", "Sem EDD documentada"),
            ("edd_overdue", "EDD vencida"),
            ("dia_vermelho", "Dia vermelho hoje"),
            ("dia_verde", "Dia verde hoje"),
            ("median_resolution", "Tempo mediano de resolução"),
        ]
        for i, (key, title) in enumerate(cards):
            frame.grid_columnconfigure(i, weight=1)
            card = tk.Frame(frame, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew", padx=4)
            tk.Frame(card, bg=KPI_ACCENTS.get(key, BRAND_COLOR), height=4).pack(fill="x", side="top")
            value_label = tk.Label(card, text="—", font=(FONT_FAMILY, 22, "bold"), bg=CARD_BG, fg=TEXT_PRIMARY)
            value_label.pack(pady=(12, 0))
            tk.Label(
                card, text=title, font=(FONT_FAMILY, 9), fg=TEXT_MUTED, bg=CARD_BG, wraplength=140, justify="center",
            ).pack(pady=(2, 12))
            self._kpi_labels[key] = value_label

    def _build_charts(self, root: tk.Tk) -> None:
        # Import tardio (matplotlib é a 1ª dependência "pesada" de terceiros
        # do projeto, DEC-099) -- mesmo raciocínio de import tardio de
        # Playwright/orchestrator já usado em `_run_update_worker`: não
        # pagar esse custo antes de a janela realmente precisar desenhar.
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        charts_frame = tk.Frame(root, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
        charts_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=8)

        self._fig = Figure(figsize=(11, 5), dpi=100, constrained_layout=True, facecolor=CARD_BG)
        axes = self._fig.subplots(2, 2)
        self._ax_category, self._ax_priority = axes[0]
        self._ax_origin, self._ax_trend = axes[1]

        self._chart_canvas = FigureCanvasTkAgg(self._fig, master=charts_frame)
        self._chart_canvas.get_tk_widget().configure(bg=CARD_BG, highlightthickness=0)
        self._chart_canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=8)

    def _style_axes(self, ax) -> None:
        """Espinha/eixo consistentes entre os 4 gráficos (design/UX,
        2026-09-03) -- sem isto cada `ax.clear()` (chamado a cada refresh)
        voltava pro visual padrão do matplotlib (moldura preta fechada nos 4
        lados), destoando do resto da tela."""
        ax.set_facecolor(CARD_BG)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(CARD_BORDER)
        ax.tick_params(colors=TEXT_MUTED, labelsize=8)
        ax.title.set_color(TEXT_PRIMARY)
        ax.title.set_fontsize(10)
        ax.title.set_fontweight("bold")

    def _build_unit_strip(self, root: tk.Tk) -> None:
        self._unit_strip = tk.Label(
            root, text="", font=(FONT_FAMILY, 9), fg=TEXT_MUTED, bg=BG_COLOR,
            justify="left", anchor="w", wraplength=1150,
        )
        self._unit_strip.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 4))

    def _build_census_table(self, root: tk.Tk) -> None:
        frame = tk.Frame(root, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
        frame.grid(row=5, column=0, sticky="nsew", padx=16, pady=(0, 14))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        filter_row = tk.Frame(frame, bg=CARD_BG)
        filter_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        tk.Label(
            filter_row, text="Filtrar (leito/prontuário/unidade/categoria/pendência):",
            font=(FONT_FAMILY, 9), bg=CARD_BG, fg=TEXT_PRIMARY,
        ).pack(side="left")
        self._census_filter_var = tk.StringVar()
        self._census_filter_var.trace_add("write", lambda *_args: self._apply_census_filter())
        ttk.Entry(filter_row, textvariable=self._census_filter_var, width=30).pack(side="left", padx=(8, 0))

        columns = [col_id for col_id, _label, _width in CENSUS_COLUMNS]
        self._census_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col_id, label, width in CENSUS_COLUMNS:
            self._census_tree.heading(col_id, text=label, command=lambda c=col_id: self._sort_census_by(c))
            self._census_tree.column(col_id, width=width, anchor="w")
        for priority, color in PRIORITY_COLORS.items():
            self._census_tree.tag_configure(priority, foreground=color)
        # Listras alternadas (design/UX, 2026-09-03) -- linha de fundo só,
        # combinada com a tag de prioridade (cor do texto) no mesmo item;
        # tags diferentes controlam propriedades diferentes sem conflito.
        self._census_tree.tag_configure("oddrow", background=CARD_BG)
        self._census_tree.tag_configure("evenrow", background="#f5f7fa")
        self._census_tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))

        vscroll = ttk.Scrollbar(frame, orient="vertical", command=self._census_tree.yview)
        self._census_tree.configure(yscrollcommand=vscroll.set)
        vscroll.grid(row=1, column=1, sticky="ns")

        # Achado de auditoria de certificação (RF-29): a tabela ganhou 3
        # colunas novas (DIH/Contexto/Pendência principal) -- sem rolagem
        # horizontal, as últimas colunas (Tempo/EDD/Dia) ficavam fora da
        # área visível, sem nenhum jeito de alcançá-las.
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self._census_tree.xview)
        self._census_tree.configure(xscrollcommand=hscroll.set)
        hscroll.grid(row=2, column=0, sticky="ew")

        self._census_tree.bind("<Double-1>", lambda _event: self._on_census_row_open())

    # ------------------------------------------------------------- update
    def _on_update(self) -> None:
        if self._updating:
            return
        self._updating = True
        self.update_button.config(state="disabled")
        self.status_label.config(text="Preparando...")

        thread = threading.Thread(target=self._run_update_worker, daemon=True)
        thread.start()
        self.root.after(150, self._poll_update_queue)

    def _run_update_worker(self) -> None:
        # Import tardio: evita custo de import do Playwright/orchestrator
        # quando a tela abre e ainda não interagiu com "Atualizar agora".
        from app.ui.errors import friendly_message
        from app.update_flow import run_update

        try:
            result = run_update(
                self.app_config,
                self._report_path,
                progress=lambda msg: self._update_queue.put(("progress", msg)),
            )
            self._update_queue.put(("done", result))
        except Exception as exc:
            logger.exception("Falha na atualização manual")
            self._update_queue.put(("error", friendly_message(exc)))

    def _poll_update_queue(self) -> None:
        # Achado real de auditoria adversarial: sem esta guarda, um usuário
        # que clica "Atualizar agora" e navega pra Configurações/Localizar
        # Paciente ANTES da atualização terminar faz este `after` (agendado
        # contra o `root`, que sobrevive à troca de tela) tentar tocar um
        # `status_label`/`update_button` já destruído -- TclError que
        # aborta o método no meio, nunca chama `_finish_update`, e perde o
        # resultado da atualização em silêncio (mesma classe de bug que
        # `_periodic_refresh` já tratava com `_is_alive`, só que aqui
        # faltava). A atualização em si (thread separada, `run_update`)
        # continua e grava tudo no banco normalmente -- só o status na TELA
        # antiga se perderia.
        if not self._is_alive():
            return
        try:
            while True:
                kind, payload = self._update_queue.get_nowait()
                if kind == "progress":
                    self.status_label.config(text=payload)
                elif kind == "done":
                    counts = payload.counts
                    self.status_label.config(
                        text=f"Atualizado — {counts['completed']}/{counts['found']} pacientes "
                        f"({counts['failed']} falha(s))"
                    )
                    self._finish_update()
                    return
                elif kind == "error":
                    self.status_label.config(text=payload)
                    self._finish_update()
                    return
        except queue.Empty:
            pass

        if self._updating:
            self.root.after(150, self._poll_update_queue)

    def _finish_update(self) -> None:
        self._updating = False
        self.update_button.config(state="normal")
        self._refresh_dashboard()

    # -------------------------------------------------------------- report
    def _on_open_report(self) -> None:
        logger.info("Usuário solicitou abrir relatório")
        if not self._report_path.exists():
            messagebox.showinfo("Sem relatório ainda", "Nenhuma atualização foi concluída ainda.")
            return
        webbrowser.open(self._report_path.as_uri())

    # ----------------------------------------------------------- dashboard
    def _is_alive(self) -> bool:
        try:
            return self.status_label.winfo_exists()
        except tk.TclError:
            return False

    def _periodic_refresh(self) -> None:
        if not self._is_alive():
            return  # janela trocada (Configurações/Localizar Paciente) -- para de reagendar
        self._refresh_dashboard()
        self._refresh_job_id = self.root.after(self.DASHBOARD_REFRESH_INTERVAL_MS, self._periodic_refresh)

    def _on_control_bar_destroyed(self, event: tk.Event) -> None:
        # Achado real de auditoria adversarial: `_is_alive` só evita que um
        # `after` já dispersado tente reagendar de novo -- não cancela o
        # job que JÁ está pendente no momento da troca de tela. Sem isto,
        # esta MainWindow inteira (Figure/FigureCanvasTkAgg do matplotlib,
        # listas de PatientCensusRow) fica presa na memória até o próprio
        # timer de 60s vencer -- se o usuário alternar telas repetidamente
        # mais rápido que isso, várias instâncias antigas acumulam ao mesmo
        # tempo. `event.widget is bar` (não apenas "algum widget morreu")
        # porque `<Destroy>` dispara em cascata pra cada filho también --
        # só a destruição do frame de controle em si (o widget mais "de
        # cima" que esta classe possui, criado primeiro) marca a troca de
        # tela de verdade.
        if event.widget is not self._control_bar or self._refresh_job_id is None:
            return
        self.root.after_cancel(self._refresh_job_id)
        self._refresh_job_id = None

    def _refresh_dashboard(self) -> None:
        """Reconsulta o banco local (nunca GSUS/IA) e redesenha cartões,
        gráficos e tabela. Conexão própria de curta duração, mesmo padrão de
        `app/ui/lookup_window.py::_on_search` -- nunca guarda conexão de
        vida longa nesta janela. Isolado em try/except: uma falha aqui
        (banco temporariamente locked, etc.) não pode derrubar a janela
        inteira nem interromper "Atualizar agora"."""
        try:
            conn = database.init_db(config.get_db_path())
            repo = Repository(conn)
            try:
                indicators = dashboard_metrics.compute_service_indicators(repo)
                unit_census = dashboard_metrics.compute_unit_census(repo)
                self._census_rows_all = dashboard_metrics.compute_patient_census_rows(repo)
                snapshots = repo.get_daily_snapshots(limit=30)
            finally:
                conn.close()
        except Exception:
            logger.exception("Falha ao atualizar a dashboard -- mantendo última visualização")
            return

        self._render_kpi_cards(indicators)
        self._render_charts(indicators, snapshots)
        self._render_unit_strip(unit_census)
        self._apply_census_filter()

    def _render_kpi_cards(self, indicators: dashboard_metrics.ServiceIndicators) -> None:
        self._kpi_labels["total_active"].config(text=str(indicators.total_active_patients))
        self._kpi_labels["with_pending"].config(
            text=f"{indicators.patients_with_active_pending} ({indicators.pct_patients_with_active_pending}%)"
        )
        self._kpi_labels["without_edd"].config(
            text=f"{indicators.patients_without_edd} ({indicators.pct_patients_without_edd}%)"
        )
        self._kpi_labels["edd_overdue"].config(
            text=f"{indicators.patients_with_edd_overdue} ({indicators.pct_patients_with_edd_overdue}%)"
        )
        self._kpi_labels["dia_vermelho"].config(text=str(indicators.patients_dia_vermelho))
        self._kpi_labels["dia_verde"].config(text=str(indicators.patients_dia_verde))
        self._kpi_labels["median_resolution"].config(text=_format_hours(indicators.median_resolution_hours))

    def _render_charts(self, indicators: dashboard_metrics.ServiceIndicators, snapshots) -> None:
        self._render_bar_chart(self._ax_category, indicators.by_category, "Pendências por categoria")
        self._render_priority_chart(indicators.by_priority)
        self._render_origin_chart(indicators.by_origin)
        self._render_trend_chart(snapshots)
        self._chart_canvas.draw_idle()

    def _render_bar_chart(self, ax, counts: dict[str, int], title: str) -> None:
        ax.clear()
        self._style_axes(ax)
        ax.set_title(title)
        if not counts:
            ax.text(0.5, 0.5, "Sem dados", ha="center", va="center", transform=ax.transAxes, color=TEXT_MUTED)
            ax.set_xticks([])
            ax.set_yticks([])
            return
        labels = list(counts.keys())
        values = [counts[label] for label in labels]
        ax.barh(labels, values, color=BRAND_COLOR)

    def _render_priority_chart(self, by_priority: dict[str, int]) -> None:
        ax = self._ax_priority
        ax.clear()
        self._style_axes(ax)
        ax.set_title("Pendências por prioridade")
        order = ["ALTA", "MEDIA", "MONITORAMENTO"]
        present = [p for p in order if by_priority.get(p)]
        if not present:
            ax.text(0.5, 0.5, "Sem dados", ha="center", va="center", transform=ax.transAxes, color=TEXT_MUTED)
            ax.set_xticks([])
            ax.set_yticks([])
            return
        values = [by_priority[p] for p in present]
        colors = [PRIORITY_COLORS[p] for p in present]
        ax.pie(
            values, labels=present, colors=colors, autopct="%1.0f%%", textprops={"fontsize": 8, "color": "white"},
            wedgeprops={"edgecolor": CARD_BG, "linewidth": 1.5},
        )

    def _render_origin_chart(self, by_origin: dict[str, int]) -> None:
        ax = self._ax_origin
        ax.clear()
        self._style_axes(ax)
        ax.set_title("Barreiras: interno × externo")
        if not by_origin:
            ax.text(0.5, 0.5, "Sem dados", ha="center", va="center", transform=ax.transAxes, color=TEXT_MUTED)
            ax.set_xticks([])
            ax.set_yticks([])
            return
        labels = [ORIGIN_LABELS.get(key, key) for key in by_origin]
        values = list(by_origin.values())
        ax.pie(
            values, labels=labels, colors=[BRAND_COLOR, "#7aa9c4"], autopct="%1.0f%%",
            textprops={"fontsize": 8, "color": "white"}, wedgeprops={"edgecolor": CARD_BG, "linewidth": 1.5},
        )

    def _render_trend_chart(self, snapshots) -> None:
        ax = self._ax_trend
        ax.clear()
        self._style_axes(ax)
        ax.set_title("Tendência de dias vermelhos")
        if len(snapshots) < 2:
            ax.text(
                0.5, 0.5, "Ainda sem histórico suficiente\n(acumula a cada execução)",
                ha="center", va="center", transform=ax.transAxes, color=TEXT_MUTED, fontsize=8,
            )
            ax.set_xticks([])
            ax.set_yticks([])
            return
        x = list(range(len(snapshots)))
        y = [row["patients_dia_vermelho"] for row in snapshots]
        labels = [row["created_at"][5:10] for row in snapshots]  # MM-DD
        ax.plot(x, y, color=PRIORITY_COLORS["ALTA"], marker="o", markersize=4, linewidth=2)
        step = max(len(labels) // 6, 1)
        ax.set_xticks(x[::step])
        ax.set_xticklabels(labels[::step], rotation=45, ha="right")

    def _render_unit_strip(self, unit_census) -> None:
        if not unit_census:
            self._unit_strip.config(text="Censo por unidade: sem pacientes ativos.")
            return
        parts = [
            f"{row.unit}: {row.patient_count} pac. ({row.patients_with_active_pending} c/ pendência)"
            for row in unit_census
        ]
        self._unit_strip.config(text="Censo por unidade  —  " + "   |   ".join(parts))

    # ------------------------------------------------------- tabela censo
    def _apply_census_filter(self) -> None:
        term = self._census_filter_var.get().strip().lower()
        if not term:
            self._census_rows_visible = list(self._census_rows_all)
        else:
            self._census_rows_visible = [
                row for row in self._census_rows_all
                if term in row.bed.lower()
                or term in row.record_number.lower()
                or term in row.unit.lower()
                or (row.main_category or "").lower().find(term) != -1
                or (row.main_description or "").lower().find(term) != -1
            ]
        self._render_census_tree()

    def _sort_census_by(self, column: str) -> None:
        reverse = self._census_sort_state["column"] == column and not self._census_sort_state["reverse"]
        self._census_sort_state = {"column": column, "reverse": reverse}
        self._render_census_tree()

    def _census_sort_key(self, row: PatientCensusRow, column: str):
        if column == "bed":
            return row.bed
        if column == "record_number":
            return row.record_number
        if column == "unit":
            return row.unit
        if column == "dih":
            return row.dih if row.dih is not None else -1
        if column == "context":
            return row.clinical_context or ""
        if column == "pending_desc":
            return row.main_description or ""
        if column == "priority":
            return dashboard_metrics.PRIORITY_RANK.get(row.main_priority, 0)
        if column == "category":
            return row.main_category or ""
        if column == "pending_count":
            return row.active_pending_count
        if column == "time":
            return row.hours_elapsed if row.hours_elapsed is not None else -1
        if column == "edd":
            return row.edd_overdue
        if column == "dia":
            return row.dia_classificacao or ""
        return ""

    def _render_census_tree(self) -> None:
        rows = list(self._census_rows_visible)
        column = self._census_sort_state["column"]
        if column is not None:
            rows.sort(key=lambda r: self._census_sort_key(r, column), reverse=self._census_sort_state["reverse"])

        self._census_tree.delete(*self._census_tree.get_children())
        for i, row in enumerate(rows):
            context = row.clinical_context or ""
            context_short = (context[:CONTEXT_MAX_CHARS] + "…") if len(context) > CONTEXT_MAX_CHARS else context
            values = (
                row.bed,
                row.record_number,
                row.unit,
                row.dih if row.dih is not None else "?",
                context_short or "—",
                row.main_description or "Sem pendências identificadas",
                row.main_category or "—",
                row.main_priority,
                row.active_pending_count,
                _format_hours(row.hours_elapsed),
                _format_edd_cell(row.edd_status, row.edd_overdue),
                row.dia_classificacao or "—",
            )
            stripe = "evenrow" if i % 2 == 0 else "oddrow"
            self._census_tree.insert("", "end", iid=row.record_number, values=values, tags=(row.main_priority, stripe))

    def _on_census_row_open(self) -> None:
        selection = self._census_tree.selection()
        if not selection:
            return
        record_number = selection[0]
        self._open_patient_report(record_number)

    def _open_patient_report(self, record_number: str) -> None:
        try:
            conn = database.init_db(config.get_db_path())
            repo = Repository(conn)
            try:
                result = generate_patient_report(repo, record_number)
            finally:
                conn.close()
        except Exception:
            logger.exception("Falha ao gerar relatório individual a partir da tabela de censo")
            messagebox.showerror("Erro", "Não foi possível gerar o relatório deste paciente. Veja o log para detalhes.")
            return

        if result in (PatientLookupResult.NOT_FOUND, PatientLookupResult.DISCHARGED):
            # Achado esperado, não erro: a tabela pode estar um pouco
            # desatualizada (refresh periódico de até 60s) em relação a uma
            # alta muito recente -- a próxima atualização automática corrige.
            messagebox.showinfo("Paciente não disponível", "Este paciente não está mais ativo -- a tabela será atualizada em instantes.")
            return

        self._census_report_path.parent.mkdir(parents=True, exist_ok=True)
        self._census_report_path.write_text(result, encoding="utf-8")
        webbrowser.open(self._census_report_path.as_uri())
