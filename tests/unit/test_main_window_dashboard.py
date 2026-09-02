"""Testa a dashboard da tela única (REPORT-004/DEC-099): cartões de KPI,
gráficos e a tabela de censo por paciente (filtro/ordenação/abrir relatório
ao clicar). Mesmo padrão de `test_lookup_window.py` -- banco real (SQLite
temporário via `GSUS_AUDITORIA_DATA_DIR`), dados 100% fictícios, só UM
tk.Tk() por teste."""
from datetime import datetime, timedelta, timezone

import pytest
import tkinter as tk

from app import config
from app.analysis.taxonomy import CATEGORY_DIAGNOSTICO, CATEGORY_PROCEDIMENTO_CIRURGIA
from app.models import Patient
from app.storage import database
from app.storage.repository import Repository
from app.ui.main_window import MainWindow


def _tk_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


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

        assert window._kpi_labels["total_active"]["text"] == "2"
        assert "1 (50.0%)" == window._kpi_labels["with_pending"]["text"]

        children = window._census_tree.get_children()
        assert len(children) == 2
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
