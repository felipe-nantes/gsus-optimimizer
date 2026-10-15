"""PEND-001: regressões da QUARTA revisão adversarial da Fase 3 no
orquestrador -- fechamento pela IA com prova anterior a um pedido mais novo,
pedido repetido depois de uma cópia, lista de pedidos copiada e ações
diferentes do mesmo remédio. Texto clínico fictício."""
import pytest

from app.analysis.rules import StructuredNote
from app.models import Patient
from app.orchestrator import _apply_rules_and_detector, _generic_alvos_compatible, _run_llm_analysis
from app.storage import database
from app.storage.repository import Repository
from tests.unit.test_orchestrator import StubLLMSequence, _add_db_note, _base_analysis, _consult, _note, _pending_item, _status_of
from tests.unit.test_pending_reconciliation_r3 import _ai_decides, _create_from_ai, _decision, _nightly, _patient, _vacancy


@pytest.fixture
def repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    yield Repository(conn)
    conn.close()


# ------------------ R4-ORCH-02: prova anterior ao pedido mais novo registrado


def test_r4_orch_02_proof_older_than_an_absorbed_newer_request_does_not_close(repo):
    _patient_obj, patient_id = _patient(repo)
    row = _create_from_ai(repo, patient_id, _vacancy(evidence="Solicitada vaga de UTI na central de regulacao.",
                                                     evidence_date="2026-08-08T08:00:00"),
                          "Solicitada vaga de UTI na central de regulacao.", "2026-08-08T08:00:00")
    _add_db_note(repo, patient_id, "2026-08-09T08:00:00", "Vaga de UTI liberada pela regulacao.", role="Médico clínico")
    new_request = "Nova solicitacao de vaga de UTI feita hoje a regulacao."
    text = f"Transporte suspenso por instabilidade, vaga perdida. {new_request}"
    _add_db_note(repo, patient_id, "2026-08-10T08:00:00", text, role="Médico clínico")
    again = _vacancy(evidence=new_request, evidence_date="2026-08-10T08:00:00")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[again])]),
                      [_note(text, timestamp="2026-08-10T08:00:00")])
    assert repo.get_pending_item_evidence(row["pending_id"])  # o pedido novo foi absorvido
    _decide_with_proof_in_window(repo, patient_id, "Vaga de UTI liberada pela regulacao.", "Paciente estavel.",
                                 "2026-08-11T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def _decide_with_proof_in_window(repo, patient_id, proof, later_text, later_iso):
    """A IA lê a prova (09/08) e a evolução nova: só assim o trecho dela está
    nas evoluções enviadas e a checagem de conflito/referência é exercitada (5ª
    revisão: sem a prova na janela, a decisão caía antes, por outro motivo)."""
    _add_db_note(repo, patient_id, later_iso, later_text, role="Médico clínico")
    window = [_note(proof, timestamp="2026-08-09T08:00:00"), _note(later_text, timestamp=later_iso)]
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_decision("P1", "CONCLUIDA", proof)]), window)


@pytest.mark.parametrize("later", [
    "Vaga de UTI cancelada pela regulacao por falta de leito. Segue aguardando vaga de UTI.",
    "Vaga de UTI cancelada pela regulacao. Segue aguardando leito de UTI.",
    "Regulacao cancelou a vaga. Aguarda transferencia para UTI.",
    "Vaga de UTI negada pela regulacao. Paciente segue na enfermaria.",
    "Transporte suspenso por instabilidade, vaga perdida.",
])
def test_r4_orch_02_later_wait_or_loss_blocks_the_ai_closure(repo, later):
    _patient_obj, patient_id = _patient(repo)
    row = _create_from_ai(repo, patient_id, _vacancy(evidence_date="2026-08-08T08:00:00"), "Aguarda vaga de UTI.",
                          "2026-08-08T08:00:00")
    _add_db_note(repo, patient_id, "2026-08-09T08:00:00", "Vaga de UTI liberada pela regulacao.", role="Médico clínico")
    _decide_with_proof_in_window(repo, patient_id, "Vaga de UTI liberada pela regulacao.", later, "2026-08-12T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_r4_orch_02_control_the_proof_in_the_window_still_closes(repo):
    _patient_obj, patient_id = _patient(repo)
    row = _create_from_ai(repo, patient_id, _vacancy(evidence_date="2026-08-08T08:00:00"), "Aguarda vaga de UTI.",
                          "2026-08-08T08:00:00")
    _add_db_note(repo, patient_id, "2026-08-09T08:00:00", "Vaga de UTI liberada pela regulacao.", role="Médico clínico")
    _decide_with_proof_in_window(repo, patient_id, "Vaga de UTI liberada pela regulacao.", "Paciente transferido.",
                                 "2026-08-12T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "RESOLVED"


# ------------------- R4-ORCH-04: cópia no dia seguinte não esconde o pedido novo


def test_r4_orch_04_copy_the_day_after_does_not_hide_a_later_re_request(repo):
    patient, patient_id = _patient(repo)
    request = "Solicito parecer da cardiologia."
    _nightly(repo, patient, patient_id, [
        ("2026-08-10T08:00:00", "Médico clínico", f"Dispneia, sopro novo. {request}"),
        ("2026-08-10T16:00:00", "Médico residente",
         "PELA CARDIOLOGIA\nAvaliado. Sem cardiopatia estrutural. Sem conduta cardiologica."),
        ("2026-08-11T08:00:00", "Médico clínico", f"Dispneia, sopro novo. {request}"),
        ("2026-08-12T08:00:00", "Médico clínico", "Melhora da dispneia. Sem queixas."),
        ("2026-08-13T08:00:00", "Médico clínico", f"FA de alta resposta desde a madrugada. {request}"),
    ], ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"])

    [row] = repo.get_active_pending_items(patient_id)
    assert (row["alvo"], row["evidence_date"]) == ("CARDIOLOGIA", "2026-08-13T08:00:00")


def test_r4_orch_04_ai_item_is_dated_by_the_new_request_not_the_old_answer(repo):
    _patient_obj, patient_id = _patient(repo)
    phrase = "Discutido com a cardiologia, que vira avaliar o paciente."
    _add_db_note(repo, patient_id, "2026-08-10T08:00:00", f"Sopro novo. {phrase}", role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-10T16:00:00",
                 "PELA CARDIOLOGIA\nAvaliado. Sem cardiopatia estrutural. Sem conduta cardiologica.")
    _add_db_note(repo, patient_id, "2026-08-11T08:00:00", f"Sopro novo. {phrase}", role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-12T08:00:00", "Melhora da dispneia.", role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-13T08:00:00", f"FA de alta resposta. {phrase}", role="Médico clínico")
    old = repo.add_pending_item(patient_id, "INTERCONSULTA", "Aguarda avaliação da cardiologia", phrase,
                                "2026-08-10T08:00:00", subcategory="SOLICITADA", source="LLM", alvo="CARDIOLOGIA")
    repo.resolve_pending_item(old, source="DETECTOR", evidence_date="2026-08-10T16:00:00")
    item = _consult("cardiologia", evidence=phrase, alvo="CARDIOLOGIA", evidence_date="2026-08-13T08:00:00")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(f"FA de alta resposta. {phrase}", timestamp="2026-08-13T08:00:00")])

    [row] = repo.get_active_pending_items(patient_id)
    assert (row["status"], row["evidence_date"]) == ("ACTIVE", "2026-08-13T08:00:00")


# ---------------- R4-RULES-07/NOI-1: lista "Exames solicitados:" copiada


def test_r4_rules_07_copied_request_list_does_not_re_create_a_done_exam(repo):
    patient, patient_id = _patient(repo, admission_date="2026-08-10")
    listing = "Exames solicitados:\n- TC de crânio"
    _nightly(repo, patient, patient_id, [
        ("2026-08-20T09:00:00", "Médico clínico", f"Cefaleia súbita.\n{listing}"),
        ("2026-08-21T09:00:00", "Médico clínico", f"TC de crânio: sem alterações agudas.\n\n{listing}"),
        ("2026-08-22T09:00:00", "Médico clínico", f"TC de crânio: sem alterações agudas.\n\n{listing}"),
    ], ["2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])

    assert [row["status"] for row in repo.get_patient_pending_items(patient_id)] == ["RESOLVED"]


def test_r4_rules_07_copied_consult_list_does_not_re_create_an_answered_consult(repo):
    patient, patient_id = _patient(repo, admission_date="2026-08-10")
    listing = "Solicito:\n- Parecer da nefrologia"
    _nightly(repo, patient, patient_id, [
        ("2026-08-20T09:00:00", "Médico clínico", f"IRA oligúrica.\n{listing}"),
        ("2026-08-20T16:00:00", "Médico nefrologista", "Paciente avaliado. Sem indicação de diálise."),
        ("2026-08-21T09:00:00", "Médico clínico", f"IRA oligúrica.\n{listing}"),
        ("2026-08-22T09:00:00", "Médico clínico", f"IRA oligúrica.\n{listing}"),
    ], ["2026-08-20", "2026-08-21", "2026-08-22"])

    assert [row["status"] for row in repo.get_patient_pending_items(patient_id)] == ["RESOLVED"]


# --------------------------- R4-ORCH-05: ação diferente é outra pendência


@pytest.mark.parametrize("a,b", [
    ("LIBERACAO VANCOMICINA", "SUSPENSAO VANCOMICINA"), ("INICIO HEMODIALISE", "SUSPENSAO HEMODIALISE"),
    ("INICIO NUTRICAO ENTERAL", "SUSPENSAO NUTRICAO ENTERAL"), ("VANCOMICINA", "SUSPENSAO VANCOMICINA"),
])
def test_r4_orch_05_different_actions_are_different_items(a, b):
    assert not _generic_alvos_compatible(a, b)


def test_r4_orch_05_release_and_stop_of_the_same_drug_stay_two_items(repo):
    _patient_obj, patient_id = _patient(repo)
    release = _pending_item(category="TERAPEUTICA", subcategory="ANTIBIOTICO_OU_ESQUEMA_IV",
                            description="Aguarda liberação de vancomicina pela CCIH", alvo=None,
                            evidence="Aguarda liberacao de vancomicina pela CCIH.", evidence_date="2026-08-10T08:00:00")
    _create_from_ai(repo, patient_id, release, "Aguarda liberacao de vancomicina pela CCIH.", "2026-08-10T08:00:00")
    stop_text = "Programada suspensao de vancomicina apos vancocinemia de amanha."
    _add_db_note(repo, patient_id, "2026-08-15T08:00:00", stop_text, role="Médico clínico")
    stop = _pending_item(category="TERAPEUTICA", subcategory="ANTIBIOTICO_OU_ESQUEMA_IV",
                         description="Aguarda suspensão de vancomicina", alvo=None, evidence=stop_text,
                         evidence_date="2026-08-15T08:00:00")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[stop])]),
                      [_note(stop_text, timestamp="2026-08-15T08:00:00")])

    assert len(repo.get_active_pending_items(patient_id)) == 2


# -------------------- REL-1: data impossível não derruba o detector do paciente


def test_rel1_impossible_date_in_a_later_note_does_not_disable_the_detector(repo):
    patient, patient_id = _patient(repo)
    _nightly(repo, patient, patient_id, [
        ("2026-08-10T08:00:00", "Médico clínico", "Solicito parecer da cardiologia. Solicito TC de crânio."),
        ("2026-08-11T08:00:00", "Médico cardiologista", "Paciente avaliado. Sugiro manter amiodarona."),
        ("2026-08-11T10:00:00", "Médico clínico", "TC de crânio: sem sinais de sangramento."),
        ("2026-08-12T08:00:00", "Enfermeiro", "Paciente sem queixas. Cartao SUS validade 12/9999."),
    ], ["2026-08-10", "2026-08-12"])

    assert {row["status"] for row in repo.get_patient_pending_items(patient_id)} == {"RESOLVED"}


def test_rel1_impossible_date_does_not_drop_an_ai_puncture_request(repo):
    _patient_obj, patient_id = _patient(repo)
    text = "Aguarda passagem de PICC pela equipe de puncao. Lote do curativo 06/0000."
    _add_db_note(repo, patient_id, "2026-08-10T20:00:00", text, role="Enfermeiro")
    item = _pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="OUTRO", description="Aguarda passagem de PICC",
                         evidence="Aguarda passagem de PICC pela equipe de puncao.", evidence_date="2026-08-10T20:00:00")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(text, timestamp="2026-08-10T20:00:00")])

    assert [row["status"] for row in repo.get_active_pending_items(patient_id)] == ["ACTIVE"]
