from pathlib import Path

import pytest

from app.analysis.rules import CATEGORY_CONSULT, CATEGORY_EXAM, StructuredNote, apply_all_rules, most_recent_note
from app.extraction.normalizer import normalize_datetime, normalize_text
from app.extraction.parser import parse_note_blocks

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "notes"


def _load_structured_notes(name: str) -> list[StructuredNote]:
    raw = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    blocks = parse_note_blocks(raw)
    return [
        StructuredNote(
            timestamp=normalize_datetime(b["timestamp_raw"]),
            specialty=b["specialty"],
            source_type=b["source_type"],
            text=normalize_text(b["text"]),
        )
        for b in blocks
    ]


def _n(timestamp, text, role="Médico clínico") -> StructuredNote:
    return StructuredNote(timestamp=timestamp, specialty=None, source_type=role, text=text)


def test_awaiting_exam_yields_one_exam_finding_with_target_and_sentence_evidence():
    notes = _load_structured_notes("awaiting_exam.txt")
    exam_findings = [f for f in apply_all_rules(notes) if f.category == CATEGORY_EXAM]
    assert len(exam_findings) == 1
    finding = exam_findings[0]
    assert finding.alvo == "TOMOGRAFIA DE ABDOME"
    assert finding.description == "Aguarda realização de tomografia de abdome"
    # PEND-001: a evidência é a FRASE do pedido, não a evolução inteira
    # (antes ia o texto com nome/prontuário/leito do cabeçalho junto).
    assert finding.evidence == "Solicitada tomografia de abdome"
    assert finding.evidence_date == "2026-08-20T08:00:00"


def test_awaiting_consult_yields_one_consult_finding_with_canonical_target():
    notes = _load_structured_notes("awaiting_consult.txt")
    consult_findings = [f for f in apply_all_rules(notes) if f.category == CATEGORY_CONSULT]
    assert len(consult_findings) == 1
    assert consult_findings[0].alvo == "CIRURGIA TORÁCICA"
    assert "cirurgia torácica" in consult_findings[0].description


def test_rules_only_create_never_close():
    """PEND-001: quem fecha é o detector (specialty_evaluation), com o
    histórico inteiro. A regra só devolve pedidos -- a conclusão na mesma
    janela não some com o achado."""
    for fixture in ("resolved_exam.txt", "resolved_consult.txt"):
        findings = apply_all_rules(_load_structured_notes(fixture))
        assert len(findings) == 1
        assert not hasattr(findings[0], "resolved")


def test_first_request_in_time_is_the_evidence_even_when_extraction_comes_newest_first():
    """A extração do GSUS entrega do dia mais novo pro mais antigo (LLM-006)."""
    notes = [
        _n("2026-08-21T08:00:00", "Reitero solicitação de parecer da cardiologia."),
        _n("2026-08-19T08:00:00", "Solicito parecer da cardiologia."),
    ]
    [finding] = apply_all_rules(notes)
    assert finding.evidence_date == "2026-08-19T08:00:00"


def test_consult_trigger_synonyms_and_lists():
    notes = [_n("2026-08-20T08:00:00", "Solicito parecer da nefrologia e da cardiologia. Peço interconsulta com a infecto.")]
    targets = {f.alvo for f in apply_all_rules(notes)}
    assert targets == {"NEFROLOGIA", "CARDIOLOGIA", "INFECTOLOGIA"}


def test_consult_without_recognizable_specialty_is_not_a_pending_item():
    """Antes: "Solicitada avaliação do quadro" virava "Aguarda avaliação de
    quadro" (51 das 78 interconsultas ativas no banco de dev não diziam a
    especialidade)."""
    notes = [_n("2026-08-20T08:00:00", "Solicitada avaliação do quadro respiratório. Solicitado parecer do caso.")]
    assert [f for f in apply_all_rules(notes) if f.category == CATEGORY_CONSULT] == []


def test_exam_request_and_result_in_other_sentences_are_not_mixed():
    """Antes, "resultado de" em QUALQUER frase da nota fechava a TC."""
    notes = [_n("2026-08-20T08:00:00", "Solicitada tomografia de crânio. Resultado de hemograma normal.")]
    [finding] = apply_all_rules(notes)
    assert finding.alvo == "TOMOGRAFIA DE CRÂNIO"


def test_request_sentence_that_already_brings_the_result_is_not_pending():
    notes = [_n("2026-08-20T08:00:00", "TC de crânio solicitada ontem e realizada hoje, sem alterações.")]
    assert apply_all_rules(notes) == []


def test_routine_lab_requests_stay_with_the_ai():
    notes = [_n("2026-08-20T08:00:00", "Solicitados hemograma, função renal e hemocultura.")]
    assert apply_all_rules(notes) == []


def test_every_occurrence_of_a_request_is_kept_oldest_first():
    notes = [
        _n("2026-08-23T08:00:00", "Reitero solicitação de parecer da nefrologia."),
        _n("2026-08-20T08:00:00", "Solicito parecer da nefrologia."),
    ]
    [finding] = apply_all_rules(notes)
    assert [date for date, _ in finding.occurrences] == ["2026-08-20T08:00:00", "2026-08-23T08:00:00"]


def test_request_sentence_citing_an_earlier_date_takes_that_date():
    """"Solicitado parecer em 20/08", escrito no dia 23: o pedido é de 20/08."""
    notes = [_n("2026-08-23T08:00:00", "Solicitado parecer da nefrologia em 20/08, sem resposta até o momento.")]
    [finding] = apply_all_rules(notes)
    assert finding.evidence_date == "2026-08-20T00:00:00"


def test_request_sentence_that_already_reports_the_answer_is_history():
    notes = [_n("2026-08-23T08:00:00", "Solicitado parecer da nefrologia, avaliado ontem.")]
    assert apply_all_rules(notes) == []


@pytest.mark.parametrize("sentence,expected", [
    # Revisão adversarial (Fase 3): o exame com RESULTADO na frase é contexto;
    # o pedido é o que vem depois do verbo -- todos os da lista.
    ("TC de crânio com AVC isquêmico em território de ACM esquerda, solicito RNM de crânio.",
     {("DIAGNOSTICO", "RESSONÂNCIA DE CRÂNIO")}),
    ("ECO com FE 30%, solicito cateterismo.", set()),
    ("USG de abdome com colelitíase, solicitado parecer da cirurgia geral.", {("INTERCONSULTA", "CIRURGIA GERAL")}),
    ("Hemoculturas negativas, solicito TC de tórax.", {("DIAGNOSTICO", "TOMOGRAFIA DE TÓRAX")}),
    ("Solicito TC de crânio e USG de abdome.",
     {("DIAGNOSTICO", "TOMOGRAFIA DE CRÂNIO"), ("DIAGNOSTICO", "ULTRASSOM DE ABDOME")}),
    ("Solicitados: RX de tórax, USG de abdome total e ECO.",
     {("DIAGNOSTICO", "RAIO-X DE TÓRAX"), ("DIAGNOSTICO", "ULTRASSOM DE ABDOME"), ("DIAGNOSTICO", "ECOCARDIOGRAMA")}),
    # Quem cita uma avaliação que já aconteceu, ou nega o pedido, não pede.
    ("Solicitado ECO conforme avaliação da cardiologia.", {("DIAGNOSTICO", "ECOCARDIOGRAMA")}),
    ("Solicitados exames conforme parecer da nefrologia.", set()),
    ("Não solicitado parecer da cardiologia no momento.", set()),
    ("Não solicito TC de crânio, sem sinais focais.", set()),
    ("Não foi solicitada TC de controle.", set()),
    ("Sem pedido de TC no momento.", set()),
    ("Solicitado parecer da vascular devido a TVP vista em doppler.", {("INTERCONSULTA", "CIRURGIA VASCULAR")}),
    # Formas comuns de pedir que a regra não pegava.
    ("Solicito parecer p/ cardiologia.", {("INTERCONSULTA", "CARDIOLOGIA")}),
    ("Solicito IC p/ Nefro.", {("INTERCONSULTA", "NEFROLOGIA")}),
    ("Solicito avaliação e conduta da cardiologia.", {("INTERCONSULTA", "CARDIOLOGIA")}),
    ("Parecer da nefrologia solicitado.", {("INTERCONSULTA", "NEFROLOGIA")}),
    ("Solicito parecer ao serviço de cirurgia torácica.", {("INTERCONSULTA", "CIRURGIA TORÁCICA")}),
    ("Solicito avaliação do Serviço Social.", {("INTERCONSULTA", "SERVIÇO SOCIAL")}),
    ("Solicito avaliação da Cx Geral.", {("INTERCONSULTA", "CIRURGIA GERAL")}),
    ("Solicito avaliação com urgência da cirurgia vascular.", {("INTERCONSULTA", "CIRURGIA VASCULAR")}),
    ("Solicitado avaliação da cirurgia de cabeça e pescoço para TQT.",
     {("INTERCONSULTA", "CIRURGIA DE CABEÇA E PESCOÇO")}),
])
def test_requests_found_per_sentence(sentence, expected):
    assert {(f.category, f.alvo) for f in apply_all_rules([_n("2026-08-21T10:00:00", sentence)])} == expected


@pytest.mark.parametrize("sentence,expected_date", [
    # Data futura é agendamento, não o pedido (antes virava o ano anterior).
    ("Solicitada nova TC de abdome, agendada para 30/08.", "2026-08-26T08:00:00"),
    ("Solicitado ecocardiograma, agendado para 25/09.", "2026-08-26T08:00:00"),
    # Data sem ligação com o pedido, ou fração clínica, não muda a data.
    ("Nova piora neurológica, força grau 4/5 em MSD, solicito TC de crânio.", "2026-08-26T08:00:00"),
    ("Cefaleia intensa, PA 18/10, solicitada TC de crânio.", "2026-08-26T08:00:00"),
    ("Reavaliar coleção vista na TC de 05/08, solicito USG de abdome.", "2026-08-26T08:00:00"),
    # Data ligada ao verbo vale -- mas nunca antes da internação.
    ("Solicitado parecer da nefrologia em 22/08, sem resposta.", "2026-08-22T00:00:00"),
    ("Solicitado parecer da nefrologia em 22/06, sem resposta.", "2026-08-26T08:00:00"),
])
def test_request_date(sentence, expected_date):
    [finding] = apply_all_rules([_n("2026-08-26T08:00:00", sentence)], admission_cutoff="2026-08-12")
    assert finding.evidence_date == expected_date


def test_two_different_exams_are_two_findings():
    notes = [_n("2026-08-20T08:00:00", "Solicitada tomografia de tórax. Solicitado ultrassom de abdome.")]
    assert {f.alvo for f in apply_all_rules(notes)} == {"TOMOGRAFIA DE TÓRAX", "ULTRASSOM DE ABDOME"}


def test_no_pending_items_yields_no_findings():
    notes = _load_structured_notes("no_pending_items.txt")
    assert apply_all_rules(notes) == []


def test_empty_record_yields_no_findings():
    notes = _load_structured_notes("empty_record.txt")
    assert apply_all_rules(notes) == []


def test_every_finding_has_non_empty_evidence():
    for fixture in ("awaiting_exam.txt", "awaiting_consult.txt", "long_admission.txt"):
        notes = _load_structured_notes(fixture)
        for finding in apply_all_rules(notes):
            assert finding.evidence.strip() != ""


# ---------------------------------------------------------- most_recent_note

def _note(timestamp, specialty="Clínica Médica") -> StructuredNote:
    return StructuredNote(timestamp=timestamp, specialty=specialty, source_type="Evolução", text="x")


def test_most_recent_note_picks_the_largest_timestamp_regardless_of_order():
    notes = [_note("2026-08-10T08:00:00"), _note("2026-08-20T08:00:00"), _note("2026-08-15T08:00:00")]
    assert most_recent_note(notes).timestamp == "2026-08-20T08:00:00"


def test_most_recent_note_returns_none_when_no_timestamp():
    assert most_recent_note([_note(None), _note(None)]) is None


def test_most_recent_note_returns_none_for_empty_list():
    assert most_recent_note([]) is None


def test_most_recent_note_skips_notes_without_timestamp_in_a_mixed_list():
    """Gap apontado na revisão de conformidade (DEC-064): os testes
    anteriores só cobriam "todas com timestamp" ou "todas sem" -- nunca uma
    lista mista, que é o caso real (uma nota mal reconhecida no meio de
    outras válidas)."""
    notes = [_note("2026-08-10T08:00:00"), _note(None), _note("2026-08-20T08:00:00"), _note(None)]
    assert most_recent_note(notes).timestamp == "2026-08-20T08:00:00"
