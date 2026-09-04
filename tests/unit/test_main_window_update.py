"""Testa a integração real de 'Atualizar agora' (orchestrator + adapter),
com GSUSClient/GSUSAdapter substituídos por fakes -- sem Playwright real,
sem GSUS real. Só UM tk.Tk() por teste (ver test_app_shell.py sobre
múltiplas instâncias serem instáveis no processo)."""
import os
import sys
import threading
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
    # UI-006: registra o `headless` que a tela pediu em cada abertura de sessão.
    launched_headless: list[bool] = []

    def __init__(self, base_url, **kwargs):
        self.page = object()
        self.base_url = base_url
        FakeClient.launched_headless.append(bool(kwargs.get("headless", False)))

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


# ------------------------------------------------------------ UI-006

class FakeAdapterSlowThreePatients:
    """Tres pacientes; a extracao do 1o sinaliza `started` e demora ~0,6 s --
    o teste espera esse sinal (nao um texto de status, que pode ser
    sobrescrito no mesmo ciclo de poll) e so entao clica "Encerrar"."""

    started = threading.Event()

    def __init__(self, page, username, password, unit, base_url=None, max_days_per_patient=None):
        pass

    def get_census(self):
        return [Patient(record_number=r, bed="2A", unit="Clínica Médica") for r in ("100", "200", "300")]

    def get_raw_notes_text(self, patient, known_days=frozenset()):
        FakeAdapterSlowThreePatients.started.set()
        time.sleep(0.6)
        return "20/08/2026 08:00 - Clinica Medica - Evolucao\nPaciente estavel, sem pendencias."


def _cfg_without_llm() -> config.AppConfig:
    return config.AppConfig(
        gsus_username="11122233344",
        unit="Clínica Médica",
        configured=True,
        model_path="nao-existe/model.gguf",
        llm_server_path="nao-existe/llama-server.exe",
    )


def test_cancel_button_stops_update_after_current_patient(tmp_path, monkeypatch):
    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterSlowThreePatients)

    FakeAdapterSlowThreePatients.started.clear()
    root = tk.Tk()
    try:
        window = MainWindow(root, _cfg_without_llm())
        assert str(window.cancel_button["state"]) == "disabled"  # nada rodando: apagado
        assert window.browser_switch.is_enabled() is True

        window._on_update()
        assert str(window.cancel_button["state"]) == "normal"
        assert window.browser_switch.is_enabled() is False  # escolha so vale na proxima

        deadline = time.monotonic() + 5
        while not FakeAdapterSlowThreePatients.started.is_set() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert FakeAdapterSlowThreePatients.started.is_set()  # 1o paciente em coleta agora

        window._on_cancel_update()
        assert "Encerrando" in window.status_label["text"]
        assert str(window.cancel_button["state"]) == "disabled"  # clique unico

        _pump_until_done(root, window, timeout=10)

        status_text = window.status_label["text"]
        assert "Interrompida pelo usuário" in status_text
        assert "/3 pacientes" in status_text
        assert "3/3" not in status_text  # parou antes de terminar todo mundo
        assert str(window.update_button["state"]) == "normal"
        assert str(window.cancel_button["state"]) == "disabled"
        assert window.browser_switch.is_enabled() is True
        assert not window._report_path.exists()  # relatorio parcial nunca e gravado
    finally:
        root.destroy()


def test_browser_mode_switch_persists_and_drives_headless(tmp_path, monkeypatch):
    import json

    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module

    FakeClient.launched_headless = []
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterOk)

    cfg = _cfg_without_llm()
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        assert window.browser_switch.get() is True  # padrao: navegador visivel (DEC-077)
        assert "Navegador visível" in window._browser_mode_label["text"]

        window.browser_switch.toggle()  # -> segundo plano
        assert cfg.browser_visible is False
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["browser_visible"] is False  # persistido na hora (vale pra tarefa agendada)
        assert "Segundo plano" in window._browser_mode_label["text"]

        window._on_update()
        _pump_until_done(root, window)
        assert "Atualizado" in window.status_label["text"]
        assert FakeClient.launched_headless == [True]

        window.browser_switch.toggle()  # -> visivel de novo
        assert cfg.browser_visible is True
        window._on_update()
        _pump_until_done(root, window)
        assert FakeClient.launched_headless == [True, False]
    finally:
        root.destroy()


def test_error_in_background_mode_adds_hint_to_switch_back(tmp_path, monkeypatch):
    """DEC-077: o GSUS ja bloqueou navegador oculto antes -- quando uma
    atualizacao em segundo plano falha, a dica mais provavel precisa
    aparecer na tela, nao so a mensagem generica."""
    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module

    class FakeAdapterLoginFails:
        def __init__(self, page, username, password, unit, base_url=None, max_days_per_patient=None):
            pass

        def get_census(self):
            from app.gsus.login import GSUSLoginError
            raise GSUSLoginError("simulado: pop-up de login nao abriu")

        def get_raw_notes_text(self, patient, known_days=frozenset()):
            raise AssertionError("nao deveria ser chamado")

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterLoginFails)

    cfg = _cfg_without_llm()
    cfg.browser_visible = False
    root = tk.Tk()
    try:
        window = MainWindow(root, cfg)
        window._on_update()
        _pump_until_done(root, window)
        status_text = window.status_label["text"]
        assert "Navegador visível" in status_text
        assert "Traceback" not in status_text
    finally:
        root.destroy()


def test_gsus_unresponsive_breaker_shows_clear_status(tmp_path, monkeypatch):
    """DEC-117: quando o disjuntor interrompe, a tela diz que foi o GSUS, nao
    o programa, e quantos pacientes ficaram."""
    from app.gsus import adapter as gsus_adapter_module
    from app.gsus import client as gsus_client_module
    from app.gsus.records import GSUSSearchUnresponsiveError

    class FakeAdapterUnresponsive:
        def __init__(self, page, username, password, unit, base_url=None, max_days_per_patient=None):
            pass

        def get_census(self):
            return [Patient(record_number=str(100 + i), bed="2A", unit="Clínica Médica") for i in range(7)]

        def reset_session(self):
            pass

        def get_raw_notes_text(self, patient, known_days=frozenset()):
            raise GSUSSearchUnresponsiveError("simulado: tela de busca não respondeu em 5 tentativas")

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(gsus_client_module, "GSUSClient", FakeClient)
    monkeypatch.setattr(gsus_adapter_module, "GSUSAdapter", FakeAdapterUnresponsive)

    root = tk.Tk()
    try:
        window = MainWindow(root, _cfg_without_llm())
        window._on_update()
        _pump_until_done(root, window)
        status_text = window.status_label["text"]
        assert "GSUS parou de responder" in status_text
        assert "0/7" in status_text
        assert str(window.update_button["state"]) == "normal"
    finally:
        root.destroy()
