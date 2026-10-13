"""Desligamento opcional do computador ao fim da atualização AGENDADA
(SCHEDULE-002, pedido do auditor, DEC-129: "para que o PC não fique ligado a
madrugada toda").

Chamado só por `app/main.py::run_auto_update`, DEPOIS de `run_update`
retornar -- nesse ponto o navegador, o llama-server e a trava de instância já
foram liberados e todo o resultado está no banco. O "Atualizar agora" manual
nunca passa por aqui.

Regras (cada "não desliga" vira uma frase no diagnóstico da tela principal,
`DIAGNOSTIC_NOTES`, pra o auditor entender de manhã por que o PC ficou ligado):
- Opção desligada (padrão) -> nada acontece.
- Alguém usou teclado/mouse nos últimos `USER_ACTIVE_WINDOW_SECONDS`, ou a
  sessão do Windows na frente da máquina é de outra pessoa -> não desliga (a
  atualização que começou à meia-noite pode terminar de manhã, com gente
  trabalhando no computador).
- Senão: janela de aviso com contagem de `WARNING_SECONDS` e "Cancelar
  desligamento". Só ao fim da contagem, e se ninguém mexeu no computador
  durante ela, roda `shutdown /s /t 0`.

Por que a contagem é do app e não do Windows (achado da revisão adversarial
da Fase 1): no `shutdown.exe`, qualquer `/t` maior que zero IMPLICA `/f` --
o Windows fecharia à força programas com trabalho não salvo. Com `/t 0` e
sem `/f`, um programa com alteração não salva segura o desligamento (tela
"aplicativos impedindo o desligamento") e o PC fica ligado. Se este processo
morrer no meio da contagem, nada é enviado: falha sempre do lado de deixar
ligado.

Sem dependência nova: `shutdown.exe` é nativo do Windows, e uso recente e
sessão ativa vêm de `GetLastInputInfo`/`WTSGetActiveConsoleSessionId` via
ctypes (mesmo padrão de `app/security/credentials.py`).
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from typing import Callable

logger = logging.getLogger(__name__)

WARNING_SECONDS = 180
USER_ACTIVE_WINDOW_SECONDS = 600

OUTCOME_DISABLED = "DESATIVADO"
OUTCOME_USER_ACTIVE = "USUARIO_ATIVO"
OUTCOME_OTHER_SESSION = "OUTRA_SESSAO"
OUTCOME_CANCELLED = "CANCELADO"
OUTCOME_NO_WINDOW = "SEM_JANELA"
OUTCOME_FAILED = "FALHOU"
OUTCOME_SHUTTING_DOWN = "DESLIGANDO"

# Frase acrescentada ao diagnóstico da última execução (faixa da tela
# principal) sempre que a opção estava ligada e o computador NÃO desligou.
DIAGNOSTIC_NOTES = {
    OUTCOME_USER_ACTIVE: "O computador não foi desligado porque estava em uso quando a atualização terminou.",
    OUTCOME_OTHER_SESSION: (
        "O computador não foi desligado porque havia outra sessão do Windows aberta nele "
        "(outro usuário, troca de usuário ou acesso remoto)."
    ),
    OUTCOME_CANCELLED: "O desligamento automático foi cancelado na tela de aviso.",
    OUTCOME_NO_WINDOW: "O computador não foi desligado: a tela de aviso do desligamento não pôde ser exibida.",
    OUTCOME_FAILED: "O computador não foi desligado: o Windows recusou o pedido de desligamento.",
}

# Sem `/f`, o Windows recusa desligar com outro usuário ainda conectado
# (troca rápida de usuário) -- é o caso "outra sessão", não uma recusa genérica.
ERROR_SHUTDOWN_USERS_LOGGED_ON = 1191

# A função REAL, guardada no import: a trava de teste compara com ela, nunca
# com `subprocess.run` no momento da chamada (que um teste pode ter trocado).
_REAL_RUN = subprocess.run
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NO_CONSOLE_SESSION = 0xFFFFFFFF


class ShutdownError(Exception):
    """O Windows recusou o desligamento (ex.: política do hospital, outro
    usuário conectado). `returncode` = código de saída do shutdown.exe."""

    def __init__(self, message: str, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def seconds_since_last_input() -> float | None:
    """Segundos desde o último toque de teclado/mouse NESTA sessão do
    Windows. `None` fora do Windows ou se a API falhar -- quem chama trata
    `None` como "não sei", nunca como "alguém está usando"."""
    if sys.platform != "win32":
        return None
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(_LastInputInfo)
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount.restype = ctypes.c_uint32
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = kernel32.GetTickCount()
    except (AttributeError, OSError):
        return None
    # GetTickCount volta a zero a cada ~49,7 dias ligado; a máscara mantém a
    # diferença certa na virada.
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def console_session_is_ours() -> bool | None:
    """True se a sessão do Windows deste processo é a que está na frente da
    máquina. False quando outra pessoa trocou de usuário e está usando o
    computador (o uso recente dela não aparece em `GetLastInputInfo`, que só
    enxerga a própria sessão). `None` = não foi possível saber."""
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.WTSGetActiveConsoleSessionId.restype = ctypes.c_uint32
        active = kernel32.WTSGetActiveConsoleSessionId()
        own = ctypes.c_uint32()
        if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(own)):
            return None
    except (AttributeError, OSError):
        return None
    if active == _NO_CONSOLE_SESSION:
        return None
    return own.value == active


def _refuse_inside_tests(runner: Callable) -> None:
    """Trava de segurança: nenhum teste pode desligar a máquina de verdade
    (mesma classe de incidente do DEC-123, quando um teste derrubou o
    llama-server real). Testes passam um `runner` falso (ou trocam
    `subprocess.run`); o real só roda fora do pytest."""
    if runner is _REAL_RUN and "PYTEST_CURRENT_TEST" in os.environ:
        raise ShutdownError("Recusado: desligamento real pedido dentro de um teste automatizado.")


def shutdown_now(runner: Callable | None = None) -> None:
    """`shutdown /s /t 0`, sem `/f` -- com tempo zero o Windows NÃO força o
    fechamento de programas (ver docstring do módulo)."""
    runner = runner or subprocess.run
    _refuse_inside_tests(runner)
    result = runner(
        ["shutdown", "/s", "/t", "0"],
        capture_output=True, text=True, errors="replace", creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ShutdownError(f"shutdown /s recusado (código {result.returncode}): {detail}", result.returncode)


def _someone_present(
    idle_seconds: Callable[[], float | None],
    console_is_ours: Callable[[], bool | None],
    window_seconds: float,
) -> str | None:
    idle = idle_seconds()
    if idle is not None and idle < window_seconds:
        logger.info(
            "Desligamento ao fim da atualização NÃO aplicado: o computador foi usado há %d s.", int(idle),
        )
        return OUTCOME_USER_ACTIVE
    if console_is_ours() is False:
        logger.info("Desligamento ao fim da atualização NÃO aplicado: outra sessão do Windows está ativa.")
        return OUTCOME_OTHER_SESSION
    return None


def after_scheduled_run(
    app_config,
    *,
    runner: Callable | None = None,
    idle_seconds: Callable[[], float | None] = seconds_since_last_input,
    console_is_ours: Callable[[], bool | None] = console_session_is_ours,
    countdown: Callable[[int], bool] | None = None,
) -> str:
    """Decide e executa o desligamento depois de uma atualização agendada.
    Devolve um dos `OUTCOME_*`. Nunca levanta exceção."""
    if not getattr(app_config, "shutdown_after_scheduled_run", False):
        return OUTCOME_DISABLED

    present = _someone_present(idle_seconds, console_is_ours, USER_ACTIVE_WINDOW_SECONDS)
    if present:
        return present

    if countdown is None:
        from app.ui.shutdown_window import show_countdown as countdown
    try:
        cancelled = countdown(WARNING_SECONDS)
    except Exception:
        # Sem janela, ninguém conseguiria cancelar -- então não desliga.
        logger.exception("Janela de aviso do desligamento não abriu -- computador permanece ligado")
        return OUTCOME_NO_WINDOW
    if cancelled:
        logger.info("Desligamento cancelado pelo usuário na janela de aviso.")
        return OUTCOME_CANCELLED

    # Alguém chegou durante a contagem e não viu/clicou a janela: não desliga.
    present = _someone_present(idle_seconds, console_is_ours, WARNING_SECONDS)
    if present:
        return present

    try:
        shutdown_now(runner)
    except (ShutdownError, OSError) as exc:
        logger.warning("Desligamento ao fim da atualização não foi possível: %s", exc)
        if getattr(exc, "returncode", None) == ERROR_SHUTDOWN_USERS_LOGGED_ON:
            return OUTCOME_OTHER_SESSION
        return OUTCOME_FAILED
    logger.info("Desligamento do computador solicitado ao Windows (opção das Configurações).")
    return OUTCOME_SHUTTING_DOWN
