from app.analysis.schemas import normalize_null_sentinels, validate_analysis_output


def _valid_output(**overrides) -> dict:
    """Saída completa e válida do novo contrato (RF-20 a RF-28, DEC-057) --
    testes usam isso como base e sobrescrevem só o que querem quebrar."""
    base = {
        "clinical_context": "Internado por AVC isquêmico.",
        "current_status": "Estável, em investigação.",
        "necessidade_hospitalar": "SIM",
        "necessidade_hospitalar_justificativa": "Monitorização neurológica contínua.",
        "objetivo_terapeutico": "Definir conduta cirúrgica versus conservadora.",
        "proximo_passo": "Parecer da cirurgia torácica.",
        "edd_data": None,
        "edd_status": "NAO_REGISTRADA",
        "dia_classificacao": "VERDE",
        "dia_causa": None,
        "pending_items": [
            {
                "category": "INTERCONSULTA",
                "subcategory": "SOLICITADA",
                "description": "Aguarda avaliação da cirurgia torácica",
                "evidence": "Solicitada avaliação da cirurgia torácica...",
                "evidence_date": "2026-08-20T10:31:00",
                "origin": "INTERNA",
                "is_inferred": False,
                "confidence": None,
                "flow_status": "SOLICITADA",
            }
        ],
        "insufficient_information": False,
    }
    base.update(overrides)
    return base


def test_valid_output_with_pending_items_has_no_errors():
    assert validate_analysis_output(_valid_output()) == []


def test_insufficient_information_true_skips_the_rest_of_validation():
    data = {
        "clinical_context": "",
        "current_status": "",
        "necessidade_hospitalar": "",
        "necessidade_hospitalar_justificativa": "",
        "objetivo_terapeutico": "",
        "proximo_passo": "",
        "edd_status": "",
        "dia_classificacao": "",
        "pending_items": [],
        "insufficient_information": True,
    }
    assert validate_analysis_output(data) == []


def test_missing_required_field_is_error():
    data = _valid_output()
    del data["pending_items"]
    errors = validate_analysis_output(data)
    assert any("pending_items" in e for e in errors)


def test_pending_item_without_evidence_is_error():
    data = _valid_output(pending_items=[
        {"category": "DIAGNOSTICO", "description": "Aguarda exame", "evidence": "   ", "is_inferred": False}
    ])
    errors = validate_analysis_output(data)
    assert any("evidence" in e for e in errors)


def test_pending_item_missing_evidence_field_is_error():
    data = _valid_output(pending_items=[
        {"category": "DIAGNOSTICO", "description": "Aguarda exame", "is_inferred": False}
    ])
    errors = validate_analysis_output(data)
    assert any("evidence" in e for e in errors)


def test_non_dict_input_is_error():
    assert validate_analysis_output("não é um dict") == ["saída não é um objeto JSON"]


def test_wrong_type_field_is_error():
    data = _valid_output(clinical_context=123)
    errors = validate_analysis_output(data)
    assert any("clinical_context" in e for e in errors)


# ---------------------------------------------------- RF-21: necessidade hospitalar

def test_necessidade_hospitalar_outside_closed_set_is_error():
    data = _valid_output(necessidade_hospitalar="TALVEZ")
    errors = validate_analysis_output(data)
    assert any("necessidade_hospitalar" in e for e in errors)


# ---------------------------------------------------- RF-22: taxonomia fechada

def test_pending_item_category_outside_taxonomy_is_error():
    data = _valid_output(pending_items=[
        {"category": "OUTRA_COISA_QUALQUER", "description": "x", "evidence": "y", "is_inferred": False}
    ])
    errors = validate_analysis_output(data)
    assert any("taxonomia fechada" in e for e in errors)


def test_pending_item_subcategory_not_in_category_is_error():
    data = _valid_output(pending_items=[
        {
            "category": "DIAGNOSTICO",
            "subcategory": "CONDUTA_DEFINIDA",  # subtipo de INTERCONSULTA, não de DIAGNOSTICO
            "description": "x", "evidence": "y", "is_inferred": False,
        }
    ])
    errors = validate_analysis_output(data)
    assert any("subcategory" in e for e in errors)


# ---------------------------------------------------- RF-25: inferência exige confiança

def test_inferred_pending_item_without_confidence_is_error():
    data = _valid_output(pending_items=[
        {"category": "DIAGNOSTICO", "description": "x", "evidence": "y", "is_inferred": True}
    ])
    errors = validate_analysis_output(data)
    assert any("confidence" in e for e in errors)


def test_inferred_pending_item_with_confidence_is_valid():
    data = _valid_output(pending_items=[
        {"category": "DIAGNOSTICO", "description": "x", "evidence": "y", "is_inferred": True, "confidence": "MEDIA"}
    ])
    assert validate_analysis_output(data) == []


# ---------------------------------------------------- RF-27: EDD

def test_edd_registrada_without_data_is_error():
    data = _valid_output(edd_status="REGISTRADA", edd_data=None)
    errors = validate_analysis_output(data)
    assert any("edd_data" in e for e in errors)


def test_edd_registrada_with_data_is_valid():
    data = _valid_output(edd_status="REGISTRADA", edd_data="2026-08-25")
    assert validate_analysis_output(data) == []


# ---------------------------------------------------- RF-28: dia vermelho exige causa

def test_dia_vermelho_without_causa_is_error():
    data = _valid_output(dia_classificacao="VERMELHO", dia_causa=None)
    errors = validate_analysis_output(data)
    assert any("dia_causa" in e for e in errors)


def test_dia_verde_without_causa_is_valid():
    data = _valid_output(dia_classificacao="VERDE", dia_causa=None)
    assert validate_analysis_output(data) == []


# ---------------------------------------------------- normalização de "null"-string
# LLM real (execução 2026-08-24) devolveu a palavra "null" como STRING em vez
# do valor JSON null -- passa despercebido por isinstance(..., str) e vira
# valor "verdadeiro" (truthy) em código downstream (html_report.py).

def test_normalize_converts_top_level_null_string_to_none():
    data = _valid_output(edd_data="null", dia_causa="null")
    normalized = normalize_null_sentinels(data)
    assert normalized["edd_data"] is None
    assert normalized["dia_causa"] is None


def test_normalize_converts_pending_item_null_string_to_none():
    data = _valid_output(pending_items=[
        {
            "category": "DIAGNOSTICO",
            "subcategory": "INVESTIGACAO_SEM_DEFINICAO",
            "description": "x",
            "evidence": "y",
            "evidence_date": "null",
            "origin": "null",
            "is_inferred": False,
            "confidence": "null",
            "flow_status": "null",
        }
    ])
    normalized = normalize_null_sentinels(data)
    item = normalized["pending_items"][0]
    assert item["evidence_date"] is None
    assert item["origin"] is None
    assert item["confidence"] is None
    assert item["flow_status"] is None


def test_normalize_is_case_insensitive_and_trims_whitespace():
    data = _valid_output(edd_data=" NuLL ", dia_causa="None")
    normalized = normalize_null_sentinels(data)
    assert normalized["edd_data"] is None
    assert normalized["dia_causa"] is None


def test_normalize_recognizes_portuguese_nulo():
    data = _valid_output(dia_causa="nulo")
    assert normalize_null_sentinels(data)["dia_causa"] is None


def test_normalize_leaves_real_values_untouched():
    data = _valid_output(edd_data="2026-08-25", dia_causa="Aguarda leito de UTI")
    normalized = normalize_null_sentinels(data)
    assert normalized["edd_data"] == "2026-08-25"
    assert normalized["dia_causa"] == "Aguarda leito de UTI"


def test_normalize_does_not_mutate_input():
    data = _valid_output(edd_data="null")
    normalize_null_sentinels(data)
    assert data["edd_data"] == "null"


def test_normalize_non_dict_input_returns_unchanged():
    assert normalize_null_sentinels("não é um dict") == "não é um dict"


def test_normalize_then_validate_catches_edd_registrada_with_null_string():
    """Sem a normalização, edd_data="null" (string) passaria como se fosse
    um valor real -- a validação só pega a inconsistência DEPOIS de
    normalizar para None de verdade (RF-27)."""
    data = _valid_output(edd_status="REGISTRADA", edd_data="null")
    normalized = normalize_null_sentinels(data)
    errors = validate_analysis_output(normalized)
    assert any("edd_data" in e for e in errors)


# ---------------------------------------------------- origem_internacao (DEC-063)

def test_origem_internacao_absent_is_valid():
    """Campo opcional -- maioria das análises incrementais não tem como
    saber a origem (só aparece na nota de admissão, RF-08)."""
    data = _valid_output()
    assert "origem_internacao" not in data
    assert validate_analysis_output(data) == []


def test_origem_internacao_string_is_valid():
    data = _valid_output(origem_internacao="Emergência")
    assert validate_analysis_output(data) == []


def test_origem_internacao_wrong_type_is_error():
    data = _valid_output(origem_internacao=123)
    errors = validate_analysis_output(data)
    assert any("origem_internacao" in e for e in errors)


def test_normalize_converts_origem_internacao_null_string_to_none():
    data = _valid_output(origem_internacao="null")
    assert normalize_null_sentinels(data)["origem_internacao"] is None


# ---------------------------------------------------- edd_status "null" (DEC-065)
# Achado real 2026-08-25 (execução contra pacientes reais com admissão
# longa): o LLM às vezes escreve "null" também num ENUM OBRIGATÓRIO
# (edd_status nunca é nullable) -- sem normalização, isso derrubava as 3
# tentativas de retry (nenhum valor de fallback existe pra "None" aqui).

def test_normalize_edd_status_null_string_with_edd_data_becomes_registrada():
    data = _valid_output(edd_status="null", edd_data="2026-08-25")
    normalized = normalize_null_sentinels(data)
    assert normalized["edd_status"] == "REGISTRADA"
    assert validate_analysis_output(normalized) == []


def test_normalize_edd_status_null_string_without_edd_data_becomes_nao_registrada():
    data = _valid_output(edd_status="null", edd_data=None)
    normalized = normalize_null_sentinels(data)
    assert normalized["edd_status"] == "NAO_REGISTRADA"
    assert validate_analysis_output(normalized) == []


def test_normalize_edd_status_null_string_uses_already_normalized_edd_data():
    """edd_data também veio como string "null" -- a normalização de
    edd_status precisa rodar DEPOIS da de edd_data, não antes."""
    data = _valid_output(edd_status="null", edd_data="null")
    normalized = normalize_null_sentinels(data)
    assert normalized["edd_data"] is None
    assert normalized["edd_status"] == "NAO_REGISTRADA"


def test_normalize_leaves_real_edd_status_untouched():
    data = _valid_output(edd_status="VENCIDA", edd_data="2026-08-20")
    normalized = normalize_null_sentinels(data)
    assert normalized["edd_status"] == "VENCIDA"
