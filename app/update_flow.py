"""Lógica de uma execução de "Atualizar agora" (UI-001) -- extraída de
`app/ui/main_window.py` (achado real, SCHEDULE-001, 2026-08-26): a tarefa
agendada só abria a janela do app, nunca disparava a atualização sozinha
(não existe humano pra clicar o botão numa execução de madrugada). Em vez
de duplicar o pipeline real (censo -> LLM -> relatório) num segundo lugar
pro modo automático, essa função vira a ÚNICA fonte de verdade -- tanto o
clique manual (`MainWindow._run_update_worker`, com fila de progresso pra
Tk) quanto `app/main.py::run_auto_update` (sem interface nenhuma) chamam
exatamente a mesma coisa.
"""
from __future__ import annotations

import logging
import msvcrt
import threading
from pathlib import Path
from typing import Callable

from app import config
from app.orchestrator import RunResult

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

LOCK_FILENAME = "update.lock"


class UpdateAlreadyRunningError(Exception):
    """Já existe uma atualização em andamento nesta máquina (achado real,
    DEC-086, 2026-08-28): a tarefa agendada disparou em cima de um "Atualizar
    agora" manual ainda em andamento -- as duas tentaram usar o mesmo GSUS,
    a mesma porta do LLM e o mesmo banco ao mesmo tempo, sem nenhuma
    exclusão mútua. A segunda instância ficou travada por horas, sem nunca
    falhar nem terminar sozinha."""


class _InstanceLock:
    """Lock de arquivo via API nativa do Windows (`msvcrt.locking`) -- sem
    dependência nova (mesmo princípio do DEC-002: se a stdlib resolve, não
    adiciona pacote). Diferente de um lock por "arquivo existe" (que fica
    preso pra sempre se o processo cair sem limpar), esse lock é do
    PRÓPRIO SISTEMA OPERACIONAL sobre o handle do arquivo -- o Windows
    libera sozinho se o processo travar/crashar/for encerrado à força,
    nunca deixando um lock "fantasma" que exigiria limpeza manual."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        try:
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self._file.close()
            self._file = None
            return False

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._file.close()
        self._file = None


def run_update(
    app_config: config.AppConfig,
    report_path: Path,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    """Roda uma atualização completa de verdade (censo -> regras -> IA ->
    relatório) contra o GSUS e o LLM reais. Levanta a exceção original em
    caso de falha -- quem chama decide como apresentar (UI-003's
    `friendly_message` pra tela, log cru no modo automático).

    Levanta `UpdateAlreadyRunningError` de cara se outra atualização (clique
    manual OU execução automática) já estiver em andamento nesta máquina --
    nunca deixa duas tentarem usar o mesmo GSUS/LLM/banco ao mesmo tempo
    (DEC-086).

    `cancel_event` (UI-006, botão "Encerrar"): quando setado, a execução para
    de forma cooperativa entre um paciente e outro (nunca no meio de uma
    gravação), derruba a análise por IA em andamento e devolve um `RunResult`
    com `status="CANCELLED"` em vez de levantar. A execução agendada
    (`--auto-update`) não passa evento nenhum."""
    lock = _InstanceLock(config.get_app_data_dir() / LOCK_FILENAME)
    if not lock.acquire():
        raise UpdateAlreadyRunningError(
            "Já existe uma atualização em andamento nesta máquina (clique manual ou tarefa agendada)."
        )
    try:
        return _run_update_locked(app_config, report_path, progress, cancel_event)
    finally:
        lock.release()


def _run_update_locked(
    app_config: config.AppConfig,
    report_path: Path,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    """Corpo real de `run_update` -- só roda com o lock de instância única
    já garantido pelo chamador."""
    # Imports tardios: evitam custo de import do Playwright/orchestrator
    # quando a tela abre e ainda não interagiu com "Atualizar agora".
    from app.analysis import run_diagnosis
    from app.analysis.llm import LLMStartupError, LocalLLM
    from app.analysis.model_downloader import ModelDownloadError, ensure_model_downloaded
    from app.gsus.adapter import GSUSAdapter
    from app.gsus.client import GSUSClient
    from app.orchestrator import run_once
    from app.security import credentials
    from app.storage import database
    from app.storage.repository import Repository

    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    # DIAG-001 (2026-09-03): `repo` precisa existir ANTES da checagem de
    # credencial pra conseguir registrar um diagnóstico mesmo nesse caso --
    # achado da auditoria de catálogo de falhas (DEC-111): esse é um dos
    # poucos caminhos que roda ANTES de `run_once` sequer começar (login
    # falho/censo falho por completo já ficam cobertos DENTRO de
    # `run_once`, ver orchestrator.py).
    conn = database.init_db(config.get_db_path())
    repo = Repository(conn)

    cred = credentials.get_credential("gsus")
    if cred is None:
        exc = RuntimeError("Credencial GSUS não configurada.")
        outcome, summary = run_diagnosis.classify_top_level_exception(exc)
        repo.save_run_diagnostic(None, outcome, summary, needs_attention=True)
        exc._gsus_diagnostic_written = True
        conn.close()
        raise exc
    username, password = cred

    # Inicia o LLM ANTES do GSUS -- carregar o modelo pode levar minutos
    # nesta máquina (hardware fraco, ver CURRENT_STATE.md); abrir a sessão
    # GSUS só depois evita ela ficar ociosa esperando o modelo (risco de
    # expirar por inatividade -- DEC-058). Se o LLM não iniciar (não
    # configurado, caminho errado, download falhou, etc.), a atualização
    # NÃO é abortada -- continua só com as regras determinísticas, mesma
    # semântica que `friendly_message` já previa pra LLMStartupError.
    # resolve_app_path (DEC-069): caminho relativo default nunca deve ser
    # resolvido contra o diretório de trabalho do processo -- só contra a
    # pasta de instalação de verdade.
    model_path = config.resolve_app_path(app_config.model_path)
    llm: LocalLLM | None
    try:
        # BUILD-001/DEC-072: o instalador não empacota o modelo (~4,92GB)
        # -- baixa sozinho na primeira vez que precisar dele. Idempotente,
        # então toda execução seguinte passa direto.
        ensure_model_downloaded(model_path, progress=report)
        llm = LocalLLM(
            model_path=model_path,
            server_path=config.resolve_app_path(app_config.llm_server_path),
            host=app_config.llm_host,
            port=app_config.llm_port,
            server_log_path=config.get_app_data_dir() / "logs" / "llama-server.log",
        )
        llm.start()
        report("Modelo local pronto. Acessando GSUS...")
    except ModelDownloadError:
        logger.exception("Download do modelo de IA falhou -- atualização continua só com regras determinísticas")
        llm = None
    except LLMStartupError:
        logger.exception("LLM local não iniciou -- atualização continua só com regras determinísticas")
        llm = None

    # Achado real (auditoria de resiliência 2026-08-28): `conn.close()`
    # ficava DEPOIS do try/finally que protege o LLM -- se `run_once`
    # levantasse (censo/login sem retry, erro inesperado), a conexão SQLite
    # nunca era fechada, vazando um handle a cada execução que falhasse.
    try:
        # UI-006: "Navegador visível" (padrão, único modo comprovado contra o
        # GSUS real -- DEC-077) x "Segundo plano" (headless), escolhido na tela
        # principal e persistido em config.json -- vale também pra execução
        # agendada, que passa por este mesmo caminho.
        with GSUSClient(app_config.gsus_base_url, headless=not app_config.browser_visible) as client:
            client.goto()
            adapter = GSUSAdapter(
                client.page, username, password, app_config.unit,
                base_url=client.base_url, max_days_per_patient=app_config.max_days_per_patient,
            )
            result = run_once(
                repo,
                adapter,
                adapter,
                app_config.unit,
                report_path,
                llm=llm,
                progress=report,
                raw_notes_retention_days=app_config.raw_notes_retention_days,
                cancel_event=cancel_event,
            )
    except Exception as exc:
        # DIAG-001: cobre os poucos caminhos que ainda rodam FORA de
        # `run_once` (abertura do navegador/`client.goto()`, achado da
        # auditoria de catálogo de falhas DEC-111) -- `run_once` já
        # registra o diagnóstico sozinho pra tudo que acontece dentro dele
        # (login/censo inclusos, ver adapter.py::_ensure_login) e marca a
        # exceção com `_gsus_diagnostic_written` pra este bloco não
        # duplicar o registro.
        if not getattr(exc, "_gsus_diagnostic_written", False):
            try:
                outcome, summary = run_diagnosis.classify_top_level_exception(exc)
                repo.save_run_diagnostic(None, outcome, summary, needs_attention=True)
            except Exception:
                logger.exception("Falha ao registrar diagnóstico da execução com erro (fora de run_once)")
        raise
    finally:
        if llm is not None:
            llm.stop()
        conn.close()

    return result
