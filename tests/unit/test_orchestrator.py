"""Testes de app/orchestrator.py -- reconciliação de pendências entre
execuções (DEC-062): reiteração de evidência, resolução automática e
isolamento entre pendências de regra (RULES-001) e de LLM."""
import pytest

from app.analysis.rules import StructuredNote
from app.models import Note, Patient
from app.orchestrator import (
    LLM_LOOKBACK_DAYS,
    RECENT_ADMISSION_GRACE_DAYS,
    _is_recent_admission,
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

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None, patient_context=None):
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


def _note(text="Solicitada tomografia de tórax.", timestamp="2026-08-20T08:00:00") -> StructuredNote:
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

    _run_llm_analysis(repo, patient_id, llm, [_note("Solicitada tomografia.")])
    _run_llm_analysis(repo, patient_id, llm, [_note("Mantém aguardo de tomografia.", timestamp="2026-08-21T08:00:00")])

    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1
    # Primeira evidência documental preservada -- nunca sobrescrita pela reiteração.
    assert active[0]["evidence_date"] == "2026-08-20T08:00:00"
    assert active[0]["evidence"] == "Solicitada tomografia."

    evidences = repo.get_pending_item_evidence(active[0]["pending_id"])
    assert len(evidences) == 1
    assert evidences[0]["text"] == "Mantém aguardo de tomografia."
    assert evidences[0]["timestamp"] == "2026-08-21T08:00:00"


def test_pending_item_missing_from_the_answer_keeps_its_state(repo):
    """PEND-001 (DEC-129, decisão 3): "sumiu da resposta" deixou de concluir
    -- antes era a ÚNICA regra de fechamento, sem evidência nenhuma. Decisão
    omitida mantém o estado."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_pending_item()]),
        _base_analysis(pending_items=[]),
    ])

    _run_llm_analysis(repo, patient_id, llm, [_note()])
    first_pending_id = repo.get_active_pending_items(patient_id)[0]["pending_id"]
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    [row] = repo.get_active_pending_items(patient_id)
    assert (row["pending_id"], row["status"]) == (first_pending_id, "ACTIVE")


def test_new_category_creates_new_item_and_keeps_the_old_one(repo):
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

    statuses = {row["category"]: row["status"] for row in repo.get_active_pending_items(patient_id)}
    assert statuses == {"DIAGNOSTICO": "ACTIVE", "INTERCONSULTA": "ACTIVE"}


def test_pending_items_are_tagged_with_llm_source(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item()])])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    row = repo.conn.execute("SELECT source FROM pending_items WHERE patient_id = ?", (patient_id,)).fetchone()
    assert row["source"] == "LLM"


# ---------------------------------------------- especialidade/origem/model_version (DEC-063)

def test_especialidade_responsavel_comes_from_most_recent_physician_role_not_the_llm(repo):
    """RF-07/RF-08: a especialidade já é metadado estruturado (o cargo no
    cabeçalho da evolução) -- não pedimos pro LLM adivinhar de novo.
    LLM-006: antes lia `specialty`, que o parser real nunca preenche (campo
    sempre NULL em produção); agora é o cargo da evolução MÉDICA mais
    recente -- uma anotação de enfermagem posterior não muda nada."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis()])
    notes = [
        StructuredNote(timestamp="2026-08-20T08:00:00", specialty=None, source_type="Médico clínico", text="x"),
        StructuredNote(timestamp="2026-08-21T08:00:00", specialty=None, source_type="Médico infectologista", text="y"),
        StructuredNote(timestamp="2026-08-21T20:00:00", specialty=None, source_type="Técnico de enfermagem", text="z"),
    ]

    _run_llm_analysis(repo, patient_id, llm, notes)

    assert repo.get_patient_state(patient_id)["especialidade_responsavel"] == "Médico infectologista"


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

    _run_llm_analysis(repo, patient_id, llm, [_note("Solicitado parecer de cardiologia.")])
    _run_llm_analysis(repo, patient_id, llm, [_note("Cardiologia avaliou, aguarda conduta.", timestamp="2026-08-22T08:00:00")])

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
    """Nunca zero notas. LLM-006: inteira, uma nota maior que o orçamento
    estourava o contexto (HTTP 400, DEC-093) e a análise falhava -- agora vai
    o começo dela, com aviso de corte."""
    from app.orchestrator import TRUNCATION_NOTICE

    huge = _note("x" * 5000)
    result, limited = _limit_to_char_budget([huge], max_chars=1000)
    assert len(result) == 1
    assert len(result[0].text) <= 1000
    assert result[0].text.endswith(TRUNCATION_NOTICE)
    assert limited is True


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


def test_llm_unrecognized_evidence_date_is_never_a_guess(repo):
    """Data ilegível da IA e trecho fora das evoluções gravadas: vale a
    evolução mais nova que a IA leu (o pedido está em algum ponto dela) --
    nunca um palpite anterior (2ª revisão adversarial)."""
    patient_id = repo.upsert_patient(_patient())
    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item(evidence_date="semana passada")])])

    _run_llm_analysis(repo, patient_id, llm, [_note()])

    assert repo.get_active_pending_items(patient_id)[0]["evidence_date"] == "2026-08-20T08:00:00"


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


# ------------------------------------------------------ RESIL-015: admissão recente
def test_is_recent_admission_accepts_today_and_yesterday_in_gsus_format():
    from datetime import date

    today = date(2026, 9, 8)
    assert RECENT_ADMISSION_GRACE_DAYS == 1
    assert _is_recent_admission("08/09/2026", today) is True
    assert _is_recent_admission("07/09/2026", today) is True
    assert _is_recent_admission("2026-09-07", today) is True  # ISO também (dado sintético)


def test_is_recent_admission_rejects_older_future_missing_or_garbage_dates():
    from datetime import date

    today = date(2026, 9, 8)
    assert _is_recent_admission("06/09/2026", today) is False  # 2 dias: fora da folga
    # Data futura (erro de digitação no GSUS): `days_since_admission` trunca em 0,
    # logo conta como recente -- benigno, o paciente é retomado na próxima execução.
    assert _is_recent_admission("09/09/2026", today) is True
    assert _is_recent_admission(None, today) is False
    assert _is_recent_admission("", today) is False
    assert _is_recent_admission("sem data", today) is False


# ------------------------------------------------------------------ EDD-001
def test_relative_discharge_forecast_fills_edd_when_llm_has_no_explicit_date(repo):
    """Pedido do pagador (2026-09-07): "alta em 48h" na evolução conta como
    EDD documentada -- convertida a partir da data da evolução, nunca pelo
    modelo, e gravada como inferida."""
    patient_id = repo.upsert_patient(_patient())
    llm = StubLLMSequence([_base_analysis(edd_data=None, edd_status="NAO_REGISTRADA")])

    _run_llm_analysis(repo, patient_id, llm, [_note(text="Estável. Previsão de alta em 48h.", timestamp="2026-09-08T10:00:00")])

    state = repo.get_patient_state(patient_id)
    assert (state["edd_data"], state["edd_status"], state["edd_inferida"]) == ("2026-09-10", "REGISTRADA", 1)


def test_explicit_llm_edd_wins_over_relative_forecast_and_is_not_marked_inferred(repo):
    patient_id = repo.upsert_patient(_patient())
    llm = StubLLMSequence([_base_analysis(edd_data="2026-09-15", edd_status="REGISTRADA")])

    _run_llm_analysis(repo, patient_id, llm, [_note(text="Previsão de alta em 48h.", timestamp="2026-09-08T10:00:00")])

    state = repo.get_patient_state(patient_id)
    assert (state["edd_data"], state["edd_inferida"]) == ("2026-09-15", 0)


# --------------------------------------------- LLM-006 (pedido do auditor, DEC-129)

def _src_note(timestamp, source_type, size=10, text=None):
    return StructuredNote(timestamp=timestamp, specialty=None, source_type=source_type, text=text or "x" * size)


def test_newest_notes_survive_the_size_cut_even_when_extraction_lists_newest_day_first():
    """Achado da Fase 2 (contagem no banco de dev: 2.641 trocas de dia "pra
    trás" contra 39 "pra frente"): a extração entrega o dia mais novo
    primeiro, e o corte por tamanho descartava justamente as evoluções mais
    recentes. Agora as notas são ordenadas por horário antes de cortar."""
    from app.orchestrator import MAX_NOTES_CHARS_FOR_LLM

    half = MAX_NOTES_CHARS_FOR_LLM // 2 + 100
    as_extracted = [  # dia mais novo primeiro, como vem do GSUS
        _src_note("2026-08-22T08:00:00", "Médico clínico", half, text="N" * half),
        _src_note("2026-08-21T08:00:00", "Médico clínico", half, text="M" * half),
        _src_note("2026-08-20T08:00:00", "Médico clínico", half, text="O" * half),
    ]
    kept, limited = _prepare_notes_for_llm(as_extracted, LLM_LOOKBACK_DAYS)
    assert limited is True
    assert [n.timestamp for n in kept] == ["2026-08-22T08:00:00"]  # a mais nova, nunca a mais antiga


def _kept(notes, budget=9000):
    kept, limited = _limit_to_char_budget(sorted(notes, key=lambda n: n.timestamp), budget)
    assert limited is True
    kept_ts = [n.timestamp for n in kept]
    assert kept_ts == sorted(kept_ts)  # sempre em ordem cronológica
    return kept_ts


def test_size_cut_keeps_the_newest_notes_and_never_drops_the_newest_physician_note():
    """Seção 2 do auditor: a evolução médica mais recente é a base de hoje
    -- entra antes de tudo. O resto segue a recência, como sempre foi."""
    kept = _kept([
        _src_note("2026-08-18T08:00:00", "Médico clínico", 3000),
        _src_note("2026-08-19T08:00:00", "Técnico de enfermagem", 3000),
        _src_note("2026-08-20T08:00:00", "Médico clínico", 3000),
        _src_note("2026-08-21T08:00:00", "Enfermeiro", 3000),
        _src_note("2026-08-21T10:00:00", "Médico residente", 3000),
        _src_note("2026-08-21T22:00:00", "Técnico de enfermagem", 3000),
    ])
    assert kept == ["2026-08-21T08:00:00", "2026-08-21T10:00:00", "2026-08-21T22:00:00"]


def test_big_newest_physician_note_is_kept_with_the_recent_nursing_that_fits():
    """Achado da revisão: com a reserva de 70%, uma médica de 7.000
    caracteres caía e só a enfermagem chegava ao modelo -- marcada como
    base de hoje, o contrário do pedido do auditor."""
    kept = _kept([
        _src_note("2026-08-21T08:00:00", "Médico clínico", 7000),
        _src_note("2026-08-21T14:00:00", "Enfermeiro", 1250),
        _src_note("2026-08-21T20:00:00", "Enfermeiro", 1250),
    ])
    assert kept == ["2026-08-21T08:00:00", "2026-08-21T20:00:00"]


def test_todays_nursing_event_wins_over_a_ten_day_old_physician_note():
    """Achado da revisão: a reserva fixa pras médicas trocava o evento de
    enfermagem de hoje (ex.: dessaturação) por uma médica de 10 dias atrás."""
    kept = _kept([
        _src_note("2026-08-11T08:00:00", "Médico clínico", 2000),
        _src_note("2026-08-12T08:00:00", "Técnico de enfermagem", 400),
        _src_note("2026-08-13T08:00:00", "Técnico de enfermagem", 400),
        _src_note("2026-08-20T08:00:00", "Médico clínico", 2000),
        _src_note("2026-08-21T08:00:00", "Médico clínico", 2000),
        _src_note("2026-08-21T21:00:00", "Enfermeiro", 3200),
    ])
    assert "2026-08-21T21:00:00" in kept and "2026-08-11T08:00:00" not in kept


def test_size_cut_leaves_no_hole_in_a_source_timeline():
    """Quando uma médica não cabe, as médicas mais antigas também ficam de
    fora -- nunca "hoje e anteontem sem ontem"."""
    kept = _kept([
        _src_note("2026-08-19T08:00:00", "Médico clínico", 2000),
        _src_note("2026-08-20T08:00:00", "Médico clínico", 4000),
        _src_note("2026-08-21T08:00:00", "Médico clínico", 4000),
        _src_note("2026-08-21T20:00:00", "Enfermeiro", 2500),
    ])
    assert kept == ["2026-08-21T08:00:00", "2026-08-21T20:00:00"]


def test_physician_note_larger_than_the_whole_budget_is_kept_truncated_not_dropped():
    """Achado da 2ª revisão: a médica mais recente maior que o orçamento
    sumia e a enfermagem virava a "base de hoje" no prompt. Agora vai o
    começo dela, com aviso de corte."""
    from app.orchestrator import TRUNCATION_NOTICE

    notes = [
        _src_note("2026-08-19T08:00:00", "Médico clínico", 20000),
        _src_note("2026-08-20T08:00:00", "Técnico de enfermagem", 50),
    ]
    kept, limited = _limit_to_char_budget(notes, 6200)
    assert limited is True
    assert [n.source_type for n in kept] == ["Médico clínico"]
    assert kept[0].text.endswith(TRUNCATION_NOTICE) and len(kept[0].text) <= 6200


def test_each_source_keeps_its_own_timeline():
    """Achado da 2ª revisão: enfermagem, multiprofissional e "fonte não
    identificada" eram um grupo só -- uma fisioterapia grande que não cabia
    derrubava as anotações de enfermagem de hoje, com orçamento sobrando."""
    kept = _kept([
        _src_note("2026-08-20T19:00:00", "Técnico de enfermagem", 400),
        _src_note("2026-08-21T08:00:00", "Médico clínico", 3000),
        _src_note("2026-08-21T19:00:00", "Técnico de enfermagem", 400),
        _src_note("2026-08-21T21:00:00", "Fisioterapeuta geral", 3500),
    ], budget=6200)
    assert kept == ["2026-08-20T19:00:00", "2026-08-21T08:00:00", "2026-08-21T19:00:00"]


def test_undated_notes_count_as_the_oldest_and_never_block_dated_ones():
    notes = [
        _src_note("2026-08-21T08:00:00", "Médico clínico", 2000),
        _src_note("2026-08-21T20:00:00", "Técnico de enfermagem", 300),
        StructuredNote(timestamp=None, specialty=None, source_type="Evolução", text="u" * 4500),
    ]
    kept, limited = _limit_to_char_budget(notes, 6200)
    assert limited is True
    assert [n.timestamp for n in kept] == ["2026-08-21T08:00:00", "2026-08-21T20:00:00"]


def test_census_location_reaches_the_analysis_engine(repo):
    """Caso 1 do auditor: unidade e leito do censo vão pro prompt como fonte
    de verdade da localização."""
    patient_id = repo.upsert_patient(
        Patient(record_number="77", bed="UTI-03", unit="UTI Geral (12 leitos)", admission_date="18/08/2026")
    )
    seen = []

    class RecordingLLM(StubLLMSequence):
        def analyze_patient(self, previous_state, new_notes, active_pending_items=None, patient_context=None):
            seen.append(patient_context)
            return super().analyze_patient(previous_state, new_notes, active_pending_items, patient_context)

    _run_llm_analysis(repo, patient_id, RecordingLLM([_base_analysis()]), [_note()])
    assert seen == [{"unit": "UTI Geral (12 leitos)", "bed": "UTI-03", "admission_date": "18/08/2026"}]


def test_when_nothing_fits_the_most_recent_physician_note_goes_alone():
    notes = [
        _src_note("2026-08-19T08:00:00", "Médico clínico", 20000),
        _src_note("2026-08-20T08:00:00", "Técnico de enfermagem", 20000),
    ]
    kept, limited = _limit_to_char_budget(notes, 9000)
    assert limited is True
    assert [n.source_type for n in kept] == ["Médico clínico"]


def test_nursing_only_increment_brings_the_latest_physician_note_as_reference(repo):
    """Achado da 2ª revisão do LLM-006: a execução da noite só manda o que é
    novo; se a evolução médica do dia já foi analisada, a anotação de
    enfermagem virava a "base de hoje". Agora a médica mais recente desta
    internação vai junto como referência."""
    from app.extraction.hashing import compute_note_hash

    patient_id = repo.upsert_patient(Patient(record_number="88", bed="05", unit="ENF. MEDICO-CIRURGICA 2", admission_date="18/08/2026"))
    for ts, role, text in (
        ("2026-08-10T08:00:00", "Médico clínico", "evolução de outra internação"),
        ("2026-08-20T09:00:00", "Médico clínico", "pielonefrite em ceftriaxona EV"),
    ):
        repo.add_note(Note(patient_id=patient_id, source_type=role, specialty=None, timestamp=ts, text=text,
                           text_hash=compute_note_hash(ts, role, None, text)))
    seen = []

    class RecordingLLM(StubLLMSequence):
        def analyze_patient(self, previous_state, new_notes, active_pending_items=None, patient_context=None):
            seen.append([(n.timestamp, n.source_type) for n in new_notes])
            return super().analyze_patient(previous_state, new_notes, active_pending_items, patient_context)

    nursing = StructuredNote(timestamp="2026-08-20T22:00:00", specialty=None, source_type="Enfermeiro", text="tranquilo")
    _run_llm_analysis(repo, patient_id, RecordingLLM([_base_analysis()]), [nursing])

    assert seen == [[("2026-08-20T09:00:00", "Médico clínico"), ("2026-08-20T22:00:00", "Enfermeiro")]]
    assert repo.get_patient_state(patient_id)["especialidade_responsavel"] == "Médico clínico"


def test_reference_physician_note_never_comes_from_a_previous_admission(repo):
    from app.extraction.hashing import compute_note_hash

    patient_id = repo.upsert_patient(Patient(record_number="89", bed="06", unit="U", admission_date="18/08/2026"))
    old = ("2026-08-01T08:00:00", "Médico clínico", "internação anterior")
    repo.add_note(Note(patient_id=patient_id, source_type=old[1], specialty=None, timestamp=old[0], text=old[2],
                       text_hash=compute_note_hash(old[0], old[1], None, old[2])))
    seen = []

    class RecordingLLM(StubLLMSequence):
        def analyze_patient(self, previous_state, new_notes, active_pending_items=None, patient_context=None):
            seen.append([n.source_type for n in new_notes])
            return super().analyze_patient(previous_state, new_notes, active_pending_items, patient_context)

    nursing = StructuredNote(timestamp="2026-08-20T22:00:00", specialty=None, source_type="Enfermeiro", text="x")
    _run_llm_analysis(repo, patient_id, RecordingLLM([_base_analysis()]), [nursing])
    assert seen == [["Enfermeiro"]]


# ============================================================== PEND-001
# Ciclo de vida das pendências (pedido do auditor, DEC-129 seções 3 e 4):
# contrato v2 (alvo + decisão por pendência), casamento por (categoria,
# alvo) entre regra e IA, detector na Fase 1. Texto clínico fictício.


class RecordingStubLLM(StubLLMSequence):
    def __init__(self, responses):
        super().__init__(responses)
        self.seen_pending = []

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None, patient_context=None):
        self.seen_pending.append(list(active_pending_items or []))
        return super().analyze_patient(previous_state, new_notes, active_pending_items, patient_context)


def _consult(specialty="infectologia", evidence_date="2026-08-20T08:00:00", **overrides):
    overrides.setdefault("evidence", f"Solicito avaliação da {specialty}.")
    return _pending_item(
        category="INTERCONSULTA", subcategory="SOLICITADA", description=f"Aguarda avaliação da {specialty}",
        evidence_date=evidence_date, **overrides,
    )


def _status_of(repo, pending_id):
    return repo.conn.execute("SELECT * FROM pending_items WHERE pending_id = ?", (pending_id,)).fetchone()


def _add_db_note(repo, patient_id, timestamp, text, role="Médico residente"):
    from app.extraction.hashing import compute_note_hash

    repo.add_note(Note(patient_id=patient_id, source_type=role, specialty=None, timestamp=timestamp, text=text,
                       text_hash=compute_note_hash(timestamp, role, None, text)))


def _one_open_consult(repo, patient_id):
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[_consult()])]), [_note()])
    [row] = repo.get_active_pending_items(patient_id)
    return row


def test_prompt_lists_registered_items_with_codes_target_and_status(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    llm = RecordingStubLLM([_base_analysis()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    [[item]] = llm.seen_pending
    assert item["ref"] == "P1"
    assert item["alvo"] == "INFECTOLOGIA"
    assert item["status"] == "ACTIVE"
    assert row["alvo"] == "INFECTOLOGIA"


def test_two_consults_of_different_specialties_are_two_items(repo):
    """Causa nº 1 da queixa do auditor (DEC-129, achado 1): interconsulta da
    IA casava só pela CATEGORIA -- infecto e reumato viravam uma só."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_consult("infectologia")]),
        _base_analysis(pending_items=[_consult("reumatologia", evidence_date="2026-08-21T08:00:00")]),
    ])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    assert {r["alvo"] for r in repo.get_active_pending_items(patient_id)} == {"INFECTOLOGIA", "REUMATOLOGIA"}


def test_decision_concluida_with_verified_evidence_resolves_with_source_and_evidence(repo):
    """A Fase 1 grava as evoluções antes da IA; a decisão só vale se a
    evolução citada estiver no banco, for posterior ao pedido e o detector
    concordar (aqui, a linha "PELA INFECTOLOGIA"). Concordando, quem encerra
    é o detector, com a evidência dele (2ª revisão: a mesma decisão nos dois
    caminhos, inclusive a checagem de conflito)."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    text = "PELA INFECTOLOGIA\nManter antibiótico por 7 dias."
    _add_db_note(repo, patient_id, "2026-08-21T10:00:00", text)
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{
        "ref": "P1", "decisao": "CONCLUIDA", "evidence": "Manter antibiótico por 7 dias.",
        "evidence_date": "30/12/2026 10:00",  # data da IA é ignorada: vale a da evolução citada
    }])])
    _run_llm_analysis(repo, patient_id, llm, [_note(text, timestamp="2026-08-21T10:00:00")])

    after = _status_of(repo, row["pending_id"])
    assert after["status"] == "RESOLVED"
    assert after["resolution_source"] == "DETECTOR"
    assert after["resolution_evidence"] == "PELA INFECTOLOGIA Manter antibiótico por 7 dias."
    assert after["resolution_evidence_date"] == "2026-08-21T10:00:00"
    assert after["resolved_at"] is not None


@pytest.mark.parametrize("quote,note_text", [
    # Revisão adversarial: a IA "concluía" com a frase de espera, com o
    # próprio pedido, ou com a resposta de outra especialidade.
    ("Segue aguardando avaliação da infectologia, ainda não respondida.",
     "Segue aguardando avaliação da infectologia, ainda não respondida."),
    ("Solicito avaliação da infectologia.", "Solicito avaliação da infectologia."),
    ("Nefrologia avaliou: sem indicação de diálise.", "PELA NEFROLOGIA\nNefrologia avaliou: sem indicação de diálise."),
])
def test_ai_conclusion_that_does_not_prove_the_item_goes_to_review(repo, quote, note_text):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    _add_db_note(repo, patient_id, "2026-08-21T10:00:00", note_text)
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{"ref": "P1", "decisao": "CONCLUIDA", "evidence": quote}])])
    _run_llm_analysis(repo, patient_id, llm, [_note(note_text, timestamp="2026-08-21T10:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_ai_conclusion_quoting_a_note_older_than_the_request_goes_to_review(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    _add_db_note(repo, patient_id, "2026-08-18T10:00:00", "PELA INFECTOLOGIA\nSem antibiótico no momento.")
    row = _one_open_consult(repo, patient_id)  # pedido de 20/08
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{
        "ref": "P1", "decisao": "CONCLUIDA", "evidence": "Sem antibiótico no momento."}])])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T10:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_ai_conclusion_of_a_kind_the_detector_does_not_know(repo):
    """Terapêutica/transferência: evolução posterior ao pedido, trecho que
    não é de espera."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    item = _pending_item(category="TRANSFERENCIA", subcategory="VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA",
                         description="Aguarda vaga em enfermaria", evidence="Aguarda vaga em enfermaria.")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]), [_note()])
    [row] = repo.get_active_pending_items(patient_id)
    _add_db_note(repo, patient_id, "2026-08-22T10:00:00", "Transferido para a enfermaria hoje às 10h.")
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{
        "ref": "P1", "decisao": "CONCLUIDA", "evidence": "Transferido para a enfermaria hoje às 10h."}])])
    _run_llm_analysis(repo, patient_id, llm, [_note("Transferido para a enfermaria hoje às 10h.", timestamp="2026-08-22T10:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "RESOLVED"


def test_decision_concluida_without_evidence_goes_to_review_not_closed(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{"ref": "P1", "decisao": "CONCLUIDA"}])])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T10:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_caso6_uncertain_goes_to_human_review_and_stays_visible(repo):
    """Caso 6 de aceitação (parte automática; a marcação manual é o
    PEND-002): sem evidência suficiente de conclusão, o item vai pra
    verificação humana -- continua aberto, no relatório e no censo."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{
        "ref": "p1", "decisao": "incerta", "evidence": None, "evidence_date": None,
    }])])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T10:00:00")])

    [open_row] = repo.get_active_pending_items(patient_id)
    assert (open_row["pending_id"], open_row["status"]) == (row["pending_id"], "IN_REVIEW")
    assert [r["pending_id"] for r in repo.get_all_active_pending_items()] == [row["pending_id"]]


def test_ai_confirming_again_reactivates_what_it_put_in_review(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    llm = StubLLMSequence([
        _base_analysis(revisao_pendencias=[{"ref": "P1", "decisao": "INCERTA"}]),
        _base_analysis(revisao_pendencias=[{
            "ref": "P1", "decisao": "MANTIDA", "evidence": "Segue aguardando infectologia.",
            "evidence_date": "2026-08-22T08:00:00",
        }]),
    ])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])
    _run_llm_analysis(repo, patient_id, llm, [_note("Segue aguardando infectologia.", timestamp="2026-08-22T08:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "ACTIVE"
    [extra] = repo.get_pending_item_evidence(row["pending_id"])
    assert extra["text"] == "Segue aguardando infectologia."


def test_ai_does_not_reactivate_what_the_detector_put_in_review(repo):
    """Senão alternaria a cada execução (detector põe, IA tira)."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    exam = _pending_item(description="Aguarda tomografia de crânio", evidence="Solicitada TC de crânio.")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[exam])]), [_note()])
    [row] = repo.get_active_pending_items(patient_id)
    repo.set_pending_in_review(row["pending_id"], actor="DETECTOR", note="exame realizado, resultado não localizado")

    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{"ref": "P1", "decisao": "MANTIDA"}])])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_decision_cancelada(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    text = "Suspenso o parecer da infectologia por melhora clínica."
    _add_db_note(repo, patient_id, "2026-08-21T10:00:00", text, role="Médico clínico")
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{
        "ref": "P1", "decisao": "CANCELADA", "evidence": text, "evidence_date": None,
    }])])
    _run_llm_analysis(repo, patient_id, llm, [_note(text, timestamp="2026-08-21T10:00:00")])

    after = _status_of(repo, row["pending_id"])
    assert (after["status"], after["resolution_source"]) == ("CANCELLED", "DETECTOR")


def test_unknown_or_repeated_codes_are_ignored(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[
        {"ref": "P7", "decisao": "CONCLUIDA", "evidence": "x"},
        {"ref": "P1", "decisao": "MANTIDA"},
        {"ref": "P1", "decisao": "CONCLUIDA", "evidence": "segunda decisão pro mesmo código"},
        "lixo",
    ])])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T10:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "ACTIVE"


def test_ai_decides_rule_items_too(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_EXAM_NOTE))
    [rule_row] = repo.get_active_pending_items(patient_id)

    _add_db_note(repo, patient_id, "2026-08-21T09:00:00", "TC de tórax normal.", role="Médico clínico")
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{
        "ref": "P1", "decisao": "CONCLUIDA", "evidence": "TC de tórax normal.", "evidence_date": "2026-08-21T09:00:00",
    }])])
    _run_llm_analysis(repo, patient_id, llm, [_note("TC de tórax normal.", timestamp="2026-08-21T09:00:00")])

    assert _status_of(repo, rule_row["pending_id"])["status"] == "RESOLVED"


def test_llm_item_with_the_same_target_as_a_rule_item_is_not_a_duplicate(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_EXAM_NOTE))
    [rule_row] = repo.get_active_pending_items(patient_id)
    assert rule_row["alvo"] == "TOMOGRAFIA DE TÓRAX"

    llm = StubLLMSequence([_base_analysis(pending_items=[_pending_item(
        description="Aguarda TC de tórax", alvo="tomografia de tórax", evidence="Mantém aguardando TC de tórax.",
        evidence_date="2026-08-21T08:00:00",
    )])])
    _run_llm_analysis(repo, patient_id, llm, [_note("Mantém aguardando TC de tórax.", timestamp="2026-08-21T08:00:00")])

    [row] = repo.get_active_pending_items(patient_id)
    assert row["pending_id"] == rule_row["pending_id"]
    assert [e["text"] for e in repo.get_pending_item_evidence(row["pending_id"])] == ["Mantém aguardando TC de tórax."]


def test_ai_re_emitting_an_already_answered_request_does_not_reopen_it(repo):
    """A janela de 14 dias ainda tem o pedido original e a IA tende a
    reemiti-lo (DEC-129, achado 1)."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15"))
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", "Solicito avaliação da infectologia.", role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-21T09:00:00", "PELA INFECTOLOGIA\nManter antibiótico.")
    llm = StubLLMSequence([_base_analysis(pending_items=[_consult()])])

    _run_llm_analysis(repo, patient_id, llm, [_note()])  # cria e o detector já fecha
    assert repo.get_active_pending_items(patient_id) == []
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-22T08:00:00")])  # reemite o mesmo pedido

    assert repo.get_active_pending_items(patient_id) == []
    assert len(repo.get_patient_pending_items(patient_id)) == 1


def test_a_new_request_after_the_evaluation_is_a_new_item(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15"))
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", "Solicito avaliação da infectologia.", role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-21T09:00:00", "PELA INFECTOLOGIA\nManter antibiótico.")
    rerequest = "Nova febre, solicito reavaliação da infectologia."
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_consult()]),
        _base_analysis(pending_items=[_consult(evidence=rerequest, evidence_date="2026-08-23T08:00:00")]),
    ])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    assert repo.get_active_pending_items(patient_id) == []  # PELA INFECTOLOGIA fechou
    _add_db_note(repo, patient_id, "2026-08-23T08:00:00", rerequest, role="Médico clínico")
    _run_llm_analysis(repo, patient_id, llm, [_note(rerequest, timestamp="2026-08-23T08:00:00")])

    [row] = repo.get_active_pending_items(patient_id)
    assert row["evidence_date"] == "2026-08-23T08:00:00"


# ------------------------------------------------- PEND-001 na Fase 1

_CONSULT_REQUEST_NOTE = (
    "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nSolicito avaliacao da nefrologia.\n\n"
)
_NEPHRO_NOTE = (
    "21/08/2026 10:00 - Profissional Teste Dois (Medico residente)\nPaciente estavel.\n"
    "PELA NEFROLOGIA\nSem indicacao de dialise.\n\n"
)


def test_phase1_rule_request_and_specialist_note_close_in_the_same_run(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_CONSULT_REQUEST_NOTE + _NEPHRO_NOTE))

    assert repo.get_active_pending_items(patient_id) == []
    [row] = repo.get_patient_pending_items(patient_id)
    assert (row["status"], row["resolution_source"], row["alvo"]) == ("RESOLVED", "DETECTOR", "NEFROLOGIA")
    assert row["resolution_evidence"].startswith("PELA NEFROLOGIA")  # o bloco inteiro, com o conteúdo
    events = [e["event_type"] for e in repo.get_pending_item_events(row["pending_id"])]
    assert events == ["CRIADA", "CONCLUIDA"]


def test_phase1_request_still_in_the_window_does_not_reopen_every_night(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    source = FixedTextRecordSource(_CONSULT_REQUEST_NOTE + _NEPHRO_NOTE)
    _process_patient_rules(repo, patient, patient_id, source)
    _process_patient_rules(repo, patient, patient_id, source)

    assert len(repo.get_patient_pending_items(patient_id)) == 1


def test_phase1_new_request_after_the_evaluation_opens_a_new_item(repo):
    """"Reitero parecer" DEPOIS da avaliação é pedido novo -- o pedido
    original, ainda na janela, não pode escondê-lo."""
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    rerequest = "22/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nReitero solicitacao de parecer da nefrologia.\n\n"
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_CONSULT_REQUEST_NOTE + _NEPHRO_NOTE))
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(
        _CONSULT_REQUEST_NOTE + _NEPHRO_NOTE + rerequest))

    [open_row] = repo.get_active_pending_items(patient_id)
    assert open_row["evidence_date"] == "2026-08-22T08:00:00"
    assert len(repo.get_patient_pending_items(patient_id)) == 2


def test_phase1_old_request_closes_from_history_outside_the_window(repo):
    """O pedido de dias atrás fecha: o detector lê a internação inteira do
    banco, não só a janela que a extração reenviou."""
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_CONSULT_REQUEST_NOTE))
    assert len(repo.get_active_pending_items(patient_id)) == 1

    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_NEPHRO_NOTE))
    assert repo.get_active_pending_items(patient_id) == []


def test_phase1_backfills_target_of_old_items_and_flags_old_rule_consults(repo):
    """Banco anterior ao PEND-001: alvo deduzido da descrição; interconsulta
    de regra antiga sem especialidade ("Aguarda avaliação de quadro") vai pra
    verificação -- nunca teria como ser fechada nem casada."""
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    good = repo.add_pending_item(patient_id, "INTERCONSULTA", "Aguarda avaliação de nefrologia",
                                 "nota inteira antiga", "2026-08-19T08:00:00", subcategory="SOLICITADA", source="RULE")
    bad = repo.add_pending_item(patient_id, "INTERCONSULTA", "Aguarda avaliação de quadro",
                                "nota inteira antiga", "2026-08-19T08:00:00", subcategory="SOLICITADA", source="RULE")

    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(
        "22/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nPaciente estavel.\n\n"))

    assert _status_of(repo, good)["alvo"] == "NEFROLOGIA"
    assert _status_of(repo, good)["status"] == "ACTIVE"
    assert _status_of(repo, bad)["status"] == "IN_REVIEW"


def test_phase1_exam_done_without_result_goes_to_review(repo):
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    done = "21/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nTomografia de torax realizada.\n\n"
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_EXAM_NOTE + done))

    [row] = repo.get_active_pending_items(patient_id)
    assert row["status"] == "IN_REVIEW"
    [event] = [e for e in repo.get_pending_item_events(row["pending_id"]) if e["event_type"] == "EM_VERIFICACAO"]
    assert event["actor"] == "DETECTOR"
    assert "resultado não foi localizado" in event["note"]



def test_conclusion_whose_quote_is_not_in_the_notes_goes_to_review(repo):
    """Avaliação real (PEND-001): a IA copiou como "evidência" a linha da
    lista de pendências do próprio prompt. Nada fecha com trecho que não
    está nas evoluções -- vai pra conferência do auditor."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{
        "ref": "P1", "decisao": "CONCLUIDA",
        "evidence": "Aguarda avaliação da infectologia -- alvo INFECTOLOGIA (desde 2026-08-20T08:00:00)",
    }])])
    _run_llm_analysis(repo, patient_id, llm, [_note("Paciente estável.", timestamp="2026-08-21T10:00:00")])

    after = _status_of(repo, row["pending_id"])
    assert after["status"] == "IN_REVIEW"
    assert after["resolution_evidence"] is None


def test_reiteration_quote_copied_from_the_prompt_is_not_stored_as_evidence(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    row = _one_open_consult(repo, patient_id)
    llm = StubLLMSequence([_base_analysis(pending_items=[_consult(
        evidence="Aguarda avaliação da infectologia -- alvo INFECTOLOGIA (desde 2026-08-20T08:00:00)",
    )])])
    _run_llm_analysis(repo, patient_id, llm, [_note("Paciente estável.", timestamp="2026-08-21T10:00:00")])

    assert repo.get_pending_item_evidence(row["pending_id"]) == []


def test_new_item_whose_own_evidence_shows_it_was_answered_is_dropped(repo):
    """Avaliação real: "Aguardo avaliação da infectologia." com evidência
    "Pela INFECTOLOGIA: paciente avaliado." -- a evidência desmente."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    llm = StubLLMSequence([_base_analysis(pending_items=[_consult(
        evidence="Pela INFECTOLOGIA: paciente avaliado.", evidence_date="2026-08-21T15:00:00",
    )])])
    _run_llm_analysis(repo, patient_id, llm, [_note("Pela INFECTOLOGIA: paciente avaliado.", timestamp="2026-08-21T15:00:00")])

    assert repo.get_patient_pending_items(patient_id) == []



def test_ai_re_emitting_a_closed_item_with_an_unverifiable_quote_is_ignored(repo):
    """Revisão adversarial: a IA reemitia a interconsulta respondida com a
    data de hoje e um trecho que não está em evolução nenhuma -- o item
    voltava e nunca mais fechava."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15"))
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", "Solicito avaliação da infectologia.", role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-21T09:00:00", "PELA INFECTOLOGIA\nManter antibiótico.")
    llm = StubLLMSequence([
        _base_analysis(pending_items=[_consult()]),
        _base_analysis(pending_items=[_consult(evidence="Mantém aguardando parecer da infectologia.",
                                               evidence_date="22/08/2026 08:00")]),
    ])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note("Paciente afebril.", timestamp="2026-08-22T08:00:00")])

    assert repo.get_active_pending_items(patient_id) == []


def test_new_item_date_comes_from_the_quoted_note_not_from_the_ai(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15"))
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", "Solicito avaliação da infectologia.", role="Médico clínico")
    llm = StubLLMSequence([_base_analysis(pending_items=[_consult(evidence_date="05/08/2026")])])
    _run_llm_analysis(repo, patient_id, llm, [_note("Solicito avaliação da infectologia.")])

    [row] = repo.get_active_pending_items(patient_id)
    assert row["evidence_date"] == "2026-08-20T08:00:00"


@pytest.mark.parametrize("bad_date", ["15/03/1958", "31/12/9999", "20/09/3026"])
def test_absurd_ai_dates_never_break_the_analysis(repo, bad_date):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15"))
    llm = StubLLMSequence([_base_analysis(pending_items=[_consult(evidence="Parecer da cardio pedido.",
                                                                  evidence_date=bad_date)])])
    _run_llm_analysis(repo, patient_id, llm, [_note()])

    [row] = repo.get_active_pending_items(patient_id)
    assert row["evidence_date"] == "2026-08-20T08:00:00"  # a evolução que a IA leu, nunca a data absurda


def test_generic_items_with_different_targets_are_not_merged(repo):
    """Revisão adversarial: "traqueostomia" virava evidência da colecistectomia."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    first = _pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="INDICACAO_DEFINIDA_AGUARDANDO_CIRURGIA",
                          description="Aguarda colecistectomia", alvo="colecistectomia", evidence="Indicada colecistectomia.")
    second = _pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="INDICACAO_DEFINIDA_AGUARDANDO_CIRURGIA",
                           description="Aguarda traqueostomia", alvo="traqueostomia", evidence="Indicada traqueostomia.")
    llm = StubLLMSequence([_base_analysis(pending_items=[first]), _base_analysis(pending_items=[second])])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    assert {row["alvo"] for row in repo.get_active_pending_items(patient_id)} == {"COLECISTECTOMIA", "TRAQUEOSTOMIA"}


def test_prompt_lists_at_most_twelve_registered_items_in_review_first(repo):
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    ids = [repo.add_pending_item(patient_id, "TERAPEUTICA", f"Pendência {i}", f"evid {i}", "2026-08-20T08:00:00",
                                 source="LLM") for i in range(15)]
    repo.set_pending_in_review(ids[0], actor="IA")
    llm = RecordingStubLLM([_base_analysis()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    [items] = llm.seen_pending
    assert len(items) == 12
    assert items[0]["description"] == "Pendência 0" and items[0]["status"] == "IN_REVIEW"


def test_rules_or_detector_failure_never_fails_the_patient(repo, monkeypatch):
    import app.orchestrator as orchestrator_module

    def boom(*args, **kwargs):
        raise RuntimeError("defeito simulado no detector")

    monkeypatch.setattr(orchestrator_module.DetectorSession, "detect", boom)
    patient = _patient()
    patient_id = repo.upsert_patient(patient)
    result = _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(_CONSULT_REQUEST_NOTE))
    assert result is not None  # as notas novas seguem pra IA
    llm = StubLLMSequence([_base_analysis()])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    assert repo.get_patient_state(patient_id) is not None


def test_ai_closure_quoting_another_item_s_answer_goes_to_review(repo):
    """Saída REAL do modelo reaplicada (caso G da avaliação): com os códigos
    trocados, "P1 CONCLUIDA" citando a evolução da nefrologia caía na
    pendência de vaga em enfermaria e a fechava. O trecho tem de falar do
    assunto da própria pendência."""
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    item = _pending_item(category="TRANSFERENCIA", subcategory="VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA",
                         description="Aguarda vaga em enfermaria", evidence="Aguarda vaga em enfermaria.")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]), [_note()])
    [row] = repo.get_active_pending_items(patient_id)
    quote = "Avaliado a pedido da equipe. Sem indicação de diálise. Sugiro manter hidratação."
    _add_db_note(repo, patient_id, "2026-08-22T10:00:00", quote, role="Médico nefrologista")
    llm = StubLLMSequence([_base_analysis(revisao_pendencias=[{"ref": "P1", "decisao": "CONCLUIDA", "evidence": quote}])])
    _run_llm_analysis(repo, patient_id, llm, [_note(quote, timestamp="2026-08-22T10:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"
