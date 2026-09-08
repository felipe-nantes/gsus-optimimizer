"""Navegação até o prontuário/internação atual e extração de evoluções
(GSUS-004 / GSUS-005).

Fluxo CONFIRMADO pelo usuário em 2026-08-20 (estrutura sanitizada -- ver
DECISIONS.md DEC-013. NENHUM dado clínico real, nome de paciente ou nome de
profissional é reproduzido neste arquivo ou em qualquer fixture):

  1. Menu "Atendimento" -> item "Pesquisar Prontuário" (tela própria,
     diferente da pesquisa de internação -- GSUS-002).
  2. Formulário de busca: campo `#codPaciente` (Nº Prontuário), digitado
     tecla por tecla + **Enter** para buscar -- o botão visual "Pesquisar"
     (`input[name="btnConsultar"]`) se mostra inerte sob qualquer forma de
     automação testada; Enter no campo é o que realmente funciona,
     confirmado pelo usuário (ver DECISIONS.md DEC-025).
  3. Resultado: bloco "Dados Pessoais" (ignorado por este módulo -- nunca
     extraímos nome/idade/nome da mãe) seguido de 1+ cards "Internação",
     um por episódio. O card do episódio atual mostra algo como "Permanece
     Internado" no cabeçalho.
  4. Abrir o card do episódio atual revela uma lista de seções por dia
     (formato "DD de MÊS de AAAA - DiaDaSemana", colapsáveis).
  5. Abrir um dia revela os blocos de evolução daquele dia, em ordem
     cronológica. Cada bloco tem cabeçalho "DD/MM/AAAA HH:MM -
     PROFISSIONAL (CARGO)" e corpo com campos rotulados que variam por
     tipo (avaliação médica, anotação de enfermagem, prescrição, etc.).

Responsabilidade deste módulo: SÓ extração bruta via DOM. A interpretação
fica com app/extraction/parser.py -- `extract_notes` devolve texto cru.

NUNCA clicar em botões de "Emissão de Documentos"/impressão -- fora do
escopo read-only (PROJECT_SPEC.md SEC-01/SEC-02), mesmo que pareçam
inofensivos (geração de PDF pode ter efeito colateral não mapeado).

CONFIRMADO em execução real em 2026-08-20: `open_current_admission` abre o
prontuário certo, com a internação certa (usuário confirmou visualmente).
`extract_notes` ainda não extrai corretamente -- ver DECISIONS.md DEC-027:
a abordagem de "achar o container do dia" errou o nível do DOM (capturava
só o cabeçalho do dia, não o conteúdo revelado ao expandir). Trocado para
capturar o texto da página inteira após expandir todos os dias, deixando
`app.extraction.parser.parse_note_blocks` (que já localiza evolução por
padrão de cabeçalho, não por posição no DOM) fazer a separação -- ainda
não validado em execução real.

Validar com `scripts/check_notes.py` (nunca imprime conteúdo clínico, só
contagem de blocos encontrados).
"""
from __future__ import annotations

import logging
import re
import time

from playwright.sync_api import Error as PlaywrightError, Frame, Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

CURRENT_ADMISSION_MARKER = "Permanece Internado"
POPUP_WAIT_MS = 5_000

# Tela real do GSUS ("Justificar Acesso ao Prontuário") que aparece quando
# o prontuário pesquisado não está internado na unidade atual agora --
# exige justificativa TEXTUAL digitada, auditada ("O acesso e a
# justificativa serão gravados", confirmado por screenshot do usuário
# 2026-08-21). NUNCA preenchida/confirmada automaticamente -- decisão
# explícita do usuário (ver DECISIONS.md DEC-035): seria uma ação de
# confirmação/escrita gerando registro de auditoria real, sob o nome do
# usuário logado, sem julgamento humano de verdade por trás (contraria
# SEC-01/SEC-02). Usado só para DETECTAR e falhar com mensagem clara.
ACCESS_JUSTIFICATION_MARKER = "Justificar Acesso ao Prontuário"

# Cabeçalho de CADA episódio de internação (atual ou antigo/com alta) --
# confirmado no formato "Internação (19 de Agosto de 2026 - Permanece
# Internado / Origem do P.A.)" para o episódio atual. CONFIRMADO em
# 2026-08-21 (usuário, múltiplos episódios reais 2014-2026, ver
# DECISIONS.md DEC-033): o mesmo padrão "Internação (" cobre TODOS os
# episódios visitados -- "P.A."/"Ambulatorial" etc. são valores do atributo
# "Origem do..." DENTRO do cabeçalho, não um prefixo de cabeçalho
# diferente. Usado só por `get_full_admission_history_text` (recurso manual
# "Localizar"), nunca pela rotina automática.
# Âncora `^` REMOVIDA em 2026-08-21 (ver DECISIONS.md DEC-039): com ela, o
# padrão não casava nada na página real e o "Localizar" repetia a busca 3x
# e desistia -- observado ao vivo pelo usuário. `^` exige que o texto do
# elemento COMECE exatamente assim, o que não se sustenta quando o
# `inner_text` do elemento traz qualquer coisa antes (rótulo, ícone,
# whitespace não normalizado).
ADMISSION_HEADER_PATTERN = re.compile(r"Internação\s*\(")

# Seletor ESTRUTURAL do card de cada episódio -- preferido sobre o padrão
# de texto acima. Baseado em id real observado na página do prontuário
# (`codInternacao5951310`, registrado no DEC-031), não em suposição sobre
# o texto exibido. Texto de UI muda/varia; id de elemento é mais estável.
EPISODE_CARD_SELECTOR = '[id^="codInternacao"]'

# Cabeçalho de episódio COMO APARECE NO TEXTO extraído. Confirmado com
# saída real do GSUS (usuário, 2026-08-21) -- há mais de um tipo de
# episódio no mesmo prontuário, com rótulos diferentes:
#     "Internação (16 de Agosto de 2026 - Permanece Internado / ...)"
#     "Pronto-Atendimento (15 de Agosto de 2026 - 16 de Agosto de 2026)"
# Por isso o padrão não fixa o rótulo: casa QUALQUER rótulo curto seguido
# de "(" e uma data por extenso. Não casa cabeçalho de DIA
# ("▶ 21 de agosto de 2026 - Sexta-Feira (Hoje)"), porque ali o que vem
# logo depois do "(" não é data. Ver DECISIONS.md DEC-040.
EPISODE_HEADER_LINE_PATTERN = re.compile(
    r"^[^\n(]{3,60}\(\s*\d{1,2}\s+de\s+[A-Za-zÀ-ÿ]+\s+de\s+\d{4}[^\n]*",
    re.MULTILINE,
)

# Extrai a PRIMEIRA data "DD de MÊS de AAAA" de um cabeçalho de internação
# -- é sempre a data de INÍCIO da internação, porque aparece logo após
# "Internação (" independente do que vem depois (status atual ou "Alta
# em ...", texto exato de episódio com alta ainda não confirmado
# literalmente -- mas a posição da 1ª data não depende disso).
_HEADER_DATE_PATTERN = re.compile(r"(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})")
_MONTHS_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


def _parse_header_date(header_text: str) -> str | None:
    """Devolve a data de início em ISO (AAAA-MM-DD) ou None se o formato
    não bater (fail-soft -- nunca impede a exibição do texto bruto)."""
    match = _HEADER_DATE_PATTERN.search(header_text)
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS_PT.get(month_name.strip().lower())
    if month is None:
        return None
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def _format_episodes_newest_first(full_text: str, header_texts: list[str] | None = None) -> str:
    """Reordena o texto extraído para GARANTIR mais-recente-primeiro por
    código, em vez de só confiar na ordem do GSUS (RF-19).

    `header_texts` é opcional: quando omitido, os cabeçalhos de episódio
    são localizados no PRÓPRIO texto por `EPISODE_HEADER_LINE_PATTERN` --
    caminho preferido desde o DEC-040, porque cobre todo tipo de episódio
    ("Internação", "Pronto-Atendimento", ...) sem depender de acertar um
    seletor de DOM por tipo.

    Se algum cabeçalho informado não for encontrado no texto, desiste da
    reformatação e devolve o texto sem alteração -- fail-soft, nunca perde
    informação por causa de uma reformatação que deu errado."""
    if header_texts is None:
        header_texts = [m.group(0).strip() for m in EPISODE_HEADER_LINE_PATTERN.finditer(full_text)]

    if not header_texts:
        return full_text

    positions = []
    cursor = 0
    for header_text in header_texts:
        idx = full_text.find(header_text, cursor)
        if idx == -1:
            logger.warning("Cabeçalho de internação não localizado no texto expandido -- mantendo ordem original.")
            return full_text
        positions.append(idx)
        cursor = idx + len(header_text)

    episodes = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(full_text)
        episodes.append({
            "date": _parse_header_date(header_texts[i]),
            "text": full_text[start:end].strip(),
        })

    episodes.sort(key=lambda ep: ep["date"] or "0000-00-00", reverse=True)

    total = len(episodes)
    parts = []
    for i, ep in enumerate(episodes, start=1):
        label = f"===== Internação {i} de {total}" + (f" — {ep['date']}" if ep["date"] else "") + " ====="
        parts.append(f"{label}\n{ep['text']}")
    return "\n\n".join(parts)


class GSUSRecordError(Exception):
    """Falha ao localizar/abrir o prontuário ou extrair evoluções."""


class GSUSAccessJustificationRequired(GSUSRecordError):
    """GSUS pediu justificativa de acesso auditada (paciente não internado
    na unidade atual agora) -- nunca preenchida/confirmada automaticamente.
    Ver `ACCESS_JUSTIFICATION_MARKER` e DECISIONS.md DEC-035."""


class GSUSNoCurrentAdmissionDays(GSUSRecordError):
    """A tela do prontuário só lista dias de episódio(s) ANTIGO(S) --
    nenhum dia pertence à internação atual. Não é falha técnica: é sinal
    de que o paciente provavelmente já não está mais internado (alta
    recente), embora ainda apareça no censo/fila do dia. Categoria
    separada de falha real no relatório -- decisão do usuário, ver
    DECISIONS.md DEC-054."""


class GSUSSearchUnresponsiveError(GSUSRecordError):
    """A tela de busca de prontuário (menu Atendimento → Pesquisar
    Prontuário) não respondeu em NENHUMA das tentativas -- a busca nem
    chegou a ser disparada. Diferente de "marcador não apareceu" (que pode
    ser paciente genuinamente sem internação atual), aqui o sinal é GSUS
    lento/instável ou sessão degradada. Achado real 2026-09-04 (DEC-117):
    44 pacientes seguidos falharam assim, 2,5 min cada, mascarados pela
    mensagem genérica de "nenhuma internação encontrada" -- o orchestrator
    usa esta classe pra refazer o login e, persistindo, interromper a
    execução em vez de gastar horas."""


# DEC-102: 3 não bastou numa instabilidade real do GSUS/máquina lenta --
# achado ao vivo 2026-09-01 (execução real, RESIL-008): 21 de 23 pacientes
# tentados falharam aqui em sequência (marcador nunca apareceu dentro de
# 3×20s), com o log histórico mostrando o MESMO erro desde 2026-08-26 (não é
# regressão desta sessão). `llm.start()` roda ANTES do GSUS (DEC-058) e fica
# residente em RAM por toda a Fase 1 -- em hardware fraco isso compete por
# recurso com o Playwright/Firefox real, tornando o corte de 3 tentativas
# curto demais sob essa carga. Mesmo padrão de fix já usado em
# NEXT_CLICK_RETRY_ATTEMPTS (DEC-024/DEC-096) -- aumentar o número de
# tentativas em vez de mudar a arquitetura (não move `llm.start()` pra
# depois da Fase 1: reintroduziria o risco de sessão GSUS expirar ociosa
# esperando o modelo carregar, exatamente o que o DEC-058 evitava).
SEARCH_RETRY_ATTEMPTS = 5
FILL_SETTLE_WAIT_MS = 1_500  # 800ms não bastou em produção real (DEC-036/037) -- máquina+rede lentas
TYPE_DELAY_MS = 80
RESULT_WAIT_MS = 20_000
MAX_EXPAND_ROUNDS = 4  # expandir um nível revela mais itens do mesmo nível; limite anti-loop
MAX_CLICKS_PER_TOGGLE = 2  # trava anti-alternância caso a detecção de "aberto" falhe
# Espera por item. 1,5s (DEC-042) foi corte agressivo demais: o log real
# mostrou 8 de 11 dias não confirmando abertura nesse prazo (DEC-047). O
# custo real é baixo porque o polling retorna assim que o corpo cresce --
# dia já aberto sai na hora; só quem depende de AJAX paga o tempo.
EXPAND_BODY_WAIT_MS = 8_000
EXPAND_POLL_MS = 150
# Teto de tempo TOTAL de expansão por prontuário. Sem isso o custo explode:
# um prontuário com dezenas de evoluções x espera por item x tentativas
# levou >15min em execução real (DEC-042). A rotina automática processa
# ~180 pacientes por rodada -- runtime precisa ser previsível, não ótimo.
EXPAND_TIME_BUDGET_S = 210
# Dias mais recentes sempre reabertos na rotina automática, mesmo já
# gravados: um dia em curso ainda recebe evolução depois da rodada
# anterior. 2 cobre a virada de meia-noite (a execução roda 00:01, então
# "ontem" e "hoje" são ambos relevantes). Ver DEC-048.
ALWAYS_REFRESH_RECENT_DAYS = 2


def _digits_only(value: str) -> str:
    """Só os dígitos -- o campo de prontuário do GSUS reformata o que é
    digitado (máscara/pontuação), e o usuário também pode digitar com ponto
    ("262.531"). O que identifica o prontuário são os dígitos (DEC-038)."""
    return re.sub(r"\D", "", value or "")


def _click_menu_to_search_screen(page: Frame | Page) -> bool:
    """Clica 'Atendimento' -> 'Pesquisar Prontuário' pra abrir a tela de
    busca de prontuário. Menu widget customizado (`<div>`, não `<a>`/
    `<button>` -- mesmo padrão do DEC-012), então o clique pode estourar
    timeout de actionability por instabilidade momentânea do GSUS.

    Achado real, grave (E2E-001, 2026-08-27): antes desta função existir,
    esse clique ficava FORA do `try/except` do loop de retry de
    `open_current_admission`/`get_full_admission_history_text` -- um
    timeout aqui escapava direto pro tratamento genérico de falha por
    paciente do orchestrator, sem NUNCA chegar a tentar de novo (o retry
    de `SEARCH_RETRY_ATTEMPTS` só protegia a espera pelo resultado da
    busca, não o clique do menu em si). Numa execução real de 183
    pacientes, 178 falharam exatamente aqui -- o retry existia, mas
    protegia a parte errada do fluxo. Devolve `False` (sem levantar) se o
    clique não completar -- quem chama decide se tenta de novo.

    Captura `PlaywrightError` (não só `TimeoutError`, que é subclasse dela)
    -- achado real (DEC-082): "Frame was detached" (não é timeout) escapava
    daqui do mesmo jeito, e o `str()` dessa exceção do Playwright inclui um
    "Call log" com o HTML do elemento resolvido -- risco de vazar atributo
    vinculado a paciente (`onmousedown`/`id`) pro log se deixado escapar até
    o handler genérico do orchestrator. Tratar aqui, com mensagem própria
    sem o texto da exceção, evita isso."""
    try:
        page.get_by_text("Atendimento", exact=True).click()
        page.get_by_text("Pesquisar Prontuário", exact=True).click()
        return True
    except PlaywrightError:
        return False


def open_current_admission(page: Frame | Page, record_number: str) -> Frame | Page:
    """Pesquisa o prontuário pelo número e abre o card do episódio de
    internação atual (o que ainda não tem alta). Retorna a página/frame
    onde a internação foi aberta -- pode ser uma NOVA janela pop-up (ver
    DEC-023): esta app já abre pop-up depois do login (DEC-009), é
    plausível que "Pesquisar" também abra um.

    ESTRATÉGIA (ver DECISIONS.md DEC-025/DEC-026): não tenta mais
    interpretar mensagens de erro na tela para decidir se deve tentar de
    novo -- duas vezes um texto "de erro" batia em legenda/texto padrão
    sempre presente na página (falso positivo, DEC-022), fazendo retry
    inútil mesmo quando a busca já tinha funcionado. Em vez disso: dispara
    a busca (Enter no campo -- é o que funciona de verdade, confirmado
    pelo usuário, DEC-025) e espera bastante (`RESULT_WAIT_MS`) só pelo
    sinal de sucesso definitivo (`CURRENT_ADMISSION_MARKER`). Só tenta de
    novo -- recarregando a tela de busca do zero -- se esse sinal não
    aparecer dentro do prazo."""
    existing_pages = _snapshot_pages(page)
    # DEC-117: conta quantas tentativas morreram JÁ no menu (busca nunca
    # disparada) pra distinguir "GSUS não responde" de "marcador não apareceu".
    menu_failures = 0
    for attempt in range(1, SEARCH_RETRY_ATTEMPTS + 1):
        if not _click_menu_to_search_screen(page):
            menu_failures += 1
            logger.warning("Menu 'Atendimento'/'Pesquisar Prontuário' não respondeu ao "
                            "clique (tentativa %d/%d).", attempt, SEARCH_RETRY_ATTEMPTS)
            continue
        result_page = _submit_search(page, record_number)

        current_card = result_page.get_by_text(CURRENT_ADMISSION_MARKER, exact=False).first
        try:
            current_card.wait_for(timeout=RESULT_WAIT_MS)
        except PlaywrightTimeoutError:
            # Antes de tratar como corrida/tentar de novo: checa se é a tela
            # de justificativa de acesso (DEC-035/054) -- ela é um MODAL
            # dentro da MESMA página (não um pop-up), então fica bloqueando
            # todo clique seguinte (inclusive dos PRÓXIMOS pacientes do
            # lote) se não for fechada explicitamente. Retry aqui não
            # ajudaria (o modal continuaria bloqueando) -- é definitivo.
            # "Cancelar" é ação de leitura/desistência, nunca "Confirmar
            # Justificativa" (essa sim geraria auditoria real -- DEC-035).
            if result_page.get_by_text(ACCESS_JUSTIFICATION_MARKER, exact=False).count() > 0:
                try:
                    result_page.get_by_text("Cancelar", exact=True).first.click(timeout=5_000)
                except PlaywrightTimeoutError:
                    logger.warning("Modal de justificativa detectado mas não foi possível fechar com Cancelar.")
                raise GSUSNoCurrentAdmissionDays(
                    "GSUS exige justificativa de acesso (paciente não internado nesta unidade agora)."
                )
            logger.warning("Resultado da busca não confirmado (tentativa %d/%d).",
                            attempt, SEARCH_RETRY_ATTEMPTS)
            # Fecha o pop-up desta tentativa antes de recarregar a busca na
            # página original (DEC-052: sem isso, pop-up órfão se acumula
            # em lote e bloqueia todo clique seguinte). Só fecha página
            # comprovadamente NOVA (ver `_close_if_new_page`/DEC-053) --
            # comparar por identidade de objeto Python não bastava.
            _close_if_new_page(existing_pages, result_page)
            continue
        # DEC-103: achado ao vivo 2026-09-01 -- este clique (depois que
        # `wait_for` já confirmou o marcador visível) ficava FORA de
        # qualquer try/except, diferente de todo o resto do laço. Quando
        # ele mesmo travava (`TimeoutError: Locator.click: Timeout 30000ms
        # exceeded` -- confirmado 2x no log real, o call log do Playwright
        # mostra "element is visible, enabled and stable" mas o clique trava
        # mesmo assim, provável algo sobrepondo o elemento no instante),
        # escapava cru pro handler genérico do orchestrator, sem nunca
        # reentrar no laço de `SEARCH_RETRY_ATTEMPTS` -- desperdiçando por
        # completo o aumento de tentativas do DEC-102 pra esse caso.
        try:
            current_card.click()
        except PlaywrightError:
            logger.warning(
                "Marcador encontrado mas o clique final não respondeu (tentativa %d/%d).",
                attempt, SEARCH_RETRY_ATTEMPTS,
            )
            _close_if_new_page(existing_pages, result_page)
            continue
        return result_page

    if menu_failures == SEARCH_RETRY_ATTEMPTS:
        # DEC-117: nenhuma tentativa passou do menu -- não há como saber se o
        # paciente tem internação atual; a causa é o GSUS/sessão, não o paciente.
        raise GSUSSearchUnresponsiveError(
            f"A tela de busca de prontuário não respondeu em {SEARCH_RETRY_ATTEMPTS} tentativas "
            "(menu Atendimento/Pesquisar Prontuário) -- GSUS lento, instável ou sessão degradada."
        )
    raise GSUSRecordError(
        f"Nenhuma internação em andamento encontrada após "
        f"{SEARCH_RETRY_ATTEMPTS} tentativas (marcador '{CURRENT_ADMISSION_MARKER}' não apareceu)."
    )


def _snapshot_pages(page: Frame | Page) -> frozenset:
    """Páginas existentes no browser context NESTE momento -- usado como
    referência para saber, depois, quais páginas são genuinamente NOVAS
    (ver `_close_if_new_page`/DECISIONS.md DEC-053)."""
    owner_page = page.page if hasattr(page, "page") else page
    return frozenset(owner_page.context.pages)


def _close_if_new_page(existing_pages: frozenset, candidate: Frame | Page) -> None:
    """Fecha `candidate` só se for uma `Page` que NÃO existia no browser
    context antes da busca começar (`existing_pages`, de `_snapshot_pages`).

    Correção real (DEC-053): a versão anterior comparava `candidate is
    original` -- identidade de objeto Python. Isso falha porque o
    Playwright cria um objeto `Page` novo a cada evento de pop-up
    capturado, MESMO que a janela do navegador por trás seja, em algum
    caso do GSUS, reaproveitada em vez de genuinamente nova -- fechar esse
    objeto fechava a janela de trabalho de verdade, derrubando a sessão
    inteira pro resto do lote ("Frame was detached", depois timeout
    mecânico e permanente em todo paciente seguinte). Checar contra a
    lista real de páginas do `context` é uma garantia objetiva: só fecha o
    que sabemos, sem margem de dúvida, que é extra.

    Nunca fecha uma página pré-existente. Falha ao fechar não interrompe o
    processo -- só vira aviso."""
    if candidate is None or candidate in existing_pages:
        return
    close = getattr(candidate, "close", None)  # Frame não tem `close`; Page tem
    if close is None:
        return
    try:
        close()
    except Exception:
        logger.warning("Não foi possível fechar uma página extra -- seguindo.")


def _submit_search(page: Frame | Page, record_number: str) -> Frame | Page:
    """Digita `#codPaciente` tecla por tecla e dispara a busca com Enter.
    Retorna a página onde a busca aconteceu (a mesma, ou um pop-up novo).

    1. `.fill()` (usado antes) não disparava os eventos de teclado que o
       autocomplete legado da tela escuta (`#nomePaciente`, desabilitado,
       ao lado -- padrão clássico). Valor aparecia certo mas formulário
       submetia vazio. Trocado para digitação simulada
       (`press_sequentially`, dispara evento por caractere).
    2. O BOTÃO Pesquisar (`input[name="btnConsultar"]`) se mostrou
       inerte sob automação -- testado exaustivamente (clique via
       Playwright, `page.mouse.click` nas coordenadas exatas, `el.click()`
       via JS, e até chamar `onclick()` diretamente): nenhum gerou efeito
       algum. O usuário confirmou o caminho real: digitar e apertar
       **Enter** (sem precisar do botão) busca na hora. Trocado para
       `field.press("Enter")`.
    3. A busca pode abrir uma janela pop-up nova (mesmo padrão do login,
       DEC-009/DEC-023) -- `page.context.expect_page()` captura isso, se
       acontecer; se não abrir pop-up nenhum, segue na mesma página.
    4. Digita UMA única vez (ver DECISIONS.md DEC-038). A comparação
       exata `input_value() == record_number` do DEC-036/037 estava errada:
       o campo do GSUS reformata o valor digitado (máscara/pontuação), então
       a igualdade literal nunca batia e o laço redigitava 3x seguidas à toa
       -- observado ao vivo pelo usuário. A conferência agora compara só os
       DÍGITOS de cada lado, que é o que de fato identifica o prontuário."""
    field = page.locator("#codPaciente")
    owner_page = page.page if hasattr(page, "page") else page  # Frame.page -> Page; Page já tem .context

    field.click()
    # Limpa por teclado, não `.fill("")` -- mesmo motivo do DEC-019: esse
    # campo tem autocomplete legado que só reage a eventos de teclado
    # reais, e `.fill()` (mesmo com string vazia) pode deixar o estado
    # JS interno dessincronizado do valor visível.
    field.press("Control+a")
    field.press("Backspace")
    page.wait_for_timeout(200)
    field.press_sequentially(record_number, delay=TYPE_DELAY_MS)
    page.wait_for_timeout(FILL_SETTLE_WAIT_MS)  # dá tempo do AJAX de validação assentar

    # Fail-closed (SEC-02): não submete um número diferente do pedido. Compara
    # só dígitos -- o campo pode reformatar (ponto/máscara) sem que isso
    # signifique valor errado.
    if _digits_only(field.input_value()) != _digits_only(record_number):
        raise GSUSRecordError(
            "O número do prontuário não foi aceito corretamente pelo campo de busca do GSUS."
        )

    try:
        with owner_page.context.expect_page(timeout=POPUP_WAIT_MS) as popup_info:
            field.press("Enter")
        new_page = popup_info.value
        new_page.wait_for_load_state()
        return new_page
    except PlaywrightTimeoutError:
        return page  # não abriu pop-up -- busca (se aconteceu) foi na mesma página


# TRÊS níveis de accordion -- confirmado por captura estrutural real da
# página do prontuário em 2026-08-21 (só tag/id/class, nenhum texto lido --
# ver DECISIONS.md DEC-040). O DEC-030 tinha mapeado só dois e misturava
# dia com evolução no mesmo seletor:
#   nível 1 (episódio):  <div class="card_header" id="historicoAtendimento0Item">
#   nível 2 (dia):       <a class="item" id="historicoEvolucaodata21/08/2026codInternacao5951310Item">
#   nível 3 (evolução):  <a class="item" id="historicoEvolucao7Item">
# Regra geral confirmada nos três níveis: o toggle tem id terminado em
# "Item" e o CORPO correspondente tem o mesmo id sem esse sufixo -- é isso
# que permite saber se já está aberto em vez de clicar às cegas.
EPISODE_TOGGLE_SELECTOR = 'div.card_header[id^="historicoAtendimento"][id$="Item"]'
# Dia identificado pela ESTRUTURA (link dentro do wrapper de dia), não pelo
# formato do id: o dia que a página já abre sozinha usa outro formato
# (`historicoEvolucao<N>Item`, sem "data") e era ignorado quando o seletor
# exigia o prefixo "historicoEvolucaodata" -- por isso o dia mais recente
# de cada episódio nunca era capturado (DEC-046).
DAY_TOGGLE_SELECTOR = 'div.pesquisar_prontuario_data > a.item'
NOTE_TOGGLE_SELECTOR = 'a.item[id^="historicoEvolucao"][id$="Item"]:not([id^="historicoEvolucaodata"])'

# Ordem importa: abrir episódio revela os dias; abrir dia revela as evoluções.
ACCORDION_LEVELS = [
    (EPISODE_TOGGLE_SELECTOR, "episódio"),
    (DAY_TOGGLE_SELECTOR, "dia"),
    (NOTE_TOGGLE_SELECTOR, "evolução"),
]

# Mantido por compatibilidade com quem importava o nome antigo.
ENCOUNTER_TOGGLE_SELECTOR = EPISODE_TOGGLE_SELECTOR


def extract_notes(
    page: Frame | Page,
    patient_id: str,
    known_days: frozenset[str] | set[str] = frozenset(),
    max_days_to_open: int | None = None,
) -> str:
    """Extrai as evoluções da internação atual (já aberta por
    `open_current_admission`), abrindo **apenas os dias que ainda não foram
    processados** -- decisão do usuário: a rotina automática pega só o que
    é novo; histórico completo é exclusividade do "Localizar" (RF-19).

    `known_days` são datas ISO (AAAA-MM-DD) já gravadas no banco
    (`Repository.get_note_days`). Os `ALWAYS_REFRESH_RECENT_DAYS` dias mais
    recentes são reabertos mesmo se conhecidos: um dia em curso continua
    recebendo evolução depois da rodada anterior. Duplicata não é problema
    -- o hash por evolução (INCR-001) descarta o que já existe; o objetivo
    aqui é custo, não deduplicação.

    Captura dia a dia, como o "Localizar" (DEC-044): o accordion do GSUS é
    exclusivo, então "expandir tudo e ler a página no fim" devolvia só os
    últimos dias abertos. Devolve o texto concatenado dos dias extraídos --
    quem separa em evoluções é `app.extraction.parser.parse_note_blocks`.

    `max_days_to_open` (opcional, `AppConfig.max_days_per_patient` --
    achado real 2026-09-01): limita a extração aos N dias mais recentes,
    mesmo pra paciente nunca extraído antes (sem isso, o primeiro contato
    com uma internação longa abre TODOS os dias dela, caro). Pedido do
    usuário pra acelerar um catch-up pontual do banco -- `None` (padrão)
    mantém o comportamento normal, sem limite."""
    deadline = time.monotonic() + EXPAND_TIME_BUDGET_S
    _expand_all(page, EPISODE_TOGGLE_SELECTOR, "episódio", deadline=deadline)

    days, days_in_current_episode = _collect_days(
        page, deadline, skip_days=set(known_days), current_episode_only=True,
        max_days_to_open=max_days_to_open,
    )
    if not days:
        if days_in_current_episode == 0:
            raise GSUSNoCurrentAdmissionDays(
                "Prontuário sem dias de internação atual na tela (só episódios antigos)."
            )
        raise GSUSRecordError("Nenhuma evolução encontrada para extrair.")

    return "\n\n".join(day["text"] for day in days if day["text"])


def get_full_admission_history_text(page: Frame | Page, record_number: str) -> str:
    """Busca o prontuário e devolve o texto de TODAS as internações
    (episódios) encontradas -- a atual e as antigas/com alta --, reordenado
    por CÓDIGO pra garantir mais-recente-primeiro (RF-19), em vez de só
    confiar na ordem que o GSUS entrega. Cada episódio vem rotulado com sua
    data de início (ver `_format_episodes_newest_first`).

    Recurso MANUAL "Localizar" (pedido do usuário 2026-08-20) -- NUNCA
    usado pela rotina automática, que continua usando só
    `open_current_admission` + `extract_notes` (internação atual). Por
    decisão do usuário: o texto devolvido aqui NÃO é persistido no banco
    (`Repository`) nem passa por regras/LLM (`app.analysis`) -- é só para
    leitura humana direta, minimizando o quanto de histórico antigo fica
    acumulado localmente.

    Bug real corrigido em 2026-08-21 (ver DECISIONS.md DEC-034): esta
    função pulava direto para `_submit_search` sem antes navegar até a
    tela "Pesquisar Prontuário" (menu Atendimento) -- diferente de
    `open_current_admission`, que já fazia esse clique de menu. Sem isso,
    `#codPaciente` não existe na tela (ainda na tela inicial pós-login) e
    o clique estoura timeout de 30s sempre. Corrigido reaproveitando o
    mesmo padrão de navegação + retry já validado em produção por
    `open_current_admission` (DEC-025/026)."""
    result_page = None
    for attempt in range(1, SEARCH_RETRY_ATTEMPTS + 1):
        if not _click_menu_to_search_screen(page):
            logger.warning("Menu 'Atendimento'/'Pesquisar Prontuário' não respondeu ao "
                            "clique (tentativa %d/%d).", attempt, SEARCH_RETRY_ATTEMPTS)
            continue
        candidate_page = _submit_search(page, record_number)

        # Espera o resultado por QUALQUER sinal confiável: o card estrutural
        # do episódio, ou o marcador "Permanece Internado" -- este último já
        # provado em produção por `open_current_admission` (DEC-025/026).
        # Não depender só do padrão de texto do cabeçalho foi o que
        # destravou este passo (DEC-039).
        result_ready = candidate_page.locator(EPISODE_CARD_SELECTOR).first.or_(
            candidate_page.get_by_text(CURRENT_ADMISSION_MARKER, exact=False).first
        )
        try:
            result_ready.wait_for(timeout=RESULT_WAIT_MS)
        except PlaywrightTimeoutError:
            # Antes de tratar como corrida/tentar de novo: checa se é a tela
            # de justificativa de acesso (definitivo -- nunca preenchida
            # sozinha, então retry não ajudaria, só re-dispararia o mesmo
            # modal e bloquearia a navegação seguinte -- foi exatamente o
            # bug observado antes deste fix).
            if candidate_page.get_by_text(ACCESS_JUSTIFICATION_MARKER, exact=False).count() > 0:
                raise GSUSAccessJustificationRequired(
                    "GSUS exige justificativa de acesso auditada "
                    "(paciente não internado nesta unidade agora)."
                ) from None
            logger.warning("Nenhuma internação encontrada ainda (tentativa %d/%d).", attempt, SEARCH_RETRY_ATTEMPTS)
            continue
        result_page = candidate_page
        break

    if result_page is None:
        raise GSUSRecordError("Nenhuma internação encontrada.")

    episode_count = _episode_locator(result_page).count()
    if episode_count == 0:
        raise GSUSRecordError("Nenhum episódio de internação localizado na tela.")

    # NÃO clicar nos episódios às cegas aqui: clique alterna, então isso
    # FECHAVA o episódio que já vinha aberto (a página abre o mais recente
    # sozinha) e escondia os dias dele -- ver DEC-045. Quem abre episódio é
    # `_expand_all`, que só clica no que está fechado.
    deadline = time.monotonic() + EXPAND_TIME_BUDGET_S
    _expand_all(result_page, EPISODE_TOGGLE_SELECTOR, "episódio", deadline=deadline)

    days, _ = _collect_days(result_page, deadline)
    if not days:
        # Sem toggles de dia reconhecidos: cai para o caminho antigo (texto
        # da página inteira). Fail-soft -- melhor devolver algo do que nada.
        logger.warning("Nenhum dia reconhecido -- devolvendo o texto da página como está.")
        return _format_episodes_newest_first(result_page.locator("body").inner_text())

    return _render_history(result_page, days)


# Id do toggle de dia carrega TUDO que precisamos, de forma estruturada --
# a data e a qual episódio ele pertence (DEC-044):
#   historicoEvolucaodata21/08/2026codInternacao5951310Item
#                        ^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^
#                        data do dia   chave do episódio
DAY_ID_PATTERN = re.compile(
    r"^historicoEvolucaodata(?P<data>\d{2}/\d{2}/\d{4})(?P<episodio>cod[A-Za-z]+\d+)Item$"
)


def _iso_from_br_date(value: str) -> str:
    """'21/08/2026' -> '2026-08-21' (ordenável). Devolve o original se não
    bater o formato -- fail-soft."""
    parts = value.split("/")
    if len(parts) != 3:
        return value
    day, month, year = parts
    return f"{year}-{month}-{day}"


def _days_to_skip(day_rows: list[list[str]], skip_days: set[str]) -> set[str]:
    """Ids de dia que podem ser pulados: já estão no banco E não estão
    entre os `ALWAYS_REFRESH_RECENT_DAYS` mais recentes da tela.

    Os mais recentes são sempre reabertos porque um dia em curso ainda
    recebe evolução depois da rodada anterior (DEC-048)."""
    if not skip_days:
        return set()

    dated = [(toggle_id, _day_date(toggle_id, [])) for toggle_id, _ in day_rows]
    recent = {
        toggle_id
        for toggle_id, date in sorted(dated, key=lambda item: item[1], reverse=True)[:ALWAYS_REFRESH_RECENT_DAYS]
    }
    # Data só do id aqui: pular é otimização, e ler o cabeçalho de cada dia
    # custaria justamente o acesso ao DOM que queremos evitar. Dia cuja data
    # não sai do id simplesmente não é pulado (fail-safe: extrai a mais).
    return {
        toggle_id
        for toggle_id, date in dated
        if date and date in skip_days and toggle_id not in recent
    }


def _days_beyond_cap(day_rows: list[list[str]], max_days_to_open: int) -> set[str]:
    """Achado real (2026-09-01, pedido do usuário pra acelerar um catch-up
    pontual do banco): mantém só os `max_days_to_open` dias mais recentes
    da tela, independente de já serem conhecidos ou não -- para quando o
    objetivo é atualizar rápido com o mais recente, aceitando não puxar o
    histórico inteiro de um paciente nunca extraído antes (que, sem este
    limite, abre TODOS os dias do episódio atual -- caro numa internação
    longa). Nunca ativado por padrão (`None` em `extract_notes`/
    `_collect_days` = comportamento normal, sem limite)."""
    dated = [(toggle_id, _day_date(toggle_id, [])) for toggle_id, _ in day_rows]
    keep = {
        toggle_id
        for toggle_id, date in sorted(dated, key=lambda item: item[1], reverse=True)[:max_days_to_open]
    }
    # Dia sem data reconhecida no id nunca é descartado por este limite --
    # mesmo raciocínio fail-safe do `_days_to_skip`: sem saber a idade,
    # melhor extrair a mais do que arriscar perder um dia genuinamente novo.
    return {toggle_id for toggle_id, date in dated if date and toggle_id not in keep}


def _current_episode_card_id(page: Frame | Page) -> str:
    """DEC-119: id do `div.card_body[id^="historicoAtendimento"]` cujo
    cabeçalho (irmão anterior, ou primeiro filho do mesmo pai) contém o
    marcador da internação atual ("Permanece Internado"). Devolve '' quando
    não há EXATAMENTE um card assim, quando a página não responde ou quando
    a resposta não é texto -- quem chama cai no critério antigo (episódio
    aberto). Só lê texto de cabeçalho, nunca o conteúdo dos dias."""
    try:
        result = page.evaluate(
            """(marker) => {
                const bodies = Array.from(document.querySelectorAll('div.card_body[id^="historicoAtendimento"]'));
                const hits = [];
                for (const body of bodies) {
                    const parts = [];
                    if (body.previousElementSibling) {
                        parts.push(body.previousElementSibling.textContent || '');
                    }
                    const parent = body.parentElement;
                    if (parent && parent.firstElementChild && parent.firstElementChild !== body) {
                        parts.push(parent.firstElementChild.textContent || '');
                    }
                    if (parts.join(' ').includes(marker)) hits.push(body.id);
                }
                return hits.length === 1 ? hits[0] : '';
            }""",
            CURRENT_ADMISSION_MARKER,
        )
    except PlaywrightError:
        return ""
    return result if isinstance(result, str) else ""


def _collect_days(
    page: Frame | Page,
    deadline: float,
    skip_days: set[str] | None = None,
    current_episode_only: bool = False,
    max_days_to_open: int | None = None,
) -> tuple[list[dict], int]:
    """Abre CADA dia e captura o texto DAQUELE dia na hora, em vez de
    expandir tudo e ler a página no final.

    Motivo (DEC-044): o accordion do GSUS é EXCLUSIVO -- abrir um dia fecha
    os outros. Expandir tudo e ler no fim só devolvia os últimos dias
    clicados, e ainda pegava conteúdo pela metade quando o AJAX do último
    ainda estava carregando. Capturar logo após confirmar a abertura de
    cada dia funciona com accordion exclusivo ou não.

    `current_episode_only` (rotina automática): considera apenas os dias da
    internação já aberta, ignorando em silêncio os episódios antigos que a
    tela lista colapsados. Sem isso, a rotina tentava abrir internação
    antiga -- fora do escopo dela (só internação atual) e caro
    (ver DEC-050).

    `max_days_to_open` (opcional -- ver `_days_beyond_cap`): limita a NO
    MÁXIMO esse tanto de dias abertos, os mais recentes primeiro,
    independente de já serem conhecidos. `None` (padrão) = sem limite,
    comportamento normal.

    Devolve `(dias, total_no_escopo)` -- o segundo valor é a contagem de
    dias pertencentes ao escopo considerado (após o filtro de episódio,
    antes do filtro de já-processados) e permite ao chamador distinguir
    "não havia internação atual" (zero aqui) de "havia, mas nada de novo
    pra extrair" (zero dias em `dias`, mas total > 0) -- ver DEC-054."""
    days: list[dict] = []

    # Lista de ids capturada UMA vez, no início. Iterar por `.nth(i)` era
    # frágil: cada clique reordena/reinsere nós e os índices deixam de
    # apontar para o mesmo dia (DEC-045). Id não muda.
    # Junto com o id: o id do CORPO do episódio que contém o dia (agrupa por
    # internação e funciona para os dois formatos de id de dia -- DEC-046) e
    # se esse episódio está aberto na tela.
    try:
        day_rows: list[list] = page.evaluate(
            """(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
                const card = el.closest('div.card_body[id^="historicoAtendimento"]');
                const open = card ? card.getBoundingClientRect().height > 60 : false;
                return [el.id, card ? card.id : '', open];
            })""",
            DAY_TOGGLE_SELECTOR,
        )
    except PlaywrightError as exc:
        # Mesmo achado do DEC-082 -- `.evaluate()` também pode estourar
        # "Frame was detached", e ficava sem tratamento local aqui. Nunca
        # devolve `([], 0)`: `extract_notes` interpretaria isso como "sem
        # internação atual" (GSUSNoCurrentAdmissionDays, categoria de
        # NÃO-falha no relatório -- DEC-054), escondendo uma falha técnica
        # real atrás de um rótulo de "provável alta". Levanta
        # `GSUSRecordError` (falha real, isolada por paciente via RF-12) --
        # mensagem própria, sem o texto da exceção original.
        raise GSUSRecordError("Não foi possível listar os dias da internação (sessão instável).") from exc

    if current_episode_only:
        # DEC-119 (achado real 2026-09-07): `extract_notes` expande TODOS os
        # episódios antes de chegar aqui (`_expand_all`), então "card aberto"
        # não distingue mais a internação atual dos episódios antigos -- 109
        # dos 183 pacientes ativos tinham dias de 2014-2025 capturados e 77
        # pendências de regra nasceram deles. Agora identifica o card da
        # internação atual pelo cabeçalho que carrega o marcador "Permanece
        # Internado" (o mesmo sinal já provado em `open_current_admission`) e
        # fica só com os dias dele; o critério antigo (episódio aberto) vira
        # fallback quando o cabeçalho não é localizado de forma inequívoca.
        current_card = _current_episode_card_id(page)
        if current_card and any(row[1] == current_card for row in day_rows):
            visible_rows = [row for row in day_rows if row[1] == current_card]
            criterion = "cabeçalho da internação atual"
        else:
            visible_rows = [row for row in day_rows if row[2]]
            criterion = "episódio aberto na tela (cabeçalho não localizado)"
        ignored = len(day_rows) - len(visible_rows)
        if ignored:
            logger.info(
                "Ignorando %d dia(s) de internação anterior (fora do escopo da rotina; critério: %s).",
                ignored, criterion,
            )
        elif not current_card and len(day_rows) > 1:
            logger.warning(
                "Card da internação atual não identificado pelo cabeçalho -- todos os %d dia(s) tratados como atuais.",
                len(day_rows),
            )
        day_rows = visible_rows

    day_rows = [row[:2] for row in day_rows]
    total = len(day_rows)
    skippable = _days_to_skip(day_rows, skip_days or set())
    if max_days_to_open is not None:
        capped = _days_beyond_cap(day_rows, max_days_to_open)
        if capped - skippable:
            logger.info(
                "Limite de %d dia(s) por paciente ativo -- ignorando %d dia(s) além dos mais recentes.",
                max_days_to_open, len(capped - skippable),
            )
        skippable = skippable | capped
    if skippable:
        logger.info("Pulando %d dia(s) já processado(s) em execução anterior.", len(skippable))

    for index, (toggle_id, episode_key) in enumerate(day_rows, start=1):
        if time.monotonic() > deadline:
            logger.warning("Orçamento de tempo esgotado -- %d de %d dia(s) capturado(s).", len(days), total)
            break

        if not toggle_id:
            logger.warning("Toggle de dia sem id -- pulando (não dá para confirmar abertura).")
            continue

        if toggle_id in skippable:
            continue

        try:
            toggle = page.locator(f'[id="{toggle_id}"]')

            # O dia pode estar escondido porque o episódio dele fechou (o
            # accordion é exclusivo -- DEC-044). Reabre os episódios e
            # tenta de novo antes de desistir dele. Na rotina automática
            # NÃO reabre: episódio fechado ali é internação antiga, fora do
            # escopo dela (DEC-050).
            if not toggle.is_visible():
                if current_episode_only:
                    continue
                _expand_all(page, EPISODE_TOGGLE_SELECTOR, "episódio", deadline=deadline)
                if not toggle.is_visible():
                    logger.warning("Dia %d/%d segue oculto -- seguindo sem ele.", index, total)
                    continue

            if not _is_expanded(page, toggle_id):
                toggle.click(timeout=8_000)
                if not _wait_expanded(page, toggle_id):
                    logger.warning("Dia %d/%d não confirmou abertura -- capturando o que houver.", index, total)

            body = page.locator(f'[id="{toggle_id[: -len("Item")]}"]')
            header_lines = (toggle.inner_text() or "").strip().splitlines()
            days.append(
                {
                    "episode": episode_key,
                    "date": _day_date(toggle_id, header_lines),
                    "header": header_lines[0].strip() if header_lines else "",
                    "text": (body.inner_text() if body.count() else "").strip(),
                }
            )
        except PlaywrightError:
            # `PlaywrightError`, não só `TimeoutError` -- mesmo achado do
            # DEC-082 (`_expand_all`): "Frame was detached" aqui também
            # arrastaria pro log um "Call log" com id de dia/episódio
            # vinculado a paciente, se deixado escapar sem tratamento local.
            logger.warning("Falha ao abrir/capturar o dia %d/%d -- seguindo.", index, total)

    # Só contagem/tamanho -- nunca conteúdo (RNF-03/SEC-03). Serve para
    # saber se a extração cobriu tudo sem ninguém precisar ler nota clínica.
    with_content = sum(1 for day in days if day["text"])
    logger.info(
        "Dias capturados: %d de %d (%d com conteúdo, %d caracteres no total).",
        len(days), total, with_content, sum(len(day["text"]) for day in days),
    )
    return days, total


def _day_date(toggle_id: str, header_lines: list[str]) -> str:
    """Data do dia em ISO, para ordenar. Prefere a data embutida no id
    (estrutural); se o id não tiver (é o caso do dia que a página abre
    sozinha -- DEC-046), extrai do texto do cabeçalho ("21 de agosto de
    2026 - ..."). Sem nenhuma das duas, devolve string vazia (vai para o
    fim da ordenação, sem quebrar nada)."""
    match = DAY_ID_PATTERN.match(toggle_id)
    if match is not None:
        return _iso_from_br_date(match.group("data"))
    if header_lines:
        return _parse_header_date(header_lines[0]) or ""
    return ""


def _episode_header_text(page: Frame | Page, episode_key: str) -> str:
    """Primeira linha do cabeçalho do episódio, que traz rótulo e datas.
    `episode_key` é o id do CORPO do card (`historicoAtendimentoN`); o
    cabeçalho correspondente é o mesmo id com sufixo "Item" (regra do
    DEC-040). Fail-soft: devolve string vazia se não achar."""
    try:
        card = page.locator(f'[id="{episode_key}Item"]')
        if card.count() == 0:
            return ""
        lines = (card.first.inner_text() or "").strip().splitlines()
        return lines[0].strip() if lines else ""
    except PlaywrightError:
        return ""


def _render_history(page: Frame | Page, days: list[dict]) -> str:
    """Monta o texto final a partir dos dias capturados: agrupa por
    episódio e ordena tudo do mais recente para o mais antigo (RF-19).

    Ordenação vem da DATA embutida no id do toggle (estrutural), não de
    interpretar texto da tela -- ver DEC-044."""
    by_episode: dict[str, list[dict]] = {}
    for day in days:
        by_episode.setdefault(day["episode"], []).append(day)

    # episódio mais recente primeiro = maior data de dia dentro dele
    episodes = sorted(
        by_episode.items(),
        key=lambda item: max(day["date"] for day in item[1]),
        reverse=True,
    )

    parts: list[str] = []
    total = len(episodes)
    for index, (episode_key, episode_days) in enumerate(episodes, start=1):
        header = _episode_header_text(page, episode_key)
        title = f"===== Internação {index} de {total}"
        if header:
            title += f" — {header}"
        parts.append(title + " =====")

        for day in sorted(episode_days, key=lambda d: d["date"], reverse=True):
            parts.append(f"--- {day['header'] or day['date']} ---")
            parts.append(day["text"] or "(sem registros neste dia)")

    return "\n\n".join(parts)


def _episode_locator(page: Frame | Page):
    """Localiza os cards de episódio de internação, preferindo o seletor
    ESTRUTURAL (`EPISODE_CARD_SELECTOR`, baseado em id real observado) e
    caindo para o padrão de TEXTO só se o estrutural não achar nada.

    Ordem deliberada (ver DECISIONS.md DEC-039): depender do texto exibido
    foi exatamente o que falhou em produção -- id de elemento é mais
    estável que rótulo de UI. O fallback existe porque o id só foi
    observado em um paciente até agora."""
    structural = page.locator(EPISODE_CARD_SELECTOR)
    if structural.count() > 0:
        return structural
    logger.warning("Nenhum card de episódio pelo id estrutural -- usando o padrão de texto como alternativa.")
    return page.get_by_text(ADMISSION_HEADER_PATTERN)


# Estado colapsado medido na árvore real (DEC-043): o corpo de um item
# fechado é sempre um invólucro mínimo -- exatamente 2 filhos e ~42px de
# altura (só uma `table.form_tabela` de placeholder). Ao abrir de verdade,
# ambos crescem muito. Medida serve para os três níveis.
COLLAPSED_MAX_CHILDREN = 2
COLLAPSED_MAX_HEIGHT_PX = 60  # 42px observado + margem


def _body_metrics(page: Frame | Page, toggle_id: str) -> tuple[int, int] | None:
    """(nº de filhos, altura em px) do corpo do toggle, ou None se não
    existir. Lê só geometria/contagem -- nunca texto."""
    if not toggle_id or not toggle_id.endswith("Item"):
        return None
    body_id = toggle_id[: -len("Item")]
    try:
        return page.evaluate(
            """(id) => {
                const el = document.getElementById(id);
                if (!el) return null;
                return [el.childElementCount, Math.round(el.getBoundingClientRect().height)];
            }""",
            body_id,
        )
    except Exception:
        return None


def _is_expanded(page: Frame | Page, toggle_id: str) -> bool:
    """Aberto ou não, medido pelo TAMANHO real do corpo.

    Tentativas anteriores falharam por confiar em indicadores que mentem
    (DEC-043): a classe "aberta" da seta aparece em TODOS os dias, abertos
    ou não, e `is_visible()` do corpo é verdadeiro mesmo colapsado (o
    invólucro de 42px é visível). O tamanho é o único sinal que
    acompanhou o estado real em todos os casos observados.

    Sem id ou sem corpo: assume fechado (clicar a mais é recuperável; não
    clicar deixa dado de fora)."""
    metrics = _body_metrics(page, toggle_id)
    if metrics is None:
        return False
    children, height = metrics
    return children > COLLAPSED_MAX_CHILDREN or height > COLLAPSED_MAX_HEIGHT_PX


def _wait_expanded(page: Frame | Page, toggle_id: str) -> bool:
    """Espera o corpo do toggle ficar visível. O conteúdo do dia vem por
    AJAX (DEC-041): esperar o EFEITO real é a única forma confiável -- é o
    mesmo princípio já usado na paginação do censo (DEC-016)."""
    deadline_polls = EXPAND_BODY_WAIT_MS // EXPAND_POLL_MS
    for _ in range(max(1, deadline_polls)):
        if _is_expanded(page, toggle_id):
            return True
        page.wait_for_timeout(EXPAND_POLL_MS)
    return _is_expanded(page, toggle_id)


def _expand_all(page: Frame | Page, selector: str, label: str, deadline: float | None = None) -> None:
    """Abre APENAS o que está fechado, e CONFIRMA que abriu antes de seguir.

    Dois bugs reais já corrigidos aqui:
    - clicar às cegas alternava (fechava de volta o que já estava aberto),
      DEC-040;
    - marcar o toggle como "já processado" no primeiro clique fazia com que
      um clique ignorado pela página (ocupada com o AJAX do item anterior)
      nunca fosse tentado de novo -- por isso só o PRIMEIRO dia de cada
      episódio expandia (DEC-041).

    Agora: repete rodadas até que nada mais esteja fechado, esperando o
    corpo de cada item aparecer de fato. `attempts_by_id` limita a
    `MAX_CLICKS_PER_TOGGLE` por item, pra nunca voltar a alternar sem fim
    caso a detecção de "aberto" falhe em algum caso não previsto.

    `deadline` (relógio monotônico) corta a expansão quando o orçamento de
    tempo acaba -- runtime previsível vale mais que completude aqui, porque
    a rotina automática repete isso para ~180 pacientes (DEC-042)."""
    attempts_by_id: dict[str, int] = {}

    for round_index in range(MAX_EXPAND_ROUNDS):
        toggles = page.locator(selector)
        try:
            toggle_count = toggles.count()
        except PlaywrightError:
            # Achado real (DEC-082, validação em produção): `.count()` em
            # si também pode estourar "Frame was detached" -- ficava fora
            # do `try/except` de dentro do loop (que só protege as
            # operações POR item), escapando sem tratamento local.
            logger.warning("Não foi possível listar itens de %s -- seguindo com o que já abriu.", label)
            return
        clicked_any = False

        for i in range(toggle_count):
            if deadline is not None and time.monotonic() > deadline:
                logger.warning("Orçamento de tempo de expansão esgotado em %s -- seguindo com o que abriu.", label)
                return
            toggle = toggles.nth(i)
            try:
                toggle_id = toggle.get_attribute("id") or ""

                if _is_expanded(page, toggle_id):
                    continue
                # Sem id não dá pra confirmar abertura -- clica só na 1ª
                # rodada, pra não ficar alternando às cegas.
                if not toggle_id and round_index > 0:
                    continue
                if attempts_by_id.get(toggle_id, 0) >= MAX_CLICKS_PER_TOGGLE:
                    continue
                if not toggle.is_visible():
                    continue

                attempts_by_id[toggle_id] = attempts_by_id.get(toggle_id, 0) + 1
                toggle.click(timeout=8_000)
                clicked_any = True
                if toggle_id and not _wait_expanded(page, toggle_id):
                    logger.warning("Item de %s não confirmou abertura -- será tentado de novo.", label)
                else:
                    page.wait_for_timeout(EXPAND_POLL_MS)
            except PlaywrightError:
                # `PlaywrightError` (não só `TimeoutError`) -- achado real
                # (DEC-082, E2E-001 2026-08-27): "Frame was detached" (não é
                # timeout) escapava daqui sem cair neste `except`, indo
                # parar no handler genérico do orchestrator com o "Call
                # log" da exceção (inclui atributo `onmousedown` vinculado a
                # paciente/episódio deste seletor) -- risco de log com dado
                # sensível. Mensagem aqui é sempre genérica, sem o texto da
                # exceção original.
                logger.warning("Não foi possível expandir um item de %s -- seguindo sem ele.", label)

        if not clicked_any:
            return

    logger.warning("Expansão de %s atingiu o limite de rodadas -- pode haver item não aberto.", label)


def _expand_everything(page: Frame | Page) -> None:
    """Expande os três níveis, na ordem (abrir episódio revela dias; abrir
    dia revela evoluções). Ver `ACCORDION_LEVELS` e DECISIONS.md DEC-040.

    Orçamento de tempo compartilhado entre os níveis, gasto na ordem em que
    importa: episódio e dia primeiro (é onde o conteúdo aparece), evolução
    por último (ver DEC-042)."""
    deadline = time.monotonic() + EXPAND_TIME_BUDGET_S
    for selector, label in ACCORDION_LEVELS:
        _expand_all(page, selector, label, deadline=deadline)
