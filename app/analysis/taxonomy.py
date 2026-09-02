"""Taxonomia FECHADA de pendências (RF-22), conforme orientação técnica de
auditoria hospitalar concorrente fornecida pelo usuário 2026-08-24 (ver
DECISIONS.md DEC-057).

7 categorias fixas -- nunca categoria livre. Cada uma tem subtipos
sugeridos pela orientação; "outro" sempre disponível como escape para caso
real não previsto (a categoria continua fechada, só o detalhe fino vai
para `description`, que é sempre texto livre com evidência).
"""
from __future__ import annotations

CATEGORY_DIAGNOSTICO = "DIAGNOSTICO"
CATEGORY_INTERCONSULTA = "INTERCONSULTA"
CATEGORY_PROCEDIMENTO_CIRURGIA = "PROCEDIMENTO_CIRURGIA"
CATEGORY_TERAPEUTICA = "TERAPEUTICA"
CATEGORY_TRANSFERENCIA = "TRANSFERENCIA"
CATEGORY_ALTA_BARREIRA = "ALTA_BARREIRA"
CATEGORY_ADMINISTRATIVA_LOGISTICA = "ADMINISTRATIVA_LOGISTICA"

SUBTYPES_BY_CATEGORY: dict[str, list[str]] = {
    CATEGORY_DIAGNOSTICO: [
        "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL",
        "EXAME_REALIZADO_AGUARDANDO_LAUDO",
        "AGUARDA_ANATOMOPATOLOGICO_OU_CULTURA",
        "INVESTIGACAO_SEM_DEFINICAO",
        "OUTRO",
    ],
    CATEGORY_INTERCONSULTA: [
        "SOLICITADA",
        "REALIZADA_SEM_CONDUTA_DEFINIDA",
        "CONDUTA_DEFINIDA",
        "OUTRO",
    ],
    CATEGORY_PROCEDIMENTO_CIRURGIA: [
        "INDICACAO_DEFINIDA_AGUARDANDO_CIRURGIA",
        "AGUARDA_CENTRO_CIRURGICO_ANESTESIA_OU_OPME",
        "AGUARDA_VAGA_UTI_POS_OPERATORIA",
        "AGUARDA_PREPARO_CLINICO_OU_DEFINICAO_ESPECIALIDADE",
        "OUTRO",
    ],
    CATEGORY_TERAPEUTICA: [
        "ANTIBIOTICO_OU_ESQUEMA_IV",
        "AJUSTE_ANTICOAGULACAO_OU_GLICEMICO",
        "DESMAME_OXIGENIO_OU_VENTILACAO",
        "CONTROLE_ALGICO_DIETA_OU_DRENO",
        "META_CLINICA_NAO_ATINGIDA",
        "OUTRO",
    ],
    CATEGORY_TRANSFERENCIA: [
        "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA",
        "REABILITACAO_OU_RETORNO_ORIGEM",
        "REGULACAO_CENTRAL_DE_LEITOS",
        "OUTRO",
    ],
    CATEGORY_ALTA_BARREIRA: [
        "FAMILIAR_CUIDADOR_OU_TRANSPORTE",
        "OXIGENIO_EQUIPAMENTO_MEDICAMENTO_OU_HOME_CARE",
        "INSTITUICAO_LONGA_PERMANENCIA_OU_SERVICO_SOCIAL",
        "BARREIRA_SOCIOFAMILIAR_OU_JUDICIAL",
        "OUTRO",
    ],
    CATEGORY_ADMINISTRATIVA_LOGISTICA: [
        "FALTA_MATERIAL_MEDICAMENTO_OPME_OU_AUTORIZACAO",
        "AGENDAMENTO_TRANSPORTE_EQUIPAMENTO_OU_PROFISSIONAL_INDISPONIVEL",
        "OUTRO",
    ],
}

VALID_CATEGORIES = set(SUBTYPES_BY_CATEGORY)

ORIGIN_INTERNAL = "INTERNA"
ORIGIN_EXTERNAL = "EXTERNA"
VALID_ORIGINS = {ORIGIN_INTERNAL, ORIGIN_EXTERNAL}

# Sugestão de classificação interna/externa por categoria -- ponto de
# partida, não regra rígida (a pendência real pode fugir do padrão, ex.:
# uma interconsulta pode depender de agenda externa). Usado só como default
# quando o LLM não indicar origem explicitamente.
DEFAULT_ORIGIN_BY_CATEGORY: dict[str, str] = {
    CATEGORY_DIAGNOSTICO: ORIGIN_INTERNAL,
    CATEGORY_INTERCONSULTA: ORIGIN_INTERNAL,
    CATEGORY_PROCEDIMENTO_CIRURGIA: ORIGIN_INTERNAL,
    CATEGORY_TERAPEUTICA: ORIGIN_INTERNAL,
    CATEGORY_ADMINISTRATIVA_LOGISTICA: ORIGIN_INTERNAL,
    CATEGORY_TRANSFERENCIA: ORIGIN_EXTERNAL,
    CATEGORY_ALTA_BARREIRA: ORIGIN_EXTERNAL,
}


def is_valid_category(category: str) -> bool:
    return category in VALID_CATEGORIES


def is_valid_subtype(category: str, subtype: str | None) -> bool:
    """subtype ausente é sempre válido (campo opcional -- nem toda
    pendência precisa detalhar subtipo)."""
    if subtype is None:
        return True
    return subtype in SUBTYPES_BY_CATEGORY.get(category, ())


def default_origin(category: str) -> str:
    return DEFAULT_ORIGIN_BY_CATEGORY.get(category, ORIGIN_INTERNAL)
