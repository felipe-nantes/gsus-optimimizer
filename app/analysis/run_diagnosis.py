"""Classificação em linguagem simples do resultado de uma execução
(DIAG-001, 2026-09-03) -- pedido explícito do usuário: o auditor que opera
o sistema (sem conhecimento técnico, ver PROJECT_SPEC.md seção 2) precisa
saber se uma falha foi causada pelo GSUS/rede/máquina (não é defeito deste
programa) ou é algo que precisa de suporte de verdade, ANTES de decidir se
vale fazer uma auditoria manual naquele dia -- sem isso, um erro do GSUS ou
o computador desligado de noite vira, aos olhos do auditor, "o sistema não
funciona".

Catálogo de causas construído a partir de uma auditoria exaustiva de TODO
caminho de falha do código existente (2026-09-03, ver DECISIONS.md
DEC-111). Achado central dessa auditoria: o NOME da classe de exceção
sozinho não basta pra classificar "foi o GSUS" -- várias exceções `GSUS*`
cobrem tanto instabilidade real do GSUS quanto casos que merecem
investigação (ex.: `GSUSRecordError` cobre desde "marcador não confirmado a
tempo" [GSUS lento, DEC-102] até "número de prontuário não aceito pelo
campo de busca" [pode ser um bug nosso, DEC-036/037/038]). A lista abaixo é
deliberadamente conservadora: um padrão só entra como "causa conhecida" se
o próprio código, nalgum DEC já registrado, atribui aquele caminho
ESPECÍFICO à instabilidade do GSUS -- qualquer coisa fora da lista
(incluindo erro bruto do Playwright escapando de um ponto ainda sem retry,
também catalogado nessa auditoria) é tratada como "precisa de
investigação", nunca o contrário. Adicionar um padrão novo aqui exige achar
o DEC correspondente primeiro -- nunca só "parece que é do GSUS"."""
from __future__ import annotations

from datetime import datetime, timezone

OUTCOME_SUCESSO = "SUCESSO"
OUTCOME_SUCESSO_PARCIAL_GSUS = "SUCESSO_PARCIAL_GSUS"
OUTCOME_FALHA_GSUS = "FALHA_GSUS"
OUTCOME_FALHA_INESPERADA = "FALHA_INESPERADA"
# UI-006 (2026-09-04): o próprio auditor clicou "Encerrar" -- não é falha de
# nada (nem do GSUS, nem deste programa), só uma execução parada no meio.
OUTCOME_CANCELADA = "CANCELADA"

# Acima desta fração de pacientes com erro (mesmo que todos de causa
# conhecida), o resumo passa a sugerir conferência manual -- muita gente
# ficou sem processar, independente do motivo.
PARTIAL_FAILURE_THRESHOLD = 0.10

# Sem execução registrada há mais que isto (uma execução diária + folga),
# a dashboard avisa que a atualização agendada pode não ter rodado --
# limitação já conhecida do agendador do Windows (RESIL-003, TASKS.md),
# nunca tratado como bug novo.
STALENESS_THRESHOLD_HOURS = 30

# Padrões de mensagem (substring, aplicados ao texto já salvo em
# `processing_queue.last_error`, formato "{TipoDaExceção}: {1ª linha}" --
# ver `orchestrator.py::_safe_error_text`) que a auditoria de catálogo
# confirmou serem causa conhecida do GSUS/rede, nunca do código deste
# programa. Ordem não importa (checagem é "algum padrão aparece no texto").
KNOWN_GSUS_ERROR_PATTERNS = [
    "GSUSCensusIncompleteError",  # paginação do censo (DEC-080/096/108)
    "Não foi possível abrir/pesquisar a tela de censo",  # GSUSCensusError, DEC-105/RESIL-011
    "Nenhuma internação em andamento encontrada após",  # GSUSRecordError, marcador (DEC-102)
    "GSUSSearchUnresponsiveError",  # tela de busca não respondeu em nenhuma tentativa (DEC-117)
    "Nenhuma evolução encontrada para extrair",  # GSUSRecordError
    "Nenhuma internação encontrada.",  # GSUSRecordError, DEC-025/026/034
    "Não foi possível listar os dias da internação (sessão instável)",  # GSUSRecordError, DEC-082
    "GSUSLoginError",  # login (pop-up/confirmação de estabelecimento)
]

# Achado real ao implementar isto (não confiar em suposição): a classe
# `TimeoutError` do Playwright (e `Error`, sua base) usam exatamente esses
# nomes curtos em `type(exc).__name__` -- NUNCA aparecem como
# "PlaywrightTimeoutError"/"PlaywrightError" no texto persistido por
# `_safe_error_text`. Um erro bruto do Playwright que escapa de um ponto
# ainda sem retry (catalogado na auditoria DEC-111 -- ex.: `_first_record_number`
# em census.py) portanto NUNCA casa com a lista acima, e cai corretamente
# em FALHA_INESPERADA -- deliberado: "TimeoutError"/"Error" sozinhos são
# genéricos demais pra assumir que é sempre culpa do GSUS (podem vir de
# qualquer lugar), e esses caminhos são justamente gaps de retry ainda não
# fechados, não instabilidade já mapeada como as da lista acima.


def is_known_gsus_error(last_error: str | None) -> bool:
    """Casa contra `KNOWN_GSUS_ERROR_PATTERNS` -- usado por paciente
    (`processing_queue.last_error`, já persistido como texto)."""
    if not last_error:
        return False
    return any(pattern in last_error for pattern in KNOWN_GSUS_ERROR_PATTERNS)


def classify_top_level_exception(exc: Exception) -> tuple[str, str]:
    """Classifica uma exceção que impediu a execução de sequer terminar a
    Fase 1 (login falhou, censo falhou por completo, ou qualquer erro não
    isolado por paciente escapou). Chamado com a exceção DE VERDADE (não um
    texto já salvo) -- por isso usa `isinstance`, mais confiável que casar
    string. Devolve (outcome, resumo em linguagem simples)."""
    import sqlite3

    from app.gsus.census import GSUSCensusError
    from app.gsus.login import GSUSLoginError

    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:  # pragma: no cover -- só em ambiente sem playwright instalado
        PlaywrightError = ()  # type: ignore[assignment]

    if isinstance(exc, (GSUSLoginError, GSUSCensusError)) or (PlaywrightError and isinstance(exc, PlaywrightError)):
        return OUTCOME_FALHA_GSUS, (
            "Não foi possível concluir o acesso ao GSUS (login ou obtenção da lista de "
            "pacientes) -- o GSUS não respondeu a tempo. Isso é uma instabilidade externa "
            "do sistema do hospital, não um defeito deste programa. A próxima execução "
            "tenta de novo automaticamente."
        )
    if isinstance(exc, RuntimeError) and "Credencial GSUS não configurada" in str(exc):
        return OUTCOME_FALHA_INESPERADA, (
            "A credencial de acesso ao GSUS não está configurada nesta máquina -- abra "
            "as Configurações e informe usuário e senha novamente."
        )
    if isinstance(exc, (sqlite3.Error, OSError)):
        return OUTCOME_FALHA_INESPERADA, (
            "Falha técnica local (banco de dados ou disco desta máquina), não relacionada "
            "ao GSUS. Pode ser espaço em disco insuficiente ou permissão de arquivo -- se "
            "persistir, vale acionar o suporte técnico."
        )
    return OUTCOME_FALHA_INESPERADA, (
        "A execução parou por um motivo que o sistema não reconhece como instabilidade já "
        "conhecida do GSUS. Vale acionar o suporte técnico informando o horário desta "
        "falha, pra investigar o log."
    )


def _benign_suffix(no_admission: int, awaiting_notes: int) -> str:
    """Complemento do resumo com as categorias que NÃO são falha: sem
    internação atual (DEC-054) e admitidos há pouco ainda sem evolução
    acessível (RESIL-015)."""
    parts = []
    if no_admission:
        parts.append(f"{no_admission} sem internação atual (provável alta recente)")
    if awaiting_notes:
        parts.append(
            f"{awaiting_notes} admitido(s) há pouco ainda sem evolução acessível "
            "(entram na próxima atualização)"
        )
    return (", " + ", ".join(parts) + ".") if parts else "."


def classify_completed_run(
    found: int,
    completed: int,
    no_admission: int,
    patient_errors: list[str],
    census_complete: bool,
    awaiting_notes: int = 0,
) -> tuple[str, str, bool]:
    """Classifica uma execução que RODOU até o fim (Fase 1 completa, run
    persistida) -- `patient_errors` é a lista de `last_error` de cada
    paciente que terminou em ERROR nesta run. Devolve (outcome, resumo em
    linguagem simples, precisa_atencao)."""
    if not census_complete:
        return (
            OUTCOME_SUCESSO_PARCIAL_GSUS,
            f"O GSUS não confirmou a lista completa de internados desta vez -- só "
            f"{found} prontuário(s) foram alcançados nesta execução. Nenhum paciente foi "
            "marcado como tendo recebido alta por causa disso (o sistema já protege contra "
            "isso); a próxima execução tenta de novo sozinha. Instabilidade conhecida do "
            "GSUS, não é defeito deste programa.",
            False,
        )

    if found == 0:
        return (
            OUTCOME_SUCESSO,
            "Nenhum paciente internado encontrado no setor configurado nesta execução.",
            False,
        )

    failed = len(patient_errors)
    if failed == 0:
        summary = f"Execução concluída sem erros -- {completed} de {found} paciente(s) processado(s)"
        summary += _benign_suffix(no_admission, awaiting_notes)
        return OUTCOME_SUCESSO, summary, False

    unknown_errors = [e for e in patient_errors if not is_known_gsus_error(e)]
    high_error_rate = (failed / found) > PARTIAL_FAILURE_THRESHOLD

    if unknown_errors:
        return (
            OUTCOME_FALHA_INESPERADA,
            f"Execução concluída -- {completed} de {found} paciente(s) processado(s), {failed} "
            f"com falha. {len(unknown_errors)} dessa(s) falha(s) têm um tipo de erro que o "
            "sistema NÃO reconhece como instabilidade já conhecida do GSUS -- vale reportar "
            "ao suporte técnico com o horário desta execução para investigação.",
            True,
        )

    if high_error_rate:
        return (
            OUTCOME_SUCESSO_PARCIAL_GSUS,
            f"Execução concluída, mas com taxa de falha alta -- só {completed} de {found} "
            f"paciente(s) processado(s) ({failed} falha(s), todas de categorias já conhecidas "
            "de instabilidade do GSUS). Não é defeito deste programa, mas considere uma "
            "conferência manual desta unidade hoje, já que a cobertura ficou bem abaixo do "
            "normal.",
            True,
        )

    return (
        OUTCOME_SUCESSO_PARCIAL_GSUS,
        f"Execução concluída -- {completed} de {found} paciente(s) processado(s), {failed} "
        "com falha pontual (categorias já conhecidas de instabilidade do GSUS -- serão "
        "retomados automaticamente na próxima execução).",
        False,
    )


def describe_cancelled_run(found: int, completed: int) -> tuple[str, str, bool]:
    """UI-006: execução interrompida pelo próprio auditor (botão "Encerrar").
    Não é falha do GSUS nem deste programa -- só registra, em linguagem
    simples, até onde a execução chegou. Nunca pede atenção: cada paciente
    já processado ficou salvo e a próxima atualização retoma o restante."""
    return (
        OUTCOME_CANCELADA,
        f"Execução encerrada pelo usuário -- {completed} de {found} paciente(s) processado(s) "
        "antes da interrupção. Nada foi perdido: o que já foi processado está salvo e a "
        "próxima atualização (manual ou agendada) continua de onde parou.",
        False,
    )


def describe_gsus_unresponsive_run(
    found: int, completed: int, consecutive_failures: int, relogin_attempted: bool,
) -> tuple[str, str, bool]:
    """DEC-117: o orchestrator interrompeu a Fase 1 porque a tela de busca de
    prontuário do GSUS não respondeu por `consecutive_failures` pacientes
    seguidos (cada um esgotando as tentativas), mesmo depois de refazer o
    login. Instabilidade do GSUS, não defeito deste programa -- mas pede
    atenção porque a cobertura do dia ficou incompleta."""
    relogin_text = (
        "mesmo depois de refazer o login automaticamente" if relogin_attempted
        else "sem conseguir refazer o login automaticamente"
    )
    return (
        OUTCOME_FALHA_GSUS,
        f"O GSUS parou de responder à busca de prontuário por {consecutive_failures} paciente(s) "
        f"seguido(s), {relogin_text}. A execução foi interrompida para não gastar horas em vão: "
        f"{completed} de {found} paciente(s) foram processados e estão salvos; os demais ficam para a "
        "próxima atualização. Instabilidade do sistema do hospital, não um defeito deste programa -- "
        "tente de novo mais tarde.",
        True,
    )


def compute_staleness_warning(last_diagnostic_at: str | None, schedule_time: str, now: datetime) -> str | None:
    """Compara o horário do último diagnóstico registrado contra o horário
    agendado -- devolve um aviso em linguagem simples se a execução
    agendada parece não ter rodado (ex.: máquina desligada/hibernando de
    noite -- limitação já conhecida do agendador do Windows, RESIL-003,
    nunca tratado como defeito deste programa), ou `None` se está dentro do
    esperado. `last_diagnostic_at is None` (banco novo, nenhum diagnóstico
    ainda) não gera aviso -- evita alarme falso logo após a instalação."""
    if last_diagnostic_at is None:
        return None
    try:
        last = datetime.fromisoformat(last_diagnostic_at)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    hours_since = (now - last).total_seconds() / 3600
    if hours_since <= STALENESS_THRESHOLD_HOURS:
        return None
    return (
        f"Nenhuma execução registrada nas últimas {round(hours_since)}h (a última foi às "
        f"{last.strftime('%d/%m %H:%M')}). Se o computador estava desligado ou hibernando no "
        f"horário programado ({schedule_time}), a atualização simplesmente não roda -- "
        "limitação conhecida do agendamento do Windows, não um defeito deste programa. "
        "Considere fazer uma auditoria manual hoje."
    )
