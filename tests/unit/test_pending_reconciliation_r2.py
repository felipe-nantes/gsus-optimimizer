"""PEND-001: regressões da SEGUNDA revisão adversarial da Fase 3 no
orquestrador -- decisões da IA, pendências novas da IA, regras na Fase 1,
migração e confiabilidade. Texto clínico fictício."""
import logging
import sqlite3

import pytest

from app.analysis.rules import StructuredNote
from app.models import Patient
from app.orchestrator import (
    _PEND001_TOTALS, _apply_rules_and_detector, _backfill_targets, _process_patient_rules, _rollback_quietly,
    _run_llm_analysis,
)
from app.storage import database
from app.storage.repository import Repository
from tests.unit.test_orchestrator import (
    FixedTextRecordSource, StubLLMSequence, _add_db_note, _base_analysis, _consult, _note, _pending_item, _status_of,
)


@pytest.fixture
def repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    yield Repository(conn)
    conn.close()


def _patient_id(repo, admission_date="2026-08-05"):
    return repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U", admission_date=admission_date))


def _decision(ref, verdict, evidence):
    return _base_analysis(revisao_pendencias=[{"ref": ref, "decisao": verdict, "evidence": evidence}])


# ------------------------------------- C24: a IA não fecha o que o detector segurou


def test_c24_ai_conclusion_does_not_close_an_item_held_for_conflicting_information(repo):
    patient_id = _patient_id(repo)
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", "Solicito avaliação da infectologia.", role="Médico clínico")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[_consult()])]),
                      [_note("Solicito avaliação da infectologia.")])
    [row] = repo.get_active_pending_items(patient_id)
    _add_db_note(repo, patient_id, "2026-08-21T10:00:00", "PELA INFECTOLOGIA\nSem indicação de antibiótico no momento.")
    _add_db_note(repo, patient_id, "2026-08-22T08:00:00", "Infectologia ainda não avaliou o paciente.",
                 role="Médico clínico")
    llm = StubLLMSequence([_decision("P1", "CONCLUIDA", "Sem indicação de antibiótico no momento.")])
    _run_llm_analysis(repo, patient_id, llm, [_note("Infectologia ainda não avaliou o paciente.",
                                                    timestamp="2026-08-22T08:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


# --------------------------- C25/C27: categorias que o detector não avalia


@pytest.mark.parametrize("quote,verdict", [
    ("Vaga de UTI negada pela central de regulacao.", "CONCLUIDA"),
    ("Transferencia para UTI adiada por instabilidade hemodinamica.", "CANCELADA"),
])
def test_c25_refusal_or_postponement_quote_goes_to_review(repo, quote, verdict):
    patient_id = _patient_id(repo)
    item = _pending_item(category="TRANSFERENCIA", subcategory="VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA",
                         description="Aguarda vaga de UTI", alvo="vaga UTI", evidence="Aguarda vaga de UTI.")
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", "Aguarda vaga de UTI.", role="Médico clínico")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note("Aguarda vaga de UTI.")])
    [row] = repo.get_active_pending_items(patient_id)
    _add_db_note(repo, patient_id, "2026-08-22T08:00:00", quote, role="Médico clínico")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_decision("P1", verdict, quote)]),
                      [_note(quote, timestamp="2026-08-22T08:00:00")])

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_c27_swapped_code_does_not_close_the_wrong_vacancy(repo):
    patient_id = _patient_id(repo)
    uti = _pending_item(category="TRANSFERENCIA", subcategory="VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA",
                        description="Aguarda vaga de UTI", alvo="vaga UTI", evidence="Aguarda vaga de UTI.")
    ward = _pending_item(category="TRANSFERENCIA", subcategory="VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA",
                         description="Aguarda vaga em enfermaria de retaguarda", alvo="vaga enfermaria retaguarda",
                         evidence="Aguarda vaga em enfermaria de retaguarda.")
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", "Aguarda vaga de UTI. Aguarda vaga em enfermaria de retaguarda.",
                 role="Médico clínico")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[uti, ward])]),
                      [_note("Aguarda vaga de UTI. Aguarda vaga em enfermaria de retaguarda.")])
    quote = "Vaga de UTI liberada pela regulacao, paciente transferido."
    _add_db_note(repo, patient_id, "2026-08-21T08:00:00", quote, role="Médico clínico")
    # O prompt lista as ativas mais recentes primeiro; a decisão vem com o
    # código da outra vaga.
    rows = {row["alvo"]: row for row in repo.get_active_pending_items(patient_id)}
    llm_codes = sorted(rows.values(), key=lambda row: row["created_at"], reverse=True)
    wrong_ref = "P1" if llm_codes[0]["alvo"] != "VAGA UTI" else "P2"
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_decision(wrong_ref, "CONCLUIDA", quote)]),
                      [_note(quote, timestamp="2026-08-21T08:00:00")])

    assert _status_of(repo, rows["VAGA ENFERMARIA RETAGUARDA"]["pending_id"])["status"] != "RESOLVED"


# ------------------------------------------ C28: mesmo texto, episódio novo


def test_c28_same_wording_days_later_is_a_new_request_not_closed_by_the_old_answer(repo):
    patient_id = _patient_id(repo)
    _add_db_note(repo, patient_id, "2026-08-10T08:00:00", "Creatinina 2,1. Aguardamos avaliação da nefrologia.",
                 role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-11T10:00:00", "Paciente avaliado. Sem indicação de diálise.",
                 role="Médico nefrologista")
    request = "Piora da função renal, creatinina 4,0. Aguardamos avaliação da nefrologia."
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", request, role="Médico clínico")
    item = _consult("nefrologia", evidence="Aguardamos avaliação da nefrologia.", alvo="NEFROLOGIA")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(request, timestamp="2026-08-20T08:00:00")])

    [row] = repo.get_active_pending_items(patient_id)
    assert row["evidence_date"] == "2026-08-20T08:00:00"


def test_c28_copy_forward_of_an_answered_request_is_still_recognized(repo):
    patient_id = _patient_id(repo)
    request = "Aguardamos avaliação da nefrologia."
    _add_db_note(repo, patient_id, "2026-08-10T08:00:00", request, role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-11T10:00:00", "Paciente avaliado. Sem indicação de diálise.",
                 role="Médico nefrologista")
    _add_db_note(repo, patient_id, "2026-08-12T08:00:00", request, role="Médico clínico")
    item = _consult("nefrologia", evidence=request, alvo="NEFROLOGIA")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(request, timestamp="2026-08-12T08:00:00")])

    assert repo.get_active_pending_items(patient_id) == []


# ------------------------------- C29: trecho inverificável não volta no tempo


def test_c29_unverifiable_quote_with_the_admission_date_cannot_be_closed_by_older_evidence(repo):
    patient_id = _patient_id(repo, admission_date="2026-08-15")
    _add_db_note(repo, patient_id, "2026-08-17T08:00:00", "PELA NEFROLOGIA\nSem indicacao de dialise no momento.")
    item = _consult("nefrologia", evidence="Aguarda reavaliacao da nefrologia por piora da creatinina.",
                    evidence_date="2026-08-15", alvo="NEFROLOGIA")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note("Piora da creatinina hoje. Acionada nefrologia para reavaliacao.",
                             timestamp="2026-08-25T08:00:00")])

    [row] = repo.get_active_pending_items(patient_id)
    assert row["status"] == "ACTIVE"
    assert row["evidence_date"] >= "2026-08-25"


# ------------------------------ C30: internação anterior não descarta pedido


def test_c30_closure_from_a_previous_admission_does_not_drop_a_new_request(repo):
    patient_id = _patient_id(repo, admission_date="2026-08-15")
    old = repo.add_pending_item(patient_id, "INTERCONSULTA", "Aguarda avaliação da cardiologia", "x",
                                "2026-03-01T08:00:00", subcategory="SOLICITADA", source="LLM", alvo="CARDIOLOGIA")
    repo.resolve_pending_item(old, source="AUDITOR", evidence_date="2026-03-02T08:00:00")
    item = _consult("cardiologia", evidence="Aguarda avaliação da cardiologia por fibrilação atrial.",
                    evidence_date="2026-08-20T08:00:00", alvo="CARDIOLOGIA")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note("Fibrilação atrial de alta resposta hoje.", timestamp="2026-08-20T08:00:00")])

    assert [row["alvo"] for row in repo.get_active_pending_items(patient_id)] == ["CARDIOLOGIA"]


# -------------------------------------- C31/C32/C38: alvo genérico não engole


def test_c31_procedure_without_ai_target_is_not_merged_into_another(repo):
    patient_id = _patient_id(repo)
    tqt = _pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="INDICACAO_DEFINIDA_AGUARDANDO_CIRURGIA",
                        description="Aguarda traqueostomia", alvo="traqueostomia", evidence="Indicada traqueostomia.")
    cole = _pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="INDICACAO_DEFINIDA_AGUARDANDO_CIRURGIA",
                         description="Aguarda colecistectomia", alvo=None, evidence="Indicada colecistectomia.")
    llm = StubLLMSequence([_base_analysis(pending_items=[tqt]), _base_analysis(pending_items=[cole])])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    assert {row["alvo"] for row in repo.get_active_pending_items(patient_id)} == {"TRAQUEOSTOMIA", "COLECISTECTOMIA"}


def test_c31_two_different_surgeries_are_two_items(repo):
    patient_id = _patient_id(repo)
    ortho = _pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="INDICACAO_DEFINIDA_AGUARDANDO_CIRURGIA",
                          description="Aguarda fixação de fêmur", alvo="cirurgia ortopédica", evidence="Fixação de fêmur.")
    vasc = _pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="INDICACAO_DEFINIDA_AGUARDANDO_CIRURGIA",
                         description="Aguarda amputação", alvo="cirurgia vascular", evidence="Amputação indicada.")
    llm = StubLLMSequence([_base_analysis(pending_items=[ortho]), _base_analysis(pending_items=[vasc])])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    assert len(repo.get_active_pending_items(patient_id)) == 2


def test_c38_two_consults_outside_the_table_stay_separate(repo):
    patient_id = _patient_id(repo)
    paliativos = _consult("equipe de cuidados paliativos", alvo="CUIDADOS PALIATIVOS",
                          evidence="Solicito avaliação da equipe de cuidados paliativos.")
    nutrologia = _consult("nutrologia", alvo="NUTROLOGIA", evidence="Solicito avaliação da nutrologia.",
                          evidence_date="2026-08-21T08:00:00")
    llm = StubLLMSequence([_base_analysis(pending_items=[paliativos]), _base_analysis(pending_items=[nutrologia])])
    _run_llm_analysis(repo, patient_id, llm, [_note()])
    _run_llm_analysis(repo, patient_id, llm, [_note(timestamp="2026-08-21T08:00:00")])

    assert {row["alvo"] for row in repo.get_active_pending_items(patient_id)} == {"CUIDADOS PALIATIVOS", "NUTROLOGIA"}


# --------------------------------- C34: cópia do pedido já atendido (regras)


def test_c34_copy_forward_of_an_answered_rule_request_is_not_re_created(repo):
    patient = Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15")
    patient_id = repo.upsert_patient(patient)
    request = "HD: pneumonia. Solicito avaliacao da nefrologia."
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", request, role="Médico clínico")
    window = [StructuredNote("2026-08-20T08:00:00", None, "Médico clínico", request)]
    _apply_rules_and_detector(repo, patient, patient_id, window, "2026-08-14", None)
    _add_db_note(repo, patient_id, "2026-08-21T08:00:00", "PELA NEFROLOGIA\nSem indicacao de dialise.")
    _apply_rules_and_detector(repo, patient, patient_id, window, "2026-08-14", None)
    assert repo.get_active_pending_items(patient_id) == []

    _add_db_note(repo, patient_id, "2026-08-22T08:00:00", request, role="Médico clínico")  # cópia da evolução
    window.append(StructuredNote("2026-08-22T08:00:00", None, "Médico clínico", request))
    _apply_rules_and_detector(repo, patient, patient_id, window, "2026-08-14", None)

    assert repo.get_active_pending_items(patient_id) == []
    assert len(repo.get_patient_pending_items(patient_id)) == 1


# ---------------------------- C36: regra antiga sem região não duplica


def test_c36_legacy_region_less_rule_exam_is_matched_by_the_new_rule(repo):
    patient = Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15")
    patient_id = repo.upsert_patient(patient)
    repo.add_pending_item(patient_id, "DIAGNOSTICO", "Aguarda realização de tomografia",
                          "Dor abdominal. Solicitada TC de abdome. Plano: TC de crânio se piora.",
                          "2026-08-20T08:00:00", subcategory="AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL", source="RULE")
    window = [StructuredNote("2026-08-20T08:00:00", None, "Médico clínico", "Dor abdominal. Solicitada TC de abdome.")]
    _apply_rules_and_detector(repo, patient, patient_id, window, "2026-08-14", None)

    assert len(repo.get_active_pending_items(patient_id)) == 1


# --------------------------------------------- C39: punção já feita (IA)


def test_c39_ai_item_quoting_the_puncture_already_done_is_not_created(repo):
    patient_id = _patient_id(repo)
    text = "Equipe de puncao: AVP puncionado com sucesso em MSE."
    _add_db_note(repo, patient_id, "2026-08-20T16:00:00", text, role="Enfermeiro")
    item = _pending_item(category="INTERCONSULTA", subcategory="SOLICITADA",
                         description="Aguarda avaliação da equipe de punção venosa", evidence=text,
                         evidence_date="2026-08-20T16:00:00")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(text, timestamp="2026-08-20T16:00:00")])

    assert repo.get_patient_pending_items(patient_id) == []


# ---------------------------------------------------- C43: totais no log


def test_c43_migration_and_ai_decisions_are_counted(repo, caplog):
    patient_id = _patient_id(repo)
    repo.add_pending_item(patient_id, "INTERCONSULTA", "Aguarda avaliação de quadro", "x", "2026-08-20T08:00:00",
                          subcategory="SOLICITADA", source="RULE")
    repo.add_pending_item(patient_id, "INTERCONSULTA", "Aguarda avaliação da nefrologia", "x", "2026-08-20T08:00:00",
                          subcategory="SOLICITADA", source="RULE")
    _PEND001_TOTALS.reset()
    with caplog.at_level(logging.INFO, logger="app.orchestrator"):
        _backfill_targets(repo, patient_id)
    assert "1 alvo(s) preenchido(s), 1 interconsulta(s) antiga(s) em verificação" in caplog.text
    assert _PEND001_TOTALS.snapshot() == {"alvos_preenchidos": 1, "migracao_em_verificacao": 1}


# ---------------------------------------------- P1: transação presa


def test_p1_rollback_after_a_swallowed_failure_frees_the_connection(tmp_path):
    db = tmp_path / "auditoria.db"
    conn = database.init_db(db)
    repo = Repository(conn)
    patient_id = repo.upsert_patient(Patient(record_number="1", bed="2A", unit="U"))
    repo.add_pending_item(patient_id, "TERAPEUTICA", "x", "x", None, source="LLM")
    conn.execute("UPDATE pending_items SET priority = 'ALTA'")  # transação implícita aberta
    assert conn.in_transaction
    _rollback_quietly(repo)
    assert not conn.in_transaction
    other = sqlite3.connect(db, timeout=0.1)
    other.execute("UPDATE pending_items SET priority = 'BAIXA'")  # não fica travado
    other.commit()
    other.close()
    conn.close()


def test_p1_rules_failure_does_not_leave_a_transaction_open(repo, monkeypatch):
    patient = Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15")
    patient_id = repo.upsert_patient(patient)

    def broken(*_args, **_kwargs):
        repo.conn.execute("UPDATE patients SET bed = bed")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("app.orchestrator._apply_rules_and_detector", broken)
    _process_patient_rules(repo, patient, patient_id, FixedTextRecordSource(
        "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nSolicito avaliacao da nefrologia.\n\n"))
    assert not repo.conn.in_transaction
