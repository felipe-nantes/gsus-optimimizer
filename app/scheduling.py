"""Agendamento via Windows Task Scheduler (SCHEDULE-001).

Usa `schtasks.exe` (nativo do Windows) via subprocess -- sem dependência
nova. O usuário não configura nada manualmente (prompt mestre seção 12/
Fase 12): o próprio app registra/atualiza a tarefa a partir do horário
escolhido na configuração inicial.
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

TASK_NAME = "GSUSAuditoria_AtualizacaoDiaria"


class SchedulingError(Exception):
    """Falha ao criar/remover a tarefa agendada."""


def register_daily_task(
    exe_path: str, time_hhmm: str, task_name: str = TASK_NAME, arguments: str = ""
) -> None:
    """Cria (ou substitui, /F) uma tarefa diária que roda `exe_path` no
    horário informado (formato HH:MM, 24h). `arguments` (ex.:
    "--auto-update") vai DEPOIS do caminho, fora das aspas -- achado real
    (SCHEDULE-001, 2026-08-26): sem uma flag assim, a tarefa agendada só
    abria a janela do app, nunca disparava a atualização sozinha (não tem
    humano pra clicar o botão numa execução de madrugada)."""
    task_run = f'"{exe_path}" {arguments}'.strip()
    result = subprocess.run(
        [
            "schtasks", "/Create", "/SC", "DAILY", "/TN", task_name,
            "/TR", task_run, "/ST", time_hhmm, "/F",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error("schtasks /Create falhou: %s", result.stderr.strip())
        raise SchedulingError(f"Não foi possível registrar a tarefa agendada: {result.stderr.strip()}")
    logger.info("Tarefa agendada '%s' registrada para %s diariamente", task_name, time_hhmm)


def task_exists(task_name: str = TASK_NAME) -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name], capture_output=True, text=True
    )
    return result.returncode == 0


def remove_task(task_name: str = TASK_NAME) -> None:
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"], capture_output=True, text=True
    )
    if result.returncode != 0 and "cannot find" not in result.stderr.lower() and "não" not in result.stderr.lower():
        logger.error("schtasks /Delete falhou: %s", result.stderr.strip())
        raise SchedulingError(f"Não foi possível remover a tarefa agendada: {result.stderr.strip()}")
