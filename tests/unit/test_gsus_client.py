"""UI-006: `GSUSClient` repassa o modo escolhido pelo usuário (navegador
visível x segundo plano) pro `firefox.launch(headless=...)`. O Playwright é
substituído por um fake -- nenhum navegador real é aberto aqui."""
from app.gsus import client as client_module


class _FakePage:
    def goto(self, url, wait_until=None):
        self.url = url


class _FakeContext:
    def set_default_timeout(self, ms):
        self.timeout = ms

    def new_page(self):
        return _FakePage()

    def close(self):
        pass


class _FakeBrowser:
    def new_context(self):
        return _FakeContext()

    def close(self):
        pass


class _FakeFirefox:
    def __init__(self, launches: list):
        self._launches = launches

    def launch(self, headless=False):
        self._launches.append(headless)
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self, launches: list):
        self.firefox = _FakeFirefox(launches)

    def stop(self):
        pass


class _FakeSyncPlaywright:
    def __init__(self, launches: list):
        self._launches = launches

    def start(self):
        return _FakePlaywright(self._launches)


def _install_fake_playwright(monkeypatch) -> list:
    launches: list = []
    monkeypatch.setattr(client_module, "sync_playwright", lambda: _FakeSyncPlaywright(launches))
    return launches


def test_client_defaults_to_visible_browser(monkeypatch):
    """DEC-077: janela visível é o único modo comprovado contra o GSUS real --
    continua sendo o padrão quando ninguém pede o contrário."""
    launches = _install_fake_playwright(monkeypatch)
    with client_module.GSUSClient("https://gsus.example") as client:
        assert client.headless is False
    assert launches == [False]


def test_client_launches_headless_when_background_mode_requested(monkeypatch):
    launches = _install_fake_playwright(monkeypatch)
    with client_module.GSUSClient("https://gsus.example", headless=True) as client:
        assert client.headless is True
        assert client.page is not None
    assert launches == [True]


# ------------------------------------------------ DEC-118: __exit__ nunca mascara a excecao

import pytest


class _FakeContextCloseFails(_FakeContext):
    def close(self):
        raise RuntimeError("simulado: contexto ja morto")


class _FakeBrowserRecording(_FakeBrowser):
    def __init__(self, log):
        self._log = log

    def new_context(self):
        return _FakeContextCloseFails()

    def close(self):
        self._log.append("browser.close")


class _FakePlaywrightRecording:
    def __init__(self, log):
        self._log = log
        self.firefox = self

    def launch(self, headless=False):
        return _FakeBrowserRecording(self._log)

    def stop(self):
        self._log.append("playwright.stop")


def test_exit_keeps_original_exception_when_closing_the_browser_fails(monkeypatch):
    """Achado real 2026-09-04: fechar o contexto lancou apos uma falha de
    login e a excecao do fechamento SUBSTITUIU a GSUSLoginError original --
    o update_flow gravou um segundo diagnostico e "Sessao GSUS encerrada"
    nunca foi logado. O original precisa subir intacto, e as demais etapas
    de encerramento ainda precisam rodar."""
    log: list = []

    class _FakeSync:
        def start(self):
            return _FakePlaywrightRecording(log)

    monkeypatch.setattr(client_module, "sync_playwright", lambda: _FakeSync())

    with pytest.raises(ValueError, match="falha original"):
        with client_module.GSUSClient("https://gsus.example"):
            raise ValueError("falha original")

    assert log == ["browser.close", "playwright.stop"]  # seguiu apesar do contexto falhar


def test_exit_without_exception_still_tolerates_close_failure(monkeypatch):
    log: list = []

    class _FakeSync:
        def start(self):
            return _FakePlaywrightRecording(log)

    monkeypatch.setattr(client_module, "sync_playwright", lambda: _FakeSync())

    with client_module.GSUSClient("https://gsus.example"):
        pass  # sem excecao no corpo: o fechamento falho nao pode virar excecao

    assert log == ["browser.close", "playwright.stop"]
