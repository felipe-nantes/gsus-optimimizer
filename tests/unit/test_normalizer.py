from app.extraction.normalizer import normalize_datetime, normalize_text


def test_normalize_text_collapses_whitespace_and_crlf():
    raw = "Paciente   estável\r\ncom   dor.\r\n\r\n\r\nMantido plano."
    result = normalize_text(raw)
    assert result == "Paciente estável\ncom dor.\n\nMantido plano."


def test_normalize_text_strips_edges():
    assert normalize_text("   texto com espaço   ") == "texto com espaço"


def test_normalize_text_empty_returns_empty():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def test_normalize_datetime_valid():
    assert normalize_datetime("20/08/2026 14:30") == "2026-08-20T14:30:00"


def test_normalize_datetime_invalid_returns_none():
    assert normalize_datetime("data qualquer") is None
    assert normalize_datetime(None) is None
