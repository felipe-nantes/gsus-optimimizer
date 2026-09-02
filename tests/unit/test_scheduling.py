"""Testa app/scheduling.py com subprocess.run mockado -- NÃO cria tarefa
real no Windows Task Scheduler. Registrar uma tarefa é uma mudança de
configuração persistente do sistema; fazer isso de verdade (mesmo que
limpo depois) fica para quando o usuário validar manualmente (SCHEDULE-001
/ Fase 12), não para a suíte automática."""
from unittest.mock import MagicMock, patch

import pytest

from app.scheduling import SchedulingError, register_daily_task, remove_task, task_exists


def _completed(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


@patch("app.scheduling.subprocess.run")
def test_register_daily_task_builds_expected_command(mock_run):
    mock_run.return_value = _completed(returncode=0)

    register_daily_task(r"C:\Program Files\GSUS Auditoria\gsus-auditoria.exe", "23:00", "TesteTarefa")

    args = mock_run.call_args[0][0]
    assert args[0] == "schtasks"
    assert "/Create" in args
    assert "DAILY" in args
    assert "TesteTarefa" in args
    assert "23:00" in args
    assert "/F" in args


@patch("app.scheduling.subprocess.run")
def test_register_daily_task_raises_on_failure(mock_run):
    mock_run.return_value = _completed(returncode=1, stderr="acesso negado")

    with pytest.raises(SchedulingError):
        register_daily_task(r"C:\app.exe", "23:00", "TesteTarefa")


@patch("app.scheduling.subprocess.run")
def test_task_exists_true_when_query_succeeds(mock_run):
    mock_run.return_value = _completed(returncode=0)
    assert task_exists("TesteTarefa") is True


@patch("app.scheduling.subprocess.run")
def test_task_exists_false_when_query_fails(mock_run):
    mock_run.return_value = _completed(returncode=1, stderr="não encontrada")
    assert task_exists("TesteTarefa") is False


@patch("app.scheduling.subprocess.run")
def test_remove_task_tolerates_already_missing(mock_run):
    mock_run.return_value = _completed(returncode=1, stderr="ERROR: The system cannot find the file specified.")
    remove_task("TesteTarefa")  # não deve levantar


@patch("app.scheduling.subprocess.run")
def test_remove_task_raises_on_unexpected_failure(mock_run):
    mock_run.return_value = _completed(returncode=1, stderr="acesso negado")
    with pytest.raises(SchedulingError):
        remove_task("TesteTarefa")
