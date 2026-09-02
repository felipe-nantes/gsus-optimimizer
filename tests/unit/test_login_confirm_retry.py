"""Testa o retry do clique em 'Confirmar' no modal de seleção de
estabelecimento (achado real, 2026-09-01): antes desta correção, uma única
falha no clique já levantava GSUSLoginError, travando a execução logo no
primeiro passo -- sem ninguém presente numa execução automática de
madrugada pra clicar manualmente e destravar (usuário precisou fazer isso
manualmente numa execução real). Mesma classe de achado do DEC-024 (censo)
e DEC-081 (menu de prontuário). Sem Playwright/browser real -- só fakes."""
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.gsus import login as login_module
from app.gsus.login import CONFIRM_RETRY_ATTEMPTS, GSUSLoginError, _confirm_establishment


class _FakeConfirmButton:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.clicks = 0

    def click(self, timeout=None):
        self.clicks += 1
        if self.clicks <= self.fail_times:
            raise PlaywrightTimeoutError("simulado: botão 'Confirmar' não respondeu")


class _FakeFrame:
    def __init__(self, button):
        self._button = button

    def locator(self, selector):
        assert selector == "#botaoConfirmar"
        return self._button


def test_confirm_establishment_succeeds_on_first_attempt(monkeypatch):
    button = _FakeConfirmButton(fail_times=0)
    monkeypatch.setattr(login_module, "get_content_frame", lambda page: _FakeFrame(button))

    _confirm_establishment(object())

    assert button.clicks == 1


def test_confirm_establishment_retries_instead_of_giving_up_on_first_failure(monkeypatch):
    """Achado real: a versão anterior desistia na 1ª falha -- travava a
    execução inteira exigindo clique manual. Agora tenta de novo."""
    button = _FakeConfirmButton(fail_times=CONFIRM_RETRY_ATTEMPTS - 1)
    monkeypatch.setattr(login_module, "get_content_frame", lambda page: _FakeFrame(button))

    _confirm_establishment(object())

    assert button.clicks == CONFIRM_RETRY_ATTEMPTS


def test_confirm_establishment_raises_gsus_login_error_after_exhausting_retries(monkeypatch):
    button = _FakeConfirmButton(fail_times=CONFIRM_RETRY_ATTEMPTS + 5)
    monkeypatch.setattr(login_module, "get_content_frame", lambda page: _FakeFrame(button))

    try:
        _confirm_establishment(object())
        assert False, "deveria ter levantado GSUSLoginError"
    except GSUSLoginError:
        pass

    assert button.clicks == CONFIRM_RETRY_ATTEMPTS
