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
