"""Testes de app/orchestrator.py -- reconciliação de pendências entre
execuções (DEC-062): reiteração de evidência, resolução automática e
isolamento entre pendências de regra (RULES-001) e de LLM."""
import pytest

from app.analysis.rules import StructuredNote
from app.models import Note, Patient
from app.orchestrator import (
    LLM_LOOKBACK_DAYS,
    _find_matching_pending,
    _limit_to_char_budget,
    _limit_to_recent_window,
    _prepare_notes_for_llm,
    _process_patient_rules,
    _reconstruct_all_notes,
    _run_llm_analysis,
    _safe_error_text,
)
from app.storage import database
from app.storage.repository import Repository


class FixedTextRecordSource:
    """RecordSource mínimo: sempre devolve o mesmo texto bruto, ignorando
    `known_days` -- suficiente pra testar _process_patient_rules isoladamente
    sem depender de fixtures em arquivo."""

    def __init__(self, text: str):
        self.text = text

    def get_raw_notes_text(self, patient, known_days=frozenset()) -> str:
        return self.text


@pytest.fixture
def repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    yield Repository(conn)
    conn.close()


class StubLLMSequence:
    """Devolve uma resposta diferente a cada chamada, na ordem passada --
    simula o LLM reavaliando o paciente em execuções sucessivas."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls = 0

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None):
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


def _base_analysis(**overrides) -> dict:
    base = {
        "clinical_context": "Contexto.",
        "current_status": "Status.",
        "necessidade_hospitalar": "SIM",
        "necessidade_hospitalar_justificativa": "Justificativa.",
        "objetivo_terapeutico": "Objetivo.",
        "proximo_passo": "Próximo passo.",
        "edd_data": None,
        "edd_status": "NAO_REGISTRADA",
        "dia_classificacao": "VERDE",
        "dia_causa": None,
        "pending_items": [],
        "insufficient_information": False,
    }
    base.update(overrides)
    return base


def _note(text="texto", timestamp="2026-08-20T08:00:00") -> StructuredNote:
    return StructuredNote(timestamp=timestamp, specialty="Clínica Médica", source_type="Evolução", text=text)


def _pending_item(**overrides) -> dict:
    base = {
        "category": "DIAGNOSTICO",
        "subcategory": "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL",
        "description": "Aguarda tomografia",
        "evidence": "Solicitada tomografia de tórax.",
        "evidence_date": "2026-08-20T08:00:00",
        "origin": "INTERNA",
        "is_inferred": False,
        "confidence": None,
        "flow_status": None,
    }
    base.update(overrides)
    return base


# --------------------------------------------------- _find_matching_pending

def test_find_matching_pending_matches_by_category_and_subcategory():
    rows = [{"category": "DIAGNOSTICO", "subcategory": "X", "pending_id": "p1"}]
    assert _find_matching_pending(rows, "DIAGNOSTICO", "X")["pending_id"] == "p1"


def test_find_matching_pending_no_match_returns_none():
    rows = [{"category": "DIAGNOSTICO", "subcategory": "X", "pending_id": "p1"}]
    assert _find_matching_pending(rows, "DIAGNOSTICO", "Y") is None
    assert _find_matching_pending(rows, "INTERCONSULTA", "X") is None


# --------------------------------------------------- _run_llm_analysis

def test_reiteration_adds_evidence_instead_of_duplicating_pending_item(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_pending_item(evidence="Solicitada tomografia.", evidence_date="2026-08-20T08:00:00")]),
        _base_analysis(pending_items=[_pending_item(evidence="Mantém aguardo de tomografia.", evidence_date="2026-08-21T08:00:00")]),
    ])

    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1
    # Primeira evidência documental preservada -- nunca sobrescrita pela reiteração.
    assert active[0]["evidence_date"] == "2026-08-20T08:00:00"
    assert active[0]["evidence"] == "Solicitada tomografia."

    evidences = repo.get_pending_item_evidence(active[0]["pending_id"])
    assert len(evidences) == 1
    assert evidences[0]["text"] == "Mantém aguardo de tomografia."
    assert evidences[0]["timestamp"] == "2026-08-21T08:00:00"


def test_pending_item_not_reconfirmed_gets_resolved(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_pending_item()]),
        _base_analysis(pending_items=[]),  # resolvido -- LLM não reconfirma
    ])

    _run_llm_analysis(repo, patient_id, llm, [_note()])
    first_pending_id = repo.get_active_pending_items(patient_id)[0]["pending_id"]

    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    assert repo.get_active_pending_items(patient_id) == []
    row = repo.conn.execute("SELECT status FROM pending_items WHERE pending_id = ?", (first_pending_id,)).fetchone()
    assert row["status"] == "RESOLVED"


def test_new_category_creates_new_item_and_resolves_the_old_one(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_pending_item(category="DIAGNOSTICO", subcategory="AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL")]),
        _base_analysis(pending_items=[_pending_item(
            category="INTERCONSULTA", subcategory="SOLICITADA",
            description="Aguarda avaliação cardiologia", evidence="Solicitado parecer de cardiologia.",
        )]),
    ])

    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1
    assert active[0]["category"] == "INTERCONSULTA"

    all_rows = repo.conn.execute("SELECT category, status FROM pending_items WHERE patient_id = ?", (patient_id,)).fetchall()
    assert len(all_rows) == 2
    statuses = {row["category"]: row["status"] for row in all_rows}
    assert statuses["DIAGNOSTICO"] == "RESOLVED"
    assert statuses["INTERCONSULTA"] == "ACTIVE"


def test_pending_items_are_tagged_with_llm_source(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item()])])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    row = repo.conn.execute("SELECT source FROM pending_items WHERE patient_id = ?", (patient_id,)).fetchone()
    assert row["source"] == "LLM"


# ---------------------------------------------- especialidade/origem/model_version (DEC-063)

def test_especialidade_responsavel_comes_from_most_recent_note_not_the_llm(repo):
    """RF-07/RF-08: a especialidade já é metadado estruturado (extraído do
    cabeçalho da evolução) -- não pedimos pro LLM adivinhar de novo."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis()])
    notes = [
        _note(timestamp="2026-08-20T08:00:00"),  # specialty="Clínica Médica" (default do helper)
        StructuredNote(timestamp="2026-08-21T08:00:00", specialty="Infectologia", source_type="Evolução", text="x"),
    ]

    _run_llm_analysis(repo, patient_id, llm, notes)

    assert repo.get_patient_state(patient_id)["especialidade_responsavel"] == "Infectologia"


def test_model_version_is_stamped_from_llm_when_available(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))

    class LLMWithVersion(StubLLMSequence):
        model_version = "model-v1.gguf"

    llm = LLMWithVersion([_base_analysis()])
    _run_llm_analysis(repo, patient_id, llm, [_note()])

    assert repo.get_patient_state(patient_id)["model_version"] == "model-v1.gguf"


def test_model_version_is_none_when_llm_has_no_such_attribute(repo):
    """StubLLM de teste (e qualquer AnalysisEngine sem `model_version`) não
    deve quebrar -- duck typing via getattr, nunca exigido pelo Protocol."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis()])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    assert repo.get_patient_state(patient_id)["model_version"] is None


def test_origem_internacao_passed_through_from_llm_analysis(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis(origem_internacao="Emergência")])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    assert repo.get_patient_state(patient_id)["origem_internacao"] == "Emergência"


def test_llm_origin_defaults_when_model_returns_null(repo):
    """Achado real (revisão de conformidade, DEC-064): o LLM devolve
    origin=null com frequência (campo opcional) -- sem fallback, a maioria
    das pendências do LLM nunca mostrava o selo interna/externa."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item(category="DIAGNOSTICO", origin=None)])])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    row = repo.conn.execute("SELECT origin FROM pending_items WHERE patient_id = ?", (patient_id,)).fetchone()
    assert row["origin"] == "INTERNA"  # default_origin(DIAGNOSTICO)


def test_llm_origin_preserved_when_model_provides_it(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item(category="TRANSFERENCIA", origin="EXTERNA")])])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    row = repo.conn.execute("SELECT origin FROM pending_items WHERE patient_id = ?", (patient_id,)).fetchone()
    assert row["origin"] == "EXTERNA"


def test_stage_progression_category_matches_by_category_alone(repo):
    """DEC-064: INTERCONSULTA/PROCEDIMENTO_CIRURGIA têm subtipo de ESTÁGIO
    (seção 4.2: solicitado -> realizado -> conduta definida). Sem isso, a
    MESMA interconsulta evoluindo de estágio virava uma pendência nova,
    perdendo a evidência original e duplicando a pendência."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_pending_item(
            category="INTERCONSULTA", subcategory="SOLICITADA",
            description="Aguarda avaliação cardiologia", evidence="Solicitado parecer de cardiologia.",
            evidence_date="2026-08-20T08:00:00",
        )]),
        _base_analysis(pending_items=[_pending_item(
            category="INTERCONSULTA", subcategory="REALIZADA_SEM_CONDUTA_DEFINIDA",
            description="Aguarda conduta cardiologia", evidence="Cardiologia avaliou, aguarda conduta.",
            evidence_date="2026-08-22T08:00:00",
        )]),
    ])

    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-22T08:00:00")])

    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1  # não duplicou apesar do subtipo ter mudado
    assert active[0]["subcategory"] == "SOLICITADA"  # dado original preservado, nunca sobrescrito
    assert active[0]["evidence_date"] == "2026-08-20T08:00:00"  # primeira evidência preservada
    evidences = repo.get_pending_item_evidence(active[0]["pending_id"])
    assert len(evidences) == 1
    assert evidences[0]["text"] == "Cardiologia avaliou, aguarda conduta."


# ------------------------------------------------- _process_patient_rules (regras, DEC-064)

_EXAM_NOTE = "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nSolicitada tomografia de torax.\n\n"
_ULTRASOUND_NOTE = "20/08/2026 10:00 - Profissional Teste Um (Medico clinico)\nSolicitado ultrassom abdominal.\n\n"
_EXAM_COMPLETION_NOTE = "21/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nTomografia de torax realizada, sem alteracoes.\n\n"


def _patient():
    return Patient(record_number="123456", bed="2A", unit="U", admission_date="2026-08-19")


def test_two_concurrent_rule_pending_items_do_not_collide(repo):
    """Achado real (DEC-064): RULES-001 usa um subtipo único (AGUARDA_EXAME_
    IMAGEM_OU_LABORATORIAL) pra TODO tipo de exame -- casar só por
    categoria+subtipo fazia um segundo exame concorrente (ultrassom) colidir
    com o primeiro (tomografia) já ativo e nunca ser persistido."""
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    record_source = FixedTextRecordSource(_EXAM_NOTE + _ULTRASOUND_NOTE)

    _process_patient_rules(repo, patient, patient_id, record_source)

    active = repo.get_active_pending_items(patient_id)
    descriptions = {row["description"] for row in active}
    assert len(active) == 2
    assert any("tomografia" in d.lower() for d in descriptions)
    assert any("ultrassom" in d.lower() for d in descriptions)


def test_rule_pending_item_not_resolved_just_because_it_did_not_reappear(repo):
    """Achado real (DEC-064): sem a nota de conclusão na MESMA janela, uma
    pendência de regra não pode ser considerada resolvida -- a nota de
    origem pode simplesmente ter saído do recorte incremental do GSUS real
    (`get_raw_notes_text` pula dias antigos já conhecidos)."""
    patient = _patient()
    patient_id = repo.upsert_patient(patient)

    record_source_day1 = FixedTextRecordSource(_EXAM_NOTE)
    _process_patient_rules(repo, patient, patient_id, record_source_day1)
    assert len(repo.get_active_pending_items(patient_id)) == 1

    # Dia seguinte: a nota original "saiu da janela" -- só uma nota
    # totalmente não relacionada aparece (nunca menciona a tomografia).
    record_source_day2 = FixedTextRecordSource(
        "22/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nPaciente estavel, sem queixas novas.\n\n"
    )
    _process_patient_rules(repo, patient, patient_id, record_source_day2)

    assert len(repo.get_active_pending_items(patient_id)) == 1  # continua ativa, não some


def test_rule_pending_item_resolves_when_completion_seen_in_same_window(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    record_source_day1 = FixedTextRecordSource(_EXAM_NOTE)
    _process_patient_rules(repo, patient, patient_id, record_source_day1)
    assert len(repo.get_active_pending_items(patient_id)) == 1

    # A conclusão chega numa nota nova -- `all_structured_notes` desta rodada
    # contém as duas (a fixture devolve o texto completo de novo, como o
    # GSUS real faria pro trecho ainda dentro da janela de atualização).
    record_source_day2 = FixedTextRecordSource(_EXAM_NOTE + _EXAM_COMPLETION_NOTE)
    _process_patient_rules(repo, patient, patient_id, record_source_day2)

    assert repo.get_active_pending_items(patient_id) == []


def test_rule_and_llm_pending_items_with_same_category_subcategory_stay_isolated(repo):
    """Gap de teste apontado na revisão de conformidade (DEC-064): uma
    pendência de REGRA e uma do LLM com a mesma categoria+subtipo pro mesmo
    paciente não podem se misturar (evidência ir pra linha errada, ou uma
    resolver a outra por engano)."""
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    record_source = FixedTextRecordSource(_EXAM_NOTE)

    _process_patient_rules(repo, patient, patient_id, record_source)
    rule_row = repo.get_active_pending_items(patient_id)[0]
    assert rule_row["source"] == "RULE"

    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item(
        category="DIAGNOSTICO", subcategory="AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL",
        description="Aguarda ressonância (achado do LLM)", evidence="Solicito RM de coluna.",
    )])])
    _run_llm_analysis(repo, patient_id, llm, [_note()])

    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 2
    sources = {row["source"] for row in active}
    assert sources == {"RULE", "LLM"}
    # A pendência de regra original continua intacta -- não foi resolvida
    # nem teve evidência da pendência do LLM anexada a ela.
    assert rule_row["pending_id"] in {row["pending_id"] for row in active}
    assert repo.get_pending_item_evidence(rule_row["pending_id"]) == []


# ------------------------------------------------- _limit_to_recent_window (DEC-066)
# Achado real 2026-08-25: paciente real com admissão longa (~43 evoluções)
# estourava timeout/contexto do LLM no hardware fraco. Decisão do usuário:
# nunca mandar mais que LLM_LOOKBACK_DAYS de evolução pro LLM.

def test_limit_to_recent_window_keeps_everything_within_range():
    notes = [_note(timestamp="2026-08-20T08:00:00"), _note(timestamp="2026-08-22T08:00:00")]
    limited, excluded = _limit_to_recent_window(notes, LLM_LOOKBACK_DAYS)
    assert len(limited) == 2
    assert excluded is False


def test_limit_to_recent_window_drops_notes_older_than_window():
    old_note = _note(timestamp="2026-07-01T08:00:00")
    recent_note = _note(timestamp="2026-08-20T08:00:00")
    limited, excluded = _limit_to_recent_window([old_note, recent_note], LLM_LOOKBACK_DAYS)
    assert limited == [recent_note]
    assert excluded is True


def test_limit_to_recent_window_is_relative_to_most_recent_note_not_today():
    """A janela é relativa à nota mais nova do lote, não a "agora" -- uma
    atualização de madrugada não pode descartar notas só porque a mais nova
    tem poucas horas de idade."""
    notes = [_note(timestamp="2020-01-01T08:00:00"), _note(timestamp="2020-01-10T08:00:00")]
    limited, excluded = _limit_to_recent_window(notes, LLM_LOOKBACK_DAYS)
    assert len(limited) == 2
    assert excluded is False


def test_limit_to_recent_window_keeps_notes_without_timestamp():
    """Nunca descarta por excesso de cautela -- nota sem data reconhecida
    sempre entra, já que não dá pra saber se é antiga."""
    undated = _note(timestamp=None)
    old_note = _note(timestamp="2026-07-01T08:00:00")
    recent_note = _note(timestamp="2026-08-20T08:00:00")
    limited, excluded = _limit_to_recent_window([undated, old_note, recent_note], LLM_LOOKBACK_DAYS)
    assert undated in limited
    assert old_note not in limited
    assert excluded is True


def test_limit_to_recent_window_no_op_when_no_note_has_timestamp():
    notes = [_note(timestamp=None), _note(timestamp=None)]
    limited, excluded = _limit_to_recent_window(notes, LLM_LOOKBACK_DAYS)
    assert limited == notes
    assert excluded is False


# ------------------------------------------- _process_patient_rules aplica a janela

def test_process_patient_limits_llm_input_but_not_rules(repo):
    """A janela de 2 semanas só limita o que vai pro LLM -- RULES-001
    continua vendo o histórico completo (não tem o custo de contexto/tempo
    do LLM). Admissão em junho (DEC-119): a nota de julho pertence a ESTA
    internação -- o filtro por data de admissão não pode descartá-la."""
    patient = Patient(record_number="123456", bed="2A", unit="U", admission_date="2026-06-15")
    patient_id = repo.upsert_patient(patient)
    old_exam_note = (
        "01/07/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Solicitada tomografia de torax.\n\n"
    )
    recent_note = (
        "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Paciente estavel, sem queixas novas.\n\n"
    )
    record_source = FixedTextRecordSource(old_exam_note + recent_note)
    llm = StubLLMSequence([_base_analysis()])

    # DEC-071: _process_patient_rules não chama mais o LLM sozinha -- quem
    # orquestra a segunda fase agora é run_once. Aqui simulamos exatamente
    # o que run_once faz com o retorno dela.
    llm_notes, window_limited = _process_patient_rules(repo, patient, patient_id, record_source)
    _run_llm_analysis(repo, patient_id, llm, llm_notes, window_limited=window_limited)

    # A regra viu a nota de julho (fora da janela de 2 semanas) e criou a
    # pendência normalmente -- RULES-001 não é limitado pela janela do LLM.
    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1
    assert active[0]["source"] == "RULE"

    # O LLM só viu as notas dentro da janela -- sinalizado no patient_state.
    assert repo.get_patient_state(patient_id)["analysis_window_limited"] == 1


def test_process_patient_does_not_flag_window_limited_for_small_backlog(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    record_source = FixedTextRecordSource(
        "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nPaciente estavel.\n\n"
    )
    llm = StubLLMSequence([_base_analysis()])

    llm_notes, window_limited = _process_patient_rules(repo, patient, patient_id, record_source)
    _run_llm_analysis(repo, patient_id, llm, llm_notes, window_limited=window_limited)

    assert repo.get_patient_state(patient_id)["analysis_window_limited"] == 0


# --------------------------------------- _safe_error_text (achado PRIVACIDADE)
# Achado real (auditoria de resiliência 2026-08-28): `str(exc)` de um erro do
# Playwright pode incluir o "Call log" inteiro (passos, URLs) e, em erros de
# "strict mode violation", o HTML dos elementos que casaram -- podendo
# carregar texto da própria tela do GSUS. Persistido sem filtro em
# `processing_queue.last_error` isso é um vazamento real pra disco.

def test_safe_error_text_keeps_only_type_and_first_line():
    exc = TimeoutError("Locator.click: Timeout 30000ms exceeded.")
    assert _safe_error_text(exc) == "TimeoutError: Locator.click: Timeout 30000ms exceeded."


def test_safe_error_text_strips_playwright_call_log_and_dom_dump():
    multi_line = (
        "Locator.click: Timeout 30000ms exceeded.\n"
        "Call log:\n"
        "  - waiting for locator(\"text=Pesquisar Prontuário\")\n"
        "  - element matched <div>Paciente Fulano de Tal, prontuário 123456</div>\n"
    )
    exc = RuntimeError(multi_line)

    result = _safe_error_text(exc)

    assert result == "RuntimeError: Locator.click: Timeout 30000ms exceeded."
    assert "Call log" not in result
    assert "123456" not in result


def test_safe_error_text_truncates_very_long_first_line():
    exc = ValueError("x" * 1000)
    assert len(_safe_error_text(exc)) <= 300


# --------------------------------------------- _reconstruct_all_notes (LLM-004)

def test_reconstruct_all_notes_returns_full_history_not_just_latest(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    repo.add_note(Note(patient_id=patient_id, source_type="Evolução", specialty="Clínica Médica",
                        timestamp="2026-08-20T08:00:00", text="Primeira evolução.", text_hash="h1"))
    repo.add_note(Note(patient_id=patient_id, source_type="Evolução", specialty="Clínica Médica",
                        timestamp="2026-08-21T08:00:00", text="Segunda evolução.", text_hash="h2"))

    notes = _reconstruct_all_notes(repo, patient_id)

    assert [n.text for n in notes] == ["Primeira evolução.", "Segunda evolução."]


def test_reconstruct_all_notes_empty_for_patient_without_any_note(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)

    assert _reconstruct_all_notes(repo, patient_id) == []


# ------------------------------------------- _limit_to_char_budget (RESIL-006)
# Achado real (backfill do LLM-005, 2026-08-29): mesmo dentro da janela de 14
# dias (DEC-066), paciente de internação longa/densa pode exceder o
# --ctx-size do llama-server (DEC-092) -- rejeitado de cara (HTTP 400),
# sempre nas 3 tentativas. Corte adicional por TAMANHO, não só por tempo.

def test_limit_to_char_budget_keeps_everything_when_under_budget():
    notes = [_note("a" * 100), _note("b" * 100)]
    result, limited = _limit_to_char_budget(notes, max_chars=1000)
    assert result == notes
    assert limited is False


def test_limit_to_char_budget_drops_oldest_notes_first_until_it_fits():
    oldest = _note("x" * 600, timestamp="2026-08-18T08:00:00")
    middle = _note("y" * 600, timestamp="2026-08-19T08:00:00")
    newest = _note("z" * 600, timestamp="2026-08-20T08:00:00")
    notes = [oldest, middle, newest]

    result, limited = _limit_to_char_budget(notes, max_chars=1300)

    assert result == [middle, newest]  # a mais antiga foi descartada primeiro
    assert limited is True


def test_limit_to_char_budget_never_returns_empty_even_if_single_note_exceeds_budget():
    huge = _note("x" * 5000)
    result, limited = _limit_to_char_budget([huge], max_chars=1000)
    assert result == [huge]  # sozinha, mesmo estourando -- nunca zero notas
    assert limited is False  # não há mais nada a cortar (só 1 nota)


def test_prepare_notes_for_llm_reports_limited_when_only_size_cuts_something():
    """Achado real: a janela de 14 dias sozinha pode não cortar nada (tudo
    dentro do prazo) e ainda assim o volume ser grande demais pro LLM --
    `window_limited` precisa refletir ISSO também, não só o corte por data."""
    notes = [_note("a" * 5000, "2026-08-19T08:00:00"), _note("b" * 5000, "2026-08-20T08:00:00")]

    result, window_limited = _prepare_notes_for_llm(notes, days=14)

    assert result == [notes[1]]  # janela de data manteve as duas, corte por tamanho descartou a mais antiga
    assert window_limited is True


# ------------------------------------------------------------- DEC-119

def test_llm_dates_in_br_format_are_normalized_before_saving(repo):
    """Achado real 2026-09-07: 111 de 204 pendências ativas do LLM tinham
    evidence_date em DD/MM (copiado da evolução) -- `hours_elapsed_since`
    não lê e o censo mostrava "tempo indeterminado" onde havia data."""
    patient_id = repo.upsert_patient(_patient())
    llm = StubLLMSequence([
        _base_analysis(
            edd_data="25/08/2026", edd_status="REGISTRADA",
            pending_items=[_pending_item(evidence_date="20/08/2026 08:00")],
        ),
        _base_analysis(pending_items=[_pending_item(evidence_date="21/08/2026 09:15")]),
    ])

    _run_llm_analysis(repo, patient_id, llm, [_note()])
    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1
    assert active[0]["evidence_date"] == "2026-08-20T08:00:00"
    assert repo.get_patient_state(patient_id)["edd_data"] == "2026-08-25"

    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T09:15:00")])
    extra = repo.get_pending_item_evidence(active[0]["pending_id"])
    assert [row["timestamp"] for row in extra] == ["2026-08-21T09:15:00"]


def test_llm_unrecognized_evidence_date_becomes_null_never_a_guess(repo):
    patient_id = repo.upsert_patient(_patient())
    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item(evidence_date="semana passada")])])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    assert repo.get_active_pending_items(patient_id)[0]["evidence_date"] is None


def test_process_patient_ignores_notes_dated_before_current_admission(repo):
    """Achado real 2026-09-07: dias de episódios antigos (2014-2025)
    entravam nas regras e geravam pendências com "anos" de espera. Nota
    anterior à admissão menos a folga de pronto-socorro não entra nas
    regras nem no LLM -- mas continua gravada (não reabrir o dia, DEC-048)."""
    patient = Patient(record_number="123456", bed="2A", unit="U", admission_date="19/08/2026")
    patient_id = repo.upsert_patient(patient)
    old_episode_note = (
        "01/07/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Solicitada tomografia de abdome.\n\n"
    )
    emergency_note = (  # 4 dias antes da admissão: dentro da folga, vale
        "15/08/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Solicitada tomografia de torax.\n\n"
    )
    recent_note = (
        "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Paciente estavel, sem queixas novas.\n\n"
    )
    record_source = FixedTextRecordSource(old_episode_note + emergency_note + recent_note)

    result = _process_patient_rules(repo, patient, patient_id, record_source)

    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1  # só a tomografia de tórax (pronto-socorro), nunca a de 01/07
    assert active[0]["evidence_date"] == "2026-08-15T08:00:00"
    assert "2026-07-01" in repo.get_note_days(patient_id)  # gravada, não analisada
    assert result is not None
    llm_notes, _ = result
    assert all(note.timestamp >= "2026-08-12" for note in llm_notes)


def test_process_patient_without_admission_date_filters_nothing(repo):
    patient = Patient(record_number="123456", bed="2A", unit="U", admission_date=None)
    patient_id = repo.upsert_patient(patient)
    old_note = (
        "01/07/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Solicitada tomografia de abdome.\n\n"
    )
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(old_note))
    assert len(repo.get_active_pending_items(patient_id)) == 1  # sem data de internação, nunca descarta
