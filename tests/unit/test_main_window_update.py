"""Testa a integração real de 'Atualizar agora' (orchestrator + adapter),
com GSUSClient/GSUSAdapter substituídos por fakes -- sem Playwright real,
sem GSUS real. Só UM tk.Tk() por teste (ver test_app_shell.py sobre
múltiplas instâncias serem instáveis no processo)."""
import os
import sys
import time
import tkinter as tk

import pytest

from app import config
from app.analysis.llm import LLMStartupError
from app.models import Patient
from app.security import credentials
from app.ui.main_window import MainWindow


def _tk_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


pytestmark = pytest.mark.skipif(not _tk_available(), reason="Sem display Tk disponível neste ambiente")


@pytest.fixture(autouse=True)
def no_real_model_download(monkeypatch):
    """`_run_update_worker` chama `ensure_model_downloaded` (BUILD-001) antes
    de qualquer coisa relacionada a LLM -- nenhum teste deste arquivo pode
    depender de rede de verdade (nem "por acaso" funcionar só porque este
    repositório de dev já tem o modelo baixado, mesmo motivo do DEC-069)."""
    monkeypatch.setattr("app.analysis.model_downloader.ensure_model_downloaded", lambda *a, **k: None)


def _pump_until_done(root: tk.Tk, window: MainWindow, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while window._updating and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)


class FakeClient:
    def __init__(self, base_url):
        self.page = object()
        self.base_url = base_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def goto(self, path: str = "") -> None:
        pass


class FakeAdapterOk:
    def __init__(self, page, username, password, unit, base_url=None, max_days_per_patient=None):
        pass

    def get_census(self):
        return [Patient(record_number="100", bed="2A", unit="Clínica Médica")]

    def get_raw_notes_text(self, patient, known_days=frozenset()):
        return "20/08/2026 08:00 - Clinica Medica - Evolucao\nPaciente estavel, sem pendencias."


def test_update_flow_success_updates_status_and_writes_report(tmp_path, monkeypatch):
    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterOk)

    # Caminhos de modelo/servidor propositalmente inexistentes: este teste
    # usa fakes para GSUS e não configura LLM algum -- não pode depender de
    # LocalLLM.start() falhar "por acaso" (ex.: se este repositório tiver
    # modelo/llama-server reais em models/runtime de outra validação, o
    # resolve_app_path (DEC-069) os resolveria corretamente e um LocalLLM de
    # verdade tentaria subir dentro do teste).
    cfg = config.AppConfig(
        gsus_username="11122233344",
        unit="Clínica Médica",
        configured=True,
        model_path="nao-existe/model.gguf",
        llm_server_path="nao-existe/llama-server.exe",
    )
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._on_update()
        _pump_until_done(root, window)

        assert "Atualizado" in window.status_label["text"]
        assert window._report_path.exists()
    finally:
        root.destroy()


def test_update_flow_without_saved_credential_shows_friendly_message(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)

    cfg = config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True)
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._on_update()
        _pump_until_done(root, window)

        status_text = window.status_label["text"]
        assert "Traceback" not in status_text
        assert "RuntimeError" not in status_text
        assert status_text.strip() != ""
    finally:
        root.destroy()


class FakeLLMOk:
    """Sucesso mínimo: nunca reporta pendência, só prova que a análise foi
    de fato chamada com as notas novas (achado DEC-058: o botão real nunca
    chamava o LLM -- `llm=None` fixo)."""
    instances: list["FakeLLMOk"] = []

    def __init__(self, model_path=None, server_path=None, host=None, port=None, server_log_path=None):
        self.model_path = model_path
        self.server_path = server_path
        self.analyze_calls = 0
        self.started = False
        self.stopped = False
        FakeLLMOk.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None):
        self.analyze_calls += 1
        return {"insufficient_information": True}


class FakeLLMStartupFails:
    def __init__(self, model_path=None, server_path=None, host=None, port=None, server_log_path=None):
        pass

    def start(self):
        raise LLMStartupError("simulado: modelo não encontrado")

    def stop(self):
        raise AssertionError("stop() não deveria ser chamado se start() falhou")


def test_update_flow_calls_llm_when_it_starts_successfully(tmp_path, monkeypatch):
    """Regressão direta do achado DEC-058: garante que 'Atualizar agora'
    realmente invoca `LocalLLM.analyze_patient`, não só as regras
    determinísticas -- o bug original passava silenciosamente porque nenhum
    teste verificava isso, só que o relatório era gerado."""
    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module
    import app.ui.main_window as main_window_module

    FakeLLMOk.instances = []
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterOk)
    monkeypatch.setattr("app.analysis.llm.LocalLLM", FakeLLMOk)

    cfg = config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True)
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._on_update()
        _pump_until_done(root, window)

        assert "Atualizado" in window.status_label["text"]
        assert len(FakeLLMOk.instances) == 1
        assert FakeLLMOk.instances[0].analyze_calls == 1
        assert FakeLLMOk.instances[0].started is True
        assert FakeLLMOk.instances[0].stopped is True
        # DEC-069: model_path/server_path default são relativos
        # ("models/model.gguf") -- o botão real tem que resolvê-los contra
        # a pasta de instalação/projeto (config.resolve_app_path), nunca
        # passar a string relativa crua adiante (achado real, DEC-059).
        from pathlib import Path
        assert Path(FakeLLMOk.instances[0].model_path).is_absolute()
        assert Path(FakeLLMOk.instances[0].server_path).is_absolute()
    finally:
        root.destroy()


def test_update_flow_continues_without_llm_when_startup_fails(tmp_path, monkeypatch):
    """LLMStartupError não pode abortar a atualização inteira -- mesma
    semântica que app/ui/errors.py::friendly_message já previa para esse
    erro ('a atualização continuou sem ele'). Regras determinísticas e
    relatório continuam funcionando sem análise por IA."""
    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterOk)
    monkeypatch.setattr("app.analysis.llm.LocalLLM", FakeLLMStartupFails)

    cfg = config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True)
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._on_update()
        _pump_until_done(root, window)

        assert "Atualizado" in window.status_label["text"]
        assert window._report_path.exists()
    finally:
        root.destroy()


def test_update_flow_maps_not_implemented_error_to_friendly_message(tmp_path, monkeypatch):
    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module

    class FakeAdapterBlocked:
        def __init__(self, page, username, password, unit, base_url=None, max_days_per_patient=None):
            pass

        def get_census(self):
            raise NotImplementedError("BLOCKED_GSUS: seletor pendente")

        def get_raw_notes_text(self, patient, known_days=frozenset()):
            raise AssertionError("não deveria ser chamado")

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterBlocked)

    # Mesmo motivo do teste de sucesso acima: LLM não é o alvo deste teste
    # e não pode depender de LocalLLM.start() falhar "por acaso".
    cfg = config.AppConfig(
        gsus_username="11122233344",
        unit="Clínica Médica",
        configured=True,
        model_path="nao-existe/model.gguf",
        llm_server_path="nao-existe/llama-server.exe",
    )
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._on_update()
        _pump_until_done(root, window)

        status_text = window.status_label["text"]
        assert "NotImplementedError" not in status_text
        assert "BLOCKED_GSUS" not in status_text
        assert "desenvolvimento" in status_text.lower()
    finally:
        root.destroy()
