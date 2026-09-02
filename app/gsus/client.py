"""Ciclo de vida do browser Playwright. Sem lógica específica do GSUS
além da escolha do engine (Firefox -- ver DEC-010).

Read-only por construção: este módulo não expõe nenhum método de clique
genérico solto na aplicação — cada ação de navegação vive em login.py /
census.py / records.py, que devem usar apenas locators de leitura/navegação
(nunca "salvar", "confirmar" ou "excluir"; ver PROJECT_SPEC.md SEC-01/SEC-02).
"""
from __future__ import annotations

import logging
import time

from playwright.sync_api import Browser, BrowserContext, Frame, Page, sync_playwright

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000

# GSUS é uma aplicação em frameset clássico: a página que o login abre é só
# a moldura -- todo o conteúdo real (após login e em todas as telas
# seguintes: censo, prontuário, evoluções) vive num frame filho chamado
# "content". Confirmado em teste real (ver DEC-009/DECISIONS.md).
CONTENT_FRAME_NAME = "content"


class GSUSClient:
    """Gerencia uma sessão de browser para um ciclo de execução."""

    def __init__(self, base_url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.base_url = base_url
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "GSUSClient":
        self._playwright = sync_playwright().start()
        # Firefox, não Chromium (frozen stack original) -- ver DEC-010:
        # login real do GSUS não completa de forma confiável em Chromium
        # (confirmado em teste), completa em Firefox.
        # headless=False -- achado real do E2E-001 (DEC-077, 2026-08-26):
        # numa máquina "limpa" de verdade, o login automatizado travava 100%
        # das vezes esperando o pop-up abrir (30s inteiros, toda tentativa),
        # enquanto o MESMO Firefox aberto manualmente (sem automação)
        # logava normalmente -- aponta pra GSUS/WAF bloqueando
        # especificamente navegador controlado em modo headless. Testado com
        # headless=False no mesmo binário/máquina: funcionou de primeira,
        # buscou pacientes normalmente. Mesmo racional do DEC-010 (Firefox
        # vs Chromium) -- escolher o caminho simples já comprovado
        # funcionando, não tentar mascarar o sinal de automação em modo
        # headless (mais frágil, não testado, incerto se resolveria).
        self._browser = self._playwright.firefox.launch(headless=False)
        self._context = self._browser.new_context()
        self._context.set_default_timeout(self.timeout_ms)
        self.page = self._context.new_page()
        logger.info("Sessão GSUS iniciada (base_url=%s)", self.base_url)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        logger.info("Sessão GSUS encerrada")

    def goto(self, path: str = "") -> None:
        assert self.page is not None, "GSUSClient deve ser usado como context manager"
        self.page.goto(f"{self.base_url}{path}", wait_until="load")


def get_content_frame(page: Page, timeout_ms: int = 15_000) -> Frame:
    """Retorna o frame 'content' (moldura interna real do GSUS). Usado por
    login.py e, a partir de GSUS-002+, por census.py/records.py -- toda
    interação pós-login acontece dentro desse frame, não em `page`
    diretamente."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        frame = page.frame(name=CONTENT_FRAME_NAME)
        if frame is not None:
            return frame
        page.wait_for_timeout(200)
    raise TimeoutError(f"Frame '{CONTENT_FRAME_NAME}' não apareceu em {timeout_ms}ms.")
