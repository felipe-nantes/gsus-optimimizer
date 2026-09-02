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
