"""Testa app.gsus.census._click_next_with_retry isoladamente (sem
Playwright/browser real) -- ver DECISIONS.md DEC-024: clique em 'Próxima'
pode estourar timeout por lentidão momentânea do servidor; deve tentar de
novo um número limitado de vezes e desistir sem derrubar a execução."""
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.gsus import census
from app.gsus.census import (
    NEXT_CLICK_RETRY_ATTEMPTS,
    SEARCH_SCREEN_RETRY_ATTEMPTS,
    GSUSCensusError,
    GSUSCensusIncompleteError,
    _click_next_with_retry,
    _navigate_and_search_with_retry,
    collect_all_pages,
)
from app.models import Patient


class _AlwaysTimesOutFirst:
    def __init__(self):
        self.click_calls = 0

    def click(self, timeout=None):
        self.click_calls += 1
        raise PlaywrightTimeoutError("simulado: elemento não ficou estável")


class _FakeNextLink:
    def __init__(self, first):
        self.first = first


class _SucceedsOnSecondAttempt:
    def __init__(self):
        self.click_calls = 0

    def click(self, timeout=None):
        self.click_calls += 1
        if self.click_calls < 2:
            raise PlaywrightTimeoutError("simulado: primeira tentativa falha")


def test_click_next_gives_up_after_max_attempts_and_returns_false():
    stub = _AlwaysTimesOutFirst()
    next_link = _FakeNextLink(stub)

    result = _click_next_with_retry(next_link)

    assert result is False
    assert stub.click_calls == NEXT_CLICK_RETRY_ATTEMPTS


def test_click_next_succeeds_on_retry_without_exhausting_attempts():
    stub = _SucceedsOnSecondAttempt()
    next_link = _FakeNextLink(stub)

    result = _click_next_with_retry(next_link)

    assert result is True
    assert stub.click_calls == 2


class _FakeNextLinkPresent:
    """`get_by_role("link", name="Próxima")` sempre acha o link (count=1) --
    simula uma tela onde a paginação claramente não terminou, mas o clique
    nunca funciona (ver DEC-080)."""

    def count(self):
        return 1


class _FakeFrame:
    def get_by_role(self, role, name=None):
        return _FakeNextLinkPresent()

    def wait_for_timeout(self, ms):
        pass  # usado por `_wait_for_first_row_to_change` (RESIL-011) -- no-op aqui


def test_collect_all_pages_raises_incomplete_instead_of_returning_partial_list(monkeypatch):
    """Achado real E2E-001 (2026-08-27, DEC-080): antes desta correção,
    `collect_all_pages` devolvia a lista parcial via `return patients` --
    o orchestrator então tratava isso como censo completo e marcava todo
    paciente fora da lista como inativo/alta. Agora deve levantar
    `GSUSCensusIncompleteError` carregando o que já foi coletado, nunca
    devolver silenciosamente uma lista parcial."""
    collected = [Patient(record_number="700001", bed="7A", unit="Clínica Médica", admission_date="20/08/2026")]
    monkeypatch.setattr(census, "_first_record_number", lambda frame: "700001")
    monkeypatch.setattr(
        census, "_extract_page_rows", lambda frame, patients, seen: patients.extend(collected)
    )
    monkeypatch.setattr(census, "_click_next_with_retry", lambda link: False)

    with pytest.raises(GSUSCensusIncompleteError) as exc_info:
        collect_all_pages(_FakeFrame())

    assert exc_info.value.patients == collected


# --------------------------------------------------- RESIL-011/DEC-105
# Auditoria de certificação pré-entrega, 2026-09-01.

def test_collect_all_pages_raises_incomplete_when_content_never_changes(monkeypatch):
    """Achado real de auditoria: SEGUNDO caminho do mesmo bug do DEC-080,
    nunca coberto antes. O clique em 'Próxima' TEM sucesso (existe link,
    `_click_next_with_retry` devolve True), mas o conteúdo da tabela nunca
    muda dentro do prazo (AJAX travado) -- antes desta correção,
    `collect_all_pages` devolvia a lista parcial em silêncio, como se fosse
    o censo COMPLETO, arriscando marcar paciente real como alta."""
    collected = [Patient(record_number="800001", bed="8A", unit="Clínica Médica", admission_date="20/08/2026")]
    monkeypatch.setattr(census, "_first_record_number", lambda frame: "800001")  # nunca muda
    monkeypatch.setattr(census, "_extract_page_rows", lambda frame, patients, seen: patients.extend(collected))
    monkeypatch.setattr(census, "_click_next_with_retry", lambda link: True)  # clique "funciona"

    with pytest.raises(GSUSCensusIncompleteError) as exc_info:
        collect_all_pages(_FakeFrame())

    assert exc_info.value.patients == collected


class _FakeSearchScreenPage:
    """Simula a sequência de 3 cliques de `_navigate_and_search_with_retry`
    (2x `get_by_text(...).click()` + 1x `locator('#btConsultar').click()`)
    -- todos landam no mesmo `.click()`, que falha nas primeiras
    `fail_times` chamadas e depois sempre funciona."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.attempts = 0

    def get_by_text(self, text, exact=True):
        return self

    def locator(self, selector):
        return self

    @property
    def first(self):  # DEC-120: `.locator("visible=true").first` nos cliques de menu
        return self

    def click(self):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise PlaywrightTimeoutError("simulado: tela de busca não respondeu")

    def wait_for_selector(self, selector):
        pass


def test_navigate_and_search_retries_after_transient_failure():
    """Achado real de auditoria (RESIL-011): diferente do resto da
    navegação no GSUS (menu de prontuário, paginação), abrir a tela de
    busca do censo nunca tinha retry -- um único hiccup aqui abortava o dia
    inteiro, ANTES de existir run_id (zero pacientes, zero relatório)."""
    page = _FakeSearchScreenPage(fail_times=1)

    _navigate_and_search_with_retry(page)  # não deve levantar

    assert page.attempts > 1  # consumiu ao menos 1 tentativa falha antes do sucesso


def test_navigate_and_search_gives_up_after_exhausting_attempts():
    page = _FakeSearchScreenPage(fail_times=999)  # nunca funciona

    with pytest.raises(GSUSCensusError):
        _navigate_and_search_with_retry(page)

    assert page.attempts == SEARCH_SCREEN_RETRY_ATTEMPTS


# --------------------------------------------------- DEC-108
# Achado real: 3 execuções autônomas na madrugada de 2026-09-01/02
# terminaram a paginação sem NENHUM sinal de erro (sem link "Próxima", sem
# retry esgotado), mas coletando bem menos pacientes do que o esperado.
# Sobreposição entre as 3 execuções mostrou pacientes vistos numa execução
# anterior, ausentes só na última, marcados como alta -- mesmo internados de
# verdade. `next_link.count() == 0` sozinho não bastava como prova de censo
# completo.

class _FakeNextLinkAbsent:
    def count(self):
        return 0


class _FakeFrameNoNextLink:
    def get_by_role(self, role, name=None):
        return _FakeNextLinkAbsent()

    def wait_for_timeout(self, ms):
        pass


def test_collect_all_pages_raises_incomplete_when_no_next_link_but_total_mismatches(monkeypatch):
    collected = [Patient(record_number="900001", bed="9A", unit="Clínica Médica", admission_date="20/08/2026")]
    monkeypatch.setattr(census, "_first_record_number", lambda frame: "900001")
    monkeypatch.setattr(census, "_extract_page_rows", lambda frame, patients, seen: patients.extend(collected))
    monkeypatch.setattr(census, "_extract_expected_total", lambda frame: 5)

    with pytest.raises(GSUSCensusIncompleteError) as exc_info:
        collect_all_pages(_FakeFrameNoNextLink())

    assert exc_info.value.patients == collected


def test_collect_all_pages_tolerates_small_gap_from_duplicate_rows(monkeypatch):
    """Contraparte de segurança: gap PEQUENO (esperado por dedup de linhas
    duplicadas na paginação real, DEC-014) não deve disparar falso positivo
    -- só prejudicaria quem já estava internado sem motivo nenhum."""
    collected = [Patient(record_number="900002", bed="9B", unit="Clínica Médica", admission_date="20/08/2026")]
    monkeypatch.setattr(census, "_first_record_number", lambda frame: "900002")
    monkeypatch.setattr(census, "_extract_page_rows", lambda frame, patients, seen: patients.extend(collected))
    monkeypatch.setattr(census, "_extract_expected_total", lambda frame: 2)  # gap=1, dentro da tolerância

    result = collect_all_pages(_FakeFrameNoNextLink())

    assert result == collected


def test_collect_all_pages_completes_normally_when_total_cannot_be_parsed(monkeypatch):
    """Degradação graciosa: rodapé fora do formato esperado (`None`) cai no
    comportamento antigo -- confia em `next_link.count() == 0` -- em vez de
    travar a execução por causa desta checagem extra."""
    collected = [Patient(record_number="900003", bed="9C", unit="Clínica Médica", admission_date="20/08/2026")]
    monkeypatch.setattr(census, "_first_record_number", lambda frame: "900003")
    monkeypatch.setattr(census, "_extract_page_rows", lambda frame, patients, seen: patients.extend(collected))
    monkeypatch.setattr(census, "_extract_expected_total", lambda frame: None)

    result = collect_all_pages(_FakeFrameNoNextLink())

    assert result == collected
