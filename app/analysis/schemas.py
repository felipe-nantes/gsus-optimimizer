"""Contrato de saída do LLM local (LLM-002), expandido conforme orientação
técnica de auditoria hospitalar concorrente fornecida pelo usuário
2026-08-24 (ver DECISIONS.md DEC-057 e PROJECT_SPEC.md RF-20 a RF-28).

Validação manual (sem dependência `jsonschema` -- RNF-07). O código deve
validar a saída ANTES de persistir; saída inválida nunca é aceita como se
fosse válida (prompt mestre seção 24).

`priority` NÃO faz parte deste contrato: é calculada por regra depois da
validação (app/analysis/priority.py, RF-24), nunca pedida ao LLM.
"""
from __future__ import annotations

from app.analysis.priority import VALID_NECESSIDADE_HOSPITALAR
from app.analysis.taxonomy import VALID_ORIGINS, is_valid_category, is_valid_subtype

VALID_EDD_STATUS = {"REGISTRADA", "NAO_REGISTRADA", "VENCIDA"}
VALID_DAY_CLASSIFICATION = {"VERDE", "VERMELHO"}
VALID_CONFIDENCE = {"ALTA", "MEDIA", "BAIXA"}

REQUIRED_TOP_LEVEL_FIELDS = {
    "clinical_context": str,
    "current_status": str,
    "necessidade_hospitalar": str,
    "necessidade_hospitalar_justificativa": str,
    "objetivo_terapeutico": str,
    "proximo_passo": str,
    "edd_status": str,
    "dia_classificacao": str,
    "pending_items": list,
    "insufficient_information": bool,
}

# Nullable por natureza (RF-27/RF-28: nunca inventar se ausente no prontuário).
# `edd_data`/`origem_internacao` geralmente só aparecem numa nota específica
# (admissão, ou a nota que registrou a previsão de alta) -- em execuções
# incrementais posteriores (RF-08, só notas novas) o LLM não tem mais como
# vê-los. `save_patient_state` preserva (COALESCE) o valor já conhecido
# quando uma análise posterior devolver null para esses dois -- `dia_causa`
# NÃO é preservado (é sempre a causa do dia de HOJE, nunca um valor antigo).
OPTIONAL_NULLABLE_STRING_FIELDS = ("edd_data", "dia_causa", "origem_internacao")

REQUIRED_PENDING_ITEM_FIELDS = {
    "category": str,
    "description": str,
    "evidence": str,
    "is_inferred": bool,
}

OPTIONAL_PENDING_ITEM_STRING_FIELDS = ("subcategory", "evidence_date", "origin", "confidence", "flow_status")

# LLMs pequenos às vezes escrevem a palavra "null" como STRING em vez do
# valor JSON null (visto em execução real 2026-08-24 -- ver DECISIONS.md
# DEC-057). `isinstance(..., str)` sozinho não pega isso, e código
# downstream (html_report.py) trataria a string como valor verdadeiro.
NULL_SENTINEL_STRINGS = {"null", "none", "nulo"}


def _is_null_sentinel(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in NULL_SENTINEL_STRINGS


def normalize_null_sentinels(data: object) -> object:
    """Troca strings-sentinela tipo "null" por None nos campos nullable do
    contrato. Deve rodar ANTES de `validate_analysis_output` -- não muta
    `data`, devolve uma cópia (pending_items também copiado)."""
    if not isinstance(data, dict):
        return data

    normalized = dict(data)
    for field in OPTIONAL_NULLABLE_STRING_FIELDS:
        if _is_null_sentinel(normalized.get(field)):
            normalized[field] = None

    # `edd_status` é ENUM OBRIGATÓRIO (nunca nullable) -- achado real
    # 2026-08-25 (execução contra pacientes reais com admissão longa,
    # DEC-065): o LLM às vezes escreve "null" aqui também, mas não há
    # equivalente sensato pra "None" num campo obrigatório. Deriva um valor
    # seguro a partir de `edd_data` (já normalizado acima): REGISTRADA se
    # sobrou uma data real, senão NAO_REGISTRADA -- nunca inventa VENCIDA
    # (isso exigiria julgar se a data já passou, o que cabe a
    # `_format_edd`/`priority.py` na leitura, não aqui).
    if _is_null_sentinel(normalized.get("edd_status")):
        normalized["edd_status"] = "REGISTRADA" if normalized.get("edd_data") else "NAO_REGISTRADA"

    pending_items = normalized.get("pending_items")
    if isinstance(pending_items, list):
        new_items = []
        for item in pending_items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            new_item = dict(item)
            for field in OPTIONAL_PENDING_ITEM_STRING_FIELDS:
                if _is_null_sentinel(new_item.get(field)):
                    new_item[field] = None
            new_items.append(new_item)
        normalized["pending_items"] = new_items

    return normalized


def _validate_pending_item(item: object, index: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return [f"pending_items[{index}] não é um objeto"]

    for field, expected_type in REQUIRED_PENDING_ITEM_FIELDS.items():
        if field not in item:
            errors.append(f"pending_items[{index}].{field} ausente")
        elif not isinstance(item[field], expected_type):
            errors.append(f"pending_items[{index}].{field} deveria ser {expected_type.__name__}")

    if "evidence" in item and isinstance(item["evidence"], str) and not item["evidence"].strip():
        errors.append(f"pending_items[{index}].evidence vazio (evidência é obrigatória)")

    for field in OPTIONAL_PENDING_ITEM_STRING_FIELDS:
        if field in item and item[field] is not None and not isinstance(item[field], str):
            errors.append(f"pending_items[{index}].{field} deveria ser string ou null")

    category = item.get("category")
    if isinstance(category, str) and not is_valid_category(category):
        errors.append(f"pending_items[{index}].category '{category}' fora da taxonomia fechada (RF-22)")
    elif isinstance(category, str):
        subcategory = item.get("subcategory")
        if not is_valid_subtype(category, subcategory):
            errors.append(f"pending_items[{index}].subcategory '{subcategory}' inválido para a categoria '{category}'")

    origin = item.get("origin")
    if origin is not None and origin not in VALID_ORIGINS:
        errors.append(f"pending_items[{index}].origin deveria ser um de {sorted(VALID_ORIGINS)} ou null")

    if item.get("is_inferred") is True:
        confidence = item.get("confidence")
        if confidence not in VALID_CONFIDENCE:
            errors.append(
                f"pending_items[{index}].confidence obrigatório e deve ser um de "
                f"{sorted(VALID_CONFIDENCE)} quando is_inferred=true (RF-25)"
            )

    return errors


def validate_analysis_output(data: object) -> list[str]:
    """Retorna lista de erros (vazia = válido). Nunca levanta exceção --
    quem chama decide o que fazer (retry, LLM_ANALYSIS_ERROR, etc.)."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["saída não é um objeto JSON"]

    for field, expected_type in REQUIRED_TOP_LEVEL_FIELDS.items():
        if field not in data:
            errors.append(f"campo obrigatório ausente: {field}")
        elif not isinstance(data[field], expected_type):
            errors.append(f"campo {field} deveria ser {expected_type.__name__}")

    for field in OPTIONAL_NULLABLE_STRING_FIELDS:
        if field in data and data[field] is not None and not isinstance(data[field], str):
            errors.append(f"campo {field} deveria ser string ou null")

    if errors:
        return errors

    if data["insufficient_information"] is True:
        return errors

    necessidade = data["necessidade_hospitalar"]
    if necessidade not in VALID_NECESSIDADE_HOSPITALAR:
        errors.append(f"necessidade_hospitalar '{necessidade}' fora das categorias fechadas (RF-21)")
    # Campo obrigatório pra auditoria (seção 3 da orientação técnica) -- só
    # checar isinstance(str) deixava "" passar (achado real, revisão de
    # conformidade DEC-064); mesmo padrão do check de evidence vazio acima.
    if isinstance(data.get("necessidade_hospitalar_justificativa"), str) and not data["necessidade_hospitalar_justificativa"].strip():
        errors.append("necessidade_hospitalar_justificativa vazia (justificativa é obrigatória, seção 3 da orientação técnica)")

    edd_status = data["edd_status"]
    if edd_status not in VALID_EDD_STATUS:
        errors.append(f"edd_status '{edd_status}' deveria ser um de {sorted(VALID_EDD_STATUS)}")
    if edd_status == "REGISTRADA" and not data.get("edd_data"):
        errors.append("edd_status='REGISTRADA' exige edd_data preenchida (RF-27)")

    dia = data["dia_classificacao"]
    if dia not in VALID_DAY_CLASSIFICATION:
        errors.append(f"dia_classificacao '{dia}' deveria ser um de {sorted(VALID_DAY_CLASSIFICATION)}")
    if dia == "VERMELHO" and not data.get("dia_causa"):
        errors.append("dia_classificacao='VERMELHO' exige dia_causa preenchida (RF-28)")

    for i, item in enumerate(data["pending_items"]):
        errors.extend(_validate_pending_item(item, i))

    return errors
