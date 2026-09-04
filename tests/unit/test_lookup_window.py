"""Testa a tela "Localizar Paciente" reformulada (DEC-067): busca só no
banco LOCAL (nunca GSUS ao vivo), síncrona (sem thread/fila -- SQLite é
rápido o bastante). Confirma que a busca é genuinamente só-leitura (nenhum
dado existente é alterado) e que os 3 resultados possíveis (relatório
aberto, alta, não encontrado) funcionam."""
import os
import sys
import tkinter as tk

import pytest

from app import config
from app.models import Patient
from app.storage import database
from app.storage.repository import Repository
from app.ui.lookup_window import LookupWindow


def _tk_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


pytestmark = pytest.mark.skipif(not _tk_available(), reason="Sem display Tk disponível neste ambiente")


def _seed_active_patient_with_state(record_number="123456"):
    conn = database.init_db(config.get_db_path())
    repo = Repository(conn)
    patient_id = repo.upsert_patient(Patient(record_number=record_number, bed="2A", unit="Clínica Médica"))
    repo.save_patient_state(patient_id, "Contexto de teste.", "Estável.")
    conn.close()


def _seed_discharged_patient(record_number="777777"):
    conn = database.init_db(config.get_db_path())
    repo = Repository(conn)
    repo.upsert_patient(Patient(record_number=record_number, bed="3A", unit="Clínica Médica"))
    repo.mark_patients_inactive_not_in([])  # nenhum ativo -> marca esse como alta
    conn.close()


def test_lookup_opens_report_for_active_patient(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_active_patient_with_state("123456")

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri))

    cfg = config.AppConfig(configured=True)
    root = tk.Tk()
    try:
        window = LookupWindow(root, cfg, on_back=lambda: None)
        window.record_number_var.set("123456")
        window._on_search()

        assert len(opened) == 1
        assert "aberto" in window.status_label["text"].lower()
        assert window._report_path.exists()
        content = window._report_path.read_text(encoding="utf-8")
        assert "LEITO 2A" in content
    finally:
        root.destroy()


def test_lookup_shows_discharged_message_without_opening_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_discharged_patient("777777")

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri))

    cfg = config.AppConfig(configured=True)
    root = tk.Tk()
    try:
        window = LookupWindow(root, cfg, on_back=lambda: None)
        window.record_number_var.set("777777")
        window._on_search()

        assert opened == []
        assert "alta" in window.status_label["text"].lower()
    finally:
        root.destroy()


def test_lookup_shows_not_found_message_for_unknown_record_number(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri))

    cfg = config.AppConfig(configured=True)
    root = tk.Tk()
    try:
        window = LookupWindow(root, cfg, on_back=lambda: None)
        window.record_number_var.set("000000")
        window._on_search()

        assert opened == []
        assert "não encontrado" in window.status_label["text"].lower()
    finally:
        root.destroy()


def test_lookup_requires_record_number_before_searching(tmp_path, monkeypatch):
    """Achado real (execução completa da suíte, 2026-08-25): sem mockar
    `messagebox.showerror`, este teste abre um diálogo NATIVO de verdade e
    fica bloqueado esperando alguém clicar OK -- ninguém clica num run
    automatizado, e o teste só "passava" depois de minutos (Windows acaba
    fechando o diálogo órfão sozinho, tempo variável). Isolado, nunca dava
    pra perceber -- só apareceu rodando a suíte inteira."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    shown = []
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda title, message: shown.append((title, message)))

    cfg = config.AppConfig(configured=True)
    root = tk.Tk()
    try:
        window = LookupWindow(root, cfg, on_back=lambda: None)
        window.record_number_var.set("")
        window._on_search()

        assert len(shown) == 1
    finally:
        root.destroy()


def test_lookup_never_modifies_existing_data(tmp_path, monkeypatch):
    """A busca é só-leitura de verdade -- diferente da versão antiga
    (DEC-031, nunca tocava o banco porque nem usava banco), a nova lê do
    banco real, mas não pode alterar nada nele."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path / "GSUSAuditoria"))
    _seed_active_patient_with_state("123456")
    monkeypatch.setattr("webbrowser.open", lambda uri: None)

    conn = database.init_db(config.get_db_path())
    before = dict(Repository(conn).get_patient_state("123456"))
    conn.close()

    cfg = config.AppConfig(configured=True)
    root = tk.Tk()
    try:
        window = LookupWindow(root, cfg, on_back=lambda: None)
        window.record_number_var.set("123456")
        window._on_search()
    finally:
        root.destroy()

    conn = database.init_db(config.get_db_path())
    after = dict(Repository(conn).get_patient_state("123456"))
    conn.close()
    assert before == after
