from pathlib import Path

from app.extraction.parser import parse_note_blocks

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "notes"


def _load(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_two_notes_awaiting_exam():
    blocks = parse_note_blocks(_load("awaiting_exam.txt"))
    assert len(blocks) == 2
    assert blocks[0]["timestamp_raw"] == "20/08/2026 08:00"
    assert blocks[0]["specialty"] is None
    assert blocks[0]["source_type"] == "Medico clinico"
    assert "tomografia de abdome" in blocks[0]["text"]


def test_parse_empty_record_returns_empty_list():
    assert parse_note_blocks(_load("empty_record.txt")) == []
    assert parse_note_blocks("") == []
    assert parse_note_blocks(None) == []


def test_parse_malformed_note_is_kept_failsoft():
    blocks = parse_note_blocks(_load("malformed_note.txt"))
    assert len(blocks) == 1
    assert blocks[0]["timestamp_raw"] is None
    assert blocks[0]["specialty"] is None
    assert blocks[0]["source_type"] is None
    assert "Paciente evoluindo bem" in blocks[0]["text"]


def test_parse_multiple_notes_extracts_role_per_note():
    blocks = parse_note_blocks(_load("multiple_specialties.txt"))
    roles = [b["source_type"] for b in blocks]
    assert roles == ["Medico clinico", "Fisioterapeuta", "Nutricionista", "Psicologo"]
    # minimização de PHI: nome do profissional nunca vira campo estruturado
    assert all(b["specialty"] is None for b in blocks)


def test_parse_does_not_split_on_blank_lines_without_header():
    """Confirma que a divisão é pelo padrão de cabeçalho, não por linha em
    branco -- texto com blank line dentro do corpo de UMA evolução não deve
    virar dois blocos."""
    raw = "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\nLinha um.\n\nLinha dois (mesma evolucao)."
    blocks = parse_note_blocks(raw)
    assert len(blocks) == 1
    assert "Linha um." in blocks[0]["text"]
    assert "Linha dois" in blocks[0]["text"]


def test_parse_multiple_headers_without_blank_line_separator():
    """Simula o texto extraído do GSUS real: vários cabeçalhos em
    sequência sem separação garantida por linha em branco."""
    raw = (
        "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Primeira evolucao.\n"
        "20/08/2026 09:00 - Profissional Teste Dois (Tecnico de enfermagem)\n"
        "Segunda evolucao."
    )
    blocks = parse_note_blocks(raw)
    assert len(blocks) == 2
    assert blocks[0]["source_type"] == "Medico clinico"
    assert "Primeira evolucao" in blocks[0]["text"]
    assert blocks[1]["source_type"] == "Tecnico de enfermagem"
    assert "Segunda evolucao" in blocks[1]["text"]
