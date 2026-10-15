"""PEND-001: regressões da TERCEIRA revisão adversarial da Fase 3 no
orquestrador -- fechamento pela IA em categorias genéricas, datas de
pendência nova, internação anterior, casamento por alvo e pedido repetido.
Cada cenário foi reproduzido por um revisor e confirmado por um verificador.
Texto clínico fictício."""
import pytest

from app.analysis import specialty_evaluation as se
from app.analysis.rules import StructuredNote
from app.analysis.specialty_evaluation import DetectorNote
from app.models import Patient
from app.orchestrator import _apply_rules_and_detector, _generic_alvos_compatible, _run_llm_analysis
from app.storage import database
from app.storage.repository import Repository
from tests.unit.test_orchestrator import (
    StubLLMSequence, _add_db_note, _base_analysis, _consult, _note, _pending_item, _status_of,
)

BOIL = ("Paciente em REG, lucido e orientado, eupneico em ar ambiente, corado, hidratado, anicterico, acianotico, "
        "afebril, diurese presente")


@pytest.fixture
def repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    yield Repository(conn)
    conn.close()


def _patient(repo, admission_date="2026-08-01"):
    patient = Patient(record_number="1", bed="2A", unit="U", admission_date=admission_date)
    return patient, repo.upsert_patient(patient)


def _decision(ref, verdict, evidence):
    return _base_analysis(revisao_pendencias=[{"ref": ref, "decisao": verdict, "evidence": evidence}])


def _vacancy(description="Aguarda vaga de UTI", alvo="vaga UTI", evidence="Aguarda vaga de UTI.", **overrides):
    return _pending_item(category="TRANSFERENCIA", subcategory="VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA",
                         description=description, alvo=alvo, evidence=evidence, **overrides)


def _therapy(description, alvo, evidence, subcategory="OUTRO", **overrides):
    return _pending_item(category="TERAPEUTICA", subcategory=subcategory, description=description, alvo=alvo,
                         evidence=evidence, **overrides)


def _create_from_ai(repo, patient_id, item, text, timestamp):
    _add_db_note(repo, patient_id, timestamp, text, role="Médico clínico")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(text, timestamp=timestamp)])
    [row] = repo.get_active_pending_items(patient_id)
    return row


def _ai_decides(repo, patient_id, verdict, quote, text, timestamp):
    _add_db_note(repo, patient_id, timestamp, text, role="Médico clínico")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_decision("P1", verdict, quote)]),
                      [_note(text, timestamp=timestamp)])


def _nightly(repo, patient, patient_id, notes, nights):
    """Fase 1 de cada noite com janela de 2 dias, como na produção. `notes`:
    (timestamp, papel, texto); `nights`: datas ISO das noites."""
    for timestamp, role, text in notes:
        _add_db_note(repo, patient_id, timestamp, text, role=role)
    for night in nights:
        window = [StructuredNote(ts, None, role, text) for ts, role, text in notes
                  if night >= ts[:10] >= _day_before(night)]
        _apply_rules_and_detector(repo, patient, patient_id, window, _day_before(patient.admission_date), None)


def _day_before(iso_day):
    from datetime import date, timedelta
    return (date.fromisoformat(iso_day[:10]) - timedelta(days=1)).isoformat()


# ------------------------------------------ R31: trecho inteiro e data certa


def test_r31_closure_quote_with_an_invented_ending_goes_to_review(repo):
    _patient_obj, patient_id = _patient(repo)
    row = _create_from_ai(repo, patient_id, _vacancy(evidence_date="2026-08-10T08:00:00"),
                          "Aguarda vaga de UTI.", "2026-08-10T08:00:00")
    _ai_decides(repo, patient_id, "CONCLUIDA", BOIL + ", transferido para a UTI as 10h.",
                BOIL + ". Mantem piora respiratoria. Aguarda vaga de UTI.", "2026-08-11T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_r31_new_item_is_dated_by_the_whole_quote_not_by_its_boilerplate_prefix(repo):
    _patient_obj, patient_id = _patient(repo)
    for day in range(2, 10):
        _add_db_note(repo, patient_id, f"2026-08-{day:02d}T08:00:00", BOIL + ". Sem queixas.", role="Médico clínico")
    _add_db_note(repo, patient_id, "2026-08-04T15:00:00",
                 "PELA CARDIOLOGIA\nAvaliado por dor toracica atipica. Sem sindrome coronariana aguda. "
                 "Sem conduta cardiologica.")
    request = BOIL + ", sopro sistolico novo em foco mitral, acionada a cardiologia para avaliacao."
    _add_db_note(repo, patient_id, "2026-08-10T08:00:00", request, role="Médico clínico")
    item = _consult("cardiologia", evidence=request + ".", evidence_date="2026-08-10T08:00:00", alvo="CARDIOLOGIA")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(request, timestamp="2026-08-10T08:00:00")])

    [row] = repo.get_active_pending_items(patient_id)
    assert (row["status"], row["evidence_date"]) == ("ACTIVE", "2026-08-10T08:00:00")


# ------------------------------- R32: histórico copiado não fecha pendência


@pytest.mark.parametrize("item,history,verdict", [
    (_vacancy(), "Paciente recebido da UTI em 05/08 apos estabilizacao.", "CONCLUIDA"),
    (_vacancy(), "Paciente recebido da UTI apos estabilizacao.", "CONCLUIDA"),
    (_vacancy("Aguarda vaga em enfermaria", "vaga enfermaria", "Aguarda vaga em enfermaria."),
     "Transferido da enfermaria para a UTI em 03/08 por choque septico.", "CONCLUIDA"),
    (_therapy("Aguarda início de hemodiálise", "hemodialise", "Indicada hemodialise, aguarda inicio."),
     "Hemodialise suspensa em 04/08 por melhora clinica e recuperacao da diurese.", "CANCELADA"),
    (_therapy("Aguarda liberação de meropenem pela CCIH", "meropenem", "Solicitado meropenem, aguarda liberacao da CCIH.",
              subcategory="ANTIBIOTICO_OU_ESQUEMA_IV"),
     "Meropenem liberado pela CCIH em 02/08 e concluido em 08/08.", "CONCLUIDA"),
])
def test_r32_history_line_copied_from_before_the_request_never_closes(repo, item, history, verdict):
    _patient_obj, patient_id = _patient(repo)
    request = item["evidence"]
    row = _create_from_ai(repo, patient_id, {**item, "evidence_date": "2026-08-10T08:00:00"},
                          f"{history} Piora do padrao respiratorio. {request}", "2026-08-10T08:00:00")
    _ai_decides(repo, patient_id, verdict, history, f"{history} Mantem piora. {request}", "2026-08-11T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


# ------------------------------------- R33: uma palavra em comum não basta


@pytest.mark.parametrize("quote", [
    "Hemodialise realizada na UTI sem intercorrencias.",
    "Paciente admitido na UTI em 2024 por pneumonia.",
    "Familia recebida na UTI para boletim medico.",
])
def test_r33_done_quote_sharing_one_word_with_the_only_open_item_goes_to_review(repo, quote):
    _patient_obj, patient_id = _patient(repo)
    row = _create_from_ai(repo, patient_id, _vacancy(evidence_date="2026-08-10T08:00:00"),
                          "Aguarda vaga de UTI.", "2026-08-10T08:00:00")
    _ai_decides(repo, patient_id, "CONCLUIDA", quote, f"{quote} Segue em enfermaria. Aguarda vaga de UTI.",
                "2026-08-11T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_r33_catheter_placement_is_not_the_start_of_dialysis(repo):
    _patient_obj, patient_id = _patient(repo)
    row = _create_from_ai(repo, patient_id,
                          _therapy("Aguarda início de hemodiálise", "inicio hemodialise", "Aguarda inicio de hemodialise.",
                                   evidence_date="2026-08-10T08:00:00"),
                          "Aguarda inicio de hemodialise.", "2026-08-10T08:00:00")
    quote = "Cateter de hemodialise passado sem intercorrencias."
    _ai_decides(repo, patient_id, "CONCLUIDA", quote, quote, "2026-08-11T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "IN_REVIEW"


def test_r33_quote_that_proves_the_vacancy_still_closes_it(repo):
    _patient_obj, patient_id = _patient(repo)
    row = _create_from_ai(repo, patient_id, _vacancy(evidence_date="2026-08-10T08:00:00"),
                          "Aguarda vaga de UTI.", "2026-08-10T08:00:00")
    quote = "Vaga de UTI liberada pela regulacao."
    _ai_decides(repo, patient_id, "CONCLUIDA", quote, f"{quote} Paciente transferido as 10h.", "2026-08-11T08:00:00")

    assert _status_of(repo, row["pending_id"])["status"] == "RESOLVED"


# ------------------------------------------------ R34: internação anterior


def _readmitted_with_an_open_march_consult(repo):
    patient, patient_id = _patient(repo, admission_date="2026-03-01")
    old = repo.add_pending_item(patient_id, "INTERCONSULTA", "Aguarda avaliação da cardiologia", "x",
                                "2026-03-02T08:00:00", subcategory="SOLICITADA", source="LLM", alvo="CARDIOLOGIA")
    patient = Patient(record_number="1", bed="2A", unit="U", admission_date="2026-08-15")
    assert repo.upsert_patient(patient) == patient_id
    _add_db_note(repo, patient_id, "2026-08-16T10:00:00",
                 "PELA CARDIOLOGIA\nAvaliacao pre-operatoria: risco cardiovascular baixo, liberado para cirurgia.")
    request = "FA de alta resposta desde hoje. Solicito parecer da cardiologia."
    _add_db_note(repo, patient_id, "2026-08-20T08:00:00", request, role="Médico clínico")
    return patient, patient_id, old, request


def test_r34_rule_request_of_a_new_admission_is_not_absorbed_nor_closed_by_older_evidence(repo):
    patient, patient_id, old, request = _readmitted_with_an_open_march_consult(repo)
    _apply_rules_and_detector(repo, patient, patient_id,
                              [StructuredNote("2026-08-20T08:00:00", None, "Médico clínico", request)], "2026-08-14", None)

    assert _status_of(repo, old)["status"] == "IN_REVIEW"
    [new] = [row for row in repo.get_active_pending_items(patient_id) if row["pending_id"] != old]
    assert (new["alvo"], new["status"], new["evidence_date"][:10]) == ("CARDIOLOGIA", "ACTIVE", "2026-08-20")


def test_r34_ai_request_of_a_new_admission_is_not_absorbed_nor_closed_by_older_evidence(repo):
    _patient_obj, patient_id, old, request = _readmitted_with_an_open_march_consult(repo)
    item = _consult("cardiologia", evidence="Solicito parecer da cardiologia.", alvo="CARDIOLOGIA")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(request, timestamp="2026-08-20T08:00:00")])

    assert _status_of(repo, old)["status"] == "IN_REVIEW"
    [new] = [row for row in repo.get_active_pending_items(patient_id) if row["pending_id"] != old]
    assert (new["alvo"], new["status"], new["evidence_date"][:10]) == ("CARDIOLOGIA", "ACTIVE", "2026-08-20")


# ------------------------------------- R35: exame sem região não engole outro


def test_r35_region_less_exam_does_not_absorb_an_exam_with_a_region(repo):
    patient, patient_id = _patient(repo)
    _nightly(repo, patient, patient_id, [
        ("2026-08-10T08:00:00", "Médico clínico", "Hemiparesia a direita. Solicitada TC por suspeita de AVC."),
        ("2026-08-11T08:00:00", "Médico clínico", "Dor abdominal difusa, distensao. Solicito TC de abdome."),
        ("2026-08-12T08:00:00", "Médico clínico", "TC: sem sinais de AVC. Mantem dor abdominal."),
    ], ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"])

    rows = {row["alvo"]: row for row in repo.get_patient_pending_items(patient_id)}
    # "TC:" sem região pode ser de qualquer uma: a de abdome vai no máximo
    # para verificação ("não diz de que região"), nunca é fechada.
    assert rows["TOMOGRAFIA DE ABDOME"]["status"] in {"ACTIVE", "IN_REVIEW"}
    assert rows["TOMOGRAFIA DE ABDOME"]["evidence_date"] == "2026-08-11T08:00:00"


# ------------------------------------- R36: alvos livres parecidos não somam


@pytest.mark.parametrize("a,b", [
    ("TRANSFUSAO HEMACIAS", "TRANSFUSAO PLAQUETAS"), ("REPOSICAO POTASSIO", "REPOSICAO MAGNESIO"),
    ("DESMAME VENTILACAO", "DESMAME SEDACAO"), ("AMPUTACAO MID", "AMPUTACAO MIE"),
    ("ANTIBIOTICO VANCOMICINA", "ANTIBIOTICO MEROPENEM"), ("DRENAGEM TORAX", "DRENAGEM ABSCESSO"),
])
def test_r36_free_text_targets_sharing_one_word_are_different_items(a, b):
    assert not _generic_alvos_compatible(a, b)


@pytest.mark.parametrize("a,b", [("DESMAME OXIGENIO", "DESMAME O2"), ("VAGA UTI", "VAGA UTI ADULTO")])
def test_r36_synonyms_and_subsets_still_match(a, b):
    assert _generic_alvos_compatible(a, b)


def test_r36_closing_the_red_cell_transfusion_keeps_the_platelet_one(repo):
    _patient_obj, patient_id = _patient(repo)
    rbc = _therapy("Aguarda transfusão de hemácias", "transfusao hemacias", "Indicada transfusao de hemacias.",
                   evidence_date="2026-08-10T08:00:00")
    platelets = _therapy("Aguarda transfusão de plaquetas", "transfusao plaquetas", "Indicada transfusao de plaquetas.",
                         evidence_date="2026-08-11T08:00:00")
    _create_from_ai(repo, patient_id, rbc, "Indicada transfusao de hemacias.", "2026-08-10T08:00:00")
    _add_db_note(repo, patient_id, "2026-08-11T08:00:00", "Indicada transfusao de plaquetas.", role="Médico clínico")
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[platelets])]),
                      [_note("Indicada transfusao de plaquetas.", timestamp="2026-08-11T08:00:00")])

    assert {row["alvo"] for row in repo.get_active_pending_items(patient_id)} == {
        "TRANSFUSAO HEMACIAS", "TRANSFUSAO PLAQUETAS"}


# ------------------------------- R37: dispositivo antigo listado depois do pedido


@pytest.mark.parametrize("item,text,quote", [
    ({**_consult("equipe de punção venosa"), "description": "Aguarda equipe de punção para segundo AVP"},
     "Solicitado a equipe de puncao segundo AVP para hemotransfusao. Sinais vitais estaveis. "
     "Dispositivos: AVP em MSE puncionado em 08/08, SVD.",
     "Solicitado a equipe de puncao segundo AVP para hemotransfusao."),
    (_pending_item(category="PROCEDIMENTO_CIRURGIA", subcategory="OUTRO", description="Aguarda passagem de CVC"),
     "Solicitada passagem de CVC pela equipe de acesso vascular. PICC em MSD passado em 02/08 com sucesso, ocluido hoje.",
     "Solicitada passagem de CVC pela equipe de acesso vascular."),
])
def test_r37_older_device_listed_after_a_new_request_does_not_drop_it(repo, item, text, quote):
    _patient_obj, patient_id = _patient(repo)
    _add_db_note(repo, patient_id, "2026-08-10T20:00:00", text, role="Enfermeiro")
    item = {**item, "evidence": quote, "evidence_date": "2026-08-10T20:00:00"}
    _run_llm_analysis(repo, patient_id, StubLLMSequence([_base_analysis(pending_items=[item])]),
                      [_note(text, timestamp="2026-08-10T20:00:00")])

    assert [row["status"] for row in repo.get_active_pending_items(patient_id)] == ["ACTIVE"]


def test_r37_success_written_after_the_request_in_the_same_note_still_counts():
    item = {"category": "PROCEDIMENTO_CIRURGIA", "subcategory": "OUTRO", "alvo": None,
            "description": "Aguarda passagem de CVC", "evidence": "x", "source": "LLM"}
    note = DetectorNote("2026-08-10T20:00:00", "Médico intensivista",
                        "Solicitada passagem de CVC. Realizada passagem de CVC em VJID sem intercorrências.")
    assert se.puncture_already_done(item, "Solicitada passagem de CVC.", note)


# ---------------------------------- R38: mesmo texto, dois dias depois


def test_r38_same_words_two_days_after_the_answer_with_a_note_in_between_is_a_new_request(repo):
    patient, patient_id = _patient(repo)
    _nightly(repo, patient, patient_id, [
        ("2026-08-10T08:00:00", "Médico clínico", "Dispneia. Solicito parecer da cardiologia."),
        ("2026-08-10T16:00:00", "Médico residente",
         "PELA CARDIOLOGIA\nAvaliado. Sem cardiopatia estrutural. Sem conduta cardiologica."),
        ("2026-08-11T08:00:00", "Médico clínico", "Paciente estavel, sem queixas."),
        ("2026-08-12T08:00:00", "Médico clínico", "Nova FA de alta resposta esta manha. Solicito parecer da cardiologia."),
    ], ["2026-08-10", "2026-08-11", "2026-08-12"])

    [row] = repo.get_active_pending_items(patient_id)
    assert (row["alvo"], row["status"], row["evidence_date"]) == ("CARDIOLOGIA", "ACTIVE", "2026-08-12T08:00:00")


# ---------------------------------- R39: interconsulta fora da tabela


def test_r39_out_of_table_consults_keep_distinct_targets():
    ccih = se.normalize_target("INTERCONSULTA", None, "Aguarda parecer da equipe de controle de infecção hospitalar")
    pain = se.normalize_target("INTERCONSULTA", None, "Aguarda parecer da equipe de controle da dor")
    therapy = se.normalize_target("INTERCONSULTA", None, "Aguarda avaliação do serviço de terapia da dor")
    assert len({ccih, pain, therapy}) == 3


# -------------------------- R42: etapa de interconsulta não engole pedido novo


def test_r42_stage_consult_item_does_not_absorb_a_new_request_to_the_same_specialty(repo):
    patient, patient_id = _patient(repo)
    stage = repo.add_pending_item(patient_id, "INTERCONSULTA", "Cardiologia aguarda ecocardiograma para definir conduta",
                                  "x", "2026-08-10T16:00:00", subcategory="REALIZADA_SEM_CONDUTA_DEFINIDA",
                                  source="LLM", alvo="CARDIOLOGIA")
    _nightly(repo, patient, patient_id, [
        ("2026-08-14T08:00:00", "Médico clínico", "Nova FA de alta resposta. Solicito parecer da cardiologia."),
        ("2026-08-14T16:00:00", "Médico residente",
         "PELA CARDIOLOGIA\nAvaliado: FA com resposta controlada apos amiodarona. Manter amiodarona."),
    ], ["2026-08-14"])

    [new] = [row for row in repo.get_patient_pending_items(patient_id) if row["pending_id"] != stage]
    assert (new["subcategory"], new["evidence_date"], new["status"]) == (
        "SOLICITADA", "2026-08-14T08:00:00", "RESOLVED")
    assert _status_of(repo, stage)["status"] == "ACTIVE"


# --------------------------------- R48: pedido curto copiado não recria


def test_r48_short_request_copied_forward_after_its_result_is_not_re_created(repo):
    patient, patient_id = _patient(repo, admission_date="2026-08-14")
    _nightly(repo, patient, patient_id, [
        ("2026-08-15T09:00:00", "Médico clínico", "Paciente estavel. Peço ECO."),
        ("2026-08-16T09:00:00", "Médico clínico", "ECO transtoracico: FE 55%, sem alteracoes segmentares."),
        ("2026-08-17T09:00:00", "Médico clínico", "Paciente estavel. Peço ECO."),
    ], ["2026-08-15", "2026-08-16", "2026-08-17"])

    assert [row["status"] for row in repo.get_patient_pending_items(patient_id)] == ["RESOLVED"]
