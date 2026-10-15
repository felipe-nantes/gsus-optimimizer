"""PEND-001: regressões da SEGUNDA revisão adversarial da Fase 3 (DEC-129).
Cada frase foi reproduzida por um revisor fechando (ou cancelando) uma
pendência que NÃO estava resolvida -- o pior erro para o auditor -- ou
deixando de reconhecer um caso comum que deveria fechar. Todo texto clínico
é fictício."""
from datetime import date, timedelta, timezone

import pytest

from app.analysis import specialties as sp
from app.analysis import specialty_evaluation as se
from app.analysis.rules import StructuredNote, apply_all_rules, consult_targets_requested, requested_exams
from app.analysis.specialty_evaluation import (
    ACTION_CANCEL, ACTION_RESOLVE, ACTION_REVIEW, DetectorNote, DetectorSession, detect_resolutions,
)

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


# ------------------------------------------- C0: bloco PELA da equipe que pediu


@pytest.mark.parametrize("text", [
    "CONDUTA:\nPELA NEFRO: parecer solicitado", "PELA NEFROLOGIA: solicitado", "PELA NEFROLOGIA: aguarda ser avaliado",
    "PELA NEFROLOGIA: a avaliar", "PELA NEFROLOGIA: IC enviada", "PELA NEFROLOGIA: feito pedido de parecer",
    "PELA NEFROLOGIA: acionada, virá avaliar", "PELA NEFROLOGIA: ciente da IC, veremos o paciente à tarde",
    "PELA NEFRO: aguardo retorno", "PLANO:\nPELA NEFROLOGIA:\n- aguarda avaliação",
    "PELA NEFROLOGIA:\n\nPaciente em hemodiálise no momento, retornaremos.",
    "PELA NEFROLOGIA\n- Ciente.\n- Paciente fora do leito, retornaremos.",
    "Paciente com piora de escórias, aguarda avaliação\npela nefrologia.",
])
def test_c0_pela_block_that_is_waiting_or_not_seen_never_closes(text):
    assert not (_actions(_consult(), text) & CLOSING)


# --------------------------------- C1/C21: evolução assinada pela especialidade


@pytest.mark.parametrize("text", [
    "Paciente não se encontrava no leito. Retorno mais tarde.",
    "Paciente em hemodiálise no momento, passaremos à tarde.",
    "Paciente no centro cirúrgico. Reavaliaremos amanhã.",
    "Paciente encontra-se em exame de imagem, voltaremos após.",
    "Ciente do pedido de avaliação. Paciente dormindo, passaremos novamente.",
    "Tentativa de avaliação sem sucesso: paciente em procedimento.",
    "Paciente ausente no quarto no momento da visita.",
    "Evolução UTI. Aguarda parecer da nefrologia.",
    "Solicito parecer da nefrologia (staff) para definir HD.",
    "Sessão de hemodiálise sem intercorrências. UF 2000 mL.",
])
def test_c1_specialist_note_without_an_evaluation_only_goes_to_review(text):
    assert _actions(_consult(), text, role="Médico nefrologista") == {ACTION_REVIEW}


@pytest.mark.parametrize("role,text", [
    ("Médico nefrologista", "Paciente avaliado. Ainda não há indicação de terapia renal substitutiva. Seguimos acompanhando."),
    ("Médico nefrologista", "Interconsulta recebida, paciente avaliado. Iniciar bicarbonato."),
    ("Médico nefrologista", "Paciente avaliado. Não respondeu à expansão volêmica. Avaliaremos HD amanhã."),
])
def test_c21_real_specialist_evaluations_close(role, text):
    assert _actions(_consult(), text, role=role) == {ACTION_RESOLVE}


def test_c1_later_no_show_notes_make_the_false_role_closure_conflicting():
    item = _consult(evidence_date="2026-08-20T10:00:00")
    found = _decide(item, [
        ("2026-08-21T08:00:00", "Médico nefrologista", "Paciente avaliado. Sem indicação de diálise."),
        ("2026-08-22T08:00:00", "Médico clínico", "Nefrologia não passou até o momento."),
    ])
    assert [f.action for f in found] == [ACTION_REVIEW]


# ---------------------------------------- C2/C3/C4/C11/C17: resultado de exame


@pytest.mark.parametrize("text", [
    "Paciente encaminhado à TC de crânio, realizado exame sem intercorrências, retornou ao leito sem sinais de "
    "desconforto respiratório.",
    "Realizada TC de crânio. Sem sinais de desconforto respiratório.",
    "Realizada TC de crânio, paciente com padrão respiratório normal.",
    "Paciente mostrou-se agitado durante a TC de crânio, interrompido o exame.",
    "TC de crânio realizada, RX de tórax sem alterações.",
    "Paciente foi à TC de crânio, ECG sem alterações.",
    "Neuro sem alterações, TC de crânio realizada.",
    "TC de crânio com contraste liberada pela nefrologia, função renal normal.",
    "TC de crânio: dia 22.", "TC de crânio: paciente instável para transporte.", "TC de crânio: se piora neurológica.",
    "TC de crânio: após hemodiálise.", "TC de crânio: em fila.", "TC de crânio: contato com radiologia.",
    "TC de crânio: depende de vaga no transporte.", "TC de crânio: para descartar hematoma.",
    "TC de crânio realizada, laudo: em confecção.", "TC de crânio realizada. Laudo: a liberar.",
    "TC de crânio realizada. Resultado: em processamento.", "TC de crânio realizada; laudo em digitação.",
])
def test_c2_c4_c11_exam_result_must_be_the_exam_s_own(text):
    assert not (_actions(_exam(), text) & CLOSING)


@pytest.mark.parametrize("description,text", [
    ("Aguarda TC de tórax", "TC de crânio sem sinais de sangramento, mantém dreno de tórax à direita."),
    ("Aguarda TC de abdome", "TC de crânio sem alterações, paciente com dor abdominal difusa."),
])
def test_c3_region_comes_from_the_exam_s_own_mention(description, text):
    assert not (_actions(_exam(description), text) & CLOSING)


def test_c11_echo_conclusion_pending_is_not_a_result():
    item = _exam("Aguarda ecocardiograma")
    assert not (_actions(item, "Ecocardiograma realizado. Conclusão: a ser emitida pelo cardiologista.") & CLOSING)


@pytest.mark.parametrize("description,text", [
    ("Aguarda realização de tomografia de crânio", "TC de cranio com AVC isquemico em territorio de ACM esquerda."),
    ("Aguarda RX de tórax", "RX de torax com consolidacao em base direita."),
    ("Aguarda ultrassom de abdome", "USG abdome total com esteatose hepatica."),
    ("Aguarda ecocardiograma", "ECO com disfuncao sistolica grave do VE."),
    ("Aguarda doppler venoso de MMII", "Doppler venoso de MMII com TVP em veia poplitea esquerda."),
    ("Aguarda doppler venoso de MMII", "Doppler venoso de MMII negativo para TVP."),
    ("Aguarda endoscopia", "EDA com ulcera gastrica Forrest III."),
    ("Aguarda realização de tomografia de crânio", "Exames: TC de cranio sem alteracoes agudas."),
    ("Aguarda realização de tomografia de crânio", "TC de cranio realizada hoje as 10h: sem sinais de sangramento."),
    ("Aguarda realização de tomografia de crânio", "TC de crânio realizada, sem sinais de sangramento."),
    ("Aguarda realização de tomografia de crânio", "TC de crânio com hematoma subdural, indicada drenagem."),
])
def test_c17_common_abnormal_result_phrasings_close(description, text):
    assert _actions(_exam(description), text) == {ACTION_RESOLVE}


# ------------------------------------------------- C5: anatomopatológico


@pytest.mark.parametrize("text", [
    "Realizada biópsia de lesão vegetante sugestiva de neoplasia, material enviado para anatomopatológico.",
    "Realizada EDA com biópsias de lesão suspeita para malignidade.",
    "Biópsia: material encaminhado ao patológico.", "Anatomopatológico: peça enviada.", "Anatomopatológico: em análise.",
])
def test_c5_biopsy_procedure_note_never_closes_the_pathology_result(text):
    assert not (_actions(_culture("Aguarda resultado de anatomopatológico"), text) & CLOSING)


@pytest.mark.parametrize("text", [
    "Anatomopatológico: adenocarcinoma moderadamente diferenciado.",
    "Resultado do AP: carcinoma espinocelular.",
    "Biópsia evidenciou processo inflamatório crônico, negativo para malignidade.",
])
def test_c5_explicit_pathology_result_closes(text):
    assert _actions(_culture("Aguarda resultado de anatomopatológico"), text) == {ACTION_RESOLVE}


# ------------------------------------------------------------- C6: cultura


@pytest.mark.parametrize("description,text", [
    ("Aguarda hemocultura", "Hemocultura positiva, em identificação."),
    ("Aguarda hemocultura", "Hemoculturas negativas com 48 horas de incubação."),
    ("Aguarda hemocultura", "Hemocultura negativa em 72 horas."),
    ("Aguarda hemocultura", "Hemocultura 1/2 com cocos Gram positivos em cachos."),
    ("Aguarda urocultura", "EAS com nitrito positivo e urocultura enviada."),
    ("Aguarda urocultura", "Hemoculturas negativas, urocultura em curso."),
])
def test_c6_partial_or_other_culture_result_never_closes(description, text):
    assert not (_actions(_culture(description), text) & CLOSING)


@pytest.mark.parametrize("description,text", [
    ("Aguarda urocultura", "Urocultura com crescimento de E. coli > 100.000 UFC/ml."),
    ("Aguarda urocultura", "Urocultura: E. coli sensível a ceftriaxona."),
    ("Aguarda hemocultura", "Hemoculturas negativas (5 dias)."),
])
def test_c6_final_culture_result_closes(description, text):
    assert _actions(_culture(description), text) == {ACTION_RESOLVE}


# ------------------------------------------------------------- C7: punção


@pytest.mark.parametrize("description,text", [
    ("Aguarda passagem de CVC", "Instalado cateter nasal de O2 a 3 L/min."),
    ("Aguarda passagem de CVC", "Passado cateter vesical de demora sem intercorrências."),
    ("Aguarda passagem de CVC", "Realizada punção venosa periférica em MSE."),
    ("Aguarda passagem de CVC", "Puncionado acesso venoso periférico em MSD."),
    ("Aguarda passagem de cateter de Shilley para hemodiálise", "Passado CVC em VSCD para infusão de noradrenalina."),
    ("Aguarda passagem de cateter de Shilley para hemodiálise",
     "Paciente com CVC em VJD passado no PS, aguarda Shilley para HD."),
    ("Aguarda passagem de PICC", "Passado CVC em VJID guiado por USG."),
    ("Aguarda punção liquórica", "Realizada paracentese de alívio, 3 litros."),
    ("Aguarda nova punção", "Realizada punção de medula óssea."),
])
def test_c7_puncture_of_another_type_never_closes(description, text):
    assert not (_actions(_puncture(description), text, role="Técnico de enfermagem") & CLOSING)


def test_c7_generic_puncture_request_only_goes_to_review():
    assert _actions(_puncture("Aguarda nova punção"), "Puncionado AVP em MSE.") == {ACTION_REVIEW}


def test_c7_father_is_not_an_arterial_line():
    assert se.puncture_target("Aguarda o pai do paciente para assinar o termo") is None


# ----------------------------------------------------------- C8: cancelamento


@pytest.mark.parametrize("item,text", [
    (_consult(), "Parecer da nefrologia suspenso por ora."),
    (_consult(), "Por enquanto dispensado parecer da nefrologia."),
    (_consult(), "Cancelado parecer da nefrologia em duplicidade."),
    (_consult(), "No momento sem necessidade de parecer da nefrologia, reavaliar se piora da função renal."),
    (_exam(), "TC de crânio suspensa no momento."),
    (_exam(), "TC de crânio suspensa devido agitação psicomotora."),
    (_exam(), "Cancelado pedido de TC de crânio em duplicidade."),
    (_exam(), "Cancelada TC de crânio com contraste por alergia ao iodo, será realizada sem contraste."),
    (_exam(), "TC de crânio suspensa por enquanto (paciente em VM)."),
    (_puncture(), "Suspensa passagem de CVC pela equipe da UTI, será realizada pela cirurgia vascular."),
])
def test_c8_suspension_without_a_definitive_reason_is_never_cancelled(item, text):
    assert ACTION_CANCEL not in _actions(item, text)


# ------------------------------------------- C9/C24: decisão da IA x conflito


def test_c9_ai_closure_needs_the_detector_s_decision_including_the_conflict_check():
    item = _consult(evidence_date="2026-08-10T08:00:00")
    notes = [
        DetectorNote("2026-08-10T08:00:00", "Médico clínico", "Solicito avaliação da nefrologia."),
        DetectorNote("2026-08-12T10:00:00", "Médico nefrologista", "Avaliado. Sem indicação de diálise no momento."),
        DetectorNote("2026-08-15T08:00:00", "Médico clínico",
                     "Reitero solicitação de parecer da nefrologia. Paciente ainda não avaliado."),
    ]
    session = DetectorSession(notes, local_tz=BRT)
    assert session.decide(item).action == ACTION_REVIEW
    assert session.agrees(item, ACTION_RESOLVE) is False


def test_c9_exam_new_request_after_a_result_is_not_a_conflict_on_the_old_one():
    item = _exam(evidence_date="2026-08-10T08:00:00")
    found = _decide(item, [
        ("2026-08-12T08:00:00", "Médico clínico", "TC de crânio sem sinais de sangramento."),
        ("2026-08-15T08:00:00", "Médico clínico", "Aguarda nova TC de crânio de controle."),
    ])
    assert [f.action for f in found] == [ACTION_RESOLVE]


# ------------------------------------------------------------ C10: datas


@pytest.mark.parametrize("text", [
    "Ecocardiograma de 03/2025: FE 30%, hipocinesia difusa.",
    "ICC com FE 30% (ECO 2025). Solicito novo ECO.",
])
def test_c10_old_echo_is_history(text):
    assert not (_actions(_exam("Aguarda ecocardiograma"), text, role="Médico cardiologista") & CLOSING)


@pytest.mark.parametrize("text", [
    "TC de crânio do dia 15: sem sinais de sangramento.", "TC de crânio (15-08): sem sinais de sangramento.",
    "TC de crânio da entrada sem sinais de sangramento.", "TC de crânio inicial sem sinais de sangramento.",
    "TC de crânio do PS sem sinais de sangramento.", "TC de crânio realizada em outro serviço sem sinais de sangramento.",
    "TC de crânio (20/08): hematoma subdural laminar à esquerda.",  # dia do pedido: pode ser anterior a ele
])
def test_c10_stale_or_same_day_exam_result_never_closes(text):
    assert not (_actions(_exam(), text) & CLOSING)


@pytest.mark.parametrize("text", ["Hemocultura 15/08: negativa.", "Hemocultura de 15/08 positiva para S. aureus.",
                                  "HMC 15/08: sem crescimento."])
def test_c10_culture_dated_before_the_request_is_history(text):
    assert not (_actions(_culture("Aguarda hemocultura de controle"), text) & CLOSING)


def test_c10_date_parsing():
    note_day = date(2026, 8, 21)
    assert se.cited_dates("eco de 03/2025: fe 30%", note_day) == [date(2025, 3, 31)]
    assert se.cited_dates("hmc 15/08: negativa", note_day) == [date(2026, 8, 15)]
    assert se.cited_dates("hemoculturas 2/2 positivas", note_day) == []
    assert se.cited_dates("cultura de 15/12", note_day) == [date(2025, 12, 15)]  # meses à frente: ano anterior
    assert se.cited_dates("tc do dia 15: sem sangramento", note_day) == [date(2026, 8, 15)]
    assert se.cited_dates("dia 3 de meropenem", note_day) == []


# --------------------------------------------- C12: conflito na mesma evolução


def test_c12_re_request_in_the_same_note_is_conflicting():
    text = "PELA NEFROLOGIA: sem indicação de HD no momento.\nCONDUTA: reitero solicitação de parecer da nefrologia."
    assert _actions(_consult(), text) == {ACTION_REVIEW}


def test_c12_note_with_the_same_timestamp_is_checked_too():
    found = _decide(_consult(), [
        ("2026-08-21T08:00:00", "Médico nefrologista", "Avaliado. Sem indicação de diálise."),
        ("2026-08-21T08:00:00", "Médico clínico", "Paciente ainda não avaliado pela nefrologia. Reitero parecer."),
    ])
    assert [f.action for f in found] == [ACTION_REVIEW]


# -------------------------------------------------- C13: modalidade de exame


def test_c13_echo_te_and_angio_mri_are_their_own_exams():
    assert se.exam_target("Aguarda ECO TE para pesquisa de endocardite") == "ECOCARDIOGRAMA TRANSESOFÁGICO"
    assert not (_actions(_exam("Aguarda ECO TE"), "ECO TT: FE 55%, sem vegetações.") & CLOSING)
    assert se.exam_target("Aguarda angio-RM de crânio") == "ANGIORRESSONÂNCIA DE CRÂNIO"
    assert not (_actions(_exam("Aguarda angio-RM de crânio"), "RM de crânio: sem lesões isquêmicas agudas.") & CLOSING)


# --------------------------------------------- C14/P0: especialidade certa


@pytest.mark.parametrize("description", [
    "Aguarda avaliação da cirurgia maxilofacial para fratura de mandíbula",
    "Aguarda avaliação da traumatologia bucomaxilofacial",
])
def test_c14_maxillofacial_is_not_general_surgery_nor_orthopedics(description):
    assert se.kind_and_target(_consult(description)) == (se.KIND_CONSULT, sp.BUCOMAXILOFACIAL)


@pytest.mark.parametrize("text", ["cirurgia ginecológica", "cirurgia endovascular", "cirurgia endócrina", "cirurgia robótica"])
def test_c14_qualified_surgery_is_not_general_surgery(text):
    assert sp.specialties_in_text(text) != [sp.CIRURGIA_GERAL]


def test_c14_general_surgery_not_closed_by_a_maxillofacial_pela_line():
    item = _consult("Aguarda avaliação da cirurgia geral")
    assert not (_actions(item, "PELA CIRURGIA MAXILOFACIAL: fratura de mandíbula, programar fixação.",
                         role="Médico residente") & CLOSING)


@pytest.mark.parametrize("description", [
    "Aguarda avaliação da cardiologia intervencionista para cateterismo",
    "Aguarda avaliação da ortopedia (grupo de mão)",
])
def test_p0_subgroup_consult_has_no_parent_specialty_target(description):
    assert se.kind_and_target(_consult(description)) is None


def test_p0_ward_doctor_role_never_closes_internal_medicine_consult():
    item = _consult("Aguarda parecer da clínica médica para compensação de DM")
    assert not (_actions(item, "Plantão noturno: chamado por febre 38,5. Colhidas hemoculturas, iniciada dipirona.",
                         role="Médico clínico") & CLOSING)


def test_p0_primary_team_routine_note_goes_to_review():
    item = _consult("Aguarda avaliação da nefrologia para confecção de fístula arteriovenosa",
                    evidence_date="2026-08-20T10:00:00")
    found = _decide(item, [
        ("2026-08-18T08:00:00", "Médico nefrologista", "Sessão de hemodiálise sem intercorrências. Mantém esquema."),
        ("2026-08-21T08:00:00", "Médico nefrologista", "Hemodiálise sem intercorrências. Mantém esquema."),
    ])
    assert [f.action for f in found] == [ACTION_REVIEW]


# --------------------------------------------------- C15: conflito estreito


@pytest.mark.parametrize("later", [
    "Aguarda USG de rins solicitado pela nefrologia.",
    "Avaliado pela nefrologia, que solicitou USG de rins.",
    "Pendencias: - parecer nefrologia (solicitado 20/08)",
    "PELA NEFROLOGIA: aguardamos USG de rins.",
    "Segue em acompanhamento com a nefrologia, aguardando vaga de hemodialise.",
])
def test_c15_follow_on_orders_are_not_conflicting(later):
    found = _decide(_consult(evidence_date="2026-08-20T08:00:00"), [
        ("2026-08-21T10:00:00", "Médico nefrologista", "Paciente avaliado. Sem indicacao de dialise. Sugiro USG de rins."),
        ("2026-08-21T15:00:00", "Médico clínico", later),
    ])
    assert [f.action for f in found] == [ACTION_RESOLVE]


def test_c15_clean_proof_after_a_conflict_closes():
    found = _decide(_consult(evidence_date="2026-08-10T08:00:00"), [
        ("2026-08-12T10:00:00", "Médico nefrologista", "Avaliado. Sem indicação de diálise."),
        ("2026-08-15T08:00:00", "Médico clínico", "Reitero solicitação de parecer da nefrologia."),
        ("2026-08-16T10:00:00", "Médico nefrologista", "Reavaliado. Indicada hemodiálise amanhã."),
    ])
    assert [(f.action, f.evidence_date) for f in found] == [(ACTION_RESOLVE, "2026-08-16T10:00:00")]


def test_c15_new_area_in_an_exam_result_is_not_waiting():
    found = _decide(_exam(evidence_date="2026-08-20T10:00:00"), [
        ("2026-08-21T14:00:00", "Médico clínico", "TC de cranio: AVC isquemico em territorio de ACM."),
        ("2026-08-21T20:00:00", "Médico clínico", "TC de cranio com nova area isquemica."),
    ])
    assert [f.action for f in found] == [ACTION_RESOLVE]


# ------------------------------------------------ C16: caso 3 em outras palavras


@pytest.mark.parametrize("text", [
    "Equipe de puncao: atendimento realizado com sucesso. Paciente aguarda resultado de exames.",
    "Acionada equipe de puncao, atendimento realizado com sucesso.",
    "Equipe de puncao realizou AVP em MSD.",
    "Equipe de puncao puncionou AVP em MSE, jelco 22.",
    "EQUIPE DE PUNCAO\nAtendimento realizado com sucesso.",
    "Atendido pela equipe de puncao, acesso periferico em MSE.",
])
def test_c16_puncture_team_success_closes(text):
    item = _consult("Aguarda avaliação para punção venosa")
    assert _actions(item, text, role="Enfermeiro", timestamp="2026-08-20T16:00:00") == {ACTION_RESOLVE}


def test_c16_team_success_closes_a_typed_request_too():
    item = _consult("Aguarda equipe de punção para acesso venoso periférico")
    assert _actions(item, "Equipe de puncao: atendimento realizado com sucesso.", role="Enfermeiro") == {ACTION_RESOLVE}


# --------------------------------------------- C18/C19/C35: bloco PELA real


@pytest.mark.parametrize("text", [
    "PELA INFECTO: paciente avaliado, em uso de pip-tazo D3, hemoculturas pendentes. Manter esquema.",
    "PELA INFECTOLOGIA: Paciente avaliado. Manter vancomicina, reavaliacao em 48h.",
    "PELA INFECTOLOGIA\nPaciente avaliado a beira leito, ainda nao ha indicacao de escalonar antibiotico.",
    "PELA INFECTOLOGIA\nSugerimos manter ceftriaxona. Retornaremos em 48h.",
    "PELA INFECTO: HMC de 18/08 com S. aureus MRSA. Iniciar vancomicina.",
    "PELA INFECTO: HD: ITU? Pielonefrite? Iniciar ceftriaxona.",
    "PELA INFECTO: sugiro desbridamento, solicito avaliacao da cirurgia vascular.",
    "PELA INFECTOLOGIA\nInterconsulta recebida, paciente avaliado. Iniciar meropenem.",
])
def test_c18_specialist_pela_evaluations_close(text):
    item = _consult("Aguarda avaliação da infectologia")
    assert _actions(item, text, role="Médico residente") == {ACTION_RESOLVE}


def test_c19_c35_second_consult_with_a_new_pela_block_is_not_stale_but_goes_to_review():
    """O cabeçalho repetido não torna o bloco novo uma cópia (2ª revisão).
    Mas a especialidade já evoluía o paciente antes deste pedido: a 3ª
    revisão mostrou a evolução de rotina dela (hemodiálise diária) fechando
    a interconsulta NOVA -- vai para verificação, nunca concluída."""
    item = _consult(evidence_date="2026-08-24T08:00:00")
    found = _decide(item, [
        ("2026-08-21T08:00:00", "Médico residente", "PELA NEFROLOGIA\nSem indicacao de dialise."),
        ("2026-08-24T08:00:00", "Médico clínico", "Solicito avaliacao da nefrologia."),
        ("2026-08-25T08:00:00", "Médico residente", "PELA NEFROLOGIA\nIndicada hemodialise, passar cateter hoje."),
    ])
    assert [(f.action, f.evidence_date) for f in found] == [(ACTION_REVIEW, "2026-08-25T08:00:00")]
    assert "já evoluía" in found[0].note


def test_c19_copy_of_the_whole_old_pela_block_is_still_history():
    item = _consult(evidence_date="2026-08-24T08:00:00")
    block = "PELA NEFROLOGIA\nSem indicacao de dialise."
    found = _decide(item, [
        ("2026-08-21T08:00:00", "Médico residente", block),
        ("2026-08-25T08:00:00", "Médico residente", block),
    ])
    assert not ({f.action for f in found} & CLOSING)


# ------------------------------------------------------------- C20/C22: regras


@pytest.mark.parametrize("sentence,expected", [
    ("Solicitado hemograma, amilase e tomografia de abdome.", ["TOMOGRAFIA DE ABDOME"]),
    ("Solicitados exames laboratoriais e ultrassom de abdome.", ["ULTRASSOM DE ABDOME"]),
    ("Solicito laboratorio, hemoculturas e RX de torax.", ["RAIO-X DE TÓRAX"]),
    ("Solicito RNM de cranio para melhor elucidacao de lesao evidenciada na TC", ["RESSONÂNCIA DE CRÂNIO"]),
    ("Solicito TC de abdome para complementar USG que mostrou colecao", ["TOMOGRAFIA DE ABDOME"]),
    ("Solicitar tomografia de cranio.", ["TOMOGRAFIA DE CRÂNIO"]),
])
def test_c20_requested_exams(sentence, expected):
    assert requested_exams(sentence) == expected


@pytest.mark.parametrize("sentence,expected", [
    ("Paciente avaliado a beira leito, com piora da funcao renal, solicitada avaliacao da nefrologia.", [sp.NEFROLOGIA]),
    ("Realizado ECG com supra de ST em parede inferior, solicitada avaliacao da cardiologia.", [sp.CARDIOLOGIA]),
    ("CD: solicitar parecer da cardiologia", [sp.CARDIOLOGIA]),
    ("Solicito reavaliacao da nefrologia", [sp.NEFROLOGIA]),
    ("solicitado parecer da nefro, avaliado ontem", []),
])
def test_c20_requested_consults(sentence, expected):
    assert consult_targets_requested(sentence) == expected


@pytest.mark.parametrize("sentence", [
    "Solicitado parecer da nefrologia por oliguria desde 18/08.",
    "Solicitada TC de cranio por cefaleia desde 18/08.",
    "Solicitado parecer da cardiologia, paciente com dor toracica em 18/08.",
    "Solicitado ECO, troponina elevada no dia 18/08.",
])
def test_c22_clinical_event_date_is_not_the_request_date(sentence):
    [finding] = apply_all_rules([StructuredNote("2026-08-22T08:00:00", None, "Médico clínico", sentence)], "2026-08-15")
    assert finding.evidence_date == "2026-08-22T08:00:00"


# ------------------------------------------------------- C23: fisioterapia


def test_c23_respiratory_physio_request_closes_by_the_respiratory_physio():
    item = _consult("Aguarda avaliação da fisioterapia respiratória")
    assert _actions(item, "Realizada higiene brônquica e aspiração de VAS. Orientada a manter fisioterapia.",
                    role="Fisioterapeuta respiratória") == {ACTION_RESOLVE}


def test_c23_generic_physio_request_closes_by_any_physio_subtype():
    item = _consult("Aguarda avaliação da fisioterapia")
    assert _actions(item, "Paciente avaliado. Iniciada cinesioterapia.", role="Fisioterapeuta motora") == {ACTION_RESOLVE}


# --------------------------------------------- C25/C26/C27: checagem da IA


@pytest.mark.parametrize("quote", [
    "Vaga de UTI negada pela central de regulacao.",
    "Transferencia adiada por instabilidade hemodinamica.",
    "Colecistectomia remarcada para a proxima semana por falta de sala.",
    "Falha no desmame de ventilacao mecanica, retornado para modo controlado.",
])
def test_c25_refusal_postponement_or_failure_is_never_done(quote):
    assert not se.quote_confirms_done(quote)


def test_c25_cancel_needs_an_explicit_definitive_reason():
    assert not se.quote_confirms_cancel("Transferencia adiada por instabilidade hemodinamica.")
    assert se.quote_confirms_cancel("Cancelada a transferência por melhora clínica.")


def test_c26_refused_subtype_of_a_detector_category_is_never_the_generic_check():
    item = _exam("Aguarda tomografia de tórax", subcategory="OUTRO")
    assert DetectorSession([], local_tz=BRT).agrees(item, ACTION_RESOLVE) is False
    consult = _item("INTERCONSULTA", "Conduta da nefrologia", subcategory="REALIZADA_SEM_CONDUTA_DEFINIDA")
    assert DetectorSession([], local_tz=BRT).agrees(consult, ACTION_RESOLVE) is False


def test_c27_shared_generic_word_does_not_identify_the_item():
    uti = {"pending_id": "a", "description": "Aguarda vaga de UTI", "alvo": "VAGA UTI"}
    ward = {"pending_id": "b", "description": "Aguarda vaga em enfermaria de retaguarda", "alvo": "VAGA ENFERMARIA RETAGUARDA"}
    quote = "Vaga de UTI liberada pela regulacao, paciente transferido."
    assert not se.quote_is_about_item(quote, ward, [uti, ward])
    assert se.quote_is_about_item(quote, uti, [uti, ward])


# --------------------------------------------- C32/C33/C38: alvo de interconsulta


def test_c32_c38_consults_outside_the_table_keep_distinct_targets():
    paliativos = se.normalize_target("INTERCONSULTA", "CUIDADOS PALIATIVOS", "Aguarda avaliação da equipe de cuidados paliativos")
    nutrologia = se.normalize_target("INTERCONSULTA", "NUTROLOGIA", "Aguarda avaliação da nutrologia")
    assert (paliativos, nutrologia) == ("CUIDADOS PALIATIVOS", "NUTROLOGIA")
    assert se.normalize_target("INTERCONSULTA", None, "Aguarda avaliação da equipe de cuidados paliativos") == \
        "CUIDADOS PALIATIVOS"


@pytest.mark.parametrize("text", [
    "Apos avaliacao da nefrologia, aguarda avaliacao da cardiologia",
    "Conforme parecer da nefrologia, aguardamos avaliacao da cardiologia.",
])
def test_c33_target_is_the_awaited_specialty_not_the_one_that_answered(text):
    assert se.consult_target(text) == sp.CARDIOLOGIA


# ------------------------------------------------- C39: punção já feita


def test_c39_quote_that_already_records_the_puncture_does_not_open_an_item():
    item = {"category": "INTERCONSULTA", "subcategory": "SOLICITADA", "alvo": None,
            "description": "Aguarda avaliação da equipe de punção venosa", "evidence": "x", "source": "LLM"}
    note = DetectorNote("2026-10-10T16:00:00", "Enfermeiro", "Equipe de puncao: AVP puncionado com sucesso em MSE.")
    assert se.puncture_already_done(item, "Equipe de puncao: AVP puncionado com sucesso em MSE.", note)
    later_loss = DetectorNote("2026-10-10T16:00:00", "Enfermeiro",
                              "AVP puncionado com sucesso pela manhã. Perda do acesso às 15h, aguarda nova punção.")
    assert not se.puncture_already_done(item, "Perda do acesso às 15h, aguarda nova punção.", later_loss)
