"""Limites de tempo (SLA) por categoria de pendência -- RF-23/RF-24.

IMPORTANTE (mesmo princípio do RULES-001/RF-07): estes são valores DEFAULT
sugeridos pela orientação técnica de auditoria concorrente (seção 6),
NÃO validados clinicamente para este hospital/setor. O sistema NUNCA declara
"atraso" a partir daqui sozinho na fase inicial -- só reporta tempo
decorrido (RF-23). Estes limites alimentam o cálculo de PRIORIDADE
(app/analysis/priority.py), que é onde de fato viram sinal visível ao
auditor. Devem ser revisados/ajustados por critério clínico/institucional
antes de uso em produção real, e podem variar por prioridade clínica
individual (a orientação é explícita: "não há um tempo único cientificamente
válido para toda TC, RM, interconsulta, cirurgia").
"""
from __future__ import annotations

from app.analysis.taxonomy import (
    CATEGORY_ADMINISTRATIVA_LOGISTICA,
    CATEGORY_ALTA_BARREIRA,
    CATEGORY_DIAGNOSTICO,
    CATEGORY_INTERCONSULTA,
    CATEGORY_PROCEDIMENTO_CIRURGIA,
    CATEGORY_TERAPEUTICA,
    CATEGORY_TRANSFERENCIA,
)

# Horas até a pendência ser considerada "acima do SLA" para fins de
# prioridade. `None` = sem limite de tempo definido (ex.: TERAPEUTICA é
# meta clínica, não espera de terceiro -- não faz sentido um SLA genérico).
DEFAULT_SLA_HOURS_BY_CATEGORY: dict[str, int | None] = {
    CATEGORY_DIAGNOSTICO: 48,
    CATEGORY_INTERCONSULTA: 24,
    CATEGORY_PROCEDIMENTO_CIRURGIA: 24,
    CATEGORY_TERAPEUTICA: None,
    CATEGORY_TRANSFERENCIA: 48,
    CATEGORY_ALTA_BARREIRA: 24,
    CATEGORY_ADMINISTRATIVA_LOGISTICA: 24,
}


def get_sla_hours(category: str) -> int | None:
    return DEFAULT_SLA_HOURS_BY_CATEGORY.get(category)


def is_over_sla(category: str, hours_elapsed: float | None) -> bool:
    """False quando não há SLA definido para a categoria OU o tempo
    decorrido é desconhecido -- nunca assume atraso sem os dois dados."""
    limit = get_sla_hours(category)
    if limit is None or hours_elapsed is None:
        return False
    return hours_elapsed > limit
