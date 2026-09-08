"""Prioridade de pendência calculada por REGRA determinística (RF-24) --
nunca pelo LLM livremente ("a LLM não deve inventar o que considera
urgente", orientação técnica seção 16, ver DECISIONS.md DEC-057).

O LLM só fornece os insumos (categoria, necessidade hospitalar, evidência
com data); este módulo decide a prioridade de forma transparente e
parametrizável, para que o critério seja sempre auditável e ajustável sem
retreinar/reformular prompt.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.analysis.sla_config import is_over_sla
from app.extraction.normalizer import normalize_iso_date
from app.analysis.taxonomy import (
    CATEGORY_DIAGNOSTICO,
    CATEGORY_INTERCONSULTA,
    CATEGORY_PROCEDIMENTO_CIRURGIA,
    CATEGORY_TRANSFERENCIA,
)

PRIORITY_ALTA = "ALTA"
PRIORITY_MEDIA = "MEDIA"
PRIORITY_MONITORAMENTO = "MONITORAMENTO"

NECESSIDADE_SIM = "SIM"
NECESSIDADE_PROVAVELMENTE_SIM = "PROVAVELMENTE_SIM"
NECESSIDADE_INCERTO = "INCERTO"
NECESSIDADE_POSSIVELMENTE_NAO = "POSSIVELMENTE_NAO"
NECESSIDADE_NAO_IDENTIFICADA = "NAO_IDENTIFICADA"
VALID_NECESSIDADE_HOSPITALAR = {
    NECESSIDADE_SIM, NECESSIDADE_PROVAVELMENTE_SIM, NECESSIDADE_INCERTO,
    NECESSIDADE_POSSIVELMENTE_NAO, NECESSIDADE_NAO_IDENTIFICADA,
}

# Categorias por natureza (cirurgia/procedimento essencial e transferência
# bloqueada) são sempre alta prioridade, independente de SLA -- seção 16.
ALWAYS_HIGH_PRIORITY_CATEGORIES = {CATEGORY_PROCEDIMENTO_CIRURGIA, CATEGORY_TRANSFERENCIA}

# Necessidade hospitalar que sugere "clinicamente apto à alta, mas retido
# por barreira" -- critério de alta prioridade da seção 16.
DISCHARGE_READY_NECESSIDADE = {NECESSIDADE_POSSIVELMENTE_NAO, NECESSIDADE_NAO_IDENTIFICADA}


def hours_elapsed_since(evidence_date_iso: str | None, now: datetime | None = None) -> float | None:
    """Horas desde `evidence_date_iso` (ISO 8601) até `now`. None se a data
    for ausente/não reconhecida -- nunca inventa tempo decorrido (RF-23).

    `evidence_date` vem de `normalize_datetime` (app/extraction/normalizer.py)
    a partir do horário exibido pelo próprio GSUS -- horário LOCAL (Brasília),
    sem timezone. Comparar isso contra UTC introduzia um erro sistemático de
    ~3h em todo tempo decorrido (bug real encontrado ao inspecionar um
    relatório de exemplo). `now` default é `datetime.now()` local -- mesmo
    "regime" (ingênuo) do que já está gravado, nunca misturar com UTC aqui."""
    if not evidence_date_iso:
        return None
    try:
        evidence_dt = datetime.fromisoformat(evidence_date_iso)
    except ValueError:
        return None
    reference = now if now is not None else datetime.now(evidence_dt.tzinfo)
    delta = reference - evidence_dt
    return max(delta.total_seconds() / 3600, 0.0)


def days_since_admission(admission_date: str | None, today: date | None = None) -> int | None:
    """Dias de internação (DIH) a partir de `patients.admission_date`. None
    se a data for ausente/não reconhecida -- nunca inventa (RF-20). Extraído
    de `html_report.py::_dih` (auditoria de certificação pré-entrega,
    2026-09-01, RF-29) -- `dashboard_metrics.py` precisa do MESMO cálculo
    pra tabela de censo da dashboard, mesmo motivo de `is_edd_overdue` já
    ter sido compartilhado entre relatório e dashboard.

    Achado real GRAVE na mesma auditoria: `patients.admission_date` vem
    CRU da coluna "Data de Internação" do GSUS (`app/gsus/census.py::
    _extract_page_rows`, só `.strip()`, sem nenhuma normalização -- ao
    contrário de `evidence_date`, que passa por
    `app.extraction.normalizer.normalize_datetime`). Formato real
    confirmado no banco de produção (2026-09-01, só o PADRÃO verificado via
    regex, nunca um valor real lido): 100% das 190 datas reais em
    DD/MM/AAAA, 0% em ISO -- ou seja, `date.fromisoformat` (o único formato
    que esta função aceitava antes) FALHAVA silenciosamente pra
    ESSENCIALMENTE TODO paciente real, sempre devolvendo `None` -- 190 de
    192 pacientes ativos mostravam "DIH não determinado" no relatório já
    em produção. Agora tenta DD/MM/AAAA (formato real confirmado) antes de
    ISO (mantido por robustez/compatibilidade com dado sintético de teste)."""
    if not admission_date:
        return None
    raw = admission_date[:10]
    parsed: date | None = None
    try:
        parsed = datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            return None
    reference = today if today is not None else date.today()
    return max((reference - parsed).days, 0)


# DEC-119: evolução datada até esta folga ANTES da "Data de Internação" do
# censo ainda conta como internação atual -- o paciente costuma passar dias
# no pronto-socorro antes da admissão formal, e um exame pedido lá é
# pendência real. Além disso é episódio antigo (o bug real eram anos).
PRE_ADMISSION_GRACE_DAYS = 7


def pre_admission_cutoff_iso(admission_date: str | None) -> str | None:
    """`AAAA-MM-DD` a partir do qual uma evolução pertence à internação
    atual (admissão menos a folga), ou None quando a data de internação é
    ausente/irreconhecível -- nesse caso nada é filtrado (nunca descarta por
    palpite). Aceita o formato real do censo (DD/MM/AAAA, DEC-099) e ISO."""
    iso = normalize_iso_date(admission_date)
    if not iso:
        return None
    return (date.fromisoformat(iso) - timedelta(days=PRE_ADMISSION_GRACE_DAYS)).isoformat()


def is_edd_overdue(edd_status: str | None, edd_data: str | None, today: date | None = None) -> bool:
    """Se a previsão de alta (EDD) já passou -- reavaliado na LEITURA, nunca
    confia só no `edd_status` gravado no momento da análise (RF-27): uma EDD
    "REGISTRADA" pode ter ficado no passado só pela passagem do tempo, sem
    nenhuma reanálise desde então pra atualizar o campo. Extraído de
    `html_report.py::_format_edd` (achado real, DEC-099/REPORT-003) -- os
    dois precisam concordar sobre o que conta como vencida, senão o
    indicador agregado (% EDD vencida) diverge do que o relatório individual
    mostra pro mesmo paciente."""
    if edd_status == "VENCIDA":
        return True
    if edd_status != "REGISTRADA" or not edd_data:
        return False
    try:
        parsed = date.fromisoformat(edd_data[:10])
    except ValueError:
        return False
    reference = today if today is not None else date.today()
    return parsed < reference


def compute_priority(
    category: str,
    necessidade_hospitalar: str | None,
    hours_elapsed: float | None = None,
) -> str:
    """Regras, na ordem da seção 16 da orientação técnica (mais grave
    primeiro): necessidade hospitalar sugerindo alta pronta > categoria
    sempre-alta (cirurgia/transferência) > SLA estourado > exame/
    interconsulta com tratamento ainda ativo (média) > monitoramento."""
    if necessidade_hospitalar in DISCHARGE_READY_NECESSIDADE:
        return PRIORITY_ALTA

    if category in ALWAYS_HIGH_PRIORITY_CATEGORIES:
        return PRIORITY_ALTA

    if is_over_sla(category, hours_elapsed):
        return PRIORITY_ALTA

    if category in (CATEGORY_DIAGNOSTICO, CATEGORY_INTERCONSULTA):
        return PRIORITY_MEDIA

    return PRIORITY_MONITORAMENTO
