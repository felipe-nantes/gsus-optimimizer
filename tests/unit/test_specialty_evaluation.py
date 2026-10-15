"""PEND-001 (pedido do auditor, DEC-129 seção 4): especialidade pelo cargo e
pela linha "PELA <ESPECIALIDADE>", alvo normalizado e o detector de
pendência já atendida. Cargos são os rótulos genéricos do GSUS (medidos só
por contagem no banco de dev); todo texto clínico abaixo é fictício."""
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from app.analysis import specialties as sp
from app.analysis import specialty_evaluation as se
from app.analysis.specialty_evaluation import (
    ACTION_CANCEL, ACTION_RESOLVE, ACTION_REVIEW, DetectorNote, detect_resolutions,
)
from app.extraction.normalizer import normalize_datetime, normalize_text
from app.extraction.parser import parse_note_blocks

BRT = timezone(timedelta(hours=-3))
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "notes"


# ------------------------------------------------------------ especialidades


@pytest.mark.parametrize("role,expected", [
    ("Médico nefrologista", sp.NEFROLOGIA),
    ("MÉDICO NEFROLOGISTA", sp.NEFROLOGIA),
    ("Médico ortopedista e traumatologista", sp.ORTOPEDIA),
    ("Médico cirurgião geral", sp.CIRURGIA_GERAL),
    ("Médico cirurgião do aparelho digestivo", sp.CIRURGIA_APARELHO_DIGESTIVO),
    ("Médico cirurgião pediátrico", sp.CIRURGIA_PEDIATRICA),
    ("Médico cirurgião cardiovascular", sp.CIRURGIA_CARDIOVASCULAR),
    ("Médico cirurgião torácico", sp.CIRURGIA_TORACICA),
    ("Médico angiologista", sp.CIRURGIA_VASCULAR),
    ("Médico em cirurgia vascular", sp.CIRURGIA_VASCULAR),
    ("Médico neurocirurgião", sp.NEUROCIRURGIA),
    ("Médico neurologista", sp.NEUROLOGIA),
    ("Médico em medicina intensiva", sp.MEDICINA_INTENSIVA),
    ("Médico ginecologista e obstetra", sp.GINECOLOGIA_OBSTETRICIA),
    ("Médico endocrinologista e metabologista", sp.ENDOCRINOLOGIA),
    ("Médico em radiologia e diagnóstico por imagem", sp.RADIOLOGIA),
    ("Médico em endoscopia", sp.ENDOSCOPIA),
    ("Médico pediatra", sp.PEDIATRIA),
    ("Fisioterapeuta respiratória", sp.FISIOTERAPIA_RESPIRATORIA),
    ("Fisioterapeuta", sp.FISIOTERAPIA),
    ("Nutricionista", sp.NUTRICAO),
    ("Assistente social", sp.SERVICO_SOCIAL),
    ("Farmacêutico hospitalar e clínico", sp.FARMACIA),
    ("Fonoaudiólogo geral", sp.FONOAUDIOLOGIA),
    ("Psicólogo hospitalar", sp.PSICOLOGIA),
])
def test_specialty_from_role(role, expected):
    assert sp.specialty_from_role(role) == expected


@pytest.mark.parametrize("role", [
    "Médico residente", "Médico generalista", "Técnico de enfermagem", "Enfermeiro", "Direita", "", None,
    "Médico clínico",  # plantonista de enfermaria/PS (2ª revisão adversarial)
])
def test_roles_that_do_not_say_the_specialty(role):
    """Residente, generalista e o plantonista "Médico clínico" podem ser de
    qualquer serviço -- o cargo deles fecharia a interconsulta com a
    evolução da própria equipe."""
    assert sp.specialty_from_role(role) is None


@pytest.mark.parametrize("word", [
    "Pneumonia", "hematoma subdural", "nefrostomia", "urocultura", "ortopneia", "angiotomografia",
    "circulação", "eletrocardiograma", "insuficiência cardíaca", "nefrotóxico",
])
def test_words_that_start_like_a_specialty_are_not_one(word):
    assert sp.specialties_in_text(word) == []


@pytest.mark.parametrize("line,expected", [
    ("PELA NEFROLOGIA....", sp.NEFROLOGIA),
    ("PELA NEFRO", sp.NEFROLOGIA),
    ("PELA REUMATO", sp.REUMATOLOGIA),
    ("Pela infecto: manter antibiótico", sp.INFECTOLOGIA),
    ("PELA CIRURGIA", sp.CIRURGIA_GERAL),
    ("PELA CIR", sp.CIRURGIA_GERAL),
    ("PELA CIRURGA", sp.CIRURGIA_GERAL),  # erro de digitação real
    ("PELA NEUROILOGIA", sp.NEUROLOGIA),  # erro de digitação real
    ("PELA CIRURGIA GERAL - ", sp.CIRURGIA_GERAL),
    ("PELA CIRURGIA PEDIATRICA.", sp.CIRURGIA_PEDIATRICA),
    ("PELA EQUIPE DA CARDIOLOGIA:", sp.CARDIOLOGIA),
    ("PELO SERVIÇO DE PSICOLOGIA", sp.PSICOLOGIA),
    ("PELA FARMÁCIA", sp.FARMACIA),
])
def test_pela_lines(line, expected):
    assert [label for label, _ in sp.pela_specialties(line)] == [expected]


@pytest.mark.parametrize("text", [
    "PELA TARDE paciente estável",
    "PELA MANHÃ avaliado pela cardiologia",  # não é a cardiologia escrevendo
    "PELA ORTOPEDIA: aguarda avaliação",  # diz que AINDA não avaliou
    "Paciente avaliado pela nefrologia",  # não começa a linha
    "PELA DRA. (nefro)",
])
def test_pela_lines_that_do_not_count(text):
    assert sp.pela_specialties(text) == []


def test_pela_line_in_the_middle_of_a_note_after_the_header():
    text = "Paciente em leito de enfermaria.\nPELA NEFROLOGIA\nMantida hemodiálise."
    assert sp.pela_specialties(text) == [(sp.NEFROLOGIA, "PELA NEFROLOGIA")]


def test_every_canonical_specialty_round_trips_as_a_target():
    """O `alvo` gravado é o rótulo canônico -- precisa voltar pra ele."""
    labels = {value for name, value in vars(sp).items()
              if name.isupper() and not name.startswith(("_", "STATUS_")) and isinstance(value, str)}
    for label in labels:
        assert se.consult_target(label) == label, label


# ------------------------------------------------------------------- alvos


@pytest.mark.parametrize("text,expected", [
    ("Aguarda avaliação da cirurgia vascular", sp.CIRURGIA_VASCULAR),
    ("parecer do serviço de psicologia", sp.PSICOLOGIA),
    ("Aguarda avaliação da ortopedia (fratura), pré-operatório da cirurgia", sp.ORTOPEDIA),
    ("IC descompensada, avaliação da cardio", sp.CARDIOLOGIA),
    ("Solicito interconsulta com a nefro", sp.NEFROLOGIA),
    ("Infectologia", sp.INFECTOLOGIA),
    ("Aguarda avaliação", None),
    ("Cardiologia e nefrologia", None),  # ambíguo, sem âncora
])
def test_consult_target(text, expected):
    assert se.consult_target(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Aguarda TC de crânio", "TOMOGRAFIA DE CRÂNIO"),
    ("Aguarda tomografia", "TOMOGRAFIA"),
    ("Aguarda eco doppler de carótidas", "DOPPLER DE PESCOÇO"),
    ("ecocardiograma com doppler", "ECOCARDIOGRAMA"),
    ("Aguarda angio-TC de tórax", "ANGIOTOMOGRAFIA DE TÓRAX"),
    ("aguarda RX de tórax", "RAIO-X DE TÓRAX"),
    ("aguarda USG abdome total", "ULTRASSOM DE ABDOME"),
    ("Aguarda hemocultura", "HEMOCULTURA"),
    ("aguarda resultado de biópsia", "ANATOMOPATOLÓGICO"),
    ("Aguarda EDA", "ENDOSCOPIA"),
    ("Aguarda RNM de coluna lombar", "RESSONÂNCIA DE COLUNA LOMBAR"),  # segmento conta (3ª revisão)
    ("Aguarda definição diagnóstica", None),
])
def test_exam_target(text, expected):
    assert se.exam_target(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Aguarda passagem de CVC", "ACESSO VENOSO CENTRAL"),
    # 2ª revisão: PICC e cateter de diálise não são um CVC qualquer, e
    # "punção venosa" sem "central" é a periférica.
    ("aguarda PICC", "PICC"),
    ("Aguarda passagem de cateter de Shilley para hemodiálise", "CATETER DE HEMODIÁLISE"),
    ("Aguarda CVC para hemodiálise", "CATETER DE HEMODIÁLISE"),
    ("Aguarda nova punção (acesso difícil)", se.PUNCTURE_GENERIC),
    ("Aguarda avaliação para punção venosa", "ACESSO VENOSO PERIFÉRICO"),
    ("aguarda punção lombar", "PUNÇÃO LOMBAR"),
    ("Aguarda punção liquórica", "PUNÇÃO LOMBAR"),
    ("Aguarda punção de derrame pleural", "TORACOCENTESE"),
    ("Aguarda o pai do paciente para assinar o termo", None),
    ("Aguarda paracentese", "PARACENTESE"),
    ("Aguarda cirurgia", None),
])
def test_puncture_target(text, expected):
    assert se.puncture_target(text) == expected


def test_normalize_target_uses_evidence_only_with_an_anchor():
    assert se.normalize_target("INTERCONSULTA", None, "Aguarda parecer", "Solicito parecer da infecto.") == sp.INFECTOLOGIA
    assert se.normalize_target(
        "INTERCONSULTA", None, "Aguarda parecer", "Avaliado pela cardio. Solicito parecer da nefro.") == sp.NEFROLOGIA
    # Revisão adversarial: a "única especialidade citada" na evidência
    # costuma ser quem PEDIU -- a evolução diária dela fecharia o pedido.
    # Especialidade fora da tabela fica com alvo GENÉRICO (2ª revisão: sem
    # ele, duas interconsultas assim viravam uma), que o detector não fecha.
    target = se.normalize_target(
        "INTERCONSULTA", "HEPATOLOGIA", "Aguarda avaliação da hepatologia",
        "Clínica médica solicita parecer da hepatologia para investigação de cirrose.")
    assert target == "HEPATOLOGIA"
    assert not se.is_canonical_target("INTERCONSULTA", target)


# ----------------------------------------------------------------- detector


def _item(category="INTERCONSULTA", description="Aguarda avaliação da infectologia", *, pending_id="p1",
          subcategory="SOLICITADA", alvo=None, evidence="Solicito avaliação da infectologia.",
          evidence_date="2026-08-20T10:00:00", created_at="2026-08-20T15:00:00+00:00", source="LLM"):
    return {
        "pending_id": pending_id, "category": category, "subcategory": subcategory, "alvo": alvo,
        "description": description, "evidence": evidence, "evidence_date": evidence_date,
        "created_at": created_at, "source": source,
    }


def _note(timestamp, text, role="Médico residente"):
    return DetectorNote(timestamp=timestamp, role=role, text=text)


def _detect(items, notes):
    return detect_resolutions(items, notes, local_tz=BRT)


def test_caso2_pela_infectologia_after_the_request_closes_the_consult():
    """Caso 2 de aceitação do auditor: "Se houver evolução 'PELA
    INFECTOLOGIA' posterior à solicitação, o sistema não deve manter
    'aguarda avaliação da Infectologia' como pendência ativa"."""
    notes = [_note("2026-08-21T09:30:00", "Paciente sem febre.\nPELA INFECTOLOGIA\nManter antibiótico por 7 dias.")]
    [found] = _detect([_item()], notes)
    assert found.action == ACTION_RESOLVE
    assert found.target == sp.INFECTOLOGIA
    assert found.evidence == "PELA INFECTOLOGIA Manter antibiótico por 7 dias."  # o bloco inteiro
    assert found.evidence_date == "2026-08-21T09:30:00"
    assert found.resolved_at == "2026-08-21T12:30:00+00:00"  # 09:30 de Brasília em UTC
    assert "Concluída automaticamente" in found.note


def test_caso3_puncture_team_success_closes_awaiting_venous_puncture():
    """Caso 3 de aceitação do auditor: "Se a equipe de punção registrar
    atendimento realizado com sucesso, o sistema não deve apresentar 'aguarda
    avaliação para punção venosa'" -- mesmo classificada como interconsulta."""
    item = _item(description="Aguarda avaliação para punção venosa", evidence="Acionada equipe de punção.")
    notes = [_note("2026-08-20T16:00:00", "Equipe de punção: atendimento realizado com sucesso.", role="Enfermeiro")]
    [found] = _detect([item], notes)
    assert (found.action, found.kind, found.target) == (ACTION_RESOLVE, se.KIND_PUNCTURE, "ACESSO VENOSO PERIFÉRICO")


def test_caso3_variant_puncture_success_in_its_own_words():
    item = _item(category="PROCEDIMENTO_CIRURGIA", subcategory="OUTRO", description="Aguarda nova punção venosa")
    notes = [_note("2026-08-20T16:00:00", "Puncionado AVP em MSE, sem intercorrências.", role="Técnico de enfermagem")]
    [found] = _detect([item], notes)
    assert found.action == ACTION_RESOLVE


def test_consult_closed_by_the_specialist_role():
    notes = [_note("2026-08-21T08:00:00", "Paciente avaliado. Sem sinais de infecção ativa.", role="Médico infectologista")]
    [found] = _detect([_item()], notes)
    assert found.action == ACTION_RESOLVE
    assert "Médico infectologista" in found.note


def test_answer_reported_by_another_team_only_goes_to_review():
    """Plano aprovado (DEC-129 decisão 4) e regra do auditor: fecham a
    evolução ASSINADA pela especialidade e a linha "PELA X". Outra equipe
    relatando a resposta é sinal fraco: verificação, nunca concluída."""
    item = _item(description="Aguarda avaliação da cirurgia torácica", evidence="Solicitada avaliação da cirurgia torácica.")
    notes = [_note("2026-08-21T08:00:00", "Parecer da cirurgia torácica: sem indicação cirúrgica no momento.",
                   role="Médico clínico")]
    [found] = _detect([item], notes)
    assert found.action == ACTION_REVIEW


@pytest.mark.parametrize("text", [
    "Reitero solicitação de parecer da infectologia.",  # antes a regra fechava com isto
    "Aguarda parecer da infectologia.",
    "Infectologia ainda não avaliou.",
    "Solicitado novamente avaliação da infectologia.",
    "Paciente com infecção de corrente sanguínea.",
])
def test_consult_not_closed_by_request_or_waiting_sentences(text):
    assert _detect([_item()], [_note("2026-08-21T08:00:00", text, role="Médico clínico")]) == []


def test_specialist_note_before_the_request_does_not_close_it():
    notes = [_note("2026-08-20T08:00:00", "PELA INFECTOLOGIA\nSem antibiótico no momento.")]
    assert _detect([_item(evidence_date="2026-08-20T10:00:00")], notes) == []


def test_date_without_time_only_accepts_the_next_day():
    """O modelo devolve "DD/MM/AAAA" e vira meia-noite -- a evolução do mesmo
    dia pode ser anterior ao pedido."""
    item = _item(evidence_date="2026-08-20T00:00:00")
    same_day = [_note("2026-08-20T18:00:00", "PELA INFECTOLOGIA\nManter antibiótico.")]
    next_day = [_note("2026-08-21T07:00:00", "PELA INFECTOLOGIA\nManter antibiótico.")]
    assert _detect([item], same_day) == []
    assert [f.action for f in _detect([item], next_day)] == [ACTION_RESOLVE]


def test_without_evidence_date_the_reference_is_when_the_system_registered_it():
    item = _item(evidence_date=None, created_at="2026-08-21T12:00:00+00:00")  # 09:00 em Brasília
    before = [_note("2026-08-21T08:30:00", "PELA INFECTOLOGIA")]
    after = [_note("2026-08-21T09:30:00", "PELA INFECTOLOGIA")]
    assert _detect([item], before) == []
    assert len(_detect([item], after)) == 1


def test_resolved_at_never_before_created_at():
    """Pedido antigo descoberto agora: a avaliação foi antes do registro."""
    item = _item(evidence_date="2026-08-10T10:00:00", created_at="2026-08-20T15:00:00+00:00")
    [found] = _detect([item], [_note("2026-08-12T09:00:00", "PELA INFECTOLOGIA")])
    assert found.resolved_at == "2026-08-20T15:00:00+00:00"
    assert found.evidence_date == "2026-08-12T09:00:00"


def test_consult_with_evaluation_done_waiting_for_conduct_is_not_closed():
    item = _item(subcategory="REALIZADA_SEM_CONDUTA_DEFINIDA")
    assert _detect([item], [_note("2026-08-21T08:00:00", "PELA INFECTOLOGIA")]) == []


def test_consult_cancelled_by_the_team():
    notes = [_note("2026-08-21T08:00:00", "Suspenso o parecer da infectologia por melhora clínica.", role="Médico clínico")]
    [found] = _detect([_item()], notes)
    assert found.action == ACTION_CANCEL
    assert "Cancelada automaticamente" in found.note


def test_old_rule_item_evidence_is_never_used_to_guess_the_target():
    """Pendência de regra antiga guardava a evolução INTEIRA como evidência."""
    item = _item(description="Aguarda avaliação de quadro", source="RULE",
                 evidence="Avaliado pela cardio. Solicitada avaliação do quadro pela nefrologia.")
    assert se.kind_and_target(item) is None


# ------------------------------------------------------------------- exames


def _exam(description="Aguarda tomografia de crânio", **kwargs):
    kwargs.setdefault("subcategory", "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL")
    kwargs.setdefault("evidence", "Solicitada TC de crânio.")
    return _item(category="DIAGNOSTICO", description=description, **kwargs)


@pytest.mark.parametrize("text", [
    "TC de crânio: sem sinais de sangramento.",
    "Tomografia de crânio sem alterações agudas.",
    "Laudo da TC de crânio: hematoma estável.",
    "TC de crânio realizada. Sem alterações agudas.",  # resultado na frase seguinte
])
def test_exam_result_closes(text):
    [found] = _detect([_exam()], [_note("2026-08-21T08:00:00", text)])
    assert found.action == ACTION_RESOLVE


@pytest.mark.parametrize("text", [
    "Aguarda laudo da TC de crânio.",
    "TC de crânio realizada, aguardando laudo.",
    "TC de crânio agendada para amanhã.",
    "Solicitada nova TC de crânio de controle.",
    "TC de tórax: sem alterações.",  # outra região
    "TC de crânio (15/08): sem alterações.",  # resultado anterior ao pedido, citado como histórico
    "Suspensa dieta para TC de crânio.",  # preparo, não cancelamento
])
def test_exam_not_closed(text):
    assert [f for f in _detect([_exam()], [_note("2026-08-21T08:00:00", text)]) if f.action != ACTION_REVIEW] == []


def test_exam_done_without_result_goes_to_review():
    """Tabela do auditor: "a equipe menciona que o exame foi realizado, mas o
    resultado não foi localizado" -> Em verificação."""
    [found] = _detect([_exam()], [_note("2026-08-21T08:00:00", "TC de crânio realizada.")])
    assert found.action == ACTION_REVIEW
    assert "resultado não foi localizado" in found.note


def test_later_result_wins_over_earlier_review():
    notes = [_note("2026-08-21T08:00:00", "TC de crânio realizada."),
             _note("2026-08-22T08:00:00", "TC de crânio: sem sangramento.")]
    [found] = _detect([_exam()], notes)
    assert found.action == ACTION_RESOLVE
    assert found.evidence_date == "2026-08-22T08:00:00"


def test_exam_cancelled():
    notes = [_note("2026-08-21T08:00:00", "TC de crânio suspensa por mudança da estratégia terapêutica.")]
    [found] = _detect([_exam()], notes)
    assert found.action == ACTION_CANCEL


def test_two_open_exams_of_the_same_kind_need_the_region_to_close():
    items = [_exam(pending_id="cranio"), _exam("Aguarda tomografia de abdome", pending_id="abdome")]
    found = _detect(items, [_note("2026-08-21T08:00:00", "Tomografia sem alterações.")])
    assert {f.action for f in found} <= {ACTION_REVIEW}  # não diz de quê: nunca fecha
    [found] = _detect(items, [_note("2026-08-21T08:00:00", "Tomografia de abdome sem alterações.")])
    assert found.pending_id == "abdome"


def test_eco_doppler_result_does_not_close_an_echocardiogram():
    item = _exam("Aguarda ecocardiograma", evidence="Solicitado ecocardiograma.")
    assert _detect([item], [_note("2026-08-21T08:00:00", "Eco doppler de carótidas: sem estenose.")]) == []


def test_culture_result():
    item = _exam("Aguarda hemocultura", subcategory="AGUARDA_ANATOMOPATOLOGICO_OU_CULTURA")
    assert _detect([item], [_note("2026-08-21T08:00:00", "Hemocultura parcial em andamento.")]) == []
    [found] = _detect([item], [_note("2026-08-23T08:00:00", "Hemoculturas negativas.")])
    assert found.action == ACTION_RESOLVE


def test_investigation_without_definition_is_never_closed_by_one_result():
    item = _exam("Investigação de nódulo pulmonar com tomografia", subcategory="INVESTIGACAO_SEM_DEFINICAO")
    assert _detect([item], [_note("2026-08-21T08:00:00", "Tomografia de tórax: nódulo estável.")]) == []


# ------------------------------------------------------------------- punção


def _puncture(description="Aguarda passagem de CVC", **kwargs):
    kwargs.setdefault("subcategory", "OUTRO")
    return _item(category="PROCEDIMENTO_CIRURGIA", description=description, **kwargs)


@pytest.mark.parametrize("text", [
    "Passado CVC em VJID sem intercorrências.",
    "Obtido acesso venoso central em VSCD.",
    "CVC em veia jugular interna direita realizado com sucesso.",
])
def test_central_line_success_closes(text):
    [found] = _detect([_puncture()], [_note("2026-08-21T08:00:00", text)])
    assert found.action == ACTION_RESOLVE


@pytest.mark.parametrize("text", [
    "Realizada troca de curativo do CVC.",
    "Realizada tentativa de punção de CVC sem sucesso.",
    "Puncionado AVP em MSE.",  # periférico não fecha pedido de central
    "Mantém CVC em VJID, pérvio.",
    "CVC instalado em 10/08, sem sinais flogísticos.",
    "Coletados exames pelo CVC, realizado flush.",
])
def test_central_line_not_closed(text):
    assert _detect([_puncture()], [_note("2026-08-21T08:00:00", text)]) == []


def test_puncture_cancelled():
    notes = [_note("2026-08-21T08:00:00", "Suspensa passagem de CVC por mudança de conduta, mantido acesso periférico.")]
    [found] = _detect([_puncture()], notes)
    assert found.action == ACTION_CANCEL


def test_puncture_suspended_without_a_definitive_reason_only_goes_to_review():
    """2ª revisão: suspensão sem motivo definitivo pode ser adiamento."""
    notes = [_note("2026-08-21T08:00:00", "Suspensa passagem de CVC, mantido acesso periférico.")]
    [found] = _detect([_puncture()], notes)
    assert found.action == ACTION_REVIEW


# --------------------------------------------------- pedido já atendido


@pytest.mark.parametrize("request_date,resolution_date,resolved_at,expected", [
    ("2026-08-19T08:00:00", "2026-08-20T10:00:00", None, True),  # pedido antes da prova
    ("2026-08-21T08:00:00", "2026-08-20T10:00:00", None, False),  # pedido NOVO depois da prova
    ("2026-08-20T00:00:00", "2026-08-20T10:00:00", None, True),  # só a data: mesmo dia conta como atendido
    (None, "2026-08-20T10:00:00", None, True),  # sem data: nunca recria em loop
    ("2026-08-21T08:00:00", None, "2026-08-21T12:00:00+00:00", True),  # decisão do auditor 09:00 BRT
    ("2026-08-21T10:00:00", None, "2026-08-21T12:00:00+00:00", False),
])
def test_request_already_handled(request_date, resolution_date, resolved_at, expected):
    assert se.request_already_handled(request_date, resolution_date, resolved_at, BRT) is expected


# ------------------------------------------------------- fixtures sintéticas


def _fixture_notes(name):
    blocks = parse_note_blocks((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return [DetectorNote(timestamp=normalize_datetime(b["timestamp_raw"]), role=b["source_type"],
                         text=normalize_text(b["text"])) for b in blocks]


def test_fixture_resolved_exam_is_closed_by_the_detector():
    item = _exam("Aguarda realização de tomografia de abdome", evidence_date="2026-08-19T08:00:00")
    [found] = _detect([item], _fixture_notes("resolved_exam.txt"))
    assert found.action == ACTION_RESOLVE


def test_fixture_resolved_consult_is_closed_by_the_detector():
    item = _item(description="Aguarda avaliação de cirurgia torácica", source="RULE",
                 evidence_date="2026-08-19T09:00:00")
    [found] = _detect([item], _fixture_notes("resolved_consult.txt"))
    assert found.action == ACTION_RESOLVE
