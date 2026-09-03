"""Teste de fumaça do shell da aplicação (APP-002) + transição
configuração -> tela principal sem reabrir o app (fix de UX/flake — ver
CURRENT_STATE.md).

Cria no máximo UM tk.Tk() por teste (várias instâncias no mesmo processo
Python são conhecidas por serem instáveis nesta versão de Tcl/Tk no Windows).
"""
import tkinter as tk
from tkinter import ttk

import pytest

from app import config, main
from app.security import credentials


def _tk_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


pytestmark = pytest.mark.skipif(not _tk_available(), reason="Sem display Tk disponível neste ambiente")


def _find_buttons_recursive(widget) -> list[tk.Button]:
    """`MainWindow` (REPORT-004/DEC-099) passou a agrupar os botões dentro
    de frames (barra de controle) em vez de serem filhos diretos de `root`
    -- uma busca só em `root.winfo_children()` não os encontra mais.
    `ttk.Button` (design/UX, 2026-09-03) não é subclasse de `tk.Button` --
    precisa checar as duas classes, senão os botões da tela principal
    (agora `ttk.Button`, pro estilo customizado) somem desta busca."""
    found = [w for w in widget.winfo_children() if isinstance(w, (tk.Button, ttk.Button))]
    for child in widget.winfo_children():
        found.extend(_find_buttons_recursive(child))
    return found


def test_is_configured_false_without_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)
    cfg = config.AppConfig(gsus_username="12345678900", unit="Clínica Médica", configured=True)
    assert main.is_configured(cfg) is False


def test_build_app_shows_setup_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)

    root = main.build_app()
    try:
        assert root.title() == "GSUS Auditoria"
        buttons = [w for w in root.winfo_children() if isinstance(w, (tk.Button, ttk.Button))]
        assert any(b["text"] == "CONCLUIR" for b in buttons)
    finally:
        root.destroy()


def test_setup_completion_transitions_to_main_window_in_place(tmp_path, monkeypatch):
    """Reproduz o bug corrigido: concluir a configuração não deve fechar o
    app -- deve mostrar a tela principal na mesma janela, sem criar um novo
    tk.Tk()."""
    from app.ui import setup_window as setup_window_module
    from app.ui.setup_window import SetupWindow

    fake_store: dict[str, tuple[str, str]] = {}

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: fake_store.get(name))
    monkeypatch.setattr(credentials, "save_credential", lambda name, user, pwd: fake_store.__setitem__(name, (user, pwd)))
    monkeypatch.setattr(setup_window_module.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(setup_window_module.messagebox, "showerror", lambda *a, **k: None)

    root = tk.Tk()
    try:
        app_config = config.AppConfig()
        setup = SetupWindow(root, app_config, on_complete=lambda: main.render(root, app_config))

        setup.username_var.set("12345678900")
        setup.password_var.set("senha-teste")
        setup.unit_var.set("Clínica Médica")
        setup._on_submit()

        buttons = _find_buttons_recursive(root)
        assert any(b["text"] == "ATUALIZAR AGORA" for b in buttons)
        assert not any(b["text"] == "CONCLUIR" for b in buttons)
    finally:
        root.destroy()


def test_navigating_from_main_window_to_setup_resets_inherited_minsize(tmp_path, monkeypatch):
    """Achado real de auditoria adversarial (DEC-101): `MainWindow` (tela
    única, REPORT-004) chama `root.minsize(1024, 700)` -- isso sobrevive à
    troca de tela (`app/main.py::render` só destrói widgets, nunca reseta
    minsize sozinho). Sem `SetupWindow` resetar isso explicitamente, a tela
    de configuração (pensada pra abrir pequena, 360x280) ficava presa no
    mínimo herdado da dashboard, abrindo enorme em vez de compacta."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))

    cfg = config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True)
    root = tk.Tk()
    try:
        main.render(root, cfg)  # mostra MainWindow -- fixa minsize(1024, 700)
        assert root.minsize() == (1024, 700)

        main._show_setup(root, cfg)  # simula clique em "Configurações"

        assert root.minsize() != (1024, 700)
    finally:
        root.destroy()


# --------------------------------- SetupWindow agenda a tarefa (INSTALL-001)
# Achado real: `scheduling.register_daily_task` existia e era testado desde
# SCHEDULE-001, mas nada no app chamava -- nem um instalador perfeito faria
# a tarefa aparecer de verdade. `_on_submit` passou a chamar
# `_register_scheduled_task`; só age em modo `frozen` (dev não tem um `.exe`
# de verdade pra agendar).

def _submit_setup(root, app_config, monkeypatch, on_complete=lambda: None):
    from app.ui.setup_window import SetupWindow

    monkeypatch.setattr(credentials, "get_credential", lambda name: None)
    monkeypatch.setattr(credentials, "save_credential", lambda name, user, pwd: None)

    setup = SetupWindow(root, app_config, on_complete=on_complete)
    setup.username_var.set("12345678900")
    setup.password_var.set("senha-teste")
    setup.unit_var.set("Clínica Médica")
    setup._on_submit()
    return setup


def test_setup_does_not_schedule_task_in_dev_mode(tmp_path, monkeypatch):
    from app.ui import setup_window as setup_window_module

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.delattr(setup_window_module.sys, "frozen", raising=False)
    monkeypatch.setattr(setup_window_module.messagebox, "showinfo", lambda *a, **k: None)

    called = []
    monkeypatch.setattr(setup_window_module.scheduling, "register_daily_task", lambda *a, **k: called.append(a))

    root = tk.Tk()
    try:
        _submit_setup(root, config.AppConfig(), monkeypatch)
        assert called == []
    finally:
        root.destroy()


def test_setup_schedules_task_when_frozen(tmp_path, monkeypatch):
    from app.ui import setup_window as setup_window_module

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(setup_window_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(setup_window_module.sys, "executable", r"C:\Program Files\GSUS Auditoria\gsus-auditoria.exe", raising=False)
    monkeypatch.setattr(setup_window_module.messagebox, "showinfo", lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(
        setup_window_module.scheduling, "register_daily_task", lambda *a, **k: calls.append((a, k))
    )

    root = tk.Tk()
    try:
        app_config = config.AppConfig(schedule_time="03:30")
        _submit_setup(root, app_config, monkeypatch)
        assert calls == [
            ((r"C:\Program Files\GSUS Auditoria\gsus-auditoria.exe", "03:30"), {"arguments": "--auto-update"})
        ]
    finally:
        root.destroy()


def test_setup_completes_even_when_scheduling_fails(tmp_path, monkeypatch):
    """Achado do próprio design: falha ao agendar não pode travar a
    configuração inteira -- usuário ainda pode atualizar manualmente. Só
    avisa (messagebox.showwarning), nunca silencioso (é um gap de
    conformidade real se ninguém perceber que a rotina automática não
    está agendada)."""
    from app.ui import setup_window as setup_window_module

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(setup_window_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(setup_window_module.sys, "executable", r"C:\app.exe", raising=False)
    monkeypatch.setattr(setup_window_module.messagebox, "showinfo", lambda *a, **k: None)

    def _raise(*a, **k):
        raise setup_window_module.scheduling.SchedulingError("acesso negado")

    monkeypatch.setattr(setup_window_module.scheduling, "register_daily_task", _raise)
    warnings = []
    monkeypatch.setattr(setup_window_module.messagebox, "showwarning", lambda title, msg: warnings.append((title, msg)))

    on_complete_calls = []
    root = tk.Tk()
    try:
        _submit_setup(root, config.AppConfig(), monkeypatch, on_complete=lambda: on_complete_calls.append(True))
        assert len(warnings) == 1
        assert on_complete_calls == [True]  # configuração completou mesmo assim
    finally:
        root.destroy()


# ------------------------------- run_auto_update / --auto-update (SCHEDULE-001)
# Achado real: a tarefa agendada só abria a janela do app -- não existe
# humano pra clicar "Atualizar agora" numa execução de madrugada. `main()`
# agora despacha pra `run_auto_update()` quando chamado com `--auto-update`
# (é isso que `register_daily_task` põe no `/TR` da tarefa, ver DEC-075).

def test_run_auto_update_skips_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)  # não configurado

    called = []
    monkeypatch.setattr("app.update_flow.run_update", lambda *a, **k: called.append((a, k)))

    main.run_auto_update()

    assert called == []


def test_run_auto_update_calls_run_update_when_configured(tmp_path, monkeypatch):
    from app.orchestrator import RunResult

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))

    cfg = config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True)
    monkeypatch.setattr(config, "load_config", lambda: cfg)

    calls = []

    def fake_run_update(app_config, report_path, progress=None):
        calls.append((app_config, report_path))
        return RunResult(run_id="r1", counts={"found": 1, "completed": 1, "failed": 0, "no_admission": 0}, report_path=report_path)

    monkeypatch.setattr("app.update_flow.run_update", fake_run_update)

    main.run_auto_update()

    assert len(calls) == 1
    assert calls[0][0] is cfg
    assert calls[0][1] == config.get_app_data_dir() / "relatorio.html"


def test_run_auto_update_does_not_raise_when_run_update_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(
        config, "load_config",
        lambda: config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True),
    )

    def _raise(*a, **k):
        raise RuntimeError("falha simulada de GSUS/LLM")

    monkeypatch.setattr("app.update_flow.run_update", _raise)

    main.run_auto_update()  # não deve levantar -- tarefa agendada não tem quem trate a exceção


def test_run_auto_update_skips_gracefully_when_another_update_is_already_running(tmp_path, monkeypatch):
    """Achado real (DEC-086/088): a tarefa agendada disparou em cima de um
    clique manual já em andamento. Diferente de uma falha real, isso deve
    ser tratado como um "pulo" gracioso -- mesmo padrão do "app não
    configurado" -- não uma exceção não tratada nem um log de erro alarmante."""
    from app.update_flow import UpdateAlreadyRunningError

    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    monkeypatch.setattr(
        config, "load_config",
        lambda: config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True),
    )

    def _raise(*a, **k):
        raise UpdateAlreadyRunningError("simulado: outra atualização em andamento")

    monkeypatch.setattr("app.update_flow.run_update", _raise)

    main.run_auto_update()  # não deve levantar


def test_main_dispatches_to_auto_update_and_skips_gui(monkeypatch):
    monkeypatch.setattr(main.sys, "argv", ["gsus-auditoria.exe", "--auto-update"])
    monkeypatch.setattr(main.config, "configure_playwright_browsers_path", lambda: None)
    monkeypatch.setattr(main, "setup_logging", lambda: None)

    auto_update_calls = []
    monkeypatch.setattr(main, "run_auto_update", lambda: auto_update_calls.append(True))

    def _fail_if_called():
        raise AssertionError("main() não deveria abrir a GUI quando --auto-update está presente")

    monkeypatch.setattr(main, "build_app", _fail_if_called)

    main.main()

    assert auto_update_calls == [True]
