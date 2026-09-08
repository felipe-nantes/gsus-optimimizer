"""Testa o limite opcional de dias por paciente na extração (achado real,
2026-09-01 -- pedido do usuário pra acelerar um catch-up pontual do banco):
sem limite, o primeiro contato com uma internação longa/densa abre TODOS os
dias do episódio atual, mesmo quando o objetivo é só pegar o mais recente
pra atualizar rápido. `max_days_to_open` (None por padrão -- comportamento
normal) restringe aos N dias mais recentes, mesmo pra dia nunca visto
antes. Sem Playwright/browser real -- só fakes."""
from app.gsus import records
from app.gsus.records import DAY_TOGGLE_SELECTOR, _collect_days, _days_beyond_cap


def _day_row(day_month_year: str, episode="codABC1", open_=True):
    return [f"historicoEvolucaodata{day_month_year}{episode}Item", f"historicoAtendimento{episode}", open_]


# ------------------------------------------------------- _days_beyond_cap

def test_days_beyond_cap_keeps_only_the_n_most_recent():
    day_rows = [
        [f"historicoEvolucaodata29/08/2026codABC1Item", "historicoAtendimentocodABC1"],
        [f"historicoEvolucaodata30/08/2026codABC1Item", "historicoAtendimentocodABC1"],
        [f"historicoEvolucaodata31/08/2026codABC1Item", "historicoAtendimentocodABC1"],
    ]

    skip = _days_beyond_cap(day_rows, max_days_to_open=1)

    assert skip == {"historicoEvolucaodata29/08/2026codABC1Item", "historicoEvolucaodata30/08/2026codABC1Item"}


def test_days_beyond_cap_keeps_everything_when_cap_covers_all_days():
    day_rows = [
        ["historicoEvolucaodata30/08/2026codABC1Item", "historicoAtendimentocodABC1"],
        ["historicoEvolucaodata31/08/2026codABC1Item", "historicoAtendimentocodABC1"],
    ]

    assert _days_beyond_cap(day_rows, max_days_to_open=5) == set()


def test_days_beyond_cap_never_discards_a_day_without_a_recognizable_date():
    """Fail-safe (mesmo raciocínio do `_days_to_skip`): sem saber a idade,
    melhor extrair a mais do que arriscar perder um dia genuinamente novo."""
    day_rows = [
        ["historicoEvolucaodata31/08/2026codABC1Item", "historicoAtendimentocodABC1"],
        ["diaAbertoAutomaticamenteSemDataNoIdItem", "historicoAtendimentocodABC1"],  # sem data no id
    ]

    assert _days_beyond_cap(day_rows, max_days_to_open=1) == set()


# --------------------------------------------------- _collect_days (integração)

class _FakeLocator:
    def __init__(self):
        self._text = "cabeçalho do dia\ntexto da evolução"

    def is_visible(self):
        return True

    def click(self, timeout=None):
        pass

    def inner_text(self):
        return self._text

    def count(self):
        return 1


class _FakeCollectDaysPage:
    """`evaluate` distingue a listagem inicial de dias (chamada com
    `DAY_TOGGLE_SELECTOR`) das checagens de `_is_expanded`/`_body_metrics`
    (chamada com o id do corpo) -- reporta sempre "já expandido" pra essas
    últimas, evitando precisar simular o clique+espera de verdade."""

    def __init__(self, day_rows):
        self._day_rows = day_rows

    def evaluate(self, script, arg=None):
        if arg == DAY_TOGGLE_SELECTOR:
            return self._day_rows
        return [10, 999]  # _body_metrics: corpo "grande" -- já expandido

    def locator(self, selector):
        return _FakeLocator()


def test_collect_days_honors_max_days_to_open_even_for_never_seen_patient():
    """Achado real: paciente nunca extraído antes (skip_days vazio) ainda
    assim deve respeitar o limite -- sem isso, o primeiro contato com uma
    internação longa abre TODOS os dias."""
    day_rows = [_day_row(d) for d in ("29/08/2026", "30/08/2026", "31/08/2026")]
    page = _FakeCollectDaysPage(day_rows)

    days, total = _collect_days(page, deadline=float("inf"), skip_days=set(), max_days_to_open=1)

    assert total == 3  # todos no escopo
    assert len(days) == 1  # só o mais recente foi de fato aberto
    assert days[0]["date"] == "2026-08-31"


def test_collect_days_without_cap_opens_every_day_as_before():
    """Comportamento normal (None) não muda -- garante que o parâmetro
    novo é puramente opt-in."""
    day_rows = [_day_row(d) for d in ("30/08/2026", "31/08/2026")]
    page = _FakeCollectDaysPage(day_rows)

    days, total = _collect_days(page, deadline=float("inf"), skip_days=set(), max_days_to_open=None)

    assert total == 2
    assert len(days) == 2


# ------------------------------------------------------------- DEC-119

from app.gsus.records import CURRENT_ADMISSION_MARKER  # noqa: E402


class _FakeCollectDaysPageWithHeader(_FakeCollectDaysPage):
    """Além da listagem de dias, responde à pergunta "qual card tem o
    cabeçalho 'Permanece Internado'?" (arg = marcador) com `current_card_id`."""

    def __init__(self, day_rows, current_card_id):
        super().__init__(day_rows)
        self._current_card_id = current_card_id

    def evaluate(self, script, arg=None):
        if arg == CURRENT_ADMISSION_MARKER:
            return self._current_card_id
        return super().evaluate(script, arg)


def test_collect_days_keeps_only_the_current_admission_card_even_when_old_episodes_are_open():
    """Achado real 2026-09-07 (DEC-119): `_expand_all` abre todos os
    episódios, então "card aberto" deixava passar dias de 2014-2025. Com o
    cabeçalho localizado, só os dias do card da internação atual entram."""
    day_rows = [
        _day_row("14/02/2022", episode="codOLD1", open_=True),
        _day_row("15/02/2022", episode="codOLD1", open_=True),
        _day_row("05/09/2026", episode="codCUR9", open_=True),
        _day_row("06/09/2026", episode="codCUR9", open_=True),
    ]
    page = _FakeCollectDaysPageWithHeader(day_rows, current_card_id="historicoAtendimentocodCUR9")

    days, total = _collect_days(page, deadline=float("inf"), skip_days=set(), current_episode_only=True)

    assert total == 2
    assert sorted(d["date"] for d in days) == ["2026-09-05", "2026-09-06"]


def test_collect_days_falls_back_to_open_episode_when_header_not_found():
    day_rows = [
        _day_row("14/02/2022", episode="codOLD1", open_=False),
        _day_row("06/09/2026", episode="codCUR9", open_=True),
    ]
    page = _FakeCollectDaysPageWithHeader(day_rows, current_card_id="")

    days, total = _collect_days(page, deadline=float("inf"), skip_days=set(), current_episode_only=True)

    assert total == 1
    assert [d["date"] for d in days] == ["2026-09-06"]


def test_collect_days_ignores_header_answer_that_matches_no_day():
    day_rows = [_day_row("06/09/2026", episode="codCUR9", open_=True)]
    page = _FakeCollectDaysPageWithHeader(day_rows, current_card_id="historicoAtendimentocodXYZ")

    days, total = _collect_days(page, deadline=float("inf"), skip_days=set(), current_episode_only=True)

    assert total == 1
    assert len(days) == 1
