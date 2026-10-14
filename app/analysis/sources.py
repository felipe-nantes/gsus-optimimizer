"""Fonte de cada evolução, a partir do cargo do profissional (LLM-006,
pedido do auditor, DEC-129 seção 2: "a evolução médica, hierarquicamente,
deve ser a principal ... a evolução da equipe multiprofissional deve dar
suporte").

O cabeçalho real do GSUS ("dd/mm/aaaa hh:mm - nome (cargo)") já vira
`notes.source_type` no parser -- "Médico infectologista", "Enfermeiro",
"Técnico de enfermagem", "Fisioterapeuta geral"... (cargo presente em ~98,5%
das evoluções do banco de dev). Classificação determinística, nunca pelo
modelo. Valor que não é cargo (ex.: "Direita"/"Esquerda", ~1,5%, sobra do
parser) cai em NAO_IDENTIFICADA -- nunca é tratado como evolução médica.
"""
from __future__ import annotations

import unicodedata

SOURCE_MEDICA = "MEDICA"
SOURCE_ENFERMAGEM = "ENFERMAGEM"
SOURCE_MULTIPROFISSIONAL = "MULTIPROFISSIONAL"
SOURCE_NAO_IDENTIFICADA = "NAO_IDENTIFICADA"

SOURCE_LABELS = {
    SOURCE_MEDICA: "EVOLUÇÃO MÉDICA",
    SOURCE_ENFERMAGEM: "ENFERMAGEM",
    SOURCE_MULTIPROFISSIONAL: "EQUIPE MULTIPROFISSIONAL",
    SOURCE_NAO_IDENTIFICADA: "FONTE NÃO IDENTIFICADA",
}

_MULTIPROFESSIONAL_PREFIXES = (
    "fisioterap", "nutricion", "psicolog", "fonoaudi", "assistente social", "farmac",
    "terapeuta ocupacional", "odontolog", "cirurgiao-dentista", "cirurgiao dentista",
    "biomedic", "educador fisico", "profissional de educacao fisica",
)


def _fold(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).strip().lower()


def classify_source(source_type: str | None) -> str:
    folded = _fold(source_type)
    if not folded:
        return SOURCE_NAO_IDENTIFICADA
    if folded.startswith("medic"):
        return SOURCE_MEDICA
    if "enfermag" in folded or folded.startswith("enfermeir"):
        return SOURCE_ENFERMAGEM
    if folded.startswith(_MULTIPROFESSIONAL_PREFIXES):
        return SOURCE_MULTIPROFISSIONAL
    return SOURCE_NAO_IDENTIFICADA


def is_physician(source_type: str | None) -> bool:
    return classify_source(source_type) == SOURCE_MEDICA


def source_label(source_type: str | None) -> str:
    return SOURCE_LABELS[classify_source(source_type)]
