"""Faixas de normalidade de sinais vitais (LLM-006, pedido do auditor,
DEC-129 seção 6: "não classificar valores normais como anormais -- FR de 18
irpm não deve ser descrita como baixa em adulto"; "aplicar faixas clínicas
previamente definidas e considerar idade, setor e suporte ventilatório").

Parametrizável, mesmo princípio de `sla_config.py`: os valores abaixo são a
PROPOSTA enviada ao auditor em 2026-10-13 (pergunta 2a) e só valem em
produção depois da validação dele. Usados em dois lugares: no prompt (o
modelo recebe a tabela) e no filtro de exibição (`clinical_filters.py`), que
corrige um qualificador que contradiga a faixa.

Sem idade no censo, o setor faz as vezes de faixa etária: pediatria, UTI
pediátrica, neonatal e alojamento conjunto (recém-nascidos) não recebem
qualificação automática por faixa de adulto.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

POPULATION_ADULT = "ADULTO"
POPULATION_PEDIATRIC = "PEDIATRICO_NEONATAL"
# Gestação muda as referências (FC materna, metas glicêmicas) e o texto
# mistura valores fetais ("FC fetal", "BCF") -- achado da 2ª revisão da
# Fase 2: sem qualificação automática pelas faixas gerais de adulto.
POPULATION_OBSTETRIC = "OBSTETRICO"


@dataclass(frozen=True)
class VitalRange:
    key: str
    label: str
    low: float | None
    high: float | None
    unit: str


# Proposta enviada ao auditor (pergunta 2a, 2026-10-13).
ADULT_RANGES: tuple[VitalRange, ...] = (
    VitalRange("FR", "frequência respiratória", 12, 20, "irpm"),
    VitalRange("FC", "frequência cardíaca", 60, 100, "bpm"),
    VitalRange("PAS", "pressão arterial sistólica", 90, 139, "mmHg"),
    VitalRange("PAD", "pressão arterial diastólica", 60, 89, "mmHg"),
    VitalRange("TAX", "temperatura axilar", 36.0, 37.7, "°C"),
    VitalRange("SPO2", "saturação de O2 em ar ambiente", 94, None, "%"),
    VitalRange("HGT", "glicemia capilar", 70, 180, "mg/dL"),
)
RANGES_BY_KEY = {r.key: r for r in ADULT_RANGES}

_PEDIATRIC_UNIT = re.compile(r"PEDIAT|\bPED\b|NEONAT|\bRN\b|\bAC\b")
_OBSTETRIC_UNIT = re.compile(r"\bGO\b|OBSTET|MATERN|\bCO\b|CENTRO OBST")


def _fold_upper(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).upper()


def population_for_unit(unit: str | None) -> str:
    """Setor do censo -> população presumida. "Enfer. GO-AC" (alojamento
    conjunto) entra como pediátrico/neonatal: os pacientes ali são os
    recém-nascidos. "Enfer. GO" (as mães) é obstétrica. Só ADULTO recebe
    qualificação automática pelas faixas."""
    folded = _fold_upper(unit).replace("-", " ").replace(".", " ")
    if _PEDIATRIC_UNIT.search(folded):
        return POPULATION_PEDIATRIC
    if _OBSTETRIC_UNIT.search(folded):
        return POPULATION_OBSTETRIC
    return POPULATION_ADULT


def is_within_range(key: str, value: float) -> bool | None:
    vital = RANGES_BY_KEY.get(key)
    if vital is None:
        return None
    if vital.low is not None and value < vital.low:
        return False
    if vital.high is not None and value > vital.high:
        return False
    return True


def _fmt(value: float) -> str:
    text = f"{value:g}"
    return text.replace(".", ",")


def prompt_block() -> str:
    parts = []
    for vital in ADULT_RANGES:
        if vital.high is None:
            parts.append(f"{vital.key} >= {_fmt(vital.low)}{vital.unit}")
        else:
            parts.append(f"{vital.key} {_fmt(vital.low)}-{_fmt(vital.high)} {vital.unit}")
    return "; ".join(parts)
