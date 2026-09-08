"""Testa a proteção de retry do clique de menu 'Atendimento'/'Pesquisar
Prontuário' (achado real E2E-001, 2026-08-27 -- ver DECISIONS.md DEC-081):
antes desta correção, um timeout nesse clique escapava do loop de retry
inteiro sem NUNCA tentar de novo -- numa execução real de 183 pacientes,
178 falharam exatamente aqui. Sem Playwright/browser real -- só fakes."""
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

import pytest

from app.gsus import records
from app.gsus.records import (
    CURRENT_ADMISSION_MARKER,
    GSUSRecordError,
    SEARCH_RETRY_ATTEMPTS,
    _click_menu_to_search_screen,
    _collect_days,
    _expand_all,
    open_current_admission,
)


class _FakeClickTarget:
    def __init__(self, should_timeout=False):
        self.should_timeout = should_timeout
        self.clicks = 0

    def locator(self, selector):  # DEC-120: `.locator("visible=true").first`
        return self

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.clicks += 1
        if self.should_timeout:
            raise PlaywrightTimeoutError("simulado: menu não respondeu")


class _FakeMenuPage:
    def __init__(self, atendimento_timeout=False, pesquisar_timeout=False):
        self._atendimento = _FakeClickTarget(atendimento_timeout)
        self._pesquisar = _FakeClickTarget(pesquisar_timeout)

    def get_by_text(self, text, exact=True):
        if text == "Atendimento":
            return self._atendimento
        if text == "Pesquisar Prontuário":
            return self._pesquisar
        raise AssertionError(f"texto inesperado: {text}")


def test_click_menu_returns_true_when_both_clicks_succeed():
    assert _click_menu_to_search_screen(_FakeMenuPage()) is True


def test_click_menu_returns_false_when_atendimento_click_times_out():
    assert _click_menu_to_search_screen(_FakeMenuPage(atendimento_timeout=True)) is False


def test_click_menu_returns_false_when_pesquisar_prontuario_click_times_out():
    assert _click_menu_to_search_screen(_FakeMenuPage(pesquisar_timeout=True)) is False


class _FakeResultLocator:
    def __init__(self):
        self.clicked = False

    def wait_for(self, timeout=None):
        return None  # sucesso imediato -- marcador "encontrado" na hora

    def click(self):
        self.clicked = True


class _FakeResultPage:
    def __init__(self):
        self.locator_obj = _FakeResultLocator()

    def get_by_text(self, text, exact=True):
        assert text == CURRENT_ADMISSION_MARKER
        return self

    @property
    def first(self):
        return self.locator_obj


def test_open_current_admission_retries_instead_of_escaping_on_menu_click_timeout(monkeypatch):
    """Reproduz o achado real: clique do menu falha na 1ª tentativa (como
    aconteceu 178 de 183 vezes numa execução real) -- antes desta correção,
    isso derrubava o paciente na hora. Agora deve consumir 1 tentativa do
    loop e seguir normalmente pra 2ª, chegando ao resultado certo."""
    attempts = {"n": 0}

    def fake_click_menu(page):
        attempts["n"] += 1
        return attempts["n"] > 1  # falha na 1ª tentativa, sucesso na 2ª

    fake_result_page = _FakeResultPage()
    monkeypatch.setattr(records, "_click_menu_to_search_screen", fake_click_menu)
    monkeypatch.setattr(records, "_submit_search", lambda page, record_number: fake_result_page)
    monkeypatch.setattr(records, "_snapshot_pages", lambda page: frozenset())

    result = open_current_admission(object(), "123456")

    assert result is fake_result_page
    assert attempts["n"] == 2  # nunca escapou sem tentar de novo
    assert fake_result_page.locator_obj.clicked is True


class _FakeClickThenSucceedLocator:
    def __init__(self, fail_times=1):
        self.click_calls = 0
        self.fail_times = fail_times

    def wait_for(self, timeout=None):
        return None  # marcador sempre "encontrado" na hora

    def click(self):
        self.click_calls += 1
        if self.click_calls <= self.fail_times:
            raise PlaywrightTimeoutError("simulado: clique final não respondeu")


class _FakeResultPageForClick:
    def __init__(self, locator):
        self._locator = locator

    def get_by_text(self, text, exact=True):
        assert text == CURRENT_ADMISSION_MARKER
        return self

    @property
    def first(self):
        return self._locator


def test_open_current_admission_retries_when_final_click_times_out(monkeypatch):
    """Achado real ao vivo (DEC-103, 2026-09-01): `current_card.click()`
    ficava FORA do try/except do laço -- se ESSE clique específico travasse
    (mesmo com o marcador já confirmado visível pelo `wait_for`), a exceção
    escapava crua ('TimeoutError: Locator.click: Timeout 30000ms exceeded',
    confirmado 2x no log real com traceback completo), desperdiçando por
    completo o aumento de `SEARCH_RETRY_ATTEMPTS` do DEC-102 pra esse caso
    específico -- a 1ª falha já derrubava o paciente, sem reentrar no laço."""
    locator = _FakeClickThenSucceedLocator(fail_times=1)
    fake_result_page = _FakeResultPageForClick(locator)
    monkeypatch.setattr(records, "_click_menu_to_search_screen", lambda page: True)
    monkeypatch.setattr(records, "_submit_search", lambda page, record_number: fake_result_page)
    monkeypatch.setattr(records, "_snapshot_pages", lambda page: frozenset())

    result = open_current_admission(object(), "123456")

    assert result is fake_result_page
    assert locator.click_calls == 2  # falhou 1x, reentrou no laço, teve sucesso na 2ª


class _FakeAlwaysTimesOutLocator:
    def wait_for(self, timeout=None):
        raise PlaywrightTimeoutError("simulado: marcador nunca aparece")


class _FakeNeverPresentLocator:
    def count(self):
        return 0


class _FakeNeverFoundResultPage:
    """Simula o marcador de internação atual NUNCA aparecendo (sem
    justificativa de acesso na tela) -- caminho de ESGOTAMENTO do loop de
    retry, diferente de `_FakeResultPage` (sucesso imediato)."""

    @property
    def first(self):
        return _FakeAlwaysTimesOutLocator()

    def get_by_text(self, text, exact=True):
        if text == CURRENT_ADMISSION_MARKER:
            return self
        return _FakeNeverPresentLocator()


def test_open_current_admission_raises_after_exhausting_all_attempts(monkeypatch):
    """Achado real ao vivo (DEC-102, 2026-09-01): quando o marcador nunca
    aparece de verdade (GSUS/máquina lenta sob carga real -- `llm.start()`
    fica residente em RAM durante toda a Fase 1, DEC-058), o loop precisa
    desistir de forma limpa depois de esgotar `SEARCH_RETRY_ATTEMPTS`
    tentativas -- nem preso pra sempre, nem escapando silenciosamente."""
    click_attempts = {"n": 0}

    def fake_click_menu(page):
        click_attempts["n"] += 1
        return True

    fake_result_page = _FakeNeverFoundResultPage()
    monkeypatch.setattr(records, "_click_menu_to_search_screen", fake_click_menu)
    monkeypatch.setattr(records, "_submit_search", lambda page, record_number: fake_result_page)
    monkeypatch.setattr(records, "_snapshot_pages", lambda page: frozenset())

    with pytest.raises(GSUSRecordError):
        open_current_admission(object(), "123456")

    assert click_attempts["n"] == SEARCH_RETRY_ATTEMPTS  # esgotou TODAS as tentativas, sem escapar antes


# ------------------------------------------- DEC-082: "Frame was detached"


class _FakeToggle:
    def __init__(self, id_value, raise_error=None):
        self._id = id_value
        self._raise_error = raise_error
        self.click_calls = 0

    def get_attribute(self, name):
        return self._id

    def is_visible(self):
        return True

    def click(self, timeout=None):
        self.click_calls += 1
        if self._raise_error is not None:
            raise self._raise_error


class _FakeToggleCollection:
    def __init__(self, toggles):
        self._toggles = toggles

    def count(self):
        return len(self._toggles)

    def nth(self, i):
        return self._toggles[i]


class _FakeExpandPage:
    """`_is_expanded`/`_wait_expanded` chamam `page.evaluate(...)`
    (`_body_metrics`) -- devolver `None` faz `_is_expanded` sempre dizer
    'ainda fechado', sem precisar simular o DOM real."""

    def __init__(self, toggles):
        self._toggles = toggles

    def locator(self, selector):
        return _FakeToggleCollection(self._toggles)

    def evaluate(self, script, *args):
        return None

    def wait_for_timeout(self, ms):
        pass


def test_expand_all_survives_frame_detached_instead_of_escaping():
    """Achado real (DEC-082, E2E-001 2026-08-27): `_expand_all` só capturava
    `PlaywrightTimeoutError` -- "Frame was detached" (uma `PlaywrightError`
    que NÃO é timeout) escapava sem tratamento, arrastando pro log um "Call
    log" do Playwright com atributo (`onmousedown`/`id`) vinculado a
    paciente/episódio. Agora deve ser tratado localmente, sem propagar."""
    toggle = _FakeToggle("historicoAtendimento2Item", raise_error=PlaywrightError("Frame was detached"))
    page = _FakeExpandPage([toggle])

    _expand_all(page, "div.card_header", "episódio")  # não deve levantar

    assert toggle.click_calls == 1


class _FakeToggleCollectionCountFails:
    """Simula `toggles.count()` estourando "Frame was detached" -- achado
    real de validação em produção (DEC-082): esse `.count()` inicial de
    cada rodada ficava FORA do `try/except` de dentro do loop."""

    def count(self):
        raise PlaywrightError("Locator.count: Frame was detached")


class _FakePageCountFails:
    def locator(self, selector):
        return _FakeToggleCollectionCountFails()


def test_expand_all_survives_frame_detached_on_count_itself():
    """Reproduz o erro real visto em produção: `Locator.count: Frame was
    detached`, dentro de `_expand_all` mas fora do loop por item."""
    _expand_all(_FakePageCountFails(), "div.card_header", "episódio")  # não deve levantar


class _FakeEvaluatePage:
    def evaluate(self, script, *args):
        raise PlaywrightError("Frame was detached")


def test_collect_days_raises_record_error_not_no_admission_on_frame_detached():
    """Achado real (DEC-082): se `.evaluate()` falhar por instabilidade de
    sessão, NUNCA deve devolver `([], 0)` -- `extract_notes` interpretaria
    isso como "sem internação atual" (GSUSNoCurrentAdmissionDays, categoria
    de NÃO-falha no relatório, DEC-054), escondendo uma falha técnica real
    atrás de um rótulo de "provável alta". Deve levantar `GSUSRecordError`
    (falha real, isolada por paciente via RF-12)."""
    import pytest

    with pytest.raises(GSUSRecordError):
        _collect_days(_FakeEvaluatePage(), deadline=0.0)


# ------------------------------------------------------- DEC-117: busca sem responder

def test_open_current_admission_raises_unresponsive_when_menu_never_responds(monkeypatch):
    """DEC-117 (achado real 2026-09-04): 44 pacientes seguidos morreram JA no
    menu (busca nunca disparada) com a mensagem generica de "nenhuma
    internacao encontrada". Quando NENHUMA tentativa passa do menu, a causa e
    o GSUS/sessao, nao o paciente -- precisa sair como classe propria pra o
    orchestrator poder refazer o login e, persistindo, interromper."""
    click_attempts = {"n": 0}

    def fake_click_menu(page):
        click_attempts["n"] += 1
        return False  # menu nunca responde

    monkeypatch.setattr(records, "_click_menu_to_search_screen", fake_click_menu)
    monkeypatch.setattr(
        records, "_submit_search", lambda page, record_number: pytest.fail("busca nao deveria ser disparada"),
    )
    monkeypatch.setattr(records, "_snapshot_pages", lambda page: frozenset())

    with pytest.raises(records.GSUSSearchUnresponsiveError) as excinfo:
        open_current_admission(object(), "123456")

    assert click_attempts["n"] == SEARCH_RETRY_ATTEMPTS
    assert "não respondeu" in str(excinfo.value)
    assert "Nenhuma internação" not in str(excinfo.value)


def test_open_current_admission_keeps_generic_error_when_menu_works_but_marker_never_appears(monkeypatch):
    """Contraprova: se o menu respondeu (busca disparada) e so o marcador nao
    apareceu, continua sendo o GSUSRecordError generico -- pode ser paciente
    sem internacao atual, nao e sinal de GSUS fora do ar."""
    monkeypatch.setattr(records, "_click_menu_to_search_screen", lambda page: True)
    monkeypatch.setattr(records, "_submit_search", lambda page, record_number: _FakeNeverFoundResultPage())
    monkeypatch.setattr(records, "_snapshot_pages", lambda page: frozenset())

    with pytest.raises(GSUSRecordError) as excinfo:
        open_current_admission(object(), "123456")

    assert not isinstance(excinfo.value, records.GSUSSearchUnresponsiveError)
    assert "Nenhuma internação em andamento encontrada" in str(excinfo.value)


def test_open_current_admission_mixed_failures_are_not_unresponsive(monkeypatch):
    """Se ao menos uma tentativa passou do menu, o GSUS respondeu de algum
    jeito -- nao dispara o disjuntor."""
    attempts = {"n": 0}

    def fake_click_menu(page):
        attempts["n"] += 1
        return attempts["n"] == 1  # so a primeira passa do menu

    monkeypatch.setattr(records, "_click_menu_to_search_screen", fake_click_menu)
    monkeypatch.setattr(records, "_submit_search", lambda page, record_number: _FakeNeverFoundResultPage())
    monkeypatch.setattr(records, "_snapshot_pages", lambda page: frozenset())

    with pytest.raises(GSUSRecordError) as excinfo:
        open_current_admission(object(), "123456")

    assert not isinstance(excinfo.value, records.GSUSSearchUnresponsiveError)


def test_open_current_admission_marker_missing_is_a_distinct_current_admission_not_found(monkeypatch):
    """RESIL-015: busca rodou (menu respondeu) e o marcador nunca apareceu ->
    subclasse própria, pra o orchestrator decidir pela data de admissão se é
    caso benigno (admissão recente) ou erro. Continua GSUSRecordError e
    continua com a MESMA mensagem (casada em KNOWN_GSUS_ERROR_PATTERNS)."""
    monkeypatch.setattr(records, "_click_menu_to_search_screen", lambda page: True)
    monkeypatch.setattr(records, "_submit_search", lambda page, record_number: _FakeNeverFoundResultPage())
    monkeypatch.setattr(records, "_snapshot_pages", lambda page: frozenset())

    with pytest.raises(records.GSUSCurrentAdmissionNotFound) as excinfo:
        open_current_admission(object(), "123456")

    assert isinstance(excinfo.value, GSUSRecordError)
    assert not isinstance(excinfo.value, records.GSUSSearchUnresponsiveError)
    assert "Nenhuma internação em andamento encontrada" in str(excinfo.value)
