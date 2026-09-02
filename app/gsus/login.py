"""Autenticação GSUS (GSUS-001).

GSUS usa o SSO estadual "Identidade Digital PR" (auth-cs.identidadedigital.pr.gov.br)
via OAuth2/OIDC (authorization code), não um formulário de login próprio.
Seletores confirmados por inspeção real da tela de login (página pública,
sem dado de paciente) em 2026-08-20 -- ver DECISIONS.md DEC-006.

Fluxo (atualizado após teste real -- ver DEC-009):
  1. goto(base_url) -- app não autenticado redireciona automaticamente para
     a tela de login do SSO.
  2. clicar em "Central de Segurança" (método usuário/senha) para revelar
     os campos CPF/Senha.
  3. preencher CPF e Senha, clicar em "Entrar".
  4. o clique abre uma NOVA JANELA (pop-up) -- a aba original só mostra
     "O sistema foi aberto em outra janela" e pode ser ignorada. O pop-up
     é a página de trabalho real a partir daqui.
  5. o pop-up mostra o modal "Selecionar Estabelecimento para Login no
     SISTEMA GSUS" (Tipo EAS / UF / Município / Estabelecimento / Unidade
     já vêm pré-preenchidos com o padrão do usuário) -- confirmar com o
     botão "Confirmar".

`login()` retorna a Page do pop-up: é ela que os módulos seguintes
(census.py, records.py) devem usar, não a `page` original passada aqui.

NÃO CONFIRMADO AINDA (BLOCKED_GSUS): o que acontece depois de confirmar o
estabelecimento (censo de internados, pesquisa de prontuário, evoluções)
continua bloqueado até handoff humano com estrutura sanitizada dessas
telas -- ver census.py/records.py.
"""
from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from app.gsus.client import get_content_frame

logger = logging.getLogger(__name__)

POST_LOGIN_URL_FRAGMENT = "/gsus-integrado"
POPUP_TIMEOUT_MS = 30_000
CONFIRM_TIMEOUT_MS = 15_000
# Achado real (2026-09-01): o clique em "Confirmar" não tinha NENHUM retry --
# uma única falha (página ainda carregando, lentidão momentânea) já
# levantava GSUSLoginError, travando a execução inteira logo no início e
# exigindo alguém clicar manualmente pra destravar. Numa execução automática
# de madrugada não existe esse alguém -- mesma classe de achado do DEC-024
# (censo)/DEC-081 (menu de prontuário), só que ainda mais crítico por ser o
# primeiro passo de tudo.
CONFIRM_RETRY_ATTEMPTS = 3


class GSUSLoginError(Exception):
    """Falha ao autenticar no GSUS (credencial inválida, página inesperada, etc.)."""


def login(page: Page, username: str, password: str) -> Page:
    """Autentica no GSUS via SSO Identidade Digital PR e confirma o
    estabelecimento/unidade padrão no pop-up que o GSUS abre em seguida.

    `username` é o CPF cadastrado no SSO estadual (não um usuário próprio do
    GSUS). Levanta GSUSLoginError em caso de falha -- fail-closed: nunca
    assume sucesso sem confirmar que o pop-up abriu e o modal foi fechado.

    Retorna a Page do pop-up -- é ela que deve ser usada dali em diante
    (census.py, records.py), não a `page` original.
    """
    try:
        page.get_by_role("button", name="Central de Segurança").click()
        # get_by_label("CPF") é ambíguo nesta tela: o DOM inclui campos ocultos
        # de outros métodos de login (SMS/Token/E-mail) que resolvem para o
        # mesmo nome acessível (confirmado em teste real -- ver DEC-008).
        # IDs estáveis capturados por inspeção real substituem role/label aqui.
        page.locator("#attribute_central").fill(username)
        page.locator("#password").fill(password)

        with page.context.expect_page(timeout=POPUP_TIMEOUT_MS) as popup_info:
            page.locator("#btn-central-acessar").click()
        gsus_page = popup_info.value
        gsus_page.wait_for_load_state()
    except PlaywrightTimeoutError as exc:
        logger.error("Login GSUS: pop-up do sistema não abriu dentro do tempo esperado.")
        raise GSUSLoginError(
            "Não foi possível acessar o GSUS (credencial incorreta, verificação em "
            "duas etapas pendente, ou o sistema não respondeu a tempo)."
        ) from exc

    _confirm_establishment(gsus_page)

    logger.info("Login GSUS confirmado (url=%s)", gsus_page.url)
    return gsus_page


def _confirm_establishment(gsus_page: Page) -> None:
    """Fecha o modal 'Selecionar Estabelecimento para Login no SISTEMA GSUS'
    aceitando os valores padrão já pré-selecionados para o usuário (Tipo
    EAS/UF/Município/Estabelecimento/Unidade). Não altera nenhum campo --
    trocar de estabelecimento é decisão fora do escopo desta automação
    read-only.

    GSUS é frameset clássico: o botão "Confirmar" (`#botaoConfirmar`) vive
    dentro do frame "content", não na página top-level -- confirmado em
    teste real (ver DECISIONS.md DEC-009)."""
    last_exc: Exception | None = None
    for attempt in range(1, CONFIRM_RETRY_ATTEMPTS + 1):
        try:
            content_frame = get_content_frame(gsus_page)
            content_frame.locator("#botaoConfirmar").click(timeout=CONFIRM_TIMEOUT_MS)
            return
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "Login GSUS: clique em 'Confirmar' não respondeu (tentativa %d/%d).",
                attempt, CONFIRM_RETRY_ATTEMPTS,
            )
    logger.error("Login GSUS: modal de seleção de estabelecimento não apareceu/fechou.")
    raise GSUSLoginError(
        "O GSUS abriu, mas não foi possível confirmar o estabelecimento/unidade."
    ) from last_exc
