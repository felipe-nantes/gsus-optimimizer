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


# ------------------------------------------------------------- DEC-119

def test_normalize_llm_datetime_accepts_iso_br_datetime_and_br_date():
    from app.extraction.normalizer import normalize_llm_datetime

    assert normalize_llm_datetime("2026-08-20T08:00:00") == "2026-08-20T08:00:00"
    assert normalize_llm_datetime("2026-08-20 08:00:00+00:00") == "2026-08-20T08:00:00"
    assert normalize_llm_datetime("20/08/2026 08:00") == "2026-08-20T08:00:00"
    assert normalize_llm_datetime("20/08/2026 08:00:30") == "2026-08-20T08:00:30"
    assert normalize_llm_datetime("20/08/2026") == "2026-08-20T00:00:00"
    assert normalize_llm_datetime(" 20/08/2026 ") == "2026-08-20T00:00:00"


def test_normalize_llm_datetime_never_invents():
    from app.extraction.normalizer import normalize_llm_datetime

    assert normalize_llm_datetime(None) is None
    assert normalize_llm_datetime("") is None
    assert normalize_llm_datetime("ontem") is None
    assert normalize_llm_datetime("31/02/2026") is None  # data impossível
    assert normalize_llm_datetime("null") is None


def test_normalize_iso_date_accepts_iso_and_br_formats():
    from app.extraction.normalizer import normalize_iso_date

    assert normalize_iso_date("2026-08-25") == "2026-08-25"
    assert normalize_iso_date("2026-08-25T10:00:00") == "2026-08-25"
    assert normalize_iso_date("25/08/2026") == "2026-08-25"
    assert normalize_iso_date("25/08/2026 10:00") == "2026-08-25"
    assert normalize_iso_date("31/02/2026") is None
    assert normalize_iso_date("em breve") is None
    assert normalize_iso_date(None) is None
