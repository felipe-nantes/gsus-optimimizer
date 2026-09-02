"""Extração do censo de internados (GSUS-002 / GSUS-003).

Estrutura da tela "Pesquisar Internação" confirmada por descrição
sanitizada do usuário em 2026-08-20 (cabeçalhos de coluna e campos do
formulário -- nunca dado de paciente real, ver DECISIONS.md DEC-011):

  Formulário: * EAS (fixo), Município (fixo), Nº Prontuário (2 campos),
  * Status Internação (dropdown, já vem em "Internado"), link "Pesquisa
  Avançada". Botões: Pesquisar / Limpar.

  Resultado: tabela paginada ("Página X de Y : Total de N registros",
  links "Próxima"/"Última"), colunas nesta ordem:
  Prontuário | Nome do Paciente | Data Nascimento | Nome da Mãe |
  Data de Internação | Descrição do Leito | Unidade Org. | Localização |
  Status | Inconsistente | Visualizar.

Este módulo só extrai record_number/bed/unit/admission_date -- `Patient`
não tem campo de nome, para minimizar dado sensível tocado pela automação
mesmo quando disponível na tela (princípio de minimização de PHI).

Caminho de navegação CONFIRMADO pelo usuário em 2026-08-20: menu
"Internação" -> item "Pesquisar Internação" -> botão "Pesquisar".

Menu e formulário NÃO usam elementos semânticos (`<a>`/`<button>`) -- é um
widget de menu customizado (`<div class="clLevel0/clLevel1">`) e inputs
`type="button"`. Confirmado por inspeção real (headless, tela sem dado de
paciente -- só formulário vazio) em 2026-08-20:
  - "Internação": `<div>` (localizado por texto, id interno instável)
  - "Pesquisar Internação": idem
  - Campo "Nº Prontuário": `#codPaciente`
  - Botão "Pesquisar": `#btConsultar`
  - Botão "Limpar": `#btLimpar`

AINDA NÃO CONFIRMADO EM EXECUÇÃO REAL:
  - Exato texto/estrutura dos links de paginação em HTML real (assumido
    `<a>Próxima</a>` simples) -- só a lógica foi testada, com fixture.
  - Estrutura real da tabela de resultado (colunas confirmadas pelo
    usuário, mas o HTML exato -- ex.: se é mesmo `<table><tbody><tr><td>` --
    não foi inspecionado, para não abrir a tela com dado de paciente real).
"""
from __future__ import annotations

import logging
import re

from playwright.sync_api import Error as PlaywrightError, Frame, Page, TimeoutError as PlaywrightTimeoutError

from app.models import Patient

logger = logging.getLogger(__name__)

COL_RECORD_NUMBER = 0
COL_ADMISSION_DATE = 4
COL_BED = 5
COL_UNIT = 6
MIN_EXPECTED_COLUMNS = 7  # até Unidade Org. (índice 6) -- ver DEC-014
NEXT_CLICK_RETRY_ATTEMPTS = 5  # ver DEC-024/DEC-096 -- 2 não bastou numa instabilidade real do GSUS (2026-08-31)
# DEC-105/RESIL-011 (auditoria de certificação, 2026-09-01): diferente de
# TODO o resto da navegação no GSUS (menu de prontuário, paginação), abrir
# a tela de busca do censo nunca teve retry -- um único hiccup aqui aborta
# o dia inteiro, ANTES de existir `run_id` (zero pacientes, zero relatório).
SEARCH_SCREEN_RETRY_ATTEMPTS = 5

# "Prontuário" sozinho também bate no rótulo do formulário de busca (que
# fica numa <table> própria, acima do resultado -- app legado, layout com
# tabelas). "Inconsistente" só existe no cabeçalho da tabela de resultado
# -- ver DECISIONS.md DEC-014 (bug real encontrado em execução real).
RESULT_TABLE_MARKER = "Inconsistente"

MAX_PAGES = 500  # trava de segurança contra paginação que nunca termina (ROBUST-001)

# DEC-108 (achado real, madrugada 2026-09-01/02 -- primeira execução
# autônoma sem supervisão): em 3 execuções seguidas do censo real, a
# paginação terminou "normalmente" (`next_link.count() == 0`, sem nenhum
# retry esgotado, sem levantar `GSUSCensusIncompleteError`) mas coletando
# totais bem diferentes e sempre abaixo do esperado (159, depois 176, depois
# 175, com o histórico recente girando perto de 190) -- e comparação de
# sobreposição entre as 3 execuções mostrou que pacientes vistos numa
# execução anterior mas ausentes só na última foram marcados como alta por
# `mark_patients_inactive_not_in`, mesmo tendo sido confirmados internados
# horas antes na mesma noite. Ou seja, "sem link Próxima" NÃO é prova
# suficiente de censo completo -- o próprio GSUS expõe quantos registros
# deveria ter ("Página X de Y : Total de N registros") e isso nunca era
# conferido contra o que foi de fato coletado.
TOTAL_RECORDS_PATTERN = re.compile(r"Total de (\d+) registros")
# `expected_total` conta linhas BRUTAS da paginação, mas `patients` já passou
# por dedup por prontuário (DEC-014 -- "duplicatas espalhadas" por corrida da
# paginação AJAX são reais, vistas em execução real, não hipotéticas).
# Tolerância pequena e absoluta pra não confundir esse ruído normal com o
# bug real do DEC-108 (gap de dezenas de pacientes).
ALLOWED_TOTAL_COUNT_GAP = 2


class GSUSCensusError(Exception):
    """Falha ao obter a lista de internados."""


class GSUSCensusIncompleteError(GSUSCensusError):
    """Paginação desistiu antes de percorrer todas as páginas (ex.: 'Próxima'
    instável -- DEC-024/DEC-080). Carrega em `.patients` os pacientes já
    coletados até aqui -- quem chama pode (e deve) processá-los normalmente,
    mas NUNCA deve tratar essa lista como o censo completo (em especial,
    nunca alimentar `Repository.mark_patients_inactive_not_in` com ela --
    marcaria paciente real, só ainda não alcançado pela paginação, como se
    tivesse recebido alta)."""

    def __init__(self, message: str, patients: list[Patient]):
        super().__init__(message)
        self.patients = patients


def get_census(gsus_frame: Frame | Page, unit: str, max_patients: int | None = None) -> list[Patient]:
    """Pesquisa e extrai os pacientes internados, percorrendo páginas de
    resultado. `gsus_frame` é o frame `content` retornado por
    `app.gsus.client.get_content_frame()` (após login).

    `max_patients`: se informado, para de paginar assim que atingir essa
    quantidade -- útil só para teste/diagnóstico rápido (ver
    `scripts/check_notes.py`, pedido do usuário 2026-08-20 pra encurtar o
    ciclo de teste). O uso de produção (relatório real) NUNCA deve passar
    esse parâmetro -- omitido (`None`, padrão), continua buscando o censo
    completo, sem mudança de comportamento."""
    _navigate_and_search_with_retry(gsus_frame)
    return collect_all_pages(gsus_frame, max_patients=max_patients)


def _navigate_and_search_with_retry(gsus_frame: Frame | Page) -> None:
    """Abre a tela de busca do censo e clica em Pesquisar, tentando de novo
    (do zero) se qualquer passo falhar -- mesmo padrão já validado em
    `app/gsus/records.py::_click_menu_to_search_screen` (DEC-024/081/096).
    Antes desta correção, um único hiccup aqui (menu customizado instável,
    igual ao resto do app) abortava a execução inteira sem nenhuma
    tentativa nova, mesmo com todo o resto do pipeline já protegido."""
    last_exc: Exception | None = None
    for attempt in range(1, SEARCH_SCREEN_RETRY_ATTEMPTS + 1):
        try:
            _navigate_to_search_screen(gsus_frame)
            gsus_frame.locator("#btConsultar").click()
            gsus_frame.wait_for_selector(f"table:has-text('{RESULT_TABLE_MARKER}')")
            return
        except PlaywrightError as exc:
            last_exc = exc
            logger.warning("Tela de busca do censo não respondeu (tentativa %d/%d).",
                            attempt, SEARCH_SCREEN_RETRY_ATTEMPTS)
    raise GSUSCensusError(
        f"Não foi possível abrir/pesquisar a tela de censo após {SEARCH_SCREEN_RETRY_ATTEMPTS} tentativas."
    ) from last_exc


def collect_all_pages(gsus_frame: Frame | Page, max_patients: int | None = None) -> list[Patient]:
    """Percorre a paginação a partir da página de resultado já carregada e
    devolve a lista (completa, ou limitada a `max_patients` -- ver
    `get_census`). Separado de `get_census` para ser testável com fixtures
    HTML estáticas, sem depender do caminho de navegação (ainda não
    confirmado -- ver `_navigate_to_search_screen`)."""
    patients: list[Patient] = []
    seen_record_numbers: set[str] = set()

    for _ in range(MAX_PAGES):
        first_record_before = _first_record_number(gsus_frame)
        _extract_page_rows(gsus_frame, patients, seen_record_numbers)

        if max_patients is not None and len(patients) >= max_patients:
            return patients[:max_patients]

        next_link = gsus_frame.get_by_role("link", name="Próxima")
        if next_link.count() == 0:
            expected_total = _extract_expected_total(gsus_frame)
            if expected_total is not None and expected_total - len(patients) > ALLOWED_TOTAL_COUNT_GAP:
                # DEC-108: o GSUS diz "acabou a paginação" mas o próprio
                # rodapé da tabela anuncia mais registros do que os
                # coletados -- não é seguro tratar como censo completo (ver
                # comentário de MAX_PAGES acima para o incidente real que
                # motivou esta checagem). Mesmo tratamento dos outros dois
                # caminhos de saída incompleta logo abaixo: devolve o que
                # já foi coletado, mas nunca como se fosse o censo inteiro.
                logger.warning(
                    "GSUS não ofereceu link 'Próxima', mas só %d de %d registro(s) "
                    "anunciados foram coletados -- tratando como censo incompleto.",
                    len(patients), expected_total,
                )
                raise GSUSCensusIncompleteError(
                    f"Paginação terminou sem 'Próxima', mas GSUS anunciou {expected_total} "
                    f"registro(s) e só {len(patients)} foram coletados -- censo pode estar "
                    "incompleto.",
                    patients,
                )
            return patients

        if not _click_next_with_retry(next_link):
            # Clique em "Próxima" instável/travando (visto em execução real
            # -- possível lentidão momentânea do servidor, não um erro de
            # seletor -- ver DECISIONS.md DEC-024). Levanta em vez de
            # devolver a lista parcial como se fosse o censo inteiro --
            # achado real DEC-080: um `return patients` aqui fazia
            # `mark_patients_inactive_not_in` (orchestrator.py) marcar TODO
            # paciente fora dessa lista parcial como se tivesse recebido
            # alta, mesmo continuando internado de verdade. Quem chama
            # ainda pode (e deve) processar `.patients` -- só não pode usar
            # essa lista pra decidir quem saiu do censo.
            logger.warning("Não foi possível clicar em 'Próxima' após %d tentativas -- "
                            "encerrando paginação com %d paciente(s) coletado(s) até aqui.",
                            NEXT_CLICK_RETRY_ATTEMPTS, len(patients))
            raise GSUSCensusIncompleteError(
                f"Paginação interrompida após {NEXT_CLICK_RETRY_ATTEMPTS} tentativas -- "
                f"{len(patients)} paciente(s) coletado(s) até aqui, censo pode estar incompleto.",
                patients,
            )

        if not _wait_for_first_row_to_change(gsus_frame, first_record_before):
            # Achado de auditoria (RESIL-011/DEC-105, 2026-09-01): isto era
            # um SEGUNDO caminho de saída do mesmo bug do DEC-080 -- o
            # clique em "Próxima" teve sucesso (`next_link.count() > 0` já
            # descartou "não existe próxima página" na checagem acima), mas
            # o conteúdo não mudou dentro do prazo. Isso é indistinguível de
            # "a paginação AJAX travou" -- devolver `patients` aqui como se
            # fosse o censo COMPLETO faria `mark_patients_inactive_not_in`
            # (orchestrator.py) marcar paciente real, só ainda não
            # alcançado, como se tivesse recebido alta. Mesmo tratamento do
            # `_click_next_with_retry` falho, logo acima.
            logger.warning("Página não mudou após clicar 'Próxima' -- encerrando paginação com "
                            "%d paciente(s) coletado(s) até aqui.", len(patients))
            raise GSUSCensusIncompleteError(
                f"Paginação parou de avançar (conteúdo não mudou) -- "
                f"{len(patients)} paciente(s) coletado(s) até aqui, censo pode estar incompleto.",
                patients,
            )

    raise GSUSCensusError(f"Paginação do censo excedeu {MAX_PAGES} páginas -- abortando (ROBUST-001).")


def _click_next_with_retry(next_link) -> bool:
    """Tenta clicar em 'Próxima' algumas vezes antes de desistir. Visto em
    execução real: o clique pode estourar timeout esperando o elemento
    ficar "estável" (30s) -- provável lentidão momentânea do servidor sob
    uso automatizado repetido, não um seletor errado (o log mostra o
    elemento certo sendo encontrado). Retorna False se todas as tentativas
    falharem (ver DECISIONS.md DEC-024)."""
    for attempt in range(1, NEXT_CLICK_RETRY_ATTEMPTS + 1):
        try:
            next_link.first.click(timeout=15_000)
            return True
        except PlaywrightTimeoutError:
            logger.warning("Clique em 'Próxima' não confirmado (tentativa %d/%d).",
                            attempt, NEXT_CLICK_RETRY_ATTEMPTS)
    return False


PAGE_CHANGE_TIMEOUT_MS = 10_000
PAGE_CHANGE_POLL_MS = 200


def _extract_expected_total(gsus_frame: Frame | Page) -> int | None:
    """Lê 'Total de N registros' do rodapé da tabela de resultado (DEC-108).
    Usada só como checagem extra de sanidade no fim da paginação -- nunca
    para decidir QUANTAS páginas percorrer (isso continua sendo o link
    'Próxima'). Retorna `None` se o texto não bater no formato esperado
    (nesse caso quem chama cai de volta no comportamento antigo, sem essa
    checagem extra -- degradação graciosa, não trava a execução)."""
    try:
        text = gsus_frame.locator("p", has_text="Total de").first.inner_text()
    except PlaywrightError:
        return None
    match = TOTAL_RECORDS_PATTERN.search(text)
    if not match:
        return None
    return int(match.group(1))


def _first_record_number(gsus_frame: Frame | Page) -> str | None:
    table = gsus_frame.locator("table").filter(has_text=RESULT_TABLE_MARKER).first
    rows = table.locator("tbody tr")
    for i in range(rows.count()):
        cells = rows.nth(i).locator("td")
        if cells.count() >= MIN_EXPECTED_COLUMNS:
            return cells.nth(COL_RECORD_NUMBER).inner_text().strip()
    return None


def _wait_for_first_row_to_change(gsus_frame: Frame | Page, previous_first_record: str | None) -> bool:
    """A paginação é assíncrona (AJAX) -- não há evento de navegação de
    página pra esperar (`wait_for_load_state` retorna antes da tabela
    atualizar, confirmado em execução real -- ver DEC-016). Espera até a
    primeira linha da tabela mudar de valor, com timeout curto."""
    deadline_iterations = PAGE_CHANGE_TIMEOUT_MS // PAGE_CHANGE_POLL_MS
    for _ in range(deadline_iterations):
        gsus_frame.wait_for_timeout(PAGE_CHANGE_POLL_MS)
        if _first_record_number(gsus_frame) != previous_first_record:
            return True
    return False


def _extract_page_rows(gsus_frame, patients: list[Patient], seen_record_numbers: set[str]) -> None:
    table = gsus_frame.locator("table").filter(has_text=RESULT_TABLE_MARKER).first
    rows = table.locator("tbody tr")
    row_count = rows.count()

    for i in range(row_count):
        cells = rows.nth(i).locator("td")
        cell_count = cells.count()
        if cell_count == 0:
            # Linha de cabeçalho com <th> em vez de <td>, sem <thead>
            # separado (confirmado em execução real -- ver DEC-015). Não é
            # erro, só não é uma linha de paciente.
            continue
        if cell_count < MIN_EXPECTED_COLUMNS:
            # Isso sim é suspeito: uma linha com <td> de verdade mas de
            # menos -- provavelmente pegou a tabela errada (ver DEC-014).
            # Falha rápido e claro em vez de travar num timeout genérico.
            raise GSUSCensusError(
                f"Linha {i} da tabela de resultado tem {cell_count} coluna(s), "
                f"esperava pelo menos {MIN_EXPECTED_COLUMNS} -- a tabela "
                "localizada provavelmente não é a de resultado do censo."
            )
        record_number = cells.nth(COL_RECORD_NUMBER).inner_text().strip()
        if not record_number:
            continue  # linha sem prontuário (cabeçalho/rodapé) -- ignora, não é paciente
        if record_number in seen_record_numbers:
            # NUNCA logar o número do prontuário -- é identificador real de
            # paciente. O logging padrão do Python imprime WARNING+ no
            # console mesmo sem handler configurado (aconteceu de verdade
            # -- ver DECISIONS.md DEC-016).
            logger.warning("Prontuário duplicado no censo (omitido do log) -- mantendo só a primeira ocorrência")
            continue
        seen_record_numbers.add(record_number)
        patients.append(
            Patient(
                record_number=record_number,
                bed=cells.nth(COL_BED).inner_text().strip(),
                unit=cells.nth(COL_UNIT).inner_text().strip(),
                admission_date=cells.nth(COL_ADMISSION_DATE).inner_text().strip(),
            )
        )


def _navigate_to_search_screen(gsus_frame) -> None:
    """Menu 'Internação' -> item 'Pesquisar Internação' -- caminho
    confirmado pelo usuário; elementos são <div> de um widget de menu
    customizado, não <a> (ver DECISIONS.md DEC-011/DEC-012). Localizar por
    texto exato é mais estável que o id interno do widget (ex.:
    'oCMenu__4803'), que não é garantidamente o mesmo entre sessões."""
    gsus_frame.get_by_text("Internação", exact=True).click()
    gsus_frame.get_by_text("Pesquisar Internação", exact=True).click()
