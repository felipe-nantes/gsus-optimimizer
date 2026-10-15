"""PEND-001: regressões da QUINTA verificação adversarial da Fase 3 (DEC-129),
só fechamento falso e travamento nas correções da 4ª rodada. Cada frase foi
reproduzida por um revisor e confirmada por um verificador. Todo texto
clínico é fictício."""
import time
from datetime import timedelta, timezone

import pytest

from app.analysis import specialties as sp
from app.analysis import specialty_evaluation as se
from app.analysis.rules import StructuredNote, apply_all_rules, consult_targets_requested
from app.analysis.specialty_evaluation import ACTION_CANCEL, ACTION_RESOLVE, ACTION_REVIEW, DetectorNote, detect_resolutions

BRT = timezone(timedelta(hours=-3))
CLOSING = {ACTION_RESOLVE, ACTION_CANCEL}


def _item(category, description, *, subcategory=None, alvo=None, evidence="pedido",
          evidence_date="2026-08-20T10:00:00", pending_id="p1"):
    return {
        "pending_id": pending_id, "category": category, "subcategory": subcategory, "alvo": alvo,
        "description": description, "evidence": evidence, "evidence_date": evidence_date,
        "created_at": "2026-08-20T15:00:00+00:00", "source": "RULE",
    }


def _consult(description="Aguarda avaliação da nefrologia"):
    return _item("INTERCONSULTA", description, subcategory="SOLICITADA")


def _exam(description="Aguarda TC de crânio"):
    return _item("DIAGNOSTICO", description, subcategory="AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL")


def _puncture(description="Aguarda passagem de CVC"):
    return _item("PROCEDIMENTO_CIRURGIA", description, subcategory="OUTRO")


def _actions(item, text, role="Médico clínico", timestamp="2026-08-21T08:00:00"):
    return {f.action for f in detect_resolutions([item], [DetectorNote(timestamp, role, text)], local_tz=BRT)}


def _decide(item, notes):
    return [f.action for f in detect_resolutions([item], [DetectorNote(*note) for note in notes], local_tz=BRT)]


# ------------------- R5-FC-01: genérico do médico só como oração inteira


@pytest.mark.parametrize("text", [
    "Realizada TC de crânio, exames laboratoriais sem alterações.", "TC de crânio realizada, hemograma sem alterações.",
    "TC de crânio realizada, glicemia normal.", "TC de crânio realizada, gasometria normal.",
    "TC de crânio realizada, eletrólitos normais.", "Realizada TC de crânio, sem alterações do quadro neurológico.",
    "Realizada TC de crânio. Sem alterações na conduta.", "Realizada TC de crânio. Sem alterações laboratoriais.",
    "Realizada TC de crânio. Sem alterações em relação ao dia anterior.", "TC de crânio realizada, PA normal.",
])
def test_r5_fc01_physician_status_of_something_else_after_done_is_not_the_result(text):
    assert not (_actions(_exam(), text, timestamp="2026-08-20T19:00:00") & CLOSING)


@pytest.mark.parametrize("text", [
    "Tomografia de crânio realizada, sem alterações.", "TC de crânio realizada. Sem alterações agudas.",
    "Realizada TC de crânio. Normal.",
])
def test_r5_fc01_physician_bare_result_after_done_still_closes(text):
    assert _actions(_exam(), text, timestamp="2026-08-20T19:00:00") == {ACTION_RESOLVE}


# ------------------- R5-FC-02/03: plano não é avaliação


@pytest.mark.parametrize("role,text,target", [
    ("Médico nefrologista", "Ciente. Retornaremos para exame físico após a sessão de hemodiálise.", "nefrologia"),
    ("Médico nefrologista", "Ciente. Paciente encaminhado ao exame de tomografia. Retornaremos mais tarde.", "nefrologia"),
    ("Médico nefrologista", "Ciente. Retornaremos após resultado da gasometria; se indicada hemodiálise, passaremos o "
                            "cateter.", "nefrologia"),
    ("Médico nefrologista", "Ciente. Retornaremos após ser realizada avaliação pela cardiologia.", "nefrologia"),
    ("Médico residente", "PELA NEFROLOGIA: ciente, retornaremos para exame físico após a hemodiálise.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: paciente reavaliado pela equipe, nefrologia passará à tarde.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: paciente avaliado pela UTI, nefro passará à tarde.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: parecer: nefro passará à tarde.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: parecer: hoje à tarde.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: resposta: amanhã.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: parecer: a ser realizado amanhã.", "nefrologia"),
    ("Médico clínico", "PELA CARDIOLOGIA: risco cirúrgico: classe a definir após ECO.", "cardiologia"),
    ("Médico clínico", "PELA NEFROLOGIA: mantida hidratação, nefro avaliará amanhã.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: iniciada furosemida, nefrologia vai avaliar à tarde.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: mantida hidratação, discutido com nefro que irá avaliar.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: mantida hidratação, nefro deve avaliar hoje.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: mantida hidratação, aguardando USG de rins para nefro avaliar.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: mantida hidratação. Nefro avaliará amanhã.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: mantida hidratação, nefrologia ficou de avaliar.", "nefrologia"),
])
def test_r5_fc02_fc03_plans_and_future_evaluations_never_close(role, text, target):
    item = _consult(f"Aguarda avaliação da {target}")
    assert not (_actions(item, text, role=role) & CLOSING)
    if text.startswith("PELA"):
        ai_item = {**item, "alvo": target.upper(), "evidence": text, "source": "LLM"}
        assert not se.evidence_shows_done(ai_item)


@pytest.mark.parametrize("role,text", [
    ("Médico nefrologista", "Paciente avaliado. Sem indicação de diálise. Retornaremos em 48h."),
    ("Médico nefrologista", "Realizada avaliação nefrológica. Sugiro manter hidratação. Passaremos amanhã."),
    ("Médico cardiologista", "Risco cardiológico intermediário (Lee 2). Pode prosseguir com a cirurgia."),
    ("Médico residente", "PELA NEFROLOGIA: paciente avaliado, sem indicação de diálise, retornaremos amanhã."),
])
def test_r5_fc02_real_evaluations_with_a_follow_up_still_close(role, text):
    target = "cardiologia" if "cardio" in role else "nefrologia"
    assert _actions(_consult(f"Aguarda avaliação da {target}"), text, role=role) == {ACTION_RESOLVE}


# --------------------- R5-FC-05: motivo negativo só sobre o próprio pedido


@pytest.mark.parametrize("item,text", [
    (_exam(), "TC de crânio suspensa: paciente agitado, sem indicação formal de sedação."),
    (_exam(), "TC de crânio suspensa: paciente agitado, sem indicação cirúrgica."),
    (_exam(), "TC de crânio cancelada, tomógrafo em manutenção, sem indicação neurocirúrgica."),
    (_exam("Aguarda ecocardiograma"), "ECO cancelado, ecocardiografista de férias, sem necessidade imediata de transferir."),
    (_exam("Aguarda ecocardiograma"), "ECO cancelado, ecocardiografista de férias, sem necessidade de TC."),
    (_exam("Aguarda ecocardiograma"), "ECO cancelado, ecocardiografista de férias, sem necessidade de exames adicionais."),
    (_exam("Aguarda TC de abdome"), "TC de abdome cancelada, tomógrafo em manutenção, sem necessidade de controle "
                                    "laboratorial."),
    (_exam("Aguarda TC de abdome"), "TC de abdome cancelada, tomógrafo em manutenção, sem indicação de cirurgia de "
                                    "urgência."),
])
def test_r5_fc05_negative_reason_about_something_else_is_not_a_cancel(item, text):
    assert ACTION_CANCEL not in _actions(item, text)


@pytest.mark.parametrize("item,text", [
    (_exam(), "TC de crânio suspensa, sem indicação."),
    (_exam(), "TC de crânio cancelada por mudança de conduta, sem necessidade do exame."),
    (_exam(), "TC de crânio suspensa por melhora clínica."),
    (_consult("Aguarda avaliação da urologia"), "Sem necessidade de avaliação da urologia por melhora clínica."),
    (_puncture(), "Suspensa passagem de CVC, sem necessidade de acesso central."),
])
def test_r5_fc05_reason_about_the_item_itself_still_cancels(item, text):
    assert _actions(item, text, role="Médico intensivista" if "CVC" in text else "Médico clínico") == {ACTION_CANCEL}


# ----------------- R5-FC-07/08: espera depois da prova em outros formatos


@pytest.mark.parametrize("later", [
    "Segue aguardando avaliação da nefrologia, solicitada desde a admissão.",
    "Segue aguardando avaliação da nefrologia, solicitada há 2 dias.",
    "Paciente internado por IRA, ainda aguarda parecer da nefrologia.",
    "Reitero parecer da nefrologia solicitado há 3 dias.",
    "PELA NEFROLOGIA: ainda não avaliado, paciente estava em exame.",
    "PELA NEFROLOGIA: nefro ainda não avaliou, paciente no centro cirúrgico pela manhã.",
    "Nefrologia:\n- aguarda avaliação", "NEFROLOGIA\nAguardando parecer.",
    "Nefro ainda não avaliou o paciente.", "Nefro aguarda avaliação.",
])
def test_r5_fc07_fc08_later_consult_wait_is_a_conflict(later):
    found = _decide(_consult(), [
        ("2026-08-21T08:00:00", "Médico nefrologista", "Paciente avaliado. Sem indicação de diálise."),
        ("2026-08-22T08:00:00", "Médico clínico", later),
    ])
    assert found == [ACTION_REVIEW]


def test_r5_fc08_nefro_abbreviation_is_bound_to_the_wait():
    assert sp.waiting_about("nefro ainda nao avaliou o paciente", sp.NEFROLOGIA, unbound_counts=False,
                            include_requests=False)
    assert sp.waiting_about("cardio ainda nao avaliou", sp.CARDIOLOGIA, unbound_counts=False, include_requests=False)


@pytest.mark.parametrize("item,proof,later", [
    (_exam("Aguarda TC de crânio de controle"), "TC de crânio: hematoma subdural estável.",
     "Aguarda TC de crânio de controle solicitada há 2 dias."),
    (_exam("Aguarda TC de crânio de controle"), "TC de crânio: hematoma subdural estável.",
     "Aguarda TC de crânio de controle, solicitada na admissão."),
    (_puncture(), "Passado CVC em VJID guiado por USG, sem intercorrências.", "Aguarda passagem de CVC, solicitada na admissão."),
    (_puncture(), "Passado CVC em VJID guiado por USG, sem intercorrências.",
     "Ainda aguarda passagem de CVC solicitada há 2 dias."),
])
def test_r5_fc07_present_wait_with_the_original_request_date_is_a_conflict(item, proof, later):
    role = "Médico intensivista" if "CVC" in proof else "Médico clínico"
    found = _decide(item, [("2026-08-21T08:00:00", role, proof), ("2026-08-22T08:00:00", "Médico clínico", later)])
    assert found == [ACTION_REVIEW]


# ------------------------------- R5-FC-09: exame de controle e região pendente


@pytest.mark.parametrize("text", [
    "TC de crânio com hematoma subdural laminar, repetir em 24h.",
    "TC de crânio com hematoma subdural laminar, controle tomográfico em 24h.",
    "TC de crânio com hematoma subdural laminar, controle em 24h.",
])
def test_r5_fc09_control_item_with_the_repeat_still_programmed_is_review(text):
    assert not (_actions(_exam("Aguarda TC de crânio de controle"), text) & CLOSING)


@pytest.mark.parametrize("later", [
    "TC de crânio de controle agendada para amanhã.", "TC de crânio de controle programada para amanhã.",
    "Repetir TC de crânio amanhã.", "Solicitada TC de crânio de controle.",
])
def test_r5_fc09_later_scheduling_of_the_control_exam_is_a_conflict(later):
    found = _decide(_exam("Aguarda TC de crânio de controle"), [
        ("2026-08-21T08:00:00", "Médico clínico", "TC de crânio: hematoma subdural estável."),
        ("2026-08-22T08:00:00", "Médico clínico", later),
    ])
    assert found == [ACTION_REVIEW]


@pytest.mark.parametrize("text", [
    "TC de crânio e abdome: hematoma subdural agudo; abdome aguarda laudo.",
    "TC de crânio e abdome com hematoma subdural agudo. Abdome: aguarda laudo.",
    "TC de crânio e abdome evidenciando hematoma subdural; laudo do abdome pendente.",
])
def test_r5_fc09_region_reported_pending_in_the_next_sentence_never_closes(text):
    assert not (_actions(_exam("Aguarda TC de abdome"), text) & CLOSING)


# --------------------------------------- R5-FC-04: trecho genérico da IA


def _generic(category, description, alvo, subcategory="OUTRO"):
    return {"pending_id": "g1", "category": category, "subcategory": subcategory, "description": description,
            "alvo": alvo}


@pytest.mark.parametrize("item,quote", [
    (_generic("TRANSFERENCIA", "Aguarda vaga de UTI", "VAGA UTI", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Vaga de enfermaria liberada pela regulacao, porem paciente com criterios de UTI."),
    (_generic("TRANSFERENCIA", "Aguarda vaga em enfermaria", "VAGA ENFERMARIA", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Vaga de UTI liberada, regulacao informa que enfermaria segue lotada."),
    (_generic("TERAPEUTICA", "Aguarda início de hemodiálise", "INICIO HEMODIALISE"),
     "Iniciada furosemida para evitar hemodialise."),
    (_generic("TERAPEUTICA", "Aguarda início de hemodiálise", "INICIO HEMODIALISE"),
     "Iniciada furosemida visando postergar hemodialise."),
    (_generic("TERAPEUTICA", "Aguarda transfusão de hemácias", "TRANSFUSAO HEMACIAS"),
     "Realizada transfusao de plaquetas; hemacias se Hb menor que 7."),
    (_generic("TERAPEUTICA", "Aguarda liberação de vancomicina pela CCIH", "LIBERACAO VANCOMICINA"),
     "Liberado meropenem pela CCIH, vancomicina em analise."),
])
def test_r5_fc04_done_word_near_a_shared_word_does_not_confirm(item, quote):
    assert not (se.quote_confirms_done(quote, item) and se.quote_is_about_item(quote, item, [item]))


# --------------------------------------------- R5-CRASH: tempo limitado


def _elapsed(call):
    start = time.perf_counter()
    call()
    return time.perf_counter() - start


@pytest.mark.parametrize("text", ["solicito tc " * 800, "tc solicitada " * 400])
def test_r5_crash01_long_unpunctuated_request_runs_bounded(text):
    note = StructuredNote("2026-08-10T10:00:00", None, "Médico residente", text)
    assert _elapsed(lambda: apply_all_rules([note])) < 5.0


@pytest.mark.parametrize("sentence", ["Solicito, " + "hoje " * 22 + "x parecer da nefrologia",
                                      "Solicito, " + "pois " * 22 + ", x parecer da nefrologia."])
def test_r5_crash02_repeated_aside_words_do_not_backtrack(sentence):
    assert _elapsed(lambda: consult_targets_requested(sentence)) < 0.5


def test_r5_crash03_long_unpunctuated_later_note_runs_bounded():
    item = _item("DIAGNOSTICO", "Aguarda TC de crânio", subcategory="AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL",
                 alvo="TOMOGRAFIA DE CRÂNIO", evidence_date="2026-08-09T10:00:00")
    notes = [DetectorNote("2026-08-10T08:00:00", "Médico clínico", "TC de crânio: sem sinais de sangramento."),
             DetectorNote("2026-08-11T08:00:00", "Médico clínico", ("tc de cranio realizada " * 900)[:20000])]
    assert _elapsed(lambda: detect_resolutions([item], notes, local_tz=BRT)) < 2.0
