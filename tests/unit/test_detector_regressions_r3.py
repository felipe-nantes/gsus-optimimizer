"""PEND-001: regressões da TERCEIRA revisão adversarial da Fase 3 (DEC-129),
sobre o detector e as regras. Cada frase foi reproduzida por um revisor e
confirmada por um verificador. Todo texto clínico é fictício."""
from datetime import timedelta, timezone

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


def _exam(description="Aguarda realização de tomografia de crânio", **kwargs):
    kwargs.setdefault("subcategory", "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL")
    return _item("DIAGNOSTICO", description, **kwargs)


def _culture(description="Aguarda hemocultura", **kwargs):
    return _exam(description, subcategory="AGUARDA_ANATOMOPATOLOGICO_OU_CULTURA", **kwargs)


def _puncture(description="Aguarda passagem de CVC", **kwargs):
    return _item("PROCEDIMENTO_CIRURGIA", description, subcategory="OUTRO", **kwargs)


def _actions(item, text, role="Médico clínico", timestamp="2026-08-21T08:00:00"):
    return {f.action for f in detect_resolutions([item], [DetectorNote(timestamp, role, text)], local_tz=BRT)}


def _decide(item, notes):
    return detect_resolutions([item], [DetectorNote(*note) for note in notes], local_tz=BRT)


# ----------------------------------- R0: "ciente", "será avaliado" não é avaliação


@pytest.mark.parametrize("role,text", [
    ("Médico nefrologista", "Ciente do caso. Avaliaremos após a sessão de hemodiálise."),
    ("Médico nefrologista", "Interconsulta recebida. Paciente será avaliado amanhã pela equipe."),
    ("Médico nefrologista", "Recebida IC. Avaliação programada para amanhã cedo."),
    ("Médico nefrologista", "Ciente."),
    ("Médico nefrologista", "Ciente. Retornaremos à tarde."),
    ("Médico residente", "PELA NEFROLOGIA: Ciente. Passaremos à tarde para avaliação."),
    ("Médico residente", "PELA NEFROLOGIA: paciente será avaliado amanhã."),
    ("Médico residente", "PELA NEFROLOGIA: ciente."),
])
def test_r0_acknowledgment_or_planned_evaluation_never_closes(role, text):
    assert not (_actions(_consult(), text, role=role) & CLOSING)


def test_r0_real_evaluation_with_a_follow_up_still_closes():
    text = "Paciente avaliado. Não respondeu à expansão volêmica. Avaliaremos HD amanhã."
    assert _actions(_consult(), text, role="Médico nefrologista") == {ACTION_RESOLVE}


# ------------------------------- R1: espera da equipe que pediu, no fim do bloco


@pytest.mark.parametrize("text", [
    "PELA NEFROLOGIA: piora de função renal, IC enviada.",
    "PELA NEFROLOGIA: creatinina em ascensão (3,5), mantida hidratação, parecer solicitado hoje.",
    "PELA NEFRO: mantém oligúria, aguardando.",
    "PELA NEFROLOGIA: paciente com piora de escórias, mantém diurese, IC solicitada.",
])
def test_r1_requesting_team_pela_line_never_closes(text):
    assert not (_actions(_consult(), text) & CLOSING)


def test_r1_new_ai_item_whose_quote_is_the_request_line_is_not_dropped():
    item = {"category": "INTERCONSULTA", "subcategory": "SOLICITADA", "alvo": "NEFROLOGIA",
            "description": "Aguarda avaliação da nefrologia", "evidence": "PELA NEFROLOGIA: piora de função renal, IC enviada.",
            "source": "LLM"}
    assert not se.evidence_shows_done(item)


# ---------------------------- R2: a especialidade já evoluía (bloco PELA)


def test_r2_routine_pela_block_of_a_specialty_already_following_goes_to_review():
    item = _consult("Aguarda avaliação da nefrologia para confecção de fístula arteriovenosa")
    found = _decide(item, [
        ("2026-08-18T08:00:00", "Médico residente", "PELA NEFROLOGIA: HD realizada sem intercorrências. Manter esquema 3x/semana."),
        ("2026-08-20T08:00:00", "Médico clínico", "Solicito avaliação da nefrologia para confecção de fístula arteriovenosa."),
        ("2026-08-21T08:00:00", "Médico residente", "PELA NEFROLOGIA: HD realizada, UF 2 L. Manter esquema 3x/semana."),
    ])
    assert [f.action for f in found] == [ACTION_REVIEW]


# ------------------------------------------------- R3: cópia com contador novo


def test_r3_copied_exam_line_with_an_updated_day_counter_is_still_a_copy():
    item = _exam("Aguarda TC de crânio de controle")
    found = _decide(item, [
        ("2026-08-19T08:00:00", "Médico clínico", "# TCE grave (DIH 4): TC de crânio com hematoma subdural laminar à esquerda."),
        ("2026-08-20T08:00:00", "Médico clínico", "# TCE grave (DIH 5): TC de crânio com hematoma subdural laminar à esquerda.\n"
                                                  "Solicito TC de crânio de controle."),
        ("2026-08-21T08:00:00", "Médico clínico", "# TCE grave (DIH 6): TC de crânio com hematoma subdural laminar à esquerda."),
    ])
    assert not ({f.action for f in found} & CLOSING)


def test_r3_copied_pela_block_with_an_updated_day_counter_is_still_a_copy():
    item = _consult("Aguarda reavaliação da infectologia")
    found = _decide(item, [
        ("2026-08-18T08:00:00", "Médico residente", "PELA INFECTOLOGIA: D3 de meropenem, manter por 7 dias."),
        ("2026-08-19T08:00:00", "Médico clínico", "PELA INFECTOLOGIA: D4 de meropenem, manter por 7 dias."),
        ("2026-08-20T08:00:00", "Médico clínico", "PELA INFECTOLOGIA: D5 de meropenem, manter por 7 dias.\n"
                                                  "Novo pico febril, solicito reavaliação da infectologia."),
        ("2026-08-21T08:00:00", "Médico clínico", "PELA INFECTOLOGIA: D6 de meropenem, manter por 7 dias."),
    ])
    assert not ({f.action for f in found} & CLOSING)


# ---------------------------------- R4: depois de "realizado", outro assunto


@pytest.mark.parametrize("description,text", [
    ("Aguarda realização de tomografia de crânio", "TC de crânio realizada, retornou ao setor sem alterações."),
    ("Aguarda realização de tomografia de crânio", "TC de crânio realizada, sem alterações hemodinâmicas durante o exame."),
    ("Aguarda realização de tomografia de crânio", "TC de crânio realizada, saturando 94% em ar ambiente."),
    ("Aguarda realização de tomografia de crânio", "TC de crânio realizada, durante o transporte aumentada vazão de noradrenalina."),
    ("Aguarda realização de tomografia de crânio", "TC de crânio realizada na presença de familiar."),
    ("Aguarda realização de tomografia de crânio", "Paciente realizou TC de crânio. Sem alterações hemodinâmicas durante o transporte."),
    ("Aguarda realização de tomografia de crânio", "Realizada TC de crânio. Sem sangramento em sítio de punção."),
    ("Aguarda realização de tomografia de crânio", "TC de crânio realizada. Mostrou-se colaborativo durante o exame."),
    ("Aguarda RX de tórax", "Realizado RX de tórax. Evidenciada hiperemia em óstio de CVC."),
])
def test_r4_patient_or_transport_text_after_the_exam_is_not_its_result(description, text):
    assert not (_actions(_exam(description), text, role="Enfermeiro") & CLOSING)


# --------------------------------------- R5: plano/indicação junto do exame


@pytest.mark.parametrize("description,text", [
    ("Aguarda TC de crânio de controle", "Repetir TC de crânio de controle do hematoma."),
    ("Aguarda TC de crânio de controle", "Necessita TC de crânio de controle do hematoma subdural."),
    ("Aguarda TC de crânio de controle", "Providenciar TC de crânio de controle do hematoma."),
    ("Aguarda TC de crânio de controle", "Levar à TC de crânio de controle do hematoma após HD."),
    ("Aguarda TC de crânio de controle", "CONDUTA:\n- TC de crânio (TCE com hematoma occipital)"),
    ("Aguarda doppler venoso de MMII", "- Doppler venoso de MMII por edema assimétrico de MIE"),
    ("Aguarda TC de abdome", "- TC de abdome por dor abdominal e massa palpável em FID"),
    ("Aguarda ecocardiograma", "Discutido com cardiologia: ECO para reavaliar disfunção ventricular."),
    ("Aguarda RX de tórax", "- RX de tórax de controle da consolidação"),
])
def test_r5_plan_or_indication_line_is_not_a_result(description, text):
    assert not (_actions(_exam(description), text) & CLOSING)


@pytest.mark.parametrize("description,text", [
    ("Aguarda hemoculturas de controle", "Colher hemoculturas de controle (bacteremia por S. aureus)."),
    ("Aguarda hemoculturas de controle", "Repetir hemoculturas de controle por bacteremia por S. aureus."),
    ("Aguarda urocultura de controle", "- Urocultura de controle (ITU por E. coli tratada)"),
    ("Aguarda urocultura", "- Urocultura: EAS com nitrito positivo"),
])
def test_r5_culture_plan_or_other_test_is_not_a_result(description, text):
    assert not (_actions(_culture(description), text) & CLOSING)


# ------------------------------------------------- R6: material da cultura


def test_r6_culture_of_another_specimen_never_closes():
    assert not (_actions(_culture("Aguarda resultado de cultura de aspirado traqueal"),
                         "Cultura de ponta de CVC: sem crescimento.") & CLOSING)
    assert not (_actions(_culture("Aguarda cultura de secreção de ferida operatória"),
                         "Cultura de líquor negativa.") & CLOSING)


def test_r6_culture_of_the_same_specimen_closes():
    assert _actions(_culture("Aguarda resultado de cultura de aspirado traqueal"),
                    "Cultura de aspirado traqueal: sem crescimento.") == {ACTION_RESOLVE}


# ---------------------------------------- R7: cancelamento condicional/negado


@pytest.mark.parametrize("item,text", [
    (_consult(), "Caso mantenha boa diurese, sem necessidade de avaliação da nefrologia."),
    (_consult(), "Sem necessidade de parecer da nefrologia? Discutir com staff."),
    (_consult(), "Não foi cancelado o parecer da nefrologia, paciente sem melhora clínica."),
    (_consult(), "Dispensado parecer da nefrologia, não houve melhora clínica, discutir HD."),
    (_exam(), "Cancelada TC de crânio, paciente evoluiu sem melhora clínica, rediscutir."),
    (_exam(), "TC de crânio cancelada? Paciente sem melhora clínica."),
    (_exam(), "Se melhora clínica, TC de crânio suspensa."),
    (_puncture(), "Suspensa passagem de CVC, sem melhora clínica, rediscutir com staff."),
    (_puncture(), "Cancelada passagem de CVC caso evolua com melhora clínica."),
])
def test_r7_conditional_questioned_or_negated_cancel_is_never_cancel(item, text):
    assert ACTION_CANCEL not in _actions(item, text)


def test_r7_definitive_cancel_still_cancels():
    assert _actions(_consult(), "Suspenso o parecer da nefrologia por melhora clínica.") == {ACTION_CANCEL}


# ----------------------------------------------------- R8: "pai" não é PAI


@pytest.mark.parametrize("text", [
    "Pai orientado e boletim médico passado.", "Visita do pai realizada, orientado sobre quadro clínico.",
    "Contato telefônico com o pai realizado.",
])
def test_r8_father_never_closes_an_arterial_line_request(text):
    assert not (_actions(_puncture("Aguarda passagem de PAI"), text, role="Enfermeiro") & CLOSING)


def test_r8_father_sentence_does_not_drop_a_new_arterial_line_request():
    item = {"category": "PROCEDIMENTO_CIRURGIA", "subcategory": "OUTRO", "alvo": None,
            "description": "Aguarda passagem de PAI", "evidence": "x", "source": "LLM"}
    note = DetectorNote("2026-08-21T08:00:00", "Enfermeiro",
                        "Aguarda passagem de PAI pela equipe. Pai orientado e boletim médico passado.")
    assert not se.puncture_already_done(item, "Aguarda passagem de PAI pela equipe.", note)


# ---------------------------- R9: avaliação datada antes do pedido no bloco


def test_r9_pela_evaluation_dated_before_the_request_never_closes():
    assert not (_actions(_consult(), "PELA NEFROLOGIA: avaliado em 15/08, sem indicação de diálise naquele momento.")
                & CLOSING)


# --------------------------------- R10: admissão/diagnóstico repete o exame


@pytest.mark.parametrize("text", [
    "Admitido por TCE grave, TC de crânio evidenciou hematoma subdural.",
    "Internado por AVC isquêmico, TC de crânio com hipodensidade em território de ACM.",
    "HD: AVCi em território de ACM esquerda (TC de crânio com hipodensidade em território de ACM E).",
])
def test_r10_admission_or_diagnosis_line_is_history(text):
    assert not (_actions(_exam("Aguarda TC de crânio de controle"), text) & CLOSING)


# ---------------------------------------------- R11/R12: região e modalidade


@pytest.mark.parametrize("description,text", [
    ("Aguarda RX de joelho direito", "RX de ombro esquerdo: sem fraturas."),
    ("Aguarda doppler venoso de MMII", "Doppler venoso de MMSS: sem sinais de TVP."),
    ("Aguarda TC de coluna cervical", "TC de coluna lombar sem fraturas."),
    ("Aguarda doppler venoso de MIE", "Doppler venoso de MID: sem sinais de TVP."),
    ("Aguarda cateterismo cardíaco", "Realizado cateterismo vesical de alívio: presença de grumos e urina turva."),
    ("Aguarda cintilografia miocárdica", "Cintilografia óssea: lesões sugestivas de metástases em coluna."),
])
def test_r11_r12_other_limb_spine_segment_or_modality_never_closes(description, text):
    assert not (_actions(_exam(description), text, role="Enfermeiro") & CLOSING)


# ----------------------------------- R13: cancelamento de outra coisa


def test_r13_cancel_word_about_another_object_does_not_cancel_the_consult():
    assert ACTION_CANCEL not in _actions(_consult(), "Conforme avaliação da nefrologia suspensa a vancomicina por melhora clínica.")


# ----------------------------------------- R14: punção não ligada à inserção


@pytest.mark.parametrize("description,text", [
    ("Aguarda punção de AVP", "Realizada punção venosa periférica, colhidos exames laboratoriais."),
    ("Aguarda punção de AVP", "Realizada punção periférica, colhidas hemoculturas."),
    ("Aguarda punção de AVP", "AVP em MSE realizada medicação conforme prescrição."),
    ("Aguarda troca de CVC", "CVC em VSCD realizada troca de equipos."),
    ("Aguarda troca de CVC (hiperemia em óstio)", "CVC em VSCD locado, curativo oclusivo íntegro."),
])
def test_r14_lab_draw_or_line_care_never_closes_an_access_request(description, text):
    assert not (_actions(_puncture(description), text, role="Enfermeiro") & CLOSING)


# ----------------------------------- R15/R16: cabeçalho que não é cabeçalho


@pytest.mark.parametrize("text", [
    "PACIENTE MANTÉM OLIGÚRIA, AGUARDA AVALIAÇÃO\nPELA NEFROLOGIA. MANTIDA FUROSEMIDA EM BIC.",
    "SOLICITADO PARECER, A SER REALIZADO\nPELA NEFROLOGIA, MANTIDA HIDRATAÇÃO VENOSA.",
])
def test_r15_all_caps_wrapped_prose_is_not_a_pela_header(text):
    assert not (_actions(_consult(), text) & CLOSING)


@pytest.mark.parametrize("text", [
    "PELA CIRURGIA (VASCULAR): sem indicação de abordagem cirúrgica no momento, manter clopidogrel.",
    "PELA CIRURGIA - VASCULAR: avaliado, sem indicação cirúrgica, manter AAS.",
    "PELA CX/TORÁCICA: avaliado, manter dreno.",
])
def test_r16_qualified_surgery_header_is_not_general_surgery(text):
    assert not (_actions(_consult("Aguarda avaliação da cirurgia geral"), text, role="Médico residente") & CLOSING)


# ------------------------------------------- R17: conflito em outros formatos


@pytest.mark.parametrize("later", [
    "Pendências: Nefrologia - aguardando avaliação.", "Nefrologia: aguarda avaliação.", "Aguarda parecer (nefrologia).",
    "Aguardando nefrologia.", "Aguardamos a nefrologia para definir HD.", "IC nefrologia pendente.",
])
def test_r17_later_waiting_in_other_formats_is_conflicting(later):
    found = _decide(_consult(), [
        ("2026-08-21T08:00:00", "Médico nefrologista", "Paciente avaliado. Sem indicação de diálise."),
        ("2026-08-22T08:00:00", "Médico clínico", later),
    ])
    assert [f.action for f in found] == [ACTION_REVIEW]


def test_r17_puncture_still_awaited_after_the_success_is_conflicting():
    found = _decide(_puncture(), [
        ("2026-08-21T08:00:00", "Médico intensivista", "Realizada passagem de CVC em VJID sem intercorrências."),
        ("2026-08-22T08:00:00", "Médico clínico", "Ainda aguarda passagem de CVC."),
    ])
    assert [f.action for f in found] == [ACTION_REVIEW]


# ------------------------------------------------- R18: "não visto" estreito


@pytest.mark.parametrize("role,text", [
    ("Médico nefrologista", "Diurese ausente nas últimas 24h, ureia 210. Indicada hemodiálise hoje."),
    ("Médico cirurgião vascular", "Pulso pedioso ausente à esquerda, extremidade fria. Indicada arteriografia."),
    ("Médico cirurgião geral", "Paciente avaliado, abdome agudo obstrutivo. Indicada laparotomia exploradora no centro cirúrgico hoje."),
    ("Médico nefrologista", "Paciente avaliado. Em hemodiálise, euvolêmico no momento. Sem indicação de UF extra."),
    ("Médico nefrologista", "Paciente avaliado, dormindo, sem queixas. Sem indicação de diálise."),
])
def test_r18_ordinary_exam_wording_is_not_patient_not_seen(role, text):
    target = {"Médico cirurgião vascular": "vascular", "Médico cirurgião geral": "cirurgia geral"}.get(role, "nefrologia")
    item = _consult(f"Aguarda avaliação da {target}")
    assert _actions(item, text, role=role) == {ACTION_RESOLVE}


def test_r18_physio_sitting_out_of_bed_is_an_evaluation():
    item = _consult("Aguarda avaliação da fisioterapia")
    assert _actions(item, "Paciente colaborativo. Realizada sedestação fora do leito por 30 min, orientado.",
                    role="Fisioterapeuta") == {ACTION_RESOLVE}


# ---------------------------------- R19: especialista pedindo exame no bloco


@pytest.mark.parametrize("text", [
    "PELA INFECTOLOGIA: solicitados ECO TE e novas hemoculturas.",
    "PELA INFECTOLOGIA: aguardamos resultado das culturas para definir antibioticoterapia.",
    "PELA NEFROLOGIA: Solicitada avaliação por IRA. Paciente avaliado, sem indicação de diálise.",
])
def test_r19_pela_block_opening_with_an_order_is_at_least_review(text):
    item = _consult("Aguarda avaliação da infectologia" if "INFECTO" in text else "Aguarda avaliação da nefrologia")
    assert _actions(item, text, role="Médico residente") in ({ACTION_REVIEW}, {ACTION_RESOLVE})


def test_r19_specialist_wait_for_cultures_with_a_conduct_closes():
    item = _consult("Aguarda avaliação da infectologia")
    assert _actions(item, "PELA INFECTOLOGIA: aguardar culturas. Manter cefepime.", role="Médico residente") == {ACTION_RESOLVE}


def test_r19_requesting_team_wait_with_a_maintained_drug_does_not_close():
    item = _consult("Aguarda avaliação da infectologia")
    assert not (_actions(item, "PELA INFECTOLOGIA: aguarda avaliação. Mantém cefepime.") & CLOSING)


# ------------------------------------------ R20/R22/R26: resultados comuns


@pytest.mark.parametrize("description,text", [
    ("Aguarda TC de crânio", "TC de crânio evidenciou AVC isquêmico, solicitado parecer da neurologia."),
    ("Aguarda TC de crânio", "TC de crânio com hematoma subdural agudo, solicitada avaliação da neurocirurgia."),
    ("Aguarda TC de crânio", "TC de crânio sem alterações agudas, aguarda RM de crânio."),
    ("Aguarda TC de crânio", "TC de crânio sem sinais de sangramento, será iniciada anticoagulação."),
    ("Aguarda USG de abdome", "USG de abdome com colelitíase, sem sinais de colecistite."),
    ("Aguarda TC de abdome", "TC de abdome com pancreatite aguda edematosa."),
    ("Aguarda RX de tórax", "RX de tórax com velamento do seio costofrênico direito."),
    ("Aguarda TC de tórax", "TC de tórax: vidro fosco bilateral."),
    ("Aguarda doppler venoso de MMII", "Doppler venoso de MMII: sistema venoso profundo pérvio e compressível."),
    ("Aguarda ecocardiograma", "ECO: PSAP 55 mmHg, hipertensão pulmonar moderada."),
    ("Aguarda TC de crânio", "Realizada TC de crânio, sem alterações agudas."),
    ("Aguarda TC de crânio", "Realizou TC de crânio, sem sinais de sangramento."),
])
def test_r20_r22_r26_common_result_phrasings_close(description, text):
    assert _actions(_exam(description), text) == {ACTION_RESOLVE}


@pytest.mark.parametrize("description,text", [
    ("Aguarda hemocultura", "Hemocultura positiva para S. aureus MRSA, solicitado ECO TE."),
    ("Aguarda hemocultura", "Hemoculturas negativas, aguarda urocultura."),
])
def test_r20_culture_result_with_a_new_order_closes(description, text):
    assert _actions(_culture(description), text) == {ACTION_RESOLVE}


# ----------------------------------------- R21: "anterior" na anatomia


@pytest.mark.parametrize("description,text", [
    ("Aguarda cateterismo", "Cateterismo: lesão de 90% em artéria descendente anterior, indicada angioplastia."),
    ("Aguarda ecocardiograma", "Ecocardiograma: FE 35%, acinesia de parede anterior."),
    ("Aguarda TC de tórax", "TC de tórax com massa em mediastino anterior."),
    ("Aguarda endoscopia", "EDA: úlcera duodenal Forrest IIa, provável causa da HDA, realizada terapia endoscópica."),
    ("Aguarda TC de crânio de controle", "TC de crânio de controle: redução do hematoma em relação ao exame anterior."),
])
def test_r21_anatomy_and_comparison_words_are_not_history(description, text):
    assert _actions(_exam(description), text) == {ACTION_RESOLVE}


# ---------------------------------- R23: punção com "tentativa" ou "não houve"


@pytest.mark.parametrize("description,text", [
    ("Aguarda passagem de CVC", "Passagem de CVC em VJID guiada por USG, sem intercorrências."),
    ("Aguarda passagem de CVC", "Realizada passagem de CVC em VJID guiado por USG em primeira tentativa, sem intercorrências."),
    ("Aguarda passagem de CVC", "Passado CVC em VJID, não houve intercorrências."),
    ("Aguarda punção de AVP", "AVP em MSE puncionado com sucesso após 2 tentativas."),
])
def test_r23_puncture_success_with_attempt_count_or_no_complication_closes(description, text):
    assert _actions(_puncture(description), text, role="Médico intensivista") == {ACTION_RESOLVE}


def test_r23_case3_team_success_with_no_complication_closes():
    item = _consult("Aguarda avaliação para punção venosa")
    text = "Equipe de punção: atendimento realizado com sucesso, paciente não apresentou intercorrências."
    assert _actions(item, text, role="Enfermeiro") == {ACTION_RESOLVE}


def test_r23_attempt_without_success_still_never_closes():
    assert not (_actions(_puncture(), "Tentativa de passagem de CVC em VJID, realizada punção arterial inadvertida.")
                & CLOSING)


# ------------------------------- R24/R27: resposta curta ou com motivo do pedido


@pytest.mark.parametrize("role,text", [
    ("Médico nefrologista", "Solicitado parecer da nefrologia por IRA. Paciente avaliado, sem indicação de diálise no momento."),
    ("Médico cardiologista", "Paciente avaliado, FA de alta resposta. Iniciada amiodarona. Solicito avaliação da equipe de cuidados paliativos."),
    ("Médico nefrologista", "Realizada avaliação nefrológica. Paciente com IRA pré-renal, hidratação venosa."),
    ("Médico nefrologista", "Paciente sem critérios para diálise de urgência no momento."),
    ("Médico nefrologista", "Solicito USG de rins e vias urinárias e gasometria venosa. Retorno com resultados."),
    ("Médico cardiologista", "Risco cardiológico intermediário (Lee 2). Pode prosseguir com a cirurgia."),
])
def test_r24_r27_short_answers_and_transcribed_reasons_close(role, text):
    target = "cardiologia" if "cardio" in role else "nefrologia"
    assert _actions(_consult(f"Aguarda avaliação da {target}"), text, role=role) == {ACTION_RESOLVE}


def test_r24_specialist_asking_its_own_specialty_still_goes_to_review():
    text = "Solicito parecer da nefrologia (staff) para definir HD."
    assert _actions(_consult(), text, role="Médico nefrologista") == {ACTION_REVIEW}


# --------------------------------------- R25: "Pela Nefrologia:" em título


def test_r25_title_case_header_after_an_unpunctuated_line_counts():
    text = "INTERCONSULTA\nPela Nefrologia: paciente avaliado, sem indicação de diálise."
    assert _actions(_consult(), text, role="Médico residente") == {ACTION_RESOLVE}


def test_r25_title_case_continuation_of_prose_does_not_count():
    text = "Paciente foi avaliado\nPela Nefrologia: sem indicação de diálise."
    assert not (_actions(_consult(), text, role="Médico residente") & CLOSING)


# ------------------------------------- R29: linha de outra especialidade


def test_r29_another_specialty_line_absorbed_in_the_block_is_not_a_conflict():
    text = "CONDUTA:\nPELA INFECTOLOGIA: manter vancomicina D5.\nCardio: aguarda avaliação."
    assert _actions(_consult("Aguarda avaliação da infectologia"), text, role="Médico residente") == {ACTION_RESOLVE}


# ---------------------------------------------------------- R30: regiões


def test_r30_region_right_after_the_colon_and_combined_regions():
    assert _actions(_exam("Aguarda TC de crânio"), "TC: crânio sem alterações agudas.") == {ACTION_RESOLVE}
    assert _actions(_exam("Aguarda TC de abdome"), "TC de tórax e abdome: sem alterações.") == {ACTION_RESOLVE}
    assert requested_exams("Solicito TC de crânio, tórax e abdome.") == [
        "TOMOGRAFIA DE CRÂNIO", "TOMOGRAFIA DE TÓRAX", "TOMOGRAFIA DE ABDOME"]


# ------------------------------------------------------ R31: trecho inteiro


def test_r31_long_quote_with_an_invented_ending_is_not_in_the_notes():
    boil = ("Paciente em REG, lucido e orientado, eupneico em ar ambiente, corado, hidratado, anicterico, acianotico, "
            "afebril, diurese presente")
    blob = se.notes_blob([boil + ". Mantem piora respiratoria. Aguarda vaga de UTI."])
    assert not se.quote_in_notes(boil + ", transferido para a UTI as 10h.", blob)


# ---------------------------------------------------- R43–R47: regras


def test_r43_a_date_tied_to_one_request_is_not_given_to_another():
    [cardio, neuro] = sorted(apply_all_rules([StructuredNote(
        "2026-08-21T09:00:00", None, "Médico clínico",
        "Reitero parecer da cardiologia solicitado em 15/08 e solicito parecer da neurologia.")], "2026-08-10"),
        key=lambda finding: finding.alvo)
    assert (cardio.evidence_date, neuro.evidence_date) == ("2026-08-21T09:00:00", "2026-08-21T09:00:00")


@pytest.mark.parametrize("sentence", [
    "Parecer da cardiologia: sem indicação de cateterismo, solicitado ECO.",
    "Resposta da nefrologia: indicada hemodiálise, solicitado cateter de Shilley.",
    "Visita da infectologia: trocado antibiótico, solicitadas hemoculturas.",
    "Avaliação da cardiologia sem indicação de intervenção, solicitado Holter.",
])
def test_r44_reported_answer_plus_another_order_is_not_a_consult_request(sentence):
    assert consult_targets_requested(sentence) == []


@pytest.mark.parametrize("sentence,expected", [
    ("Solicitada avaliação da nefrologia, ainda não avaliado.", [sp.NEFROLOGIA]),
    ("Solicitada avaliação da cardiologia, aguarda ser avaliado.", [sp.CARDIOLOGIA]),
    ("Solicitada avaliação da cirurgia geral para paciente avaliada no PS com dor abdominal.", [sp.CIRURGIA_GERAL]),
    ("Paciente avaliado à beira leito e solicitada avaliação da nefrologia.", [sp.NEFROLOGIA]),
    ("Solicito parecer da cardiologia, paciente já avaliado pela nefrologia ontem.", [sp.CARDIOLOGIA]),
    ("solicitado parecer da nefro, avaliado ontem", []),
])
def test_r45_answered_check_only_right_after_the_request(sentence, expected):
    assert consult_targets_requested(sentence) == expected


@pytest.mark.parametrize("text,expected", [
    ("Exames solicitados:\n- Tomografia de crânio\n- Ultrassom de abdome", {"TOMOGRAFIA DE CRÂNIO", "ULTRASSOM DE ABDOME"}),
    ("Solicitado ecocardiograma de controle, último sem alterações.", {"ECOCARDIOGRAMA"}),
    ("Solicitados hemograma, ureia, creatinina, sódio, potássio, magnésio, cálcio, TGO, TGP, bilirrubinas e "
     "tomografia de abdome.", {"TOMOGRAFIA DE ABDOME"}),
    ("Paciente não solicita alta, solicitada tomografia de crânio.", {"TOMOGRAFIA DE CRÂNIO"}),
    ("Nefrologia solicitou USG de rins e vias urinárias.", {"ULTRASSOM DE RINS E VIAS URINÁRIAS"}),
])
def test_r46_common_request_formats_create_items(text, expected):
    findings = apply_all_rules([StructuredNote("2026-08-21T10:00:00", None, "Médico clínico", text)], "2026-08-09")
    assert {finding.alvo for finding in findings} == expected


@pytest.mark.parametrize("sentence", [
    "Na admissão, solicitada TC de crânio que evidenciou AVC isquêmico.",
    "Solicitado ECO que mostrou FE de 30%.",
    "Solicitada TC de tórax por dispneia, evidenciando TEP bilateral.",
    "Solicitado USG de abdome que veio normal.",
    "Realizada TC de crânio solicitada pela neurologia.",
    "Familiar solicita resultado da TC de crânio.",
])
def test_r47_history_or_result_sentences_are_not_requests(sentence):
    assert requested_exams(sentence) == []
