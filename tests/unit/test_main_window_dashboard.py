"""Testa a dashboard da tela única (REPORT-004/DEC-099): cartões de KPI,
gráficos e a tabela de censo por paciente (filtro/ordenação/abrir relatório
ao clicar). Mesmo padrão de `test_lookup_window.py` -- banco real (SQLite
temporário via `GSUS_AUDITORIA_DATA_DIR`), dados 100% fictícios, só UM
tk.Tk() por teste."""
from datetime import datetime, timedelta, timezone
import os
import sys

import pytest
import tkinter as tk

from app import config
from app.analysis.taxonomy import CATEGORY_DIAGNOSTICO, CATEGORY_PROCEDIMENTO_CIRURGIA
from app.models import Patient
from app.storage import database
from app.storage.repository import Repository
from app.ui.main_window import MainWindow


def _tk_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


pytestmark = pytest.mark.skipif(not _tk_available(), reason="Sem display Tk disponível neste ambiente")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _seed_two_patients():
    conn = database.init_db(config.get_db_path())
    repo = Repository(conn)
    recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))

    urgent = repo.upsert_patient(Patient(record_number="100", bed="1A", unit="2A"))
    repo.add_pending_item(urgent, CATEGORY_PROCEDIMENTO_CIRURGIA, "Aguarda cirurgia", "evid", recent)

    calm = repo.upsert_patient(Patient(record_number="200", bed="2B", unit="2A"))
    repo.save_patient_state(calm, "Contexto.", "Estável.")

    conn.close()
    return urgent, calm


def test_dashboard_refresh_populates_kpi_cards_and_census_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        root.update()

        assert window._kpi_labels["total_active"]["text"] == "2"
        assert "1 (50.0%)" == window._kpi_labels["with_pending"]["text"]
        assert set(window._kpi_labels) == {
            "total_active", "with_pending", "without_edd", "edd_overdue",
            "dia_vermelho", "dia_verde", "median_resolution",
        }
        assert all(
            child.winfo_x() + child.winfo_width() <= window._control_bar.winfo_width()
            for child in window._control_bar.winfo_children()
        )

        children = window._census_tree.get_children()
        assert len(children) == 2
        assert window._census_count_label["text"] == "2 pacientes"
        assert "100" in children and "200" in children
        columns = list(window._census_tree["columns"])
        urgent_values = window._census_tree.item("100", "values")
        assert urgent_values[columns.index("priority")] == "ALTA"
        assert urgent_values[columns.index("category")] == CATEGORY_PROCEDIMENTO_CIRURGIA
    finally:
        root.destroy()


def test_census_tree_shows_dih_context_and_main_description_columns(tmp_path, monkeypatch):
    """Achado de auditoria de certificação pré-entrega (2026-09-01, RF-29):
    a tabela não tinha DIH/Contexto, e "pendência principal" mostrava só a
    categoria -- RF-29 pede as 3 colunas separadas, iguais ao relatório
    HTML (html_report.py)."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    conn = database.init_db(config.get_db_path())
    repo = Repository(conn)
    patient_id = repo.upsert_patient(
        Patient(record_number="300", bed="4A", unit="2A", admission_date="2026-08-20")
    )
    repo.save_patient_state(patient_id, "Paciente em investigação de dor abdominal.", "Estável.")
    repo.add_pending_item(
        patient_id, CATEGORY_DIAGNOSTICO, "Aguarda resultado de tomografia", "evid",
        _iso(datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    conn.close()

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)

        columns = list(window._census_tree["columns"])
        values = window._census_tree.item("300", "values")
        assert values[columns.index("dih")] not in ("", "?", None)
        assert "investigação de dor abdominal" in values[columns.index("context")]
        assert values[columns.index("pending_desc")] == "Aguarda resultado de tomografia"
        assert values[columns.index("category")] == CATEGORY_DIAGNOSTICO
    finally:
        root.destroy()


def test_census_filter_narrows_visible_rows_without_requerying_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        assert len(window._census_tree.get_children()) == 2

        window._census_filter_var.set("1A")

        children = window._census_tree.get_children()
        assert children == ("100",)
        assert window._census_count_label["text"] == "1 de 2 pacientes"
    finally:
        root.destroy()


def test_census_sort_by_pending_count_toggles_ascending_then_descending(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()  # "100" tem 1 pendência, "200" tem 0

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)

        window._sort_census_by("pending_count")
        assert window._census_tree.get_children() == ("200", "100")  # ascendente: 0 antes de 1

        window._sort_census_by("pending_count")
        assert window._census_tree.get_children() == ("100", "200")  # 2º clique inverte
    finally:
        root.destroy()


def test_double_click_opens_individual_report_for_selected_patient(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri))

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._census_tree.selection_set("100")

        window._on_census_row_open()

        assert len(opened) == 1
        assert window._census_report_path.exists()
        assert "LEITO 1A" in window._census_report_path.read_text(encoding="utf-8")
    finally:
        root.destroy()


def test_dashboard_refresh_on_empty_database_does_not_crash(tmp_path, monkeypatch):
    """1ª execução do app -- banco vazio -- não pode quebrar a janela
    inteira (mesmo raciocínio de `compute_service_indicators` com banco
    vazio, ver test_dashboard_metrics.py)."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        assert window._kpi_labels["total_active"]["text"] == "0"
        assert window._census_tree.get_children() == ()
    finally:
        root.destroy()


def test_periodic_refresh_stops_rescheduling_after_window_is_torn_down(tmp_path, monkeypatch):
    """Mesmo cenário de `app/main.py::render` trocando pra Configurações/
    Localizar Paciente -- destrói os widgets desta janela, mas o `tk.Tk()`
    raiz continua vivo. Sem a checagem `_is_alive`, o `after` agendado
    tentaria atualizar um `status_label` já destruído (achado de design,
    nunca chegou a acontecer em produção porque foi pensado aqui)."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        for widget in root.winfo_children():
            widget.destroy()

        rescheduled = []
        monkeypatch.setattr(root, "after", lambda *a, **k: rescheduled.append(True))

        window._periodic_refresh()  # não deve levantar TclError

        assert rescheduled == []
    finally:
        root.destroy()


# ---------------------------------------------------- achados de auditoria (DEC-101)

def test_control_bar_destroy_cancels_pending_refresh_job_immediately(tmp_path, monkeypatch):
    """Achado real de auditoria adversarial: `_is_alive` só evitava
    REAGENDAR -- o job do `after` já pendente no momento da troca de tela
    continuava vivo até seu próprio timer de 60s vencer, prendendo a
    MainWindow inteira (Figure do matplotlib incluída) na memória até lá.
    Confirma que a troca de tela cancela o job NA HORA."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        job_id = window._refresh_job_id
        assert job_id is not None

        cancelled = []
        original_after_cancel = root.after_cancel

        def spy(id_):
            cancelled.append(id_)
            return original_after_cancel(id_)

        monkeypatch.setattr(root, "after_cancel", spy)

        for widget in root.winfo_children():  # mesmo padrão de app/main.py::render
            widget.destroy()

        assert cancelled == [job_id]
        assert window._refresh_job_id is None
    finally:
        root.destroy()


def test_poll_update_queue_ignores_destroyed_widgets_instead_of_crashing(tmp_path, monkeypatch):
    """Achado real de auditoria adversarial: clicar 'Atualizar agora' e
    navegar pra Configurações/Localizar Paciente ANTES da atualização
    terminar destruía status_label/update_button desta instância --
    _poll_update_queue tentava configurá-los mesmo assim (TclError), o que
    abortava o método no meio e perdia o resultado da atualização em
    silêncio (nunca chamava _finish_update)."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._updating = True
        window._update_queue.put(("done", type("R", (), {"counts": {"completed": 1, "found": 1, "failed": 0}})()))

        for widget in root.winfo_children():  # mesmo padrão de app/main.py::render
            widget.destroy()

        window._poll_update_queue()  # não pode levantar TclError
    finally:
        root.destroy()


def test_refresh_dashboard_survives_transient_database_failure(tmp_path, monkeypatch):
    """Achado real de auditoria adversarial: o try/except que protege
    _refresh_dashboard (documentado como blindagem contra banco
    momentaneamente locked, disco cheio, etc.) nunca era exercitado por
    nenhum teste -- uma regressão que deixasse a exceção escapar derrubaria
    a janela inteira sem que nenhum teste pegasse."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()

    cfg = config.AppConfig(configured=True, unit="2A")
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        assert window._kpi_labels["total_active"]["text"] == "2"

        import app.ui.main_window as main_window_module

        def _raise(*_args, **_kwargs):
            raise RuntimeError("banco temporariamente indisponível (simulado)")

        monkeypatch.setattr(main_window_module.database, "init_db", _raise)

        window._refresh_dashboard()  # não pode levantar

        assert window._kpi_labels["total_active"]["text"] == "2"  # mantém o último valor renderizado
    finally:
        root.destroy()


def test_unit_census_card_shows_table_and_expands_into_its_own_window(tmp_path, monkeypatch):
    """UI-008 (pedido do pagador: "censo por unidade: muito importante"):
    tabelinha em destaque na tela principal (unidades + total) e botão
    "Ampliar" que abre a tabela inteira numa janela própria, reaproveitada
    se já estiver aberta."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()

    root = tk.Tk()
    try:
        window = MainWindow(root, config.AppConfig(configured=True, unit="2A"))
        root.update()

        rows = window._unit_tree.get_children()
        assert len(rows) == 2  # 1 unidade + linha de total
        unit_values = window._unit_tree.item(rows[0], "values")
        assert (unit_values[0], unit_values[1], unit_values[2]) == ("2A", "2", "1")
        assert window._unit_tree.item(rows[1], "values")[0] == "Total"
        assert "2 pacientes ativos" in window._unit_strip["text"]

        window._open_unit_census_window()
        root.update()
        assert window._unit_window.winfo_exists()
        assert len(window._unit_window_tree.get_children()) == 2
        first_window = window._unit_window
        window._open_unit_census_window()  # segunda chamada: mesma janela, só traz pra frente
        assert window._unit_window is first_window
    finally:
        root.destroy()


# ------------------------------------------- Fase 1 dos ajustes do auditor (DEC-129)

def _all_label_texts(widget) -> list[str]:
    texts = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            texts.append(child.cget("text"))
        texts.extend(_all_label_texts(child))
    return texts


def test_patient_census_expands_into_its_own_window_with_independent_filter(tmp_path, monkeypatch):
    """UI-009 (seção 9 do auditor): "Ampliar" no censo de pacientes, como no
    censo por unidade -- janela própria, reaproveitada, com filtro e
    ordenação que não mexem na tabela da tela principal."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()

    root = tk.Tk()
    try:
        window = MainWindow(root, config.AppConfig(configured=True, unit="2A"))
        window._census_expand_button.invoke()
        root.update()

        assert window._census_window.winfo_exists()
        assert set(window._census_window_tree.get_children()) == {"100", "200"}
        assert window._census_window_count_label["text"] == "2 pacientes"
        first_window = window._census_window
        window._open_census_window()  # segunda chamada: mesma janela
        assert window._census_window is first_window

        # Achado da revisão: minimizada, `lift` sozinho não trazia de volta.
        window._census_window.iconify()
        root.update()
        window._open_census_window()
        root.update()
        assert window._census_window.state() == "normal"

        window._census_window_filter_var.set("2B")
        assert window._census_window_tree.get_children() == ("200",)
        assert window._census_window_count_label["text"] == "1 de 2 pacientes"
        assert len(window._census_tree.get_children()) == 2  # tela principal intacta

        window._census_window_filter_var.set("")
        window._sort_census_window_by("pending_count")
        assert window._census_window_tree.get_children() == ("200", "100")
    finally:
        root.destroy()


def test_expanded_census_opens_report_and_follows_dashboard_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri))

    root = tk.Tk()
    try:
        window = MainWindow(root, config.AppConfig(configured=True, unit="2A"))
        window._open_census_window()
        window._census_window_tree.selection_set("100")
        window._on_census_window_row_open()
        assert len(opened) == 1
        assert "LEITO 1A" in window._census_report_path.read_text(encoding="utf-8")

        conn = database.init_db(config.get_db_path())
        Repository(conn).upsert_patient(Patient(record_number="300", bed="3C", unit="2A"))
        conn.close()
        window._refresh_dashboard()
        assert "300" in window._census_window_tree.get_children()

        window._census_window.destroy()
        window._refresh_dashboard()  # janela fechada: não pode quebrar a dashboard
        assert len(window._census_tree.get_children()) == 3
    finally:
        root.destroy()


def test_edd_labels_carry_the_portuguese_name(tmp_path, monkeypatch):
    """UI-010 (seção 9 do auditor): "EDD vencida" ganha "(data provável de
    alta)"; o mesmo vale pros outros rótulos de EDD da tela."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))

    root = tk.Tk()
    try:
        window = MainWindow(root, config.AppConfig(configured=True, unit="2A"))
        texts = _all_label_texts(root)
        assert "EDD vencida\n(data provável de alta)" in texts
        assert "Sem EDD documentada\n(data provável de alta)" in texts
        assert window._census_tree.heading("edd", "text") == "EDD (prev. alta)"
    finally:
        root.destroy()


def test_browser_toggle_never_reverts_options_saved_by_another_window(tmp_path, monkeypatch):
    """Achado da 2ª revisão (SCHEDULE-002): com duas janelas do programa
    abertas, o interruptor do navegador salvava a configuração INTEIRA da
    memória -- religando o "desligar ao terminar" que a outra janela tinha
    acabado de desligar. Agora muda só o próprio campo no disco."""
    from app.security import credentials

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)
    cfg = config.AppConfig(configured=True, unit="2A", shutdown_after_scheduled_run=True)
    config.save_config(cfg)

    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        other_window_cfg = config.load_config()
        other_window_cfg.shutdown_after_scheduled_run = False  # a outra janela desliga a opção
        config.save_config(other_window_cfg)

        window.browser_switch.toggle()  # nesta janela, só o modo do navegador

        on_disk = config.load_config()
        assert on_disk.browser_visible is False
        assert on_disk.shutdown_after_scheduled_run is False
        assert on_disk.configured is True
    finally:
        root.destroy()


@pytest.mark.parametrize("enabled", [True, False])
def test_sidebar_tells_when_the_pc_will_shut_down_after_the_update(tmp_path, monkeypatch, enabled):
    """SCHEDULE-002: com a opção ligada, a tela principal avisa -- o auditor
    não precisa abrir as Configurações pra saber se o PC vai desligar."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))

    root = tk.Tk()
    try:
        window = MainWindow(
            root, config.AppConfig(configured=True, unit="2A", shutdown_after_scheduled_run=enabled),
        )
        if enabled:
            assert window._shutdown_note["text"] == "Desliga o computador ao terminar"
        else:
            assert window._shutdown_note is None
            assert "Desliga o computador ao terminar" not in _all_label_texts(root)
    finally:
        root.destroy()


def test_census_tree_says_awaiting_ai_instead_of_a_dash_for_unanalyzed_patient(tmp_path, monkeypatch):
    """UI-008: o pagador perguntou o que era o traço no painel -- paciente
    que a IA ainda não analisou agora diz "Aguardando análise de IA";
    quem já foi analisado mantém o traço só onde de fato não há dado."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_two_patients()

    root = tk.Tk()
    try:
        window = MainWindow(root, config.AppConfig(configured=True, unit="2A"))
        root.update()
        columns = list(window._census_tree["columns"])
        unanalyzed = window._census_tree.item("100", "values")
        analyzed = window._census_tree.item("200", "values")
        assert unanalyzed[columns.index("context")] == "Aguardando análise de IA"
        assert unanalyzed[columns.index("dia")] == "Aguardando análise de IA"
        assert analyzed[columns.index("context")] == "Contexto."
        assert analyzed[columns.index("dia")] == "—"
        assert window._kpi_labels["median_resolution"]["text"] == "sem histórico ainda"
    finally:
        root.destroy()


def test_with_pending_card_footer_counts_items_in_review(tmp_path, monkeypatch):
    """PEND-001: as em verificação seguem abertas (contam no número de cima)
    e aparecem no rodapé do cartão."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    urgent, _calm = _seed_two_patients()
    conn = database.init_db(config.get_db_path())
    repo = Repository(conn)
    [row] = repo.get_active_pending_items(urgent)
    repo.set_pending_in_review(row["pending_id"], actor="IA")
    conn.close()

    root = tk.Tk()
    try:
        window = MainWindow(root, config.AppConfig(configured=True, unit="2A"))
        root.update()
        assert window._kpi_labels["with_pending"]["text"] == "1 (50.0%)"
        assert window._kpi_footers["with_pending"]["text"] == "1 pend. em verificação"
        assert window._kpi_footers["total_active"]["text"] == "Consolidado da base local"
        values = window._census_tree.item("100", "values")
        columns = list(window._census_tree["columns"])
        assert values[columns.index("pending_desc")] == "[Em verificação] Aguarda cirurgia"
        assert values[columns.index("pending_count")] == "1 (1 em verif.)"
    finally:
        root.destroy()
