"""Testes da captura dia-a-dia (DEC-044) -- só as partes puras, sem
Playwright/GSUS real. Dados 100% fictícios."""
import pytest

from app.gsus.records import DAY_ID_PATTERN, _iso_from_br_date, _render_history


class FakePage:
    """Substitui só o que `_render_history` usa: localizar o card do
    episódio e ler a primeira linha dele."""

    def __init__(self, headers: dict[str, str]):
        self._headers = headers

    def locator(self, selector: str):
        # `_episode_header_text` monta `[id="<chave>Item"]`
        wanted = selector.split('"')[1]
        return FakeLocator(self._headers.get(wanted))


class FakeLocator:
    def __init__(self, text: str | None):
        self._text = text

    def count(self) -> int:
        return 0 if self._text is None else 1

    @property
    def first(self):
        return self

    def inner_text(self) -> str:
        return self._text or ""


def test_day_id_pattern_extracts_date_and_episode():
    match = DAY_ID_PATTERN.match("historicoEvolucaodata21/08/2026codInternacao5951310Item")
    assert match is not None
    assert match.group("data") == "21/08/2026"
    assert match.group("episodio") == "codInternacao5951310"


def test_day_id_pattern_covers_other_episode_types():
    match = DAY_ID_PATTERN.match("historicoEvolucaodata15/08/2026codProntoAtendimento1234567Item")
    assert match is not None
    assert match.group("episodio") == "codProntoAtendimento1234567"


@pytest.mark.parametrize("bad_id", [
    "historicoEvolucao7Item",  # evolução individual, não dia
    "historicoAtendimento0Item",  # episódio
    "historicoEvolucaodata21/08/2026codInternacao5951310",  # sem sufixo Item
])
def test_day_id_pattern_rejects_other_levels(bad_id):
    assert DAY_ID_PATTERN.match(bad_id) is None


def test_iso_from_br_date():
    assert _iso_from_br_date("21/08/2026") == "2026-08-21"
    assert _iso_from_br_date("formato estranho") == "formato estranho"


def test_render_history_groups_by_episode_and_sorts_newest_first():
    days = [
        {"episode": "historicoAtendimento1", "date": "2026-08-15",
         "header": "15 de agosto de 2026 - Sábado", "text": "conteudo PA 15"},
        {"episode": "historicoAtendimento0", "date": "2026-08-20",
         "header": "20 de agosto de 2026 - Quinta-Feira", "text": "conteudo INT 20"},
        {"episode": "historicoAtendimento0", "date": "2026-08-21",
         "header": "21 de agosto de 2026 - Sexta-Feira", "text": "conteudo INT 21"},
    ]
    page = FakePage({
        "historicoAtendimento0Item": "Internação (20 de Agosto de 2026 - Permanece Internado)",
        "historicoAtendimento1Item": "Pronto-Atendimento (15 de Agosto de 2026 - 16 de Agosto de 2026)",
    })

    result = _render_history(page, days)

    # episódio mais recente primeiro
    assert result.index("Internação 1 de 2") < result.index("Internação 2 de 2")
    assert result.index("conteudo INT 21") < result.index("conteudo PA 15")
    # dias mais recentes primeiro dentro do episódio
    assert result.index("conteudo INT 21") < result.index("conteudo INT 20")
    assert "Pronto-Atendimento (15 de Agosto de 2026" in result


def test_render_history_marks_empty_day():
    days = [{"episode": "historicoAtendimento0", "date": "2026-08-21", "header": "21 de agosto", "text": ""}]
    result = _render_history(FakePage({}), days)
    assert "(sem registros neste dia)" in result


def test_render_history_without_episode_header_still_labels():
    days = [{"episode": "historicoAtendimento0", "date": "2026-08-21", "header": "21 de agosto", "text": "algo"}]
    result = _render_history(FakePage({}), days)  # card do episódio não encontrado
    assert "Internação 1 de 1" in result
    assert "algo" in result


def test_day_date_prefers_id_then_header():
    from app.gsus.records import _day_date

    # id estruturado tem prioridade
    assert _day_date("historicoEvolucaodata21/08/2026codInternacao5951310Item", []) == "2026-08-21"
    # dia auto-aberto pela página: id sem data -> cai para o cabeçalho (DEC-046)
    assert _day_date("historicoEvolucao5951310Item",
                     ["▶ 21 de agosto de 2026 - Sexta-Feira (Hoje)"]) == "2026-08-21"
    # sem nenhum dos dois: string vazia, sem quebrar
    assert _day_date("historicoEvolucao5951310Item", []) == ""


# ------------------------------------------------- pular dias já processados

def _rows(*dates_and_ids):
    """[(toggle_id, episode_key)] no formato que `_days_to_skip` recebe."""
    return [[toggle_id, "historicoAtendimento0"] for toggle_id in dates_and_ids]


def _day_id(date_br: str) -> str:
    return f"historicoEvolucaodata{date_br}codInternacao5951310Item"


def test_days_to_skip_empty_when_nothing_known():
    from app.gsus.records import _days_to_skip

    rows = _rows(_day_id("21/08/2026"), _day_id("20/08/2026"))
    assert _days_to_skip(rows, set()) == set()


def test_days_to_skip_keeps_two_most_recent_even_if_known():
    """Dia em curso ainda recebe evolução depois da rodada anterior."""
    from app.gsus.records import _days_to_skip

    rows = _rows(_day_id("21/08/2026"), _day_id("20/08/2026"),
                 _day_id("19/08/2026"), _day_id("18/08/2026"))
    known = {"2026-08-21", "2026-08-20", "2026-08-19", "2026-08-18"}

    skipped = _days_to_skip(rows, known)

    assert _day_id("21/08/2026") not in skipped  # mais recente: sempre reabre
    assert _day_id("20/08/2026") not in skipped  # 2º mais recente: idem
    assert _day_id("19/08/2026") in skipped
    assert _day_id("18/08/2026") in skipped


def test_days_to_skip_never_skips_unknown_day():
    from app.gsus.records import _days_to_skip

    rows = _rows(_day_id("21/08/2026"), _day_id("20/08/2026"), _day_id("19/08/2026"))
    skipped = _days_to_skip(rows, {"2026-08-21"})  # só o mais recente é conhecido
    assert skipped == set()  # e ele está entre os sempre-reabertos


def test_days_to_skip_never_skips_day_without_date_in_id():
    """Fail-safe: sem data no id, extrai a mais em vez de perder dado."""
    from app.gsus.records import _days_to_skip

    rows = _rows(_day_id("21/08/2026"), _day_id("20/08/2026"), "historicoEvolucao5951310Item")
    skipped = _days_to_skip(rows, {"2026-08-21", "2026-08-20"})
    assert "historicoEvolucao5951310Item" not in skipped
