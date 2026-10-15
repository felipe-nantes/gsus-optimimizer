"""PEND-001: regressões da revisão adversarial da Fase 3 (DEC-129). Cada
frase abaixo foi reproduzida por um revisor fechando (ou cancelando) uma
pendência que NÃO estava resolvida -- o pior erro para o auditor -- ou
deixando de reconhecer um caso comum. Todo texto clínico é fictício."""
from datetime import timedelta, timezone

import pytest

from app.analysis import specialties as sp
from app.analysis import specialty_evaluation as se
from app.analysis.specialty_evaluation import (
    ACTION_CANCEL, ACTION_RESOLVE, ACTION_REVIEW, DetectorNote, detect_resolutions,
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


def _puncture(description="Aguarda passagem de CVC", **kwargs):
    return _item("PROCEDIMENTO_CIRURGIA", description, subcategory="OUTRO", **kwargs)


def _actions(item, text, role="Médico clínico", timestamp="2026-08-21T08:00:00"):
    return {f.action for f in detect_resolutions([item], [DetectorNote(timestamp, role, text)], local_tz=BRT)}


# ---------------------------------------------- interconsulta: nunca fecha


@pytest.mark.parametrize("text", [
    "Feito parecer à nefrologia.", "Enviado parecer à nefrologia.",
    "Encaminhado parecer à nefrologia por piora da função renal.", "Necessário parecer da nefrologia.",
    "Paciente necessita de parecer da nefrologia.", "Falta parecer da nefrologia.", "Indico parecer da nefrologia.",
    "Parecer da nefrologia agendado para segunda-feira.", "Parecer da nefrologia marcado para sexta.",
    "Pendências: parecer da nefrologia, TC de crânio.", "Deverá ser avaliado pela nefrologia.",
    "Paciente ficou de ser avaliado pela nefrologia.", "Até o momento sem ser visto pela nefrologia.",
    "Avaliação da nefrologia: hoje à tarde.", "Avaliação da nefrologia: não realizada ainda.",
    "Avaliação da nefrologia - ?", "Discutir se nefrologia indica HD.", "Conduta: parecer da nefrologia.",
    "- Parecer da nefrologia", "Parecer nefrologia.", "Parecer da nefrologia em andamento.",
    "Parecer da nefrologia enviado via sistema.", "Parecer da nefrologia previsto para segunda-feira.",
    "Avaliação da nefrologia: a definir.", "Paciente avaliado pela nefrologia em internação prévia.",
    "Paciente renal crônico, avaliado pela nefrologia no ambulatório há 1 mês.",
    "Avaliado pela nefrologia em 18.08, sem indicação de HD.",
    "CONDUTA:\nPELA NEFRO: aguardando\nPELA CARDIO: eco amanhã", "PELA NEFROLOGIA: não avaliou ainda",
    "PELA NEFROLOGIA: sem resposta até o momento", "PELA NEFROLOGIA: pendente",
    "PELA NEFROLOGIA: solicitado parecer hoje", "PELA NEFROLOGIA: paciente fora do leito, retornaremos à tarde.",
    "HISTÓRICO:\nPELA NEFROLOGIA (18/08): sem indicação de HD.\nHoje com piora de escórias, aguarda reavaliação.",
    "Parecer da nefrologia suspenso até estabilização clínica.",
    "Suspensa avaliação da nefrologia hoje por procedimento, reagendar.",
])
def test_consult_never_closed_by_requests_plans_history_or_postponement(text):
    assert not (_actions(_consult(), text) & CLOSING)


def test_specialist_note_that_did_not_see_the_patient_goes_to_review():
    actions = _actions(_consult(), "Interconsulta recebida. Paciente fora do leito (em exame de imagem), "
                                   "retornaremos à tarde.", role="Médico nefrologista")
    assert actions == {ACTION_REVIEW}


def test_later_request_or_waiting_after_the_specialist_note_is_conflicting():
    """Notas na ordem do GSUS (mais nova primeiro): a reiteração depois da
    avaliação torna a informação conflitante -- verificação, não concluída."""
    item = _consult(evidence_date="2026-08-10T08:00:00")
    notes = [
        DetectorNote("2026-08-15T08:00:00", "Médico clínico",
                     "Reitero solicitação de parecer da nefrologia. Paciente ainda não avaliado."),
        DetectorNote("2026-08-12T10:00:00", "Médico nefrologista", "Avaliado. Sem indicação de diálise no momento."),
    ]
    [found] = detect_resolutions([item], notes, local_tz=BRT)
    assert found.action == ACTION_REVIEW
    assert "conflitante" in found.note


def test_copy_forward_of_a_plan_line_does_not_block_a_real_closure():
    """A cópia diária da linha antiga de pendências não é conflito -- só
    frase NOVA depois da prova é."""
    item = _consult(evidence_date="2026-08-10T08:00:00")
    notes = [
        DetectorNote("2026-08-10T08:00:00", "Médico clínico", "Pendências: parecer da nefrologia."),
        DetectorNote("2026-08-12T10:00:00", "Médico nefrologista", "Avaliado. Sem indicação de diálise."),
        DetectorNote("2026-08-13T08:00:00", "Médico clínico", "Pendências: parecer da nefrologia."),
    ]
    [found] = detect_resolutions([item], notes, local_tz=BRT)
    assert found.action == ACTION_RESOLVE


# ------------------------------------------------- alvo: nunca o pedinte


@pytest.mark.parametrize("description", [
    "Aguarda avaliação para confecção de FAV, solicitada pela nefrologia",
    "Aguarda avaliação pré-anestésica para cirurgia",
    "Aguarda avaliação da hepatologia solicitada pela clínica médica",
    "Aguarda avaliação da CCP para TQT, conforme pneumologia",  # alvo certo: CCP
    "Aguarda avaliação da cardiologia e da nefrologia",
])
def test_consult_target_is_never_the_requesting_team_or_a_guess(description):
    target = se.kind_and_target(_consult(description))
    assert target is None or target[1] in (sp.CIRURGIA_CABECA_PESCOCO,)


@pytest.mark.parametrize("description,expected", [
    ("Aguarda avaliação da equipe de acesso vascular para PICC", (se.KIND_PUNCTURE, "PICC")),
    ("Aguarda avaliação do time de acesso vascular para punção venosa", (se.KIND_PUNCTURE, "ACESSO VENOSO PERIFÉRICO")),
    ("Aguarda avaliação do time de acesso vascular", (se.KIND_PUNCTURE, se.PUNCTURE_GENERIC)),
    ("Aguarda avaliação da Cx pediátrica", (se.KIND_CONSULT, sp.CIRURGIA_PEDIATRICA)),
    ("Aguarda avaliação da Cx Geral", (se.KIND_CONSULT, sp.CIRURGIA_GERAL)),
    ("Aguarda avaliação do Serviço Social", (se.KIND_CONSULT, sp.SERVICO_SOCIAL)),
    ("Solicito parecer p/ cardiologia", (se.KIND_CONSULT, sp.CARDIOLOGIA)),
    ("Aguarda avaliação e conduta da cardiologia", (se.KIND_CONSULT, sp.CARDIOLOGIA)),
    ("Aguarda avaliação da fisioterapia motora", (se.KIND_CONSULT, sp.FISIOTERAPIA_MOTORA)),
])
def test_consult_targets(description, expected):
    assert se.kind_and_target(_consult(description)) == expected


@pytest.mark.parametrize("role,expected", [
    ("Médico cirurgião de cabeça e pescoço", sp.CIRURGIA_CABECA_PESCOCO),
    ("Médico cirurgião da mão", sp.CIRURGIA_MAO),
    ("Médico cirurgião oncológico", sp.CIRURGIA_ONCOLOGICA),
    ("Médico cancerologista pediátrico", sp.ONCOLOGIA),
    ("Médico cirurgião do trauma", None),
    ("Cirurgião dentista - traumatologista bucomaxilofacial", sp.BUCOMAXILOFACIAL),
    ("Enfermeiro estomaterapeuta", sp.ESTOMATERAPIA),
])
def test_surgical_subspecialty_roles_never_become_general_surgery(role, expected):
    assert sp.specialty_from_role(role) == expected


def test_general_surgery_consult_not_closed_by_head_and_neck_surgeon():
    item = _consult("Aguarda avaliação da cirurgia geral para gastrostomia")
    assert not (_actions(item, "Pós-operatório de laringectomia, sem sangramento.",
                         role="Médico cirurgião de cabeça e pescoço") & CLOSING)


def test_motor_physio_request_not_closed_by_respiratory_physio():
    item = _consult("Aguarda avaliação da fisioterapia motora")
    assert not (_actions(item, "Paciente em VM, modo PCV. Realizada aspiração de TOT.",
                         role="Fisioterapeuta respiratória") & CLOSING)


@pytest.mark.parametrize("line,expected", [
    ("PELO SERVIÇO SOCIAL", sp.SERVICO_SOCIAL), ("PELA CX GERAL", sp.CIRURGIA_GERAL),
    ("PELA NCR", sp.NEUROCIRURGIA), ("PELA TRAUMATO", sp.ORTOPEDIA), ("PELA EQUIPE DE CX VASCULAR", sp.CIRURGIA_VASCULAR),
])
def test_pela_abbreviations(line, expected):
    assert [label for label, _ in sp.pela_specialties(line)] == [expected]


@pytest.mark.parametrize("line", [
    # Avaliação com o modelo real (caso D): o especialista pede exame na
    # MESMA linha do "PELA" -- é a avaliação dele, conta.
    "PELA INFECTOLOGIA: paciente avaliado. Sugiro cefazolina e solicitar ressonância magnética do pé.",
    "PELA NEFROLOGIA: avaliado, solicito USG de rins. Aguardamos exames para definir.",
])
def test_pela_line_with_the_specialist_s_exam_request_on_the_same_line_counts(line):
    assert [label for label, _ in sp.pela_specialties(line)] in ([sp.INFECTOLOGIA], [sp.NEFROLOGIA])


@pytest.mark.parametrize("line", [
    "PELA NEFROLOGIA: solicitado parecer hoje", "PELA NEFRO: aguardando", "PELA NEFROLOGIA: aguarda avaliação",
    "PELA NEFROLOGIA: solicito nova avaliação", "PELA NEFROLOGIA: pendente",
])
def test_pela_line_that_is_the_requesting_team_s_plan_does_not_count(line):
    assert sp.pela_specialties(line) == []


def test_pela_line_followed_by_the_specialist_s_own_request_still_counts():
    """O especialista pedir exame é o normal -- vira pendência nova."""
    text = "PELA NEFROLOGIA\nSem indicação de diálise. Solicito ultrassom de rins e vias urinárias."
    assert [label for label, _ in sp.pela_specialties(text)] == [sp.NEFROLOGIA]


# ------------------------------------------------------- exame: nunca fecha


@pytest.mark.parametrize("text", [
    "Pendências: TC de crânio.", "Conduta: TC de crânio.", "CD: TC de crânio sem contraste.",
    "Exames: TC de crânio, hemograma.", "TC de crânio: sexta-feira.", "Ainda sem resultado da TC de crânio.",
    "Resultado da TC de crânio ainda não disponível.", "Laudo da TC de crânio não liberado.",
    "Cobrar laudo da TC de crânio.", "Verificar resultado da TC de crânio.", "TC de crânio da admissão: sem sangramento.",
    "TC de seios da face: sinusopatia maxilar.", "RX de tórax: sem alterações.",
    "TC de crânio realizada.\nAo exame: neurológico sem alterações.",
    "TC de crânio cancelada pelo setor por falta de contraste, reagendar.",
    "TC de crânio suspensa por instabilidade hemodinâmica, realizar assim que estável.",
    "TC de crânio suspensa hoje, remarcar.",
])
def test_exam_never_closed_by_lists_missing_results_other_regions_or_postponement(text):
    assert not (_actions(_exam(), text) & CLOSING)


@pytest.mark.parametrize("text", [
    "Tomografia de crânio realizada. Resultado não disponível no sistema.",
    "TC de crânio realizada; laudo ainda não liberado.",
    "TC de crânio realizada. Laudo não localizado.",
    "TC de crânio realizada, resultado não encontrado.",
])
def test_exam_done_with_result_not_found_is_in_review(text):
    """Exatamente a definição do auditor de "Em verificação"."""
    assert _actions(_exam(), text) == {ACTION_REVIEW}


@pytest.mark.parametrize("description,text", [
    ("Aguarda ecocardiograma transesofágico", "ETT: FE 60%, sem vegetações. Prosseguir com ETE."),
    ("Aguarda doppler arterial de MMII", "Doppler venoso de MMII: sem sinais de TVP."),
    ("Aguarda ultrassom de abdome", "USG de rins e vias urinárias: sem hidronefrose."),
    ("Aguarda tomografia de abdome", "TC de seios da face: sinusopatia maxilar."),
    ("Aguarda RX de tórax", "RX de bacia: sem fraturas."),
    ("Aguarda RX de tórax", "RX de mão esquerda: fratura de 5º metacarpo."),
])
def test_exam_modality_and_region_must_match(description, text):
    assert not (_actions(_exam(description), text) & CLOSING)


def test_region_less_exam_target_takes_the_region_from_the_request():
    """Alvo "TC" (da IA) ou regra antiga "Aguarda realização de tomografia":
    a região vem da descrição/evidência -- a TC de crânio não fecha a de abdome."""
    item = _exam("Aguarda TC de abdome", alvo="TOMOGRAFIA", evidence="Dor abdominal. Solicitada tomografia de abdome.")
    assert se.kind_and_target(item) == (se.KIND_EXAM, "TOMOGRAFIA DE ABDOME")
    assert not (_actions(item, "Queda do leito. TC de cranio: sem sangramento.") & CLOSING)


@pytest.mark.parametrize("description,text", [
    ("Aguarda realização de tomografia de crânio", "TC de crânio sem sinais de sangramento."),
    ("Aguarda realização de tomografia de crânio", "TC crânio s/ alterações."),
    ("Aguarda RX de tórax", "RX de tórax sem pneumotórax."),
    ("Aguarda anatomopatológico", "Biópsia: adenocarcinoma moderadamente diferenciado."),
    ("Aguarda urocultura", "Urocultura com E. coli sensível a ceftriaxona."),
    ("Aguarda hemocultura", "Hemocultura com S. aureus sensível a oxacilina."),
    ("Aguarda hemocultura", "Hemoculturas 2/2 positivas para Staphylococcus aureus."),
])
def test_common_result_phrasings_close(description, text):
    subcategory = "AGUARDA_ANATOMOPATOLOGICO_OU_CULTURA" if "cultura" in description or "anatomo" in description else None
    item = _exam(description, subcategory=subcategory or "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL")
    assert _actions(item, text) == {ACTION_RESOLVE}


@pytest.mark.parametrize("text", ["Hemoculturas colhidas, resultado em 48h.",
                                  "Sem resultado de hemocultura até o momento.", "Hemocultura: verificar resultado.",
                                  "Hemoculturas sem crescimento até o momento."])
def test_culture_partial_or_pending_never_closes(text):
    item = _exam("Aguarda hemocultura", subcategory="AGUARDA_ANATOMOPATOLOGICO_OU_CULTURA")
    assert not (_actions(item, text) & CLOSING)


# ------------------------------------------------------ punção: nunca fecha


@pytest.mark.parametrize("description,text", [
    ("Aguarda passagem de CVC", "CVC ainda não passado."),
    ("Aguarda passagem de CVC", "CVC não passado por plaquetopenia."),
    ("Aguarda passagem de CVC", "Não foi passado CVC por plaquetopenia."),
    ("Aguarda passagem de CVC", "Acesso central a ser passado pela equipe da UTI."),
    ("Aguarda nova punção venosa", "Paciente sem acesso, necessário novo AVP, a ser puncionado."),
    ("Aguarda nova punção venosa", "Perda de AVP, acionada equipe de punção. Passagem de SNE realizada com sucesso."),
    ("Aguarda nova punção venosa", "Acionada equipe de punção. Sondagem vesical de demora realizada com sucesso."),
    ("Aguarda nova punção venosa", "Equipe de punção não compareceu no plantão. Hemodiálise realizada com sucesso."),
    ("Aguarda acesso arterial (PAI)", "Realizada punção arterial para gasometria."),
    ("Aguarda passagem de CVC", "Realizada punção de artéria femoral para gasometria."),
])
def test_puncture_never_closed_by_negation_future_or_unrelated_success(description, text):
    assert not (_actions(_puncture(description), text, role="Enfermeiro") & CLOSING)


@pytest.mark.parametrize("text", [
    "Realizada passagem de CVC em VJID sem intercorrências.",
    "Passado CVC em VJID guiado por USG, solicitado RX de controle.",
    "Passado CVC duplo lúmen em VSCD para infusão de noradrenalina.",
    "Inserido PICC em MSD.",
])
def test_common_central_line_write_ups_close(text):
    assert _actions(_puncture(), text) == {ACTION_RESOLVE}


def test_caso3_literal_team_sentence_still_closes():
    item = _consult("Aguarda avaliação para punção venosa")
    assert _actions(item, "Equipe de punção: atendimento realizado com sucesso.", role="Enfermeiro") == {ACTION_RESOLVE}


# ------------------------------------------------------------- datas


def test_clinical_fractions_are_not_dates():
    from datetime import date
    assert se.cited_dates("hemoculturas 2/2 positivas", date(2026, 8, 21)) == []
    assert se.cited_dates("forca grau 4/5 em msd", date(2026, 8, 21)) == []
    assert se.cited_dates("pa 18/10 mmhg", date(2026, 8, 21)) == []
    assert se.cited_dates("tc de 15/08 sem alteracoes", date(2026, 8, 21)) == [date(2026, 8, 15)]
    assert se.cited_dates("agendada para 30/08", date(2026, 8, 21)) == []  # futuro: agendamento
    assert se.cited_dates("cultura de 28/12", date(2027, 1, 5)) == [date(2026, 12, 28)]


@pytest.mark.parametrize("evidence_date", ["9999-12-31T00:00:00", "1958-03-15T00:00:00", "0001-01-01T00:00:00"])
def test_absurd_dates_never_raise(evidence_date):
    item = _consult(evidence_date=evidence_date)
    detect_resolutions([item], [DetectorNote("2026-08-21T08:00:00", "Médico nefrologista", "Avaliado.")], local_tz=BRT)
    detect_resolutions([item], [DetectorNote("2026-08-21T08:00:00", "Médico nefrologista", "Avaliado.")])
    assert se.resolution_time_utc("1958-03-15T10:00:00", "2026-08-20T15:00:00+00:00") in (
        None, "2026-08-20T15:00:00+00:00")


def test_evidence_shows_done_only_for_a_pela_line():
    """Revisão: as heurísticas inteiras aqui jogavam fora pedidos legítimos."""
    base = {"category": "INTERCONSULTA", "subcategory": "SOLICITADA", "alvo": "NEFROLOGIA",
            "description": "Aguarda avaliação da nefrologia", "source": "LLM"}
    assert se.evidence_shows_done({**base, "evidence": "Pela NEFROLOGIA: paciente avaliado."})
    for quote in ("Necessário parecer da nefrologia.", "Enviado parecer à nefrologia.",
                  "Pendências: parecer da nefrologia."):
        assert not se.evidence_shows_done({**base, "evidence": quote})
