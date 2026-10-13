"""SCHEDULE-002 (pedido do auditor, DEC-129): desligar o computador ao fim da
atualização agendada. Nenhum teste aqui chama o `shutdown.exe` de verdade --
todo comando passa por um `runner` falso (ou por `subprocess.run` trocado),
e a trava de `app/shutdown.py` recusa o comando real dentro do pytest
(testada só pela função da trava, nunca chamando o caminho real)."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import tkinter as tk

from app import config, main, shutdown
from app.security import credentials
from app.storage import database
from app.storage.repository import Repository

SHUTDOWN_NOW = ["shutdown", "/s", "/t", "0"]


class FakeRunner:
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


def _cfg(enabled=True):
    return config.AppConfig(unit="Clínica Médica", configured=True, shutdown_after_scheduled_run=enabled)


def _run(cfg, runner, idle=(None,), ours=True, countdown=lambda seconds: False):
    """`idle`: valores devolvidos a cada consulta de uso recente (1ª antes da
    contagem, 2ª depois dela)."""
    idle_values = list(idle)
    return shutdown.after_scheduled_run(
        cfg, runner=runner,
        idle_seconds=lambda: idle_values.pop(0) if len(idle_values) > 1 else idle_values[0],
        console_is_ours=lambda: ours,
        countdown=countdown,
    )


def test_disabled_option_never_touches_windows():
    runner = FakeRunner()
    assert _run(_cfg(enabled=False), runner, idle=(9999.0,)) == shutdown.OUTCOME_DISABLED
    assert runner.calls == []


def test_recent_keyboard_or_mouse_use_skips_shutdown_without_any_warning():
    """A atualização da meia-noite pode acabar de manhã, com o auditor já
    usando a máquina -- desligar na cara dele seria pior que deixar ligado."""
    runner = FakeRunner()
    shown = []
    outcome = _run(_cfg(), runner, idle=(30.0,), countdown=lambda s: shown.append(s) or False)
    assert outcome == shutdown.OUTCOME_USER_ACTIVE
    assert runner.calls == [] and shown == []


def test_other_windows_session_at_the_console_skips_shutdown():
    """Troca rápida de usuário: o uso recente da outra pessoa não aparece no
    `GetLastInputInfo` desta sessão -- a checagem de sessão cobre esse caso."""
    runner = FakeRunner()
    outcome = _run(_cfg(), runner, idle=(3 * 3600.0,), ours=False)
    assert outcome == shutdown.OUTCOME_OTHER_SESSION
    assert runner.calls == []


def test_idle_machine_counts_down_in_the_app_then_shuts_down_without_forcing():
    """Achado da revisão adversarial: no shutdown.exe, `/t` maior que zero
    implica `/f` (fecha programas com trabalho não salvo à força). Nada pode
    ser enviado ao Windows antes do fim da contagem, e o comando final é
    `/t 0` sem `/f` -- assim trabalho não salvo segura o desligamento."""
    runner = FakeRunner()
    seen_before_countdown_ended = []

    def countdown(seconds):
        seen_before_countdown_ended.append(list(runner.calls))
        return False

    outcome = _run(_cfg(), runner, idle=(3 * 3600.0,), countdown=countdown)
    assert outcome == shutdown.OUTCOME_SHUTTING_DOWN
    assert seen_before_countdown_ended == [[]]
    assert runner.calls == [SHUTDOWN_NOW]
    assert "/f" not in runner.calls[0] and "/t" in runner.calls[0]
    assert runner.calls[0][runner.calls[0].index("/t") + 1] == "0"


def test_unknown_idle_and_session_do_not_block_shutdown():
    """`None` = a API não respondeu -- não é prova de que alguém está na
    máquina, então segue o pedido do auditor."""
    runner = FakeRunner()
    assert _run(_cfg(), runner, idle=(None,), ours=None) == shutdown.OUTCOME_SHUTTING_DOWN
    assert runner.calls == [SHUTDOWN_NOW]


def test_cancel_in_warning_window_sends_nothing_to_windows():
    runner = FakeRunner()
    outcome = _run(_cfg(), runner, countdown=lambda seconds: True)
    assert outcome == shutdown.OUTCOME_CANCELLED
    assert runner.calls == []


def test_someone_arriving_during_the_countdown_keeps_the_pc_on():
    runner = FakeRunner()
    outcome = _run(_cfg(), runner, idle=(3 * 3600.0, 20.0))
    assert outcome == shutdown.OUTCOME_USER_ACTIVE
    assert runner.calls == []


def test_warning_window_failure_keeps_the_pc_on():
    """Sem janela ninguém conseguiria cancelar -- então não desliga."""
    runner = FakeRunner()

    def broken_countdown(seconds):
        raise tk.TclError("no display name")

    assert _run(_cfg(), runner, countdown=broken_countdown) == shutdown.OUTCOME_NO_WINDOW
    assert runner.calls == []


def test_windows_refusal_is_reported():
    runner = FakeRunner(returncode=1, stderr="Acesso negado.")
    assert _run(_cfg(), runner) == shutdown.OUTCOME_FAILED
    assert runner.calls == [SHUTDOWN_NOW]


def test_windows_refusing_because_another_user_is_signed_in_is_reported_as_other_session():
    """Achado da 2ª revisão: sem `/f`, o Windows recusa (código 1191) quando
    outra conta continua conectada pela troca rápida de usuário -- a faixa
    não pode culpar uma "regra de segurança" que não existe."""
    runner = FakeRunner(returncode=shutdown.ERROR_SHUTDOWN_USERS_LOGGED_ON, stderr="Outros usuários conectados.")
    assert _run(_cfg(), runner) == shutdown.OUTCOME_OTHER_SESSION
    assert "outra sessão do Windows" in shutdown.DIAGNOSTIC_NOTES[shutdown.OUTCOME_OTHER_SESSION]


def test_every_outcome_that_leaves_the_pc_on_has_a_plain_language_note():
    for outcome in (
        shutdown.OUTCOME_USER_ACTIVE, shutdown.OUTCOME_OTHER_SESSION, shutdown.OUTCOME_CANCELLED,
        shutdown.OUTCOME_NO_WINDOW, shutdown.OUTCOME_FAILED,
    ):
        assert "computador" in shutdown.DIAGNOSTIC_NOTES[outcome] or "desligamento" in shutdown.DIAGNOSTIC_NOTES[outcome]
    assert shutdown.OUTCOME_SHUTTING_DOWN not in shutdown.DIAGNOSTIC_NOTES
    assert shutdown.OUTCOME_DISABLED not in shutdown.DIAGNOSTIC_NOTES


def test_real_shutdown_command_is_refused_inside_pytest():
    """Trava contra um teste futuro desligar a máquina de desenvolvimento
    (classe do incidente DEC-123). Só a função da trava é chamada aqui --
    nunca `shutdown_now` com o runner real."""
    assert "PYTEST_CURRENT_TEST" in os.environ
    with pytest.raises(shutdown.ShutdownError):
        shutdown._refuse_inside_tests(shutdown._REAL_RUN)
    shutdown._refuse_inside_tests(FakeRunner())  # runner falso passa


def test_runner_is_resolved_at_call_time_so_patching_subprocess_run_is_honoured(monkeypatch):
    """Achado da revisão adversarial: com `runner=subprocess.run` como valor
    padrão, um teste que troca `subprocess.run` (padrão do projeto) não
    trocava o que seria executado. Agora o runner é resolvido na chamada; a
    trava continua recusando a função real."""
    fake = FakeRunner()
    monkeypatch.setattr(subprocess, "run", fake)
    assert shutdown._REAL_RUN is not fake
    assert shutdown.after_scheduled_run(
        _cfg(), idle_seconds=lambda: None, console_is_ours=lambda: None, countdown=lambda s: False,
    ) == shutdown.OUTCOME_SHUTTING_DOWN
    assert fake.calls == [SHUTDOWN_NOW]


def test_windows_probes_return_none_or_a_sane_value():
    idle = shutdown.seconds_since_last_input()
    ours = shutdown.console_session_is_ours()
    if sys.platform == "win32":
        assert idle is None or idle >= 0
        assert ours in (True, False, None)
    else:
        assert idle is None and ours is None


# ----------------------------------------------- integração com --auto-update

def _configured(monkeypatch, tmp_path, outcome="DESLIGANDO", enabled=True):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("11122233344", "senha"))
    cfg = config.AppConfig(
        gsus_username="11122233344", unit="Clínica Médica", configured=True,
        shutdown_after_scheduled_run=enabled,
    )
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    calls = []
    monkeypatch.setattr(shutdown, "after_scheduled_run", lambda app_config: calls.append(app_config) or outcome)
    return cfg, calls


def _fake_successful_run(monkeypatch, save_diagnostic=True):
    from app.orchestrator import RunResult

    def fake_run_update(app_config, report_path, progress=None):
        if save_diagnostic:
            conn = database.init_db(config.get_db_path())
            Repository(conn).save_run_diagnostic(None, "SUCESSO", "Atualização concluída.", False)
            conn.close()
        return RunResult(
            run_id="r1", counts={"found": 1, "completed": 1, "failed": 0, "no_admission": 0}, report_path=report_path,
        )

    monkeypatch.setattr("app.update_flow.run_update", fake_run_update)


def _latest_summary():
    conn = database.init_db(config.get_db_path())
    try:
        return Repository(conn).get_latest_run_diagnostic()["summary"]
    finally:
        conn.close()


def test_auto_update_offers_shutdown_after_a_completed_run(tmp_path, monkeypatch):
    cfg, calls = _configured(monkeypatch, tmp_path)
    _fake_successful_run(monkeypatch)
    main.run_auto_update()
    assert calls == [cfg]
    assert _latest_summary() == "Atualização concluída."  # desligou: nada a explicar


def test_auto_update_explains_on_screen_why_the_pc_stayed_on(tmp_path, monkeypatch):
    _configured(monkeypatch, tmp_path, outcome=shutdown.OUTCOME_USER_ACTIVE)
    _fake_successful_run(monkeypatch)
    main.run_auto_update()
    assert _latest_summary() == "Atualização concluída. " + shutdown.DIAGNOSTIC_NOTES[shutdown.OUTCOME_USER_ACTIVE]


def test_auto_update_rereads_the_option_before_deciding(tmp_path, monkeypatch):
    """Achado da revisão adversarial: a execução pode durar a madrugada; se
    o auditor desligou a opção nas Configurações nesse meio tempo, vale o
    que está gravado agora, não o que foi lido à meia-noite."""
    _configured(monkeypatch, tmp_path)
    loaded = [
        config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True,
                         shutdown_after_scheduled_run=True),
        config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True,
                         shutdown_after_scheduled_run=False),
    ]
    monkeypatch.setattr(config, "load_config", lambda: loaded.pop(0))
    decided_with = []
    monkeypatch.setattr(
        shutdown, "after_scheduled_run",
        lambda app_config: decided_with.append(app_config.shutdown_after_scheduled_run) or "DESATIVADO",
    )
    _fake_successful_run(monkeypatch)
    main.run_auto_update()
    assert decided_with == [False]


def test_auto_update_offers_shutdown_even_after_a_failed_run(tmp_path, monkeypatch):
    """Pergunta 1b ao auditor (padrão interno): uma falha não é motivo pra
    deixar o PC ligado -- o motivo aparece na tela de manhã."""
    cfg, calls = _configured(monkeypatch, tmp_path)

    def _raise(*a, **k):
        raise RuntimeError("falha simulada de GSUS")

    monkeypatch.setattr("app.update_flow.run_update", _raise)
    main.run_auto_update()
    assert calls == [cfg]


def test_auto_update_never_shuts_down_when_another_update_is_running(tmp_path, monkeypatch):
    from app.update_flow import UpdateAlreadyRunningError

    _cfg_obj, calls = _configured(monkeypatch, tmp_path)

    def _raise(*a, **k):
        raise UpdateAlreadyRunningError("simulado: atualização manual em andamento")

    monkeypatch.setattr("app.update_flow.run_update", _raise)
    main.run_auto_update()
    assert calls == []


def test_auto_update_never_shuts_down_when_app_is_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)
    calls = []
    monkeypatch.setattr(shutdown, "after_scheduled_run", lambda app_config: calls.append(app_config))
    main.run_auto_update()
    assert calls == []


def test_auto_update_survives_unexpected_error_in_shutdown_step(tmp_path, monkeypatch):
    _configured(monkeypatch, tmp_path)
    _fake_successful_run(monkeypatch, save_diagnostic=False)

    def _boom(app_config):
        raise RuntimeError("erro inesperado")

    monkeypatch.setattr(shutdown, "after_scheduled_run", _boom)
    main.run_auto_update()  # tarefa agendada não tem quem trate a exceção


# ------------------------------------------------------- janela de aviso (Tk)

def _tk_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


tk_only = pytest.mark.skipif(not _tk_available(), reason="Sem display Tk disponível neste ambiente")


@tk_only
def test_countdown_reaches_zero_and_closes_without_cancelling():
    from app.ui.shutdown_window import ShutdownCountdown

    root = tk.Tk()
    countdown = ShutdownCountdown(root, 2)
    assert countdown.time_label["text"] == "0:02"
    countdown.tick()
    assert countdown.time_label["text"] == "0:01"
    countdown.tick()
    assert countdown.cancelled is False
    with pytest.raises(tk.TclError):
        root.winfo_exists()  # janela destruída ao chegar a zero


@tk_only
def test_countdown_cancel_button_closes_and_reports_cancel():
    from app.ui.shutdown_window import ShutdownCountdown

    root = tk.Tk()
    countdown = ShutdownCountdown(root, 180)
    countdown.cancel_button.invoke()
    assert countdown.cancelled is True
    with pytest.raises(tk.TclError):
        root.winfo_exists()


@tk_only
def test_closing_the_warning_window_counts_as_cancel():
    """Quem fecha a janela está na frente da máquina -- não desliga."""
    from app.ui.shutdown_window import ShutdownCountdown

    root = tk.Tk()
    countdown = ShutdownCountdown(root, 180)
    root.tk.call(root.protocol("WM_DELETE_WINDOW"))  # mesmo callback do X da janela
    assert countdown.cancelled is True


def test_format_remaining():
    from app.ui.shutdown_window import format_remaining

    assert format_remaining(180) == "3:00"
    assert format_remaining(61) == "1:01"
    assert format_remaining(-5) == "0:00"
