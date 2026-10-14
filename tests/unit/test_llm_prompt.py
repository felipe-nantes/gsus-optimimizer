"""Testes de app/analysis/llm.py::build_prompt -- especificamente o marcador
de "evolução mais recente" (DEC-058), que corrige um achado real: o LLM
justificou necessidade_hospitalar citando uma evolução antiga, ignorando que
a mais recente já indicava alta."""
from app.analysis.llm import build_prompt
from app.analysis.rules import StructuredNote

MARKER = "EVOLUÇÃO MAIS RECENTE"


def _note(timestamp, text="texto"):
    return StructuredNote(timestamp=timestamp, specialty="Clínica Médica", source_type="Evolução", text=text)


def test_marks_the_chronologically_last_note_as_most_recent():
    notes = [
        _note("2026-08-10T08:00:00", "mais antiga"),
        _note("2026-08-20T08:00:00", "mais recente"),
        _note("2026-08-15T08:00:00", "meio"),
    ]
    prompt = build_prompt(previous_state=None, new_notes=notes)

    lines = prompt.splitlines()
    marked_lines = [line for line in lines if MARKER in line]
    assert len(marked_lines) == 1
    assert "2026-08-20T08:00:00" in marked_lines[0]


def test_marker_follows_timestamp_value_not_list_position():
    """Regressão direta: a nota mais recente pode vir em qualquer posição da
    lista (a ordem de extração do GSUS não é uma garantia deste módulo) --
    o marcador tem que seguir o timestamp, nunca a posição."""
    notes = [
        _note("2026-08-20T08:00:00", "mais recente, mas primeira da lista"),
        _note("2026-08-10T08:00:00", "mais antiga, mas última da lista"),
    ]
    prompt = build_prompt(previous_state=None, new_notes=notes)

    marked_lines = [line for line in prompt.splitlines() if MARKER in line]
    assert len(marked_lines) == 1
    assert "2026-08-20T08:00:00" in marked_lines[0]


def test_no_marker_when_no_note_has_a_timestamp():
    notes = [_note(None, "sem data"), _note(None, "também sem data")]
    prompt = build_prompt(previous_state=None, new_notes=notes)
    assert MARKER not in prompt


def test_no_marker_when_there_are_no_new_notes():
    prompt = build_prompt(previous_state=None, new_notes=[])
    assert MARKER not in prompt


def test_single_note_is_marked_as_most_recent():
    prompt = build_prompt(previous_state=None, new_notes=[_note("2026-08-20T08:00:00")])
    assert MARKER in prompt


# --------------------------------------------- LLM-006 (pedido do auditor, DEC-129)

PHYSICIAN_MARKER = "EVOLUÇÃO MÉDICA MAIS RECENTE"


def _role_note(timestamp, role, text="texto"):
    return StructuredNote(timestamp=timestamp, specialty=None, source_type=role, text=text)


def test_later_nursing_note_never_takes_the_physician_marker():
    """Seção 2 do auditor: "um registro recente de enfermagem não deve
    substituir a evolução médica" -- antes o marcador ia pra qualquer nota
    pela hora, e a anotação de enfermagem virava a situação de hoje."""
    notes = [
        _role_note("2026-08-20T09:00:00", "Médico clínico", "evolução médica"),
        _role_note("2026-08-20T22:00:00", "Técnico de enfermagem", "anotação de enfermagem"),
    ]
    prompt = build_prompt(previous_state=None, new_notes=notes)
    marked = [line for line in prompt.splitlines() if PHYSICIAN_MARKER in line]
    assert len(marked) == 1
    assert "2026-08-20T09:00:00" in marked[0] and "EVOLUÇÃO MÉDICA" in marked[0]


def test_each_note_header_names_its_source_and_role():
    notes = [
        _role_note("2026-08-20T09:00:00", "Médico infectologista"),
        _role_note("2026-08-20T10:00:00", "Fisioterapeuta geral"),
        _role_note("2026-08-20T11:00:00", "Enfermeiro"),
        _role_note("2026-08-20T12:00:00", "Direita"),
    ]
    prompt = build_prompt(previous_state=None, new_notes=notes)
    assert "[2026-08-20T09:00:00 - EVOLUÇÃO MÉDICA - Médico infectologista]" in prompt
    assert "[2026-08-20T10:00:00 - EQUIPE MULTIPROFISSIONAL - Fisioterapeuta geral]" in prompt
    assert "[2026-08-20T11:00:00 - ENFERMAGEM - Enfermeiro]" in prompt
    assert "[2026-08-20T12:00:00 - FONTE NÃO IDENTIFICADA - Direita]" in prompt
    assert "sem especialidade" not in prompt  # campo que o parser nunca preenche


def test_notes_are_presented_in_chronological_order():
    notes = [_role_note("2026-08-22T08:00:00", "Médico clínico", "C"), _role_note("2026-08-20T08:00:00", "Médico clínico", "A")]
    prompt = build_prompt(previous_state=None, new_notes=notes)
    assert prompt.index("2026-08-20T08:00:00") < prompt.index("2026-08-22T08:00:00")


def test_census_location_block_is_the_first_thing_in_the_prompt():
    prompt = build_prompt(
        previous_state=None, new_notes=[_role_note("2026-08-20T08:00:00", "Médico clínico")],
        patient_context={"unit": "UTI Geral (12 leitos)", "bed": "UTI-03", "admission_date": "18/08/2026"},
    )
    first_line = prompt.splitlines()[0]
    assert first_line.startswith("LOCALIZAÇÃO ATUAL (censo do GSUS -- fonte de verdade)")
    assert "UTI Geral (12 leitos)" in first_line and "UTI-03" in first_line and "18/08/2026" in first_line
    assert "pediátrico" not in prompt


def test_pediatric_unit_gets_the_no_adult_ranges_instruction():
    prompt = build_prompt(
        previous_state=None, new_notes=[], patient_context={"unit": "Neonatal - UTI (10 leitos)", "bed": "1", "admission_date": None},
    )
    assert "Setor pediátrico/neonatal: não classifique sinais vitais pelas faixas de adulto." in prompt


def test_system_prompt_carries_the_auditor_rules():
    from app.analysis.llm import SYSTEM_PROMPT

    for fragment in (
        "EVOLUÇÃO MÉDICA MAIS RECENTE", "FONTES DO PRONTUÁRIO", "PELA <ESPECIALIDADE>", "LOCALIZAÇÃO",
        "leito de observação", "acesso venoso periférico sem complicação", "recebe cuidados de enfermagem",
        "FR 12-20 irpm", "FR de 18 irpm é normal", "VERDE quando", "VERMELHO quando",
        "nunca repita o mesmo diagnóstico",
    ):
        assert fragment in SYSTEM_PROMPT, fragment


def test_prompt_and_notes_budget_stay_within_the_calibrated_total():
    """Achado da revisão adversarial do LLM-006: a calibração real
    (DEC-093/094) garantiu o TOTAL system prompt + notas dentro do
    --ctx-size 8192 (com resposta e reparo por cima). Aumentar o prompt sem
    baixar o orçamento das notas volta a estourar o contexto nos pacientes
    de internação longa."""
    from app.analysis.llm import SYSTEM_PROMPT
    from app.orchestrator import CALIBRATED_PROMPT_PLUS_NOTES_CHARS, MAX_NOTES_CHARS_FOR_LLM

    assert len(SYSTEM_PROMPT) + MAX_NOTES_CHARS_FOR_LLM <= CALIBRATED_PROMPT_PLUS_NOTES_CHARS
