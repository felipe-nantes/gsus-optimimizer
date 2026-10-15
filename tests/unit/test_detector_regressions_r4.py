"""PEND-001: regressões da QUARTA revisão adversarial da Fase 3 (DEC-129),
sobre o detector, as regras e a checagem do trecho da IA. Cada frase foi
reproduzida por um revisor e confirmada por um verificador. Todo texto
clínico é fictício."""
from datetime import date, timedelta, timezone

import pytest

from app.analysis import specialties as sp
from app.analysis import specialty_evaluation as se
from app.analysis.rules import StructuredNote, apply_all_rules, consult_targets_requested, requested_exams
from app.analysis.specialty_evaluation import ACTION_CANCEL, ACTION_RESOLVE, ACTION_REVIEW, DetectorNote, detect_resolutions

BRT = timezone(timedelta(hours=-3))
CLOSING = {ACTION_RESOLVE, ACTION_CANCEL}


def _item(category, description, *, subcategory=None, alvo=None, evidence="pedido",
          evidence_date="2026-08-20T10:00:00", source="RULE", pending_id="p1"):
    return {
        "pending_id": pending_id, "category": category, "subcategory": subcategory, "alvo": alvo,
        "description": description, "evidence": evidence, "evidence_date": evidence_date,
        "created_at": "2026-08-20T15:00:00+00:00", "source": source,
    }


def _consult(description="Aguarda avaliação da nefrologia", **kwargs):
    return _item("INTERCONSULTA", description, subcategory="SOLICITADA", **kwargs)


def _exam(description="Aguarda TC de crânio", **kwargs):
    kwargs.setdefault("subcategory", "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL")
    return _item("DIAGNOSTICO", description, **kwargs)


def _culture(description, **kwargs):
    return _exam(description, subcategory="AGUARDA_ANATOMOPATOLOGICO_OU_CULTURA", **kwargs)


def _puncture(description="Aguarda passagem de CVC", **kwargs):
    return _item("PROCEDIMENTO_CIRURGIA", description, subcategory="OUTRO", **kwargs)


def _actions(item, text, role="Médico clínico", timestamp="2026-08-21T08:00:00"):
    return {f.action for f in detect_resolutions([item], [DetectorNote(timestamp, role, text)], local_tz=BRT)}


def _decide(item, notes):
    return [f.action for f in detect_resolutions([item], [DetectorNote(*note) for note in notes], local_tz=BRT)]


def _rule_targets(text):
    return {f.alvo for f in apply_all_rules([StructuredNote("2026-08-21T10:00:00", None, "Médico clínico", text)],
                                            "2026-08-09")}


# ------------------------------ F01: espera no fim da linha "PELA X" da equipe


@pytest.mark.parametrize("text", [
    "PELA NEFROLOGIA: iniciada furosemida, aguarda.", "PELA NEFROLOGIA: mantida hidratação, IC em aberto.",
    "PELA NEFROLOGIA: mantida hidratação, pendente.", "PELA NEFROLOGIA: mantida hidratação, equipe ainda não compareceu.",
    "PELA NEFROLOGIA: mantida hidratação, aguardamos.", "PELA NEFROLOGIA: mantida hidratação, parecer pendente.",
    "PELA NEFROLOGIA: mantida hidratação, IC pendente.", "PELA NEFROLOGIA: mantida hidratação venosa. Aguarda.",
    "PELA NEFROLOGIA: mantida hidratação, ainda não passou.", "PELA NEFROLOGIA: mantida hidratação, não veio avaliar.",
])
def test_f01_requesting_team_pela_line_with_a_trailing_wait_never_closes(text):
    assert not (_actions(_consult(), text) & CLOSING)
    ai_item = {"category": "INTERCONSULTA", "subcategory": "SOLICITADA", "alvo": "NEFROLOGIA",
               "description": "Aguarda avaliação da nefrologia", "evidence": text, "source": "LLM"}
    assert not se.evidence_shows_done(ai_item)


def test_f01_specialist_block_with_its_own_pending_culture_still_closes():
    text = "PELA INFECTOLOGIA: avaliado, manter cefepime, cultura de aspirado pendente."
    assert _actions(_consult("Aguarda avaliação da infectologia"), text, role="Médico residente") == {ACTION_RESOLVE}


# ------------------------------ F02: resultado antigo + espera do controle


@pytest.mark.parametrize("item,text", [
    (_exam("Aguarda TC de crânio de controle"), "TC de crânio com hematoma subdural laminar, aguarda TC de controle."),
    (_culture("Aguarda hemoculturas de controle"),
     "Hemoculturas positivas para S. aureus, aguarda hemoculturas de controle."),
    (_exam("Aguarda RX de tórax de controle"), "RX de tórax com consolidação em base direita, aguarda RX de controle."),
    (_exam("Aguarda TC de crânio de controle"), "TC de crânio com hematoma subdural laminar, aguarda nova TC de crânio."),
    (_exam("Aguarda TC de crânio de controle"),
     "TC de crânio com hematoma subdural laminar, solicitada TC de crânio de controle em 24h."),
])
def test_f02_same_sentence_result_and_wait_for_the_same_exam_never_closes(item, text):
    assert not (_actions(item, text) & CLOSING)


def test_f02_later_wait_for_the_control_exam_is_a_conflict():
    found = _decide(_exam("Aguarda TC de crânio de controle"), [
        ("2026-08-21T08:00:00", "Médico clínico", "TC de crânio: hematoma subdural estável."),
        ("2026-08-22T08:00:00", "Médico clínico", "Aguarda TC de crânio de controle."),
    ])
    assert found == [ACTION_REVIEW]


def test_f02_result_with_a_different_exam_awaited_still_closes():
    assert _actions(_exam(), "TC de crânio sem alterações agudas, aguarda RM de crânio.") == {ACTION_RESOLVE}


# ------------------------------------------ F03: "avaliação" que não é


@pytest.mark.parametrize("role,text,target", [
    ("Médico nefrologista", "Ciente. Será realizada avaliação após a sessão de hemodiálise.", "nefrologia"),
    ("Médico nefrologista", "Interconsulta recebida. Aguardamos alta da UTI para avaliação.", "nefrologia"),
    ("Médico clínico", "PELA CARDIOLOGIA: risco cirúrgico solicitado.", "cardiologia"),
    ("Médico clínico", "PELA CARDIOLOGIA: risco cirúrgico pendente.", "cardiologia"),
    ("Médico clínico", "PELA NEFROLOGIA: parecer: pendente.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: resposta: pendente.", "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: parecer: solicitado hoje.", "nefrologia"),
    ("Médico nefrologista", "Ciente. HD: a definir após avaliação. Retornaremos à tarde.", "nefrologia"),
    ("Médico nefrologista", "Ciente da interconsulta. Paciente mantido em ventilação mecânica, sedado. "
                            "Retornaremos amanhã para avaliação.", "nefrologia"),
    ("Médico residente", "PELA NEFROLOGIA: ciente. Iniciada noradrenalina pela equipe da UTI. Retornaremos à tarde.",
     "nefrologia"),
    ("Médico clínico", "PELA NEFROLOGIA: mantida furosemida em BIC, acionada nefrologia que virá avaliar à tarde.",
     "nefrologia"),
    ("Médico nefrologista", "Ciente. Orientada enfermagem quanto ao jejum. Avaliaremos após o exame.", "nefrologia"),
    ("Médico nefrologista", "Ciente. Dieta suspensa para exame. Retornaremos à tarde.", "nefrologia"),
])
def test_f03_not_an_evaluation_never_closes(role, text, target):
    assert not (_actions(_consult(f"Aguarda avaliação da {target}"), text, role=role) & CLOSING)


def test_f03_surgical_risk_with_a_class_still_closes():
    text = "Risco cardiológico intermediário (Lee 2). Pode prosseguir com a cirurgia."
    assert _actions(_consult("Aguarda avaliação da cardiologia"), text, role="Médico cardiologista") == {ACTION_RESOLVE}


def test_f03_real_evaluation_followed_by_a_return_still_closes():
    text = "Paciente avaliado. Sugerimos manter ceftriaxona. Retornaremos em 48h."
    assert _actions(_consult("Aguarda avaliação da infectologia"), text, role="Médico infectologista") == {ACTION_RESOLVE}


# ------------------------------- F04: "PELA X: aguarda" depois da resposta


@pytest.mark.parametrize("later", [
    "PELA NEFROLOGIA: aguarda avaliação.", "PELA NEFRO: ainda não avaliado.", "PELA NEFROLOGIA: aguardando.",
    "PELA NEFROLOGIA: IC enviada, aguarda.", "PELA NEFROLOGIA: parecer pendente.",
    "PELA NEFROLOGIA: aguarda parecer.", "PELA NEFROLOGIA:\n- aguarda avaliação",
])
def test_f04_requesting_team_pela_wait_after_the_answer_is_a_conflict(later):
    found = _decide(_consult(), [
        ("2026-08-21T08:00:00", "Médico nefrologista", "Paciente avaliado. Sem indicação de diálise."),
        ("2026-08-22T08:00:00", "Médico clínico", later),
    ])
    assert found == [ACTION_REVIEW]


# --------------------------------------------- F05: laudo por região


@pytest.mark.parametrize("item,text", [
    (_exam("Aguarda TC de abdome"), "TC de crânio e abdome realizadas: crânio sem alterações; abdome aguarda laudo."),
    (_exam("Aguarda TC de abdome"), "TC de crânio e de abdome: crânio sem sangramento, abdome com laudo pendente."),
    (_exam("Aguarda TC de abdome"), "TC de crânio e abdome: crânio sem sinais de sangramento. Abdome: aguarda laudo."),
    (_exam("Aguarda TC de crânio"), "TC de crânio e abdome realizadas: abdome sem alterações; crânio aguarda laudo."),
])
def test_f05_result_for_one_listed_region_does_not_close_another(item, text):
    assert not (_actions(item, text) & CLOSING)


def test_f05_result_naming_no_region_closes_every_listed_region():
    assert _actions(_exam("Aguarda TC de abdome"), "TC de tórax e abdome: sem alterações.") == {ACTION_RESOLVE}


# ---------------------------------------- F06/F07: dispositivo de outro tipo


@pytest.mark.parametrize("description,text", [
    ("Aguarda troca de CVC", "Cancelada passagem de PICC por melhora clínica."),
    ("Aguarda passagem de CVC", "Suspensa passagem de Shilley em VJID, sem necessidade de hemodiálise."),
    ("Aguarda punção de AVP", "Suspensa passagem de CVC, sem necessidade, manter acesso periférico."),
    ("Aguarda passagem de PAI", "Suspensa passagem de CVC, sem necessidade, mantida PAI em radial esquerda."),
])
def test_f06_cancelling_another_device_does_not_cancel_the_request(description, text):
    assert ACTION_CANCEL not in _actions(_puncture(description), text, role="Médico intensivista")


def test_f06_cancelling_the_same_device_still_cancels():
    assert _actions(_puncture(), "Suspensa passagem de CVC por melhora clínica.", role="Médico intensivista") == {
        ACTION_CANCEL}


@pytest.mark.parametrize("description,text", [
    ("Aguarda passagem de CVC", "Passada PAI em femoral direita sem intercorrências."),
    ("Aguarda passagem de CVC", "Realizada punção de PAI femoral esquerda guiada por USG, sem intercorrências."),
    ("Aguarda passagem de CVC", "PAI em femoral direita puncionada com sucesso."),
    ("Aguarda passagem de CVC", "Passada PAI em radial direita guiada por USG, punção de CVC a seguir."),
    ("Aguarda passagem de PAI", "Passado CVC em VJID guiado por USG, PAI em radial esquerda a seguir."),
])
def test_f07_another_device_success_does_not_close(description, text):
    assert not (_actions(_puncture(description), text, role="Médico intensivista") & CLOSING)


def test_f07_new_cvc_request_is_not_dropped_by_a_femoral_arterial_line():
    item = {"category": "PROCEDIMENTO_CIRURGIA", "subcategory": "OUTRO", "alvo": None,
            "description": "Aguarda passagem de CVC", "evidence": "x", "source": "LLM"}
    note = DetectorNote("2026-08-21T08:00:00", "Médico intensivista",
                        "Solicito passagem de CVC para início de noradrenalina. Passada PAI em femoral esquerda "
                        "sem intercorrências.")
    assert not se.puncture_already_done(item, "Solicito passagem de CVC para início de noradrenalina.", note)


# ---------------------------------- F08: texto de enfermagem não é laudo


@pytest.mark.parametrize("description,role,text", [
    ("Aguarda TC de crânio", "Enfermeiro", "TC de crânio realizada às 15h. Sem alterações no período."),
    ("Aguarda TC de crânio", "Enfermeiro", "Realizada TC de crânio. Sem lesões por pressão."),
    ("Aguarda TC de crânio", "Enfermeiro", "TC de crânio realizada, sem alterações durante o procedimento."),
    ("Aguarda TC de crânio", "Enfermeiro", "Realizada TC de crânio às 14h. Sem alterações."),
    ("Aguarda TC de crânio", "Médico clínico", "Realizada TC de crânio, mantém edema de membros inferiores."),
    ("Aguarda TC de crânio", "Médico clínico", "TC de crânio realizada, segue com insuficiência renal aguda."),
    ("Aguarda TC de crânio", "Médico clínico", "Realizada TC de crânio. Normal a ausculta pulmonar."),
    ("Aguarda RX de tórax", "Enfermeiro", "RX de tórax realizado no leito. Normal ao toque, abdome flácido."),
])
def test_f08_nursing_or_clinical_text_after_the_exam_is_not_its_result(description, role, text):
    assert not (_actions(_exam(description), text, role=role, timestamp="2026-08-20T19:00:00") & CLOSING)


def test_f08_physician_terse_result_after_done_still_closes():
    assert _actions(_exam("Aguarda TC de tórax"), "Tomografia de tórax realizada, sem alterações.") == {ACTION_RESOLVE}


# -------------------------------------------------- F09: datas relativas


@pytest.mark.parametrize("text", [
    "TC de crânio de ontem: hematoma subdural laminar à esquerda.",
    "TC de crânio da semana passada: hematoma subdural laminar.",
    "TC de crânio (ontem): hematoma subdural laminar.",
    "TC de crânio de anteontem: hematoma subdural laminar.",
    "TC de crânio realizada na sexta-feira: hematoma subdural laminar.",
])
def test_f09_relative_date_before_the_request_never_closes(text):
    assert not (_actions(_exam(), text, timestamp="2026-08-20T19:00:00") & CLOSING)


@pytest.mark.parametrize("text", [
    "PELA NEFROLOGIA: paciente avaliado ontem, sem indicação de diálise.",
    "PELA NEFROLOGIA: avaliado no dia 18, sem indicação de diálise.",
])
def test_f09_evaluation_dated_by_a_relative_day_before_the_request_never_closes(text):
    assert not (_actions(_consult(), text, role="Médico residente", timestamp="2026-08-20T19:00:00") & CLOSING)


def test_f09_relative_dates_are_dates():
    note_day = date(2026, 8, 21)  # sexta-feira
    assert se.cited_dates("tc de ontem", note_day) == [date(2026, 8, 20)]
    assert se.cited_dates("realizada na quarta-feira", note_day) == [date(2026, 8, 19)]
    assert se.cited_dates("eco da semana passada", note_day) == [date(2026, 8, 14)]


# ------------------------------------------------ F10: material da cultura


@pytest.mark.parametrize("description,text", [
    ("Aguarda cultura de fragmento ósseo", "Cultura de swab de ferida do pé: Pseudomonas aeruginosa."),
    ("Aguarda cultura de fragmento ósseo", "Cultura de tecido de partes moles: S. aureus sensível a oxacilina."),
    ("Aguarda cultura de lavado broncoalveolar", "Cultura de escarro negativa."),
    ("Aguarda cultura de aspirado traqueal", "Cultura de escarro negativa."),
    ("Aguarda cultura de secreção de ferida operatória", "Cultura de fragmento ósseo: S. aureus."),
])
def test_f10_culture_of_a_different_specimen_never_closes(description, text):
    assert not (_actions(_culture(description), text) & CLOSING)


@pytest.mark.parametrize("label", [label for label, _pattern in se._SPECIMENS])
def test_f10_every_specimen_label_round_trips(label):
    assert se.exam_target(f"CULTURA DE {label}") == f"CULTURA DE {label}"


# --------------------------------- F11: motivo de outra coisa / outra especialidade


@pytest.mark.parametrize("item,role,text", [
    (_exam("Aguarda ecocardiograma"), "Médico clínico",
     "ECO cancelado, ecocardiografista de férias, sem necessidade de transferir."),
    (_exam(), "Médico clínico", "TC de crânio suspensa: paciente agitado, sem indicação de sedação."),
    (_consult("Aguarda avaliação da cirurgia vascular"), "Médico residente",
     "PELA INFECTOLOGIA: sem necessidade de avaliação da cirurgia vascular, manter cefepime."),
    (_consult("Aguarda avaliação da urologia"), "Médico nefrologista",
     "Paciente avaliado. Sem indicação de diálise. Sem necessidade de avaliação da urologia."),
])
def test_f11_cancel_reason_about_another_object_or_by_another_specialty_is_review(item, role, text):
    assert ACTION_CANCEL not in _actions(item, text, role=role)


def test_f11_requesting_team_withdrawal_still_cancels():
    assert _actions(_consult("Aguarda avaliação da urologia"), "Sem necessidade de avaliação da urologia por melhora clínica.") \
        == {ACTION_CANCEL}


# ---------------------------------- F12: especialidade já evoluía (cabeçalho)


@pytest.mark.parametrize("header", ["NEFROLOGIA:\n", "Nefro: ", "Nefrologia - evolução: "])
def test_f12_specialty_following_under_its_own_header_goes_to_review(header):
    found = _decide(_consult("Aguarda avaliação da nefrologia para confecção de fístula arteriovenosa"), [
        ("2026-08-18T08:00:00", "Médico residente", f"{header}HD realizada sem intercorrências, UF 2 L. "
                                                    "Manter esquema 3x/semana."),
        ("2026-08-20T10:00:00", "Médico clínico", "Solicito avaliação da nefrologia para confecção de fístula."),
        ("2026-08-21T08:00:00", "Médico residente", "PELA NEFROLOGIA: HD realizada, UF 2 L. Manter esquema 3x/semana."),
    ])
    assert found == [ACTION_REVIEW]


# ------------------------------------------- REL-1: data impossível não derruba


@pytest.mark.parametrize("text", ["validade 12/9999", "processo 12/9999-1", "cartao 06/0000", "lote 1/0000",
                                  "senha 3/0000", "codigo 12/0000"])
def test_rel1_impossible_month_year_is_ignored(text):
    assert se.cited_dates(text, date(2026, 8, 11)) == []


def test_rel1_a_later_note_with_an_impossible_date_does_not_break_the_detector():
    items = [_puncture(evidence_date="2026-08-10T08:00:00", pending_id="cvc"),
             _consult("Aguarda avaliação da infectologia", evidence_date="2026-08-10T08:00:00", pending_id="ic")]
    notes = [DetectorNote("2026-08-11T08:00:00", "Médico infectologista", "Paciente avaliado. Sugiro manter cefepime."),
             DetectorNote("2026-08-11T09:00:00", "Médico intensivista", "Passado CVC em VJID com sucesso."),
             DetectorNote("2026-08-12T08:00:00", "Enfermeiro", "Cartao SUS validade 12/9999 conferido.")]
    assert sorted(f.action for f in detect_resolutions(items, notes, local_tz=BRT)) == [ACTION_RESOLVE, ACTION_RESOLVE]


# --------------------------------------- R4-ORCH-01/03/05: trecho genérico da IA


def _generic(category, description, alvo, subcategory="OUTRO"):
    return {"pending_id": "g1", "category": category, "subcategory": subcategory, "description": description,
            "alvo": alvo}


@pytest.mark.parametrize("item,quote", [
    (_generic("TERAPEUTICA", "Aguarda hemodiálise", "HEMODIALISE"),
     "Realizado cateterismo pela hemodinamica sem intercorrencias."),
    (_generic("TERAPEUTICA", "Aguarda hemodiálise", "HEMODIALISE"), "Extubado hemodinamicamente estavel."),
    (_generic("PROCEDIMENTO_CIRURGIA", "Aguarda traqueostomia", "TRAQUEOSTOMIA"),
     "Realizada aspiracao traqueal com saida de secrecao espessa."),
    (_generic("TERAPEUTICA", "Aguarda transfusão de plaquetas", "TRANSFUSAO PLAQUETAS"),
     "Transferido para a UTI por plaquetopenia grave."),
    (_generic("PROCEDIMENTO_CIRURGIA", "Aguarda gastrostomia", "GASTROSTOMIA"), "Iniciada gastroprotecao com omeprazol."),
    (_generic("PROCEDIMENTO_CIRURGIA", "Aguarda colecistectomia", "COLECISTECTOMIA"),
     "Realizado USG de abdome que confirmou colecistite aguda."),
    (_generic("TERAPEUTICA", "Aguarda início de hemodiálise", "INICIO HEMODIALISE"),
     "Iniciada noradrenalina, hemodinamica instavel."),
    (_generic("TRANSFERENCIA", "Aguarda vaga de UTI", "VAGA UTI", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Realizada gasometria arterial a pedido da UTI."),
    (_generic("TRANSFERENCIA", "Aguarda vaga de UTI", "VAGA UTI", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Recebeu visita da equipe da UTI."),
    (_generic("TRANSFERENCIA", "Aguarda vaga de UTI", "VAGA UTI", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Hemodialise realizada na UTI sem intercorrencias."),
    (_generic("TRANSFERENCIA", "Aguarda vaga em enfermaria", "VAGA ENFERMARIA", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Recebido da enfermaria apos piora clinica."),
    (_generic("TERAPEUTICA", "Aguarda hemodiálise", "HEMODIALISE"), "Cateter de hemodialise passado sem intercorrencias."),
    (_generic("TERAPEUTICA", "Aguarda suspensão de vancomicina", "SUSPENSAO VANCOMICINA"),
     "Vancomicina liberada pela CCIH por mais 7 dias."),
    (_generic("TERAPEUTICA", "Aguarda suspensão da hemodiálise", "SUSPENSAO HEMODIALISE"),
     "Realizada sessao de hemodialise sem intercorrencias."),
])
def test_r4_orch_unrelated_done_quote_does_not_confirm_the_item(item, quote):
    assert not (se.quote_confirms_done(quote, item) and se.quote_is_about_item(quote, item, [item]))


@pytest.mark.parametrize("item,quote", [
    (_generic("TRANSFERENCIA", "Aguarda vaga de UTI", "VAGA UTI", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Vaga de UTI liberada pela regulacao."),
    (_generic("TRANSFERENCIA", "Aguarda vaga de UTI", "VAGA UTI", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Transferido para a UTI as 14h."),
    (_generic("TERAPEUTICA", "Aguarda início de hemodiálise", "INICIO HEMODIALISE"),
     "Iniciada hemodialise sem intercorrencias."),
    (_generic("TERAPEUTICA", "Aguarda liberação de vancomicina pela CCIH", "LIBERACAO VANCOMICINA"),
     "Vancomicina liberada pela CCIH por mais 7 dias."),
])
def test_r4_orch_real_done_quote_still_confirms_the_item(item, quote):
    assert se.quote_confirms_done(quote, item) and se.quote_is_about_item(quote, item, [item])


@pytest.mark.parametrize("item,quote", [
    (_generic("TERAPEUTICA", "Aguarda liberação de meropenem pela CCIH", "MEROPENEM", "ANTIBIOTICO_OU_ESQUEMA_IV"),
     "Vancomicina suspensa por melhora clinica, mantido meropenem."),
    (_generic("TRANSFERENCIA", "Aguarda vaga em enfermaria", "VAGA ENFERMARIA", "VAGA_OUTRO_HOSPITAL_UTI_OU_ENFERMARIA"),
     "Noradrenalina suspensa por melhora clinica, programada transferencia para enfermaria."),
    (_generic("TERAPEUTICA", "Aguarda hemodiálise", "HEMODIALISE"),
     "Noradrenalina suspensa por melhora clinica, hemodinamicamente estavel."),
    (_generic("TERAPEUTICA", "Aguarda hemodiálise", "HEMODIALISE"),
     "Dieta suspensa por melhora clinica, mantem programacao de hemodialise."),
])
def test_r4_orch_stopping_another_thing_does_not_cancel_the_item(item, quote):
    assert not se.quote_confirms_cancel(quote, item)


def test_r4_orch_cancelling_the_item_itself_still_cancels():
    item = _generic("TERAPEUTICA", "Aguarda hemodiálise", "HEMODIALISE")
    assert se.quote_confirms_cancel("Hemodialise suspensa por melhora clinica e recuperacao da diurese.", item)


# ------------------------------------------- R4-RULES-01..06: criação de pedidos


@pytest.mark.parametrize("text,expected", [
    ("Solicito avaliação da cardiologia: paciente com dor torácica típica e troponina em ascensão.", {"CARDIOLOGIA"}),
    ("Solicito parecer da nefrologia: IRA KDIGO 3, oligúrico, avaliar HD.", {"NEFROLOGIA"}),
    ("Solicito parecer da nefrologia - IRA oligúrica.", {"NEFROLOGIA"}),
    ("Solicitado parecer da cirurgia vascular - pé diabético com necrose.", {sp.CIRURGIA_VASCULAR}),
    ("Solicito IC com a infectologia: bacteremia por S. aureus.", {"INFECTOLOGIA"}),
    ("Solicitado parecer da nefrologia:\nPaciente com IRA oligúrica.", {"NEFROLOGIA"}),
    ("Parecer da nefrologia: solicitado.", {"NEFROLOGIA"}),
    ("IC nefrologia: solicitada.", {"NEFROLOGIA"}),
    ("Parecer da cardiologia - solicitado em 20/08.", {"CARDIOLOGIA"}),
    ("Solicito, por gentileza, avaliação da nefrologia.", {"NEFROLOGIA"}),
    ("Solicito, gentilmente, parecer da cardiologia.", {"CARDIOLOGIA"}),
    ("Solicitamos, por favor, avaliação da cirurgia vascular.", {sp.CIRURGIA_VASCULAR}),
    ("Solicito novamente, parecer da nefrologia.", {"NEFROLOGIA"}),
    ("Solicito, devido à piora da função renal, parecer da nefrologia.", {"NEFROLOGIA"}),
    ("Paciente com IRA oligúrica, solicitado hoje, parecer da nefrologia.", {"NEFROLOGIA"}),
    ("Solicitado parecer da nefrologia, será avaliado amanhã.", {"NEFROLOGIA"}),
    ("Solicitado parecer da nefrologia, paciente será avaliado à tarde.", {"NEFROLOGIA"}),
    ("Solicito reavaliação da nefrologia (avaliado em 18/08, sem indicação de HD na ocasião) por piora de escórias.",
     {"NEFROLOGIA"}),
    ("Solicitada nova avaliação da cardiologia, vista pela equipe em 15/08, agora com FA de alta resposta.",
     {"CARDIOLOGIA"}),
])
def test_r4_rules_common_consult_request_formats_create_items(text, expected):
    assert _rule_targets(text) == expected


@pytest.mark.parametrize("text", [
    "Parecer da cardiologia: sem indicação de cateterismo, solicitado ECO.",
    "solicitado parecer da nefro, avaliado ontem",
    "Solicitado hemograma, parecer da nefrologia sem indicação de HD.",
])
def test_r4_rules_reported_answers_still_do_not_create_a_consult(text):
    assert consult_targets_requested(text) == []


@pytest.mark.parametrize("text,expected", [
    ("Solicito RX de tórax por febre e tosse há 2 dias.", "RAIO-X DE TÓRAX"),
    ("Solicito USG de abdome, paciente com dor abdominal há 3 dias.", "ULTRASSOM DE ABDOME"),
    ("Paciente internado por pneumonia, solicito RX de tórax de controle.", "RAIO-X DE TÓRAX"),
    ("Solicito TC de crânio, paciente admitido com rebaixamento do nível de consciência.", "TOMOGRAFIA DE CRÂNIO"),
    ("Paciente com HAS, DM e antecedente de AVC, solicito TC de crânio.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicito TC de crânio de controle para comparar com a TC anterior.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicito ECO, último em 2024 com FE 35%.", "ECOCARDIOGRAMA"),
    ("Solicito TC de abdome com contraste para comparar com exame externo.", "TOMOGRAFIA DE ABDOME"),
    ("Solicito doppler venoso de MMII por edema assimétrico iniciado após a admissão.",
     "DOPPLER VENOSO DE MEMBROS INFERIORES"),
    ("Solicitado USG de abdome por dor abdominal desde a chegada.", "ULTRASSOM DE ABDOME"),
    ("Solicito TC de crânio, aguardo resultado.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicito RX de tórax, a ser realizado no leito.", "RAIO-X DE TÓRAX"),
    ("Solicitada TC de crânio, será realizada à tarde.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicitada TC de crânio, ainda não realizada.", "TOMOGRAFIA DE CRÂNIO"),
    ("TC de crânio solicitada, ainda não realizada.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicitado ECO, aguarda laudo.", "ECOCARDIOGRAMA"),
    ("Solicito TC de crânio, reavaliar após resultado.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicito TC de crânio, se normal, puncionar líquor.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicito TC de crânio, ECG normal.", "TOMOGRAFIA DE CRÂNIO"),
    ("Solicito ECO, troponina normal.", "ECOCARDIOGRAMA"),
    ("Solicito TC de abdome, amilase normal.", "TOMOGRAFIA DE ABDOME"),
    ("Solicito angioTC de tórax, D-dímero com resultado elevado.", "ANGIOTOMOGRAFIA DE TÓRAX"),
])
def test_r4_rules_exam_requests_with_context_clauses_create_items(text, expected):
    assert expected in _rule_targets(text)


@pytest.mark.parametrize("text", [
    "Se piora neurológica, solicitar TC de crânio.", "Caso persista a dor, solicitar USG de abdome.",
    "Se manter oligúria, solicitar parecer da nefrologia.", "Considerar solicitar ECO.",
    "Avaliar necessidade de solicitar TC de crânio.", "Sem necessidade de solicitar TC de crânio no momento.",
    "Não há indicação de solicitar parecer da nefrologia.", "Desnecessário solicitar ECO no momento.",
])
def test_r4_rules_conditional_or_negated_plans_create_nothing(text):
    assert _rule_targets(text) == set()


def test_r4_rules_conduct_list_infinitive_is_still_an_order():
    assert _rule_targets("CD: solicitar TC de abdome; solicitar parecer da cardiologia.") == {
        "TOMOGRAFIA DE ABDOME", "CARDIOLOGIA"}


@pytest.mark.parametrize("sentence", [
    "Na admissão, solicitada TC de crânio que evidenciou AVC isquêmico.",
    "Solicitada TC de tórax por dispneia, evidenciando TEP bilateral.",
    "Solicitado ECO, realizado ontem.",
])
def test_r4_rules_history_or_result_sentences_still_create_nothing(sentence):
    assert requested_exams(sentence) == []


@pytest.mark.parametrize("text,expected", [
    ("Solicito USG doppler venoso de MMII.", {"DOPPLER VENOSO DE MEMBROS INFERIORES"}),
    ("Solicito ultrassom com doppler venoso de MMII.", {"DOPPLER VENOSO DE MEMBROS INFERIORES"}),
    ("Solicito novo RX de tórax, RX de ontem com velamento.", {"RAIO-X DE TÓRAX"}),
])
def test_r4_rules_usg_doppler_and_region_less_remention_are_one_item(text, expected):
    assert _rule_targets(text) == expected


def test_r4_rules_list_evidence_is_the_line_as_written():
    [finding] = apply_all_rules([StructuredNote("2026-08-21T10:00:00", None, "Médico clínico",
                                                "Exames solicitados:\n- TC de crânio")], "2026-08-09")
    assert finding.evidence == "TC de crânio"
