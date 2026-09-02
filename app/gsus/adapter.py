"""Único ponto de integração entre a automação real do GSUS (Playwright) e
o orchestrator (app/orchestrator.py). Implementa os Protocols CensusSource
e RecordSource usando app/gsus/login.py, census.py e records.py.

Quando GSUS-004/005 (prontuário/evoluções) forem desbloqueados, só este
arquivo e records.py deveriam precisar de ajuste -- o orchestrator e todo o
resto do pipeline já são testados com fontes sintéticas.
"""
from __future__ import annotations

import logging

from playwright.sync_api import Error as PlaywrightError, Page

from app.gsus import census as gsus_census
from app.gsus import login as gsus_login
from app.gsus import records as gsus_records
from app.gsus.client import get_content_frame
from app.models import Patient

logger = logging.getLogger(__name__)

# Achado real (DEC-083, E2E-001 2026-08-27): numa execução longa (~180
# pacientes, mais de 1h), a sessão do GSUS expira no meio do lote --
# confirmado pelo usuário com print real da tela ("Sua sessão expirou.
# Favor, desconectar-se e fazer o login novamente."). A partir daí, TODO
# paciente seguinte falhava (menu "Atendimento" inexistente nessa tela,
# frame recarregado -- exatamente os sintomas do DEC-080/081/082, que eram
# tratamento de SINTOMA, não da causa raiz). Único jeito de descobrir foi o
# usuário ver a tela real -- não dava pra diagnosticar só por log.
SESSION_EXPIRED_MARKER = "Sua sessão expirou"

# Achado real (DEC-084, mesma sessão de validação do DEC-083): nem toda
# rajada de falhas rápidas é sessão expirada -- o usuário viu, dessa vez, a
# aba do PRÓPRIO FIREFOX crashada ("Gah. Your tab just crashed." -- página
# nativa do navegador, não do GSUS). Sintoma idêntico (cascata de falhas
# instantâneas, sem os ~90s normais de retry real) mas causa diferente
# (provável exaustão de memória/CPU nesta máquina fraca, DEC-070, numa
# sessão de horas). Mesma solução (`_relogin`) resolve os dois casos --
# abandonar a página morta e abrir uma nova de qualquer forma.
TAB_CRASHED_MARKER = "Your tab just crashed"


class GSUSAdapter:
    def __init__(
        self, page: Page, username: str, password: str, unit: str,
        base_url: str | None = None, max_days_per_patient: int | None = None,
    ):
        self._page = page
        self._username = username
        self._password = password
        self._unit = unit
        # Necessário pra `_relogin()` -- refazer login do zero exige
        # navegar uma página NOVA até a tela inicial (a página original,
        # pré-login, já foi sobrescrita por `self._page` desde o 1º login).
        self._base_url = base_url
        # Achado real (2026-09-01): pedido do usuário pra acelerar um
        # catch-up pontual do banco -- ver `records.extract_notes`/
        # `_days_beyond_cap`. `None` (padrão) mantém o comportamento normal.
        self._max_days_per_patient = max_days_per_patient
        self._logged_in = False

    def _ensure_login(self):
        """Retorna o frame `content` (moldura real do GSUS pós-login)."""
        if not self._logged_in:
            # login() retorna a Page do pop-up que o GSUS abre após a
            # autenticação -- é ela a página de trabalho real dali em diante
            # (ver DEC-009 / app/gsus/login.py).
            self._page = gsus_login.login(self._page, self._username, self._password)
            self._logged_in = True
        elif self._needs_relogin():
            logger.warning("Sessão GSUS indisponível (expirada ou aba crashada) -- refazendo login.")
            self._relogin()
        return get_content_frame(self._page)

    def _session_expired(self) -> bool:
        """Checa o marcador real da tela de sessão expirada (DEC-083). Só
        texto de UI fixo, nunca dado de paciente. `PlaywrightError` (ex.:
        página numa transição) conta como "não detectado" -- não é sinal
        confiável o bastante pra derrubar o fluxo normal só por causa desta
        checagem específica (`_needs_relogin` cobre o caso de erro de
        outro jeito, ver `_tab_crashed`).

        Achado da auditoria de resiliência 2026-08-28 (não confirmado contra
        o GSUS real, mas plausível o bastante pra blindar): GSUS é um
        frameset com um frame `content` (DEC-009) -- se a tela de expiração
        substituir o CONTEÚDO do frame em vez de navegar a página
        top-level, `get_by_text` em `self._page` nunca encontraria o
        marcador. Checa os dois lugares; qualquer problema no caminho do
        frame (ausente, timeout curto, o que for) só significa "não
        detectado por este caminho" -- best-effort, nunca propaga."""
        try:
            if self._page.get_by_text(SESSION_EXPIRED_MARKER, exact=False).count() > 0:
                return True
        except PlaywrightError:
            pass
        try:
            frame = get_content_frame(self._page, timeout_ms=500)
            return frame.get_by_text(SESSION_EXPIRED_MARKER, exact=False).count() > 0
        except Exception:
            return False

    def _tab_crashed(self) -> bool:
        """Checa o marcador da tela NATIVA do Firefox de aba crashada
        (DEC-084) -- "Gah. Your tab just crashed.", nunca conteúdo do GSUS.
        Diferente de `_session_expired`: aqui um `PlaywrightError` ao tentar
        checar É o próprio sinal de problema (página realmente não
        responde mais) -- conta como "sim, crashou", não como "não
        detectado"."""
        try:
            return self._page.get_by_text(TAB_CRASHED_MARKER, exact=False).count() > 0
        except PlaywrightError:
            return True

    def _needs_relogin(self) -> bool:
        """Combina os dois sinais conhecidos de sessão morta (DEC-083/084).
        Qualquer um dos dois -- ou uma página tão quebrada que nem a
        checagem funciona -- já é motivo pra abandonar a página atual e
        logar de novo."""
        return self._session_expired() or self._tab_crashed()

    def _relogin(self) -> None:
        """Refaz o login do zero numa aba NOVA do mesmo contexto do
        navegador (a aba antiga, já com sessão morta, é descartada -- não
        dá pra reaproveitá-la pra logar de novo, `login()` espera a tela
        inicial do SSO). Fecha a aba antiga só depois do novo login
        confirmado, pra nunca ficar sem nenhuma página utilizável se o
        relogin falhar."""
        if not self._base_url:
            raise gsus_login.GSUSLoginError(
                "Sessão GSUS expirou e não é possível refazer login automaticamente "
                "(base_url não configurada)."
            )
        old_page = self._page
        new_page = old_page.context.new_page()
        new_page.goto(self._base_url, wait_until="load")
        self._page = gsus_login.login(new_page, self._username, self._password)
        try:
            old_page.close()
        except PlaywrightError:
            pass

    def get_census(self) -> list[Patient]:
        content_frame = self._ensure_login()
        return gsus_census.get_census(content_frame, self._unit)

    def get_raw_notes_text(self, patient: Patient, known_days: frozenset[str] | set[str] = frozenset()) -> str:
        content_frame = self._ensure_login()
        # Snapshot ANTES de abrir a busca -- é o que permite, depois,
        # diferenciar página genuinamente nova de página pré-existente com
        # segurança (ver DECISIONS.md DEC-053).
        existing_pages = gsus_records._snapshot_pages(content_frame)
        # open_current_admission pode abrir uma nova janela pop-up para o
        # prontuário (mesmo padrão do login -- ver DEC-023); usar SEMPRE a
        # página que ela retornar, não necessariamente content_frame.
        record_page = gsus_records.open_current_admission(content_frame, patient.record_number)
        try:
            # `known_days` evita reabrir dia já processado -- abrir dia é a
            # operação cara (ver DEC-048).
            return gsus_records.extract_notes(
                record_page, patient.record_number, known_days,
                max_days_to_open=self._max_days_per_patient,
            )
        finally:
            # Fechar o pop-up do prontuário é OBRIGATÓRIO num lote: deixá-lo
            # aberto derrubava o processamento a partir do 2º paciente
            # ("Frame was detached", depois timeout no menu) -- ver DEC-049.
            # Fechar por IDENTIDADE DE OBJETO (`is self._page`) não bastava:
            # o Playwright cria uma Page nova a cada pop-up capturado, mesmo
            # quando -- em algum caso do GSUS -- a janela real por trás é a
            # mesma sessão de trabalho, o que derrubava a sessão inteira
            # (DEC-053). Comparar contra `existing_pages` é objetivo.
            gsus_records._close_if_new_page(existing_pages, record_page)

    def lookup_full_history(self, record_number: str) -> str:
        """Recurso manual "Localizar" (UI-004) -- texto bruto de TODAS as
        internações do prontuário, atual e antigas. Só leitura: quem chama
        isso NÃO deve persistir o retorno no banco nem rodar regras/LLM
        (decisão do usuário 2026-08-20 -- ver DECISIONS.md DEC-031)."""
        content_frame = self._ensure_login()
        return gsus_records.get_full_admission_history_text(content_frame, record_number)
