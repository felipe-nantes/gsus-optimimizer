from pathlib import Path

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


def test_awaiting_exam_yields_active_exam_finding():
    notes = _load_structured_notes("awaiting_exam.txt")
    findings = apply_all_rules(notes)
    exam_findings = [f for f in findings if f.category == CATEGORY_EXAM]
    assert len(exam_findings) == 1
    assert "tomografia" in exam_findings[0].description.lower()
    assert exam_findings[0].resolved is False


def test_resolved_exam_yields_a_resolved_finding():
    """`apply_all_rules` devolve o achado MARCADO resolvido, não o omite --
    achado real (DEC-064): omitir não deixava o orchestrator saber se era
    "resolvido de verdade" ou "não apareceu nesta janela" (ver DEC-064 em
    orchestrator.py)."""
    notes = _load_structured_notes("resolved_exam.txt")
    findings = apply_all_rules(notes)
    exam_findings = [f for f in findings if f.category == CATEGORY_EXAM]
    assert len(exam_findings) == 1
    assert exam_findings[0].resolved is True


def test_awaiting_consult_yields_active_consult_finding():
    notes = _load_structured_notes("awaiting_consult.txt")
    findings = apply_all_rules(notes)
    consult_findings = [f for f in findings if f.category == CATEGORY_CONSULT]
    assert len(consult_findings) == 1
    assert "cirurgia" in consult_findings[0].description.lower()
    assert consult_findings[0].resolved is False


def test_resolved_consult_yields_a_resolved_finding():
    notes = _load_structured_notes("resolved_consult.txt")
    findings = apply_all_rules(notes)
    consult_findings = [f for f in findings if f.category == CATEGORY_CONSULT]
    assert len(consult_findings) == 1
    assert consult_findings[0].resolved is True


# ---------------------------------------------- "aguardando laudo/parecer" (DEC-064)
# Achado real da revisão de conformidade: "aguardando laudo"/"aguarda
# parecer" citam a palavra que marcava conclusão, mas significam o oposto.

def test_awaiting_laudo_does_not_resolve_the_exam():
    notes = [
        StructuredNote(timestamp="2026-08-20T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Solicitada tomografia de tórax."),
        StructuredNote(timestamp="2026-08-21T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Mantém aguardando laudo da tomografia."),
    ]
    exam_findings = [f for f in apply_all_rules(notes) if f.category == CATEGORY_EXAM]
    assert len(exam_findings) == 1
    assert exam_findings[0].resolved is False


def test_genuine_completion_still_resolves_the_exam():
    """Garante que o fix não ficou conservador demais -- conclusão de
    verdade (sem "aguard" na mesma frase) continua resolvendo."""
    notes = [
        StructuredNote(timestamp="2026-08-20T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Solicitada tomografia de tórax."),
        StructuredNote(timestamp="2026-08-21T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Tomografia de tórax realizada, sem alterações."),
    ]
    exam_findings = [f for f in apply_all_rules(notes) if f.category == CATEGORY_EXAM]
    assert len(exam_findings) == 1
    assert exam_findings[0].resolved is True


def test_awaiting_parecer_does_not_resolve_the_consult():
    notes = [
        StructuredNote(timestamp="2026-08-20T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Solicitada avaliação da cirurgia torácica."),
        StructuredNote(timestamp="2026-08-21T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Aguarda parecer da cirurgia torácica."),
    ]
    consult_findings = [f for f in apply_all_rules(notes) if f.category == CATEGORY_CONSULT]
    assert len(consult_findings) == 1
    assert consult_findings[0].resolved is False


def test_genuine_consult_completion_still_resolves():
    notes = [
        StructuredNote(timestamp="2026-08-20T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Solicitada avaliação da cirurgia torácica."),
        StructuredNote(timestamp="2026-08-21T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                       text="Parecer da cirurgia torácica: sem indicação cirúrgica no momento."),
    ]
    consult_findings = [f for f in apply_all_rules(notes) if f.category == CATEGORY_CONSULT]
    assert len(consult_findings) == 1
    assert consult_findings[0].resolved is True


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
