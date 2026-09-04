"""Testa a detecção de sessão morta (expirada -- DEC-083, ou aba do Firefox
crashada -- DEC-084) + relogin automático do GSUSAdapter (achados reais,
E2E-001 2026-08-27): numa execução longa (~180 pacientes, mais de 1h), a
sessão do GSUS pode expirar OU a aba pode crashar de exaustão de
memória/CPU (hardware fraco, DEC-070) no meio do lote -- confirmado pelo
usuário vendo as duas telas reais. Sem essa detecção, TODO paciente
seguinte falhava até o app ser reiniciado manualmente. Sem Playwright/
browser real -- só fakes."""
import pytest

from app.gsus import adapter as adapter_module
from app.gsus.adapter import GSUSAdapter
from app.gsus.login import GSUSLoginError


class _FakeTextLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class _FakeContext:
    def __init__(self, new_page):
        self._new_page = new_page
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        return self._new_page


class _FakePage:
    """`present_markers`: conjunto de textos que essa página "mostra" --
    simula `get_by_text` respondendo count>0 só pro marcador certo, 0 pros
    demais (como uma página real só bate com o texto que ela de fato tem).
    `raise_on_check`: simula a própria checagem falhando (página tão
    quebrada que nem `get_by_text` funciona)."""

    def __init__(self, present_markers=(), context=None, raise_on_check=False):
        self._present_markers = set(present_markers)
        self.context = context
        self.closed = False
        self.goto_calls = []
        self.raise_on_check = raise_on_check

    def get_by_text(self, text, exact=True):
        if self.raise_on_check:
            raise adapter_module.PlaywrightError("simulado: página não responde")
        return _FakeTextLocator(1 if text in self._present_markers else 0)

    def close(self):
        self.closed = True

    def goto(self, url, wait_until=None):
        self.goto_calls.append((url, wait_until))


def _make_adapter(page, base_url="https://gsus.pr.gov.br"):
    return GSUSAdapter(page, "12345678900", "senha", "Auditoria", base_url=base_url)


def test_session_expired_detects_marker_text():
    adapter = _make_adapter(_FakePage(present_markers={adapter_module.SESSION_EXPIRED_MARKER}))
    assert adapter._session_expired() is True


def test_session_expired_false_when_marker_absent():
    adapter = _make_adapter(_FakePage())
    assert adapter._session_expired() is False


def test_session_expired_false_when_check_itself_raises():
    """`_session_expired` sozinho é conservador: erro na checagem não conta
    como detectado -- `_needs_relogin` (via `_tab_crashed`) é quem cobre
    esse caso de forma mais ampla."""
    adapter = _make_adapter(_FakePage(raise_on_check=True))
    assert adapter._session_expired() is False


def test_tab_crashed_detects_marker_text():
    """Achado real (DEC-084): aba do PRÓPRIO Firefox crashada ("Gah. Your
    tab just crashed.") -- sintoma idêntico à sessão expirada (cascata de
    falhas instantâneas), causa diferente (exaustão de recurso, não GSUS)."""
    adapter = _make_adapter(_FakePage(present_markers={adapter_module.TAB_CRASHED_MARKER}))
    assert adapter._tab_crashed() is True


def test_tab_crashed_true_when_check_itself_raises():
    """Diferente de `_session_expired`: aqui um erro na PRÓPRIA checagem já
    conta como "crashou" -- uma página genuinamente morta nem deixa checar."""
    adapter = _make_adapter(_FakePage(raise_on_check=True))
    assert adapter._tab_crashed() is True


def test_needs_relogin_true_for_either_condition():
    assert _make_adapter(_FakePage(present_markers={adapter_module.SESSION_EXPIRED_MARKER}))._needs_relogin() is True
    assert _make_adapter(_FakePage(present_markers={adapter_module.TAB_CRASHED_MARKER}))._needs_relogin() is True
    assert _make_adapter(_FakePage(raise_on_check=True))._needs_relogin() is True


def test_needs_relogin_false_when_page_healthy():
    assert _make_adapter(_FakePage())._needs_relogin() is False


def test_relogin_opens_fresh_page_navigates_and_closes_old_one(monkeypatch):
    new_page = _FakePage()
    old_page = _FakePage(context=_FakeContext(new_page))
    relogged_page = _FakePage()
    monkeypatch.setattr(adapter_module.gsus_login, "login", lambda page, u, p: relogged_page)

    adapter = _make_adapter(old_page)
    adapter._relogin()

    assert new_page.goto_calls == [("https://gsus.pr.gov.br", "load")]
    assert adapter._page is relogged_page
    assert old_page.closed is True  # sessão morta descartada, sem acumular aba órfã


def test_relogin_without_base_url_raises_clear_error():
    adapter = _make_adapter(_FakePage(), base_url=None)

    with pytest.raises(GSUSLoginError):
        adapter._relogin()


def test_ensure_login_relogs_automatically_when_session_expired(monkeypatch):
    """O cenário real do DEC-083: `_logged_in` já é True (login inicial já
    aconteceu horas antes), mas a sessão morreu no meio do lote. Sem esta
    correção, `_ensure_login` devolvia o frame da sessão morta pra sempre."""
    new_page = _FakePage()
    old_page = _FakePage(present_markers={adapter_module.SESSION_EXPIRED_MARKER}, context=_FakeContext(new_page))
    relogged_page = _FakePage()
    monkeypatch.setattr(adapter_module.gsus_login, "login", lambda page, u, p: relogged_page)
    fake_frame = object()
    monkeypatch.setattr(adapter_module, "get_content_frame", lambda page: fake_frame)

    adapter = _make_adapter(old_page)
    adapter._logged_in = True

    result = adapter._ensure_login()

    assert adapter._page is relogged_page
    assert result is fake_frame


def test_ensure_login_relogs_automatically_when_tab_crashed(monkeypatch):
    """O cenário real do DEC-084: mesma recuperação, causa diferente (aba
    crashada em vez de sessão GSUS expirada)."""
    new_page = _FakePage()
    old_page = _FakePage(present_markers={adapter_module.TAB_CRASHED_MARKER}, context=_FakeContext(new_page))
    relogged_page = _FakePage()
    monkeypatch.setattr(adapter_module.gsus_login, "login", lambda page, u, p: relogged_page)
    fake_frame = object()
    monkeypatch.setattr(adapter_module, "get_content_frame", lambda page: fake_frame)

    adapter = _make_adapter(old_page)
    adapter._logged_in = True

    result = adapter._ensure_login()

    assert adapter._page is relogged_page
    assert result is fake_frame


def test_ensure_login_does_not_relogin_when_session_still_valid(monkeypatch):
    """Não pode custar um relogin a cada paciente -- só quando a página
    realmente está morta (sessão expirada ou aba crashada)."""
    page = _FakePage()
    fake_frame = _FakePage()  # também serve de frame falso (mesma interface get_by_text)
    monkeypatch.setattr(adapter_module, "get_content_frame", lambda p, timeout_ms=15_000: fake_frame)
    relogin_calls = []
    adapter = _make_adapter(page)
    adapter._logged_in = True
    adapter._relogin = lambda: relogin_calls.append(1)

    adapter._ensure_login()

    assert relogin_calls == []


# ------------------------ _session_expired também dentro do frame `content`
# Achado da auditoria de resiliência (não confirmado contra o GSUS real, mas
# blindado por segurança): GSUS é um frameset (DEC-009) -- se a tela de
# expiração substituir o CONTEÚDO do frame em vez de navegar a página
# top-level, checar só `self._page` nunca encontraria o marcador.

def test_session_expired_detects_marker_inside_content_frame(monkeypatch):
    page = _FakePage()  # marcador ausente no nível da página
    frame_with_marker = _FakePage(present_markers={adapter_module.SESSION_EXPIRED_MARKER})
    monkeypatch.setattr(adapter_module, "get_content_frame", lambda p, timeout_ms=15_000: frame_with_marker)

    adapter = _make_adapter(page)

    assert adapter._session_expired() is True


def test_session_expired_false_when_absent_from_both_page_and_frame(monkeypatch):
    page = _FakePage()
    frame_without_marker = _FakePage()
    monkeypatch.setattr(adapter_module, "get_content_frame", lambda p, timeout_ms=15_000: frame_without_marker)

    adapter = _make_adapter(page)

    assert adapter._session_expired() is False


def test_session_expired_false_when_content_frame_is_unavailable(monkeypatch):
    """Página sem marcador e frame indisponível (ex.: página realmente
    quebrada, `TimeoutError` de `get_content_frame`) -- best-effort, nunca
    propaga."""
    page = _FakePage()

    def _raise(p, timeout_ms=15_000):
        raise TimeoutError("simulado: frame 'content' não apareceu")

    monkeypatch.setattr(adapter_module, "get_content_frame", _raise)

    adapter = _make_adapter(page)

    assert adapter._session_expired() is False


# ------------------------------------------------------------ DEC-117: reset_session

def test_reset_session_relogs_even_without_expired_marker(monkeypatch):
    """DEC-117: sessao "logada" mas o menu nao responde ha N pacientes -- o
    orchestrator pede uma sessao nova mesmo sem marcador de expiracao."""
    new_page = _FakePage()
    old_page = _FakePage(context=_FakeContext(new_page))  # nenhum marcador presente
    relogged_page = _FakePage()
    monkeypatch.setattr(adapter_module.gsus_login, "login", lambda page, u, p: relogged_page)

    adapter = _make_adapter(old_page)
    adapter._logged_in = True
    adapter.reset_session()

    assert adapter._page is relogged_page
    assert old_page.closed is True


def test_reset_session_is_noop_before_first_login(monkeypatch):
    monkeypatch.setattr(adapter_module.gsus_login, "login", lambda page, u, p: pytest.fail("nao deveria logar"))
    page = _FakePage(context=_FakeContext(_FakePage()))
    adapter = _make_adapter(page)

    adapter.reset_session()

    assert adapter._page is page
    assert page.closed is False
