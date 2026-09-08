"""Testes de app/analysis/priority.py (RF-24). Dados 100% fictícios."""
from datetime import date, datetime

from app.analysis.priority import (
    NECESSIDADE_NAO_IDENTIFICADA,
    NECESSIDADE_POSSIVELMENTE_NAO,
    NECESSIDADE_SIM,
    PRIORITY_ALTA,
    PRIORITY_MEDIA,
    PRIORITY_MONITORAMENTO,
    compute_priority,
    days_since_admission,
    hours_elapsed_since,
    is_edd_overdue,
)
from app.analysis.taxonomy import (
    CATEGORY_ADMINISTRATIVA_LOGISTICA,
    CATEGORY_DIAGNOSTICO,
    CATEGORY_PROCEDIMENTO_CIRURGIA,
    CATEGORY_TRANSFERENCIA,
)


def test_hours_elapsed_since_naive_local_timestamps_no_utc_drift():
    """Bug real (encontrado inspecionando um relatório de exemplo):
    `evidence_date` é horário LOCAL sem timezone (vem do que o GSUS exibe,
    via normalize_datetime) -- comparar contra UTC inflava o tempo decorrido
    em ~3h sempre. now e evidence_date aqui estão no mesmo "regime" (naive),
    exatamente como acontece em produção."""
    now = datetime(2026, 8, 24, 19, 17, 0)
    evidence = "2026-08-20T08:00:00"
    hours = hours_elapsed_since(evidence, now=now)
    # 20/08 08:00 -> 24/08 19:17 = 4 dias e 11h17min = 107,28h
    assert 107.0 < hours < 107.5


def test_hours_elapsed_since_missing_date_returns_none():
    assert hours_elapsed_since(None) is None
    assert hours_elapsed_since("") is None


def test_hours_elapsed_since_unparseable_date_returns_none():
    assert hours_elapsed_since("não é uma data") is None


def test_hours_elapsed_since_never_negative():
    """Evidência "no futuro" (relógio dessincronizado, etc.) nunca vira
    tempo negativo -- fail-safe."""
    now = datetime(2026, 8, 20, 8, 0, 0)
    future_evidence = "2026-08-24T19:17:00"
    assert hours_elapsed_since(future_evidence, now=now) == 0.0


# ------------------------------------------------------- compute_priority

def test_discharge_ready_necessidade_is_always_alta():
    assert compute_priority(CATEGORY_ADMINISTRATIVA_LOGISTICA, NECESSIDADE_POSSIVELMENTE_NAO, None) == PRIORITY_ALTA
    assert compute_priority(CATEGORY_ADMINISTRATIVA_LOGISTICA, NECESSIDADE_NAO_IDENTIFICADA, None) == PRIORITY_ALTA


def test_procedimento_and_transferencia_always_alta_regardless_of_sla():
    assert compute_priority(CATEGORY_PROCEDIMENTO_CIRURGIA, NECESSIDADE_SIM, hours_elapsed=1) == PRIORITY_ALTA
    assert compute_priority(CATEGORY_TRANSFERENCIA, NECESSIDADE_SIM, hours_elapsed=1) == PRIORITY_ALTA


def test_diagnostico_within_sla_is_media():
    # SLA de DIAGNOSTICO é 48h (sla_config.py) -- dentro do prazo
    assert compute_priority(CATEGORY_DIAGNOSTICO, NECESSIDADE_SIM, hours_elapsed=10) == PRIORITY_MEDIA


def test_diagnostico_over_sla_is_alta():
    assert compute_priority(CATEGORY_DIAGNOSTICO, NECESSIDADE_SIM, hours_elapsed=49) == PRIORITY_ALTA


def test_administrativa_within_sla_is_monitoramento():
    # não é DIAGNOSTICO/INTERCONSULTA nem categoria sempre-alta -> monitoramento se dentro do SLA
    assert compute_priority(CATEGORY_ADMINISTRATIVA_LOGISTICA, NECESSIDADE_SIM, hours_elapsed=1) == PRIORITY_MONITORAMENTO


def test_unknown_hours_never_triggers_sla_alta():
    """Sem tempo decorrido conhecido, nunca assume SLA estourado (RF-23:
    nunca declarar atraso sem os dois dados)."""
    assert compute_priority(CATEGORY_DIAGNOSTICO, NECESSIDADE_SIM, hours_elapsed=None) == PRIORITY_MEDIA


# ------------------------------------------------------------- is_edd_overdue
# Extraído de html_report.py::_format_edd (achado real, DEC-099/REPORT-003):
# o indicador agregado (% EDD vencida) e o relatório individual precisam
# concordar sobre o que conta como vencida pro MESMO paciente.

def test_edd_overdue_status_vencida_is_always_overdue():
    assert is_edd_overdue("VENCIDA", "2026-08-01") is True


def test_edd_overdue_status_vencida_without_data_still_overdue():
    """`edd_status='VENCIDA'` sozinho já é suficiente -- não depende de `edd_data`."""
    assert is_edd_overdue("VENCIDA", None) is True


def test_edd_registrada_in_the_past_is_overdue():
    assert is_edd_overdue("REGISTRADA", "2026-08-20", today=date(2026, 8, 25)) is True


def test_edd_registrada_today_is_not_overdue():
    assert is_edd_overdue("REGISTRADA", "2026-08-25", today=date(2026, 8, 25)) is False


def test_edd_registrada_in_the_future_is_not_overdue():
    assert is_edd_overdue("REGISTRADA", "2026-08-30", today=date(2026, 8, 25)) is False


def test_edd_nao_registrada_is_never_overdue():
    assert is_edd_overdue("NAO_REGISTRADA", None) is False


def test_edd_registrada_without_data_is_not_overdue():
    """Contrato exige edd_data quando REGISTRADA (RF-27) -- mas fail-safe
    aqui também: nunca declara vencida sem uma data real pra comparar."""
    assert is_edd_overdue("REGISTRADA", None) is False


def test_edd_registrada_with_unparseable_data_is_not_overdue():
    """Nunca inventa atraso a partir de um dado que não conseguiu interpretar."""
    assert is_edd_overdue("REGISTRADA", "data-invalida") is False


# --------------------------------------------------------- days_since_admission
# Extraído de html_report.py::_dih (RF-29, auditoria de certificação
# 2026-09-01) -- compartilhado com dashboard_metrics.py, mesmo motivo de
# is_edd_overdue já ser compartilhado.

def test_days_since_admission_computes_dih():
    assert days_since_admission("2026-08-20", today=date(2026, 8, 25)) == 5


def test_days_since_admission_accepts_real_gsus_format_ddmmyyyy():
    """Achado real GRAVE (auditoria de certificação, 2026-09-01):
    `patients.admission_date` vem CRU do GSUS em DD/MM/AAAA (nunca
    normalizado, ao contrário de `evidence_date`) -- confirmado no banco de
    produção real (regex, nunca um valor específico lido): 100% das datas
    reais nesse formato, 0% em ISO. Antes deste teste, `days_since_admission`
    (extraído de `html_report.py::_dih`) só aceitava ISO -- devolvia `None`
    pra ESSENCIALMENTE TODO paciente real (190 de 192 no relatório em
    produção)."""
    assert days_since_admission("20/08/2026", today=date(2026, 8, 25)) == 5


def test_days_since_admission_accepts_ddmmyyyy_with_time_suffix():
    """`admission_date[:10]` já corta a hora se o GSUS incluir -- confirma
    que o corte funciona igual pro formato DD/MM/AAAA (10 caracteres, igual
    ao ISO)."""
    assert days_since_admission("20/08/2026 14:30", today=date(2026, 8, 25)) == 5


def test_days_since_admission_missing_date_returns_none():
    assert days_since_admission(None) is None
    assert days_since_admission("") is None


def test_days_since_admission_unparseable_date_returns_none():
    assert days_since_admission("não é uma data") is None


def test_days_since_admission_never_negative():
    """Admissão "no futuro" (relógio dessincronizado) nunca vira DIH negativo."""
    assert days_since_admission("2026-08-30", today=date(2026, 8, 25)) == 0


# ------------------------------------------------------------- DEC-119

def test_pre_admission_cutoff_uses_census_format_and_grace():
    from app.analysis.priority import PRE_ADMISSION_GRACE_DAYS, pre_admission_cutoff_iso

    assert PRE_ADMISSION_GRACE_DAYS == 7
    assert pre_admission_cutoff_iso("19/08/2026") == "2026-08-12"  # formato real do censo (DEC-099)
    assert pre_admission_cutoff_iso("2026-08-19") == "2026-08-12"
    assert pre_admission_cutoff_iso(None) is None
    assert pre_admission_cutoff_iso("data invalida") is None
