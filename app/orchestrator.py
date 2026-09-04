"""Orquestra uma execução completa (prompt mestre seção 37):

censo -> fila -> extração -> normalização -> hash -> regras -> LLM ->
persistência -> relatório.

Censo e extração de evoluções são recebidos via Protocol (duck typing) para
que este módulo funcione tanto com o GSUS real (app/gsus, parcialmente
BLOCKED_GSUS -- ver DECISIONS.md) quanto com fontes sintéticas em teste
(tests/e2e/test_pipeline.py). Falha em um paciente nunca derruba o lote
(RF-12) -- é isolada, registrada e reportada no relatório final.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from app.analysis import run_diagnosis
from app.analysis.llm import LLMAnalysisError
from app.analysis.priority import compute_priority, hours_elapsed_since
from app.analysis.rules import StructuredNote, apply_all_rules, most_recent_note
from app.analysis.taxonomy import CATEGORY_INTERCONSULTA, CATEGORY_PROCEDIMENTO_CIRURGIA, default_origin
from app.extraction.hashing import compute_note_hash
from app.extraction.normalizer import normalize_datetime, normalize_text
from app.extraction.parser import parse_note_blocks
from app.gsus.census import GSUSCensusIncompleteError
from app.gsus.records import GSUSNoCurrentAdmissionDays, GSUSSearchUnresponsiveError
from app.models import Note, Patient
from app.reports import dashboard_metrics
from app.reports.html_report import generate_report
from app.security import pseudonym
from app.storage import database
from app.storage.repository import Repository

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

# Achado real 2026-08-25 (DEC-066): paciente com muito backlog acumulado (1ª
# análise depois de muito tempo sem rodar, ou admissão bem longa) manda TODO
# o histórico pro LLM numa chamada só -- estourava tempo (timeout mesmo com
# 480s) e, num caso, a própria janela de contexto do modelo (HTTP 400
# imediato), no hardware fraco confirmado do alvo. Decisão do usuário: nunca
# mandar mais que as últimas 2 semanas de evolução pro LLM -- RULES-001
# continua vendo o histórico completo (não tem esse custo).
LLM_LOOKBACK_DAYS = 14

# Achado real (RESIL-006, 2026-08-29): mesmo dentro da janela de 14 dias,
# paciente de internação longa/densa pode ter volume de evolução grande o
# bastante pra exceder `LocalLLM.ctx_size` (DEC-092) -- llama-server rejeita
# o prompt de cara (HTTP 400), sempre nas 3 tentativas (não é uma falha de
# CONTEÚDO que o reparo do DEC-089 resolve -- é tamanho). Confirmado ao vivo
# no backfill do LLM-005: pacientes com ~8-9K caracteres de nota sempre
# couberam (mesmo com reparo, que soma a saída anterior por cima); a partir
# de ~15K, sempre estouraram já na 1ª tentativa. 9000 fica no topo da faixa
# confirmada boa, com margem pro system prompt (~5,1K chars) + resposta +
# uma eventual tentativa de reparo, sem precisar aumentar `--ctx-size` (mais
# RAM, risco de repetir a falha de alocação do DEC-092 numa máquina com
# pouca margem).
MAX_NOTES_CHARS_FOR_LLM = 9000

# Achado real (auditoria de resiliência 2026-08-28, DEC-087): um
# `llama-server` com slot de geração preso responde `/health` normalmente
# (processo vivo) mas nunca mais processa nada -- sem isto, cada paciente
# restante da Fase 2 queimaria seu timeout inteiro (até 90min, DEC-070)
# contra um servidor visivelmente morto. 2 = uma checagem de saúde ruim pode
# ser um blip transitório (não interrompe na primeira); duas seguidas é
# tratado como indisponibilidade real.
MAX_CONSECUTIVE_UNHEALTHY_CHECKS = 2

# DEC-117 (achado real 2026-09-04): numa execução de 183 pacientes, a tela
# de busca de prontuário do GSUS parou de responder às 12:00 e ficou assim
# por 2 horas -- 44 pacientes seguidos esgotaram as 5 tentativas de 30s
# (2,5 min cada, 110 min no total pra 3 sucessos), com a sessão ainda
# "logada" (nenhum marcador de sessão expirada/aba crashada, DEC-083/084,
# apareceu -- então `_ensure_login` nunca refez o login). Duas alavancas,
# nesta ordem: depois de RELOGIN_AFTER pacientes seguidos com
# `GSUSSearchUnresponsiveError`, pede ao adapter uma sessão nova
# (`reset_session`, uma vez por sequência); persistindo até ABORT_AFTER,
# interrompe a Fase 1 com diagnóstico claro em vez de moer a fila inteira.
# Qualquer paciente que passe do menu (sucesso, sem internação, ou outro
# erro) zera a contagem -- é sinal de que o GSUS voltou a responder.
# 3 e 6 = ~7,5 min e ~15 min de espera no pior caso, contra 110 min reais.
GSUS_UNRESPONSIVE_RELOGIN_AFTER = 3
GSUS_UNRESPONSIVE_ABORT_AFTER = 6


class CensusSource(Protocol):
    def get_census(self) -> list[Patient]: ...


class RecordSource(Protocol):
    def get_raw_notes_text(
        self, patient: Patient, known_days: frozenset[str] | set[str] = frozenset()
    ) -> str: ...


class AnalysisEngine(Protocol):
    def analyze_patient(
        self,
        previous_state: dict | None,
        new_notes: list[StructuredNote],
        active_pending_items: list[dict] | None = None,
    ) -> dict: ...


@dataclass
class RunResult:
    run_id: str
    counts: dict
    report_path: Path
    # UI-006 (2026-09-04): "COMPLETED" (rodou até o fim), "CANCELLED"
    # (encerrada pelo usuário no meio, via `cancel_event`) ou "ABORTED_GSUS"
    # (DEC-117: Fase 1 interrompida pelo disjuntor de GSUS sem responder).
    # Nunca "FAILED" -- falha continua sendo exceção, não resultado.
    status: str = "COMPLETED"


def run_once(
    repo: Repository,
    census_source: CensusSource,
    record_source: RecordSource,
    unit: str,
    report_output_path: Path,
    llm: AnalysisEngine | None = None,
    progress: ProgressCallback | None = None,
    raw_notes_retention_days: int | None = None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    def report(message: str) -> None:
        logger.info(message)
        if progress:
            progress(message)

    # UI-006 (2026-09-04, botão "Encerrar"): cancelamento COOPERATIVO --
    # checado só em pontos seguros (antes do censo e entre um paciente e
    # outro na Fase 1; entre um item e outro na Fase 2). Nunca interrompe uma
    # gravação no meio: cada paciente ou termina inteiro ou nem começa, e o
    # que sobrou continua PENDING pra próxima execução retomar sozinha.
    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def empty_counts() -> dict:
        return {"found": 0, "completed": 0, "failed": 0, "no_admission": 0}

    # Achado real (auditoria de resiliência 2026-08-28): antes desta trava,
    # qualquer exceção não prevista escapando desta função (falha de
    # login/censo sem retry, erro inesperado na fila, etc.) deixava a run
    # com status RUNNING pra sempre -- ninguém nunca escrevia FAILED -- e o
    # relatório do dia anterior ficava parado, sem nenhum aviso de que a
    # auditoria de hoje não rodou. `run_id` só passa a existir a partir de
    # `repo.start_run()` (abaixo); continuar `None` cobre uma falha ainda
    # mais cedo (login/censo), quando não há run nenhuma pra marcar FAILED.
    run_id: str | None = None
    try:
        report("Preparando...")
        repo.resume_incomplete_runs()

        if cancelled():
            # Encerrado enquanto ainda carregava o modelo/abria o navegador --
            # não há run nem censo; sai antes de tocar o GSUS.
            report("Encerrado pelo usuário antes de acessar o GSUS.")
            return RunResult(
                run_id="", counts=empty_counts(), report_path=report_output_path, status="CANCELLED",
            )

        report("Acessando GSUS...")
        report("Obtendo pacientes...")
        try:
            patients = census_source.get_census()
            census_complete = True
        except GSUSCensusIncompleteError as exc:
            # Achado real DEC-080: paginação pode desistir no meio (GSUS
            # instável) e devolver só uma fatia do censo real. Processamos
            # essa fatia normalmente (melhor que nada), mas NUNCA marcamos
            # quem ficou de fora como inativo/alta -- isso exigiria um censo
            # genuinamente completo. `mark_patients_inactive_not_in` fica
            # pulado nesta run; uma próxima execução com censo completo
            # corrige sozinha quem continua realmente internado
            # (`upsert_patient` reativa).
            patients = exc.patients
            census_complete = False
            logger.warning(
                "Censo incompleto nesta execução (%d paciente(s) coletado(s)) -- "
                "pulando mark_patients_inactive_not_in pra não marcar paciente real como alta.",
                len(patients),
            )
            report(
                f"Aviso: paginação do censo foi interrompida (GSUS instável) -- "
                f"{len(patients)} paciente(s) coletado(s) nesta execução. Pacientes fora "
                "dessa lista NÃO serão marcados como inativos desta vez."
            )

        patient_ids = [repo.upsert_patient(p) for p in patients]
        if census_complete:
            repo.mark_patients_inactive_not_in(patient_ids)
        patients_by_id = dict(zip(patient_ids, patients))

        # RETENTION-001/DEC-068: limpeza leve a cada execução -- texto bruto
        # de evolução de paciente com alta há muito tempo não precisa mais
        # ficar no banco (RF-17 só precisa do resumo estruturado, que nunca é
        # apagado aqui). `None` = retenção desabilitada (comportamento
        # antigo, usado por quem não passa o parâmetro -- ex. testes que não
        # têm opinião sobre isso).
        if raw_notes_retention_days is not None:
            purged = repo.purge_old_notes_for_discharged_patients(raw_notes_retention_days)
            if purged:
                logger.info("Retenção: %d evolução(ões) de paciente(s) com alta antiga apagada(s)", purged)

        run_id = repo.start_run()
        repo.enqueue_patients(run_id, patient_ids)

        total = len(patient_ids)
        processed = 0
        # DEC-109: até aqui (DEC-071), coleta+regras (rápido, mas depende do
        # GSUS responder) e análise por IA (lento, CPU/GPU local -- DEC-070)
        # rodavam em duas fases estritamente sequenciais -- a IA só começava
        # depois que TODO paciente já tivesse passado pela Fase 1, deixando a
        # IA ociosa o tempo todo em que a Fase 1 esperava o GSUS. Achado real
        # 2026-09-02: numa execução de 182 pacientes isso significava só 16
        # entrando na Fase 2 depois de mais de 1h de Fase 1. Agora a Fase 2
        # roda numa thread dedicada (`_llm_phase2_worker`), consumindo uma
        # fila conforme a Fase 1 vai liberando cada paciente -- as duas fases
        # se sobrepõem de verdade, sem exigir que a IA espere o lote inteiro.
        # `queued_patient_ids` substitui a antiga lista `llm_tasks`
        # (guardava tupla completa) só pra saber quem já foi enfileirado
        # nesta run, usado no passo de "backlog" logo depois do laço.
        llm_queue: queue.Queue = queue.Queue()
        queued_patient_ids: set[str] = set()
        llm_worker_thread: threading.Thread | None = None
        # DEC-109 (revisão adversarial, 2026-09-02): `worker_failed` é o único
        # jeito de `run_once` saber que a thread da Fase 2 morreu de um jeito
        # inesperado (não pelo caminho normal do disjuntor de saúde, que já
        # tem seu próprio `report()`) -- sem isto, achado real da revisão: um
        # erro na conexão/loop da thread desaparecia num traceback de stderr
        # que o `logging` configurado (`app/main.py`) nunca via, e a run
        # inteira era reportada como concluída com sucesso mesmo com a Fase 2
        # inteira pulada em silêncio.
        worker_failed = threading.Event()
        # `backlog_tasks` precisa existir ANTES do `try` abaixo -- se algo
        # explodir cedo (ex.: no laço da Fase 1) o `finally` ainda referencia
        # esta variável pra decidir o que enfileirar antes de encerrar a
        # thread.
        backlog_tasks: list[tuple[str, list[StructuredNote], bool]] = []
        # UI-006: vira True quando o usuário clica "Encerrar" no meio da Fase 1.
        run_cancelled = False
        # DEC-117: disjuntor de GSUS sem responder (ver constantes no topo).
        gsus_unresponsive = False
        consecutive_unresponsive = 0
        relogin_attempted = False
        if llm is not None:
            llm_worker_thread = threading.Thread(
                target=_llm_phase2_worker,
                args=(
                    _get_db_path(repo), run_id, unit, report_output_path, llm, llm_queue, report, worker_failed,
                    cancel_event,
                ),
                name="llm-phase2-worker",
                daemon=True,
            )
            llm_worker_thread.start()
        # DEC-109 (revisão adversarial, 2026-09-02): TUDO daqui até o
        # `finally` precisa ficar dentro deste `try` -- achado real e
        # reproduzido pela revisão: antes desta correção, uma exceção em
        # QUALQUER ponto entre o início da thread da Fase 2 e o envio do
        # sentinela (`finish_run`, o laço de backlog abaixo, etc.) pulava
        # direto pro `except` mais externo (mais abaixo) sem nunca enfileirar
        # `_LLM_QUEUE_DONE` nem chamar `.join()` -- a thread ficava PRA
        # SEMPRE bloqueada em `llm_queue.get()`, vazando ela e a conexão
        # SQLite dela pelo resto da vida do processo (grave de verdade no
        # botão "Atualizar agora" da UI, processo de vida longa -- clique
        # seguinte inicia mais uma thread zumbi, cada vez mais conexões
        # concorrentes escrevendo no mesmo banco). O `finally` garante o
        # sentinela+join sempre rodar, não importa por onde a função saia.
        try:
            while True:
                if cancelled():
                    # UI-006: o paciente anterior já terminou inteiro (gravado);
                    # o próximo nem começa -- continua PENDING pra próxima execução.
                    run_cancelled = True
                    report("Encerrando: o paciente atual já foi concluído, parando a coleta...")
                    break
                # Achado real (auditoria de certificação pré-entrega, 2026-09-01,
                # RESIL-011/DEC-105): confirmado no banco de PRODUÇÃO real -- várias runs
                # tinham dezenas/centenas de pacientes presos em PENDING sem
                # NENHUM `ERROR` correspondente, porque `repo.next_pending`
                # (dequeue) rodava fora de qualquer try/except: uma falha de
                # escrita SQLite aqui (lock momentâneo, disco lento) abortava a
                # run inteira de uma vez, não isolava por paciente (RF-12). Se
                # o PRÓPRIO dequeue falhar, não há como saber quem seria o
                # próximo paciente pra marcar erro -- para o loop com segurança
                # (os pacientes ainda PENDING são retomados no próximo censo,
                # não ficam perdidos, só adiados).
                try:
                    patient_id = repo.next_pending(run_id)
                except Exception:
                    logger.exception("Falha ao buscar próximo paciente da fila -- interrompendo Fase 1 desta run")
                    break
                if patient_id is None:
                    break
                processed += 1
                # Mesma classe de achado: `report()`/`mark_processing()` também
                # rodavam fora do try/except por paciente logo abaixo -- uma
                # falha aqui (callback de progresso quebrado, escrita SQLite)
                # tinha o mesmo efeito de abortar todo mundo de uma vez. Isolado
                # agora: falha aqui marca ESTE paciente como erro e segue pro
                # próximo, em vez de abortar a fila inteira.
                try:
                    report(f"Processando paciente {processed} de {total} (regras)...")
                    repo.mark_processing(run_id, patient_id)
                except Exception as exc:
                    logger.exception(
                        "Falha ao registrar início do processamento do paciente %s", pseudonym.for_log(patient_id),
                    )
                    try:
                        repo.mark_error(run_id, patient_id, _safe_error_text(exc))
                    except Exception:
                        logger.exception(
                            "Falha também ao marcar erro do paciente %s -- pulando", pseudonym.for_log(patient_id),
                        )
                    continue
                try:
                    llm_input = _process_patient_rules(repo, patients_by_id[patient_id], patient_id, record_source)
                    repo.mark_done(run_id, patient_id)
                    consecutive_unresponsive = 0
                    relogin_attempted = False
                    if llm is not None and llm_input is not None:
                        llm_notes, window_limited = llm_input
                        queued_patient_ids.add(patient_id)
                        llm_queue.put((patient_id, llm_notes, window_limited))
                except GSUSNoCurrentAdmissionDays:
                    # Não é falha técnica -- paciente sem internação atual na
                    # tela (provável alta recente, ainda no censo/fila do dia).
                    # Categoria separada no relatório, decisão do usuário -- ver
                    # DEC-054.
                    repo.mark_no_admission(run_id, patient_id)
                    consecutive_unresponsive = 0
                    relogin_attempted = False
                except GSUSSearchUnresponsiveError as exc:
                    # DEC-117: a busca nem chegou a ser disparada -- causa é o
                    # GSUS/sessão, não o paciente. Continua isolado por paciente
                    # (RF-12), mas alimenta o disjuntor.
                    logger.error(
                        "Tela de busca do GSUS não respondeu para o paciente %s (%d seguido(s))",
                        pseudonym.for_log(patient_id), consecutive_unresponsive + 1,
                    )
                    repo.mark_error(run_id, patient_id, _safe_error_text(exc))
                    consecutive_unresponsive += 1
                    if consecutive_unresponsive >= GSUS_UNRESPONSIVE_ABORT_AFTER:
                        gsus_unresponsive = True
                        report(
                            f"Interrompendo: o GSUS não responde à busca de prontuário há "
                            f"{consecutive_unresponsive} pacientes seguidos -- o restante fica para a próxima atualização."
                        )
                        break
                    if consecutive_unresponsive >= GSUS_UNRESPONSIVE_RELOGIN_AFTER and not relogin_attempted:
                        relogin_attempted = True
                        reset_session = getattr(record_source, "reset_session", None)
                        if reset_session is not None:
                            report(
                                f"GSUS sem responder há {consecutive_unresponsive} pacientes seguidos -- "
                                "refazendo o login antes de continuar..."
                            )
                            try:
                                reset_session()
                            except Exception:
                                logger.exception("Falha ao refazer o login do GSUS após falhas seguidas na busca")
                except Exception as exc:  # isolamento de falha por paciente -- RF-12
                    logger.exception("Falha ao processar paciente %s", pseudonym.for_log(patient_id))
                    repo.mark_error(run_id, patient_id, _safe_error_text(exc))
                    # Outro erro = o GSUS respondeu de algum jeito; zera o disjuntor.
                    consecutive_unresponsive = 0
                    relogin_attempted = False

            # DEC-117: interrompido pelo disjuntor conta como FAILED no banco
            # (a Fase 1 não terminou), mas devolve resultado em vez de exceção.
            stopped_early = run_cancelled or gsus_unresponsive
            final_status = "CANCELLED" if run_cancelled else ("FAILED" if gsus_unresponsive else "COMPLETED")
            try:
                repo.finish_run(run_id, final_status)
            except Exception:
                # DEC-109 (revisão adversarial): achado real -- esta chamada
                # ficava sem proteção, diferente de tudo ao redor. Antes da
                # Fase 2 rodar em paralelo, não havia OUTRO escritor tocando o
                # banco neste instante; agora a thread da Fase 2 pode estar
                # gravando análise ao mesmo tempo, e o timeout padrão do
                # sqlite3 (5s) podia estourar sob contenção momentânea --
                # abortando uma run cuja Fase 1 inteira já persistiu com
                # sucesso, só por causa de uma trava passageira nesta última
                # gravação de status.
                logger.exception(
                    "Falha ao marcar run como COMPLETED -- Fase 1 já persistiu tudo, seguindo mesmo assim"
                )
            if stopped_early:
                # UI-006/DEC-117: encerrado pelo usuário ou pelo disjuntor de GSUS
                # -- NÃO regrava o relatório do dia com uma fatia parcial (o
                # anterior, completo, continua valendo), não enfileira backlog
                # pra IA (abaixo) e derruba na hora a análise por IA que
                # estiver em andamento: sem isto, o botão só responderia depois
                # de a análise atual terminar (até minutos em CPU). A thread da
                # Fase 2 trata a falha da chamada como qualquer outra (isolada
                # por paciente) e, vendo o evento/sentinela, descarta o resto da
                # fila -- nada fica marcado como analisado sem ter terminado.
                if llm is not None and hasattr(llm, "stop"):
                    try:
                        llm.stop()
                    except Exception:
                        logger.exception("Falha ao parar o LLM após encerramento pelo usuário")
            else:
                report("Gerando relatório (regras)...")
                try:
                    generate_report(repo, run_id, unit, report_output_path)
                except Exception:
                    # Achado real (auditoria de resiliência): esta chamada estava
                    # fora de qualquer try/except -- uma falha de ESCRITA aqui
                    # (disco cheio, permissão, etc.) abortava a execução inteira
                    # ANTES da Fase 2 (IA) sequer começar, mesmo com a Fase 1 já
                    # 100% concluída e persistida no banco.
                    logger.exception("Falha ao gerar relatório (fase de regras) -- Fase 2 (IA) segue mesmo assim")

            # LLM-004 / DEC-090 (achado confirmado pela auditoria de
            # resiliência): o sinal de "nota nova" é consumido e destruído em
            # `_process_patient_rules` (hash de `repo.add_note`) antes da Fase 2
            # sequer existir -- um paciente cuja análise falhou (ou cuja vez
            # nunca chegou numa Fase 2 interrompida em execução anterior) NUNCA
            # mais entra sozinho na fila só por causa disso; só uma evolução
            # genuinamente nova o resgataria, o que pode levar dias ou nunca
            # acontecer. Recoloca esses pacientes na fila TODA execução, usando
            # o HISTÓRICO COMPLETO já extraído (não uma nota isolada) já que
            # nunca tiveram nenhuma análise bem-sucedida pra incorporar.
            #
            # DEC-109: diferente dos pacientes "frescos" (enfileirados durante a
            # Fase 1, acima, na ordem em que cada um termina -- já consumidos ou
            # em consumo pela thread da Fase 2 a esta altura), o backlog só pode
            # ser calculado DEPOIS que a Fase 1 termina (`already_queued` precisa
            # do conjunto final). Continua ordenado do menor pro maior volume de
            # nota (DEC-085) -- preserva a intenção original pra esta fatia da
            # fila, mesmo que os itens frescos não sigam mais essa ordem.
            #
            # Acréscimo deliberadamente SEM try/except (revisão adversarial,
            # DEC-109): uma falha aqui continua propagando pro `except` mais
            # externo, marcando a run FAILED -- contrato já testado
            # (`test_pipeline_marks_run_as_failed_instead_of_stuck_running_on_uncaught_error`)
            # e mantido de propósito. O que muda é só o `finally` logo
            # abaixo: ele SEMPRE roda antes da exceção continuar subindo,
            # então o sentinela/join da thread da Fase 2 nunca deixam de
            # acontecer só porque o backlog explodiu.
            # UI-006/DEC-117: sem backlog quando interrompido antes do fim.
            for pending_patient_id in ([] if stopped_early else repo.get_active_patients_pending_ai_analysis()):
                if pending_patient_id in queued_patient_ids:
                    continue
                backlog_notes = _reconstruct_all_notes(repo, pending_patient_id)
                if not backlog_notes:
                    continue
                windowed, window_limited = _prepare_notes_for_llm(backlog_notes, LLM_LOOKBACK_DAYS)
                backlog_tasks.append((pending_patient_id, windowed, window_limited))
            backlog_tasks.sort(key=lambda task: sum(len(note.text) for note in task[1]))
        finally:
            if llm_worker_thread is not None:
                for task in backlog_tasks:
                    llm_queue.put(task)
                llm_queue.put(_LLM_QUEUE_DONE)
                if stopped_early:
                    report("Encerrando a análise por IA...")
                else:
                    report("Aguardando a análise por IA (já em andamento em paralelo desde a Fase 1) terminar a fila...")
                # DEC-109 (revisão adversarial): `.join()` sem timeout algum
                # já existia em espírito antes (Fase 2 sequencial tinha o
                # mesmo teto de ~90min/paciente sem disjuntor eficaz contra um
                # slot travado que ainda responde `/health`) -- não é
                # regressão nova, mas agora que a Fase 2 é uma unidade isolada
                # e esperável, dá pra pelo menos logar sinal de vida periódico
                # em vez de bloquear em silêncio indefinidamente.
                while llm_worker_thread.is_alive():
                    llm_worker_thread.join(timeout=300)
                    if llm_worker_thread.is_alive():
                        logger.info(
                            "Fase 2 (análise por IA) ainda em andamento em segundo plano (%d na fila)...",
                            llm_queue.qsize(),
                        )
                if worker_failed.is_set():
                    report(
                        "Aviso: a análise por IA foi interrompida por um erro inesperado -- "
                        "pacientes pendentes serão retomados na próxima execução."
                    )
                else:
                    report("Análise por IA concluída.")

        # REPORT-003 Fase 2 / DEC-099: retrato agregado do serviço, gravado
        # depois que a Fase 2 (IA) já terminou pra esta run -- é o estado
        # final do dia, não um intermediário. Isolado em try/except pelo
        # mesmo motivo de `generate_report` acima: uma falha aqui (disco
        # cheio, etc.) não pode jogar fora uma execução que já persistiu
        # tudo o que importa em `patient_state`/`pending_items`.
        if not stopped_early:  # UI-006/DEC-117: retrato parcial do dia poluiria a tendência
            try:
                dashboard_metrics.save_snapshot(repo, run_id)
            except Exception:
                logger.exception("Falha ao gravar snapshot diário -- execução segue concluída mesmo assim")

        # DIAG-001 (2026-09-03, pedido do usuário): registra, em linguagem
        # simples, o que aconteceu nesta execução -- pra que o auditor (sem
        # conhecimento técnico) saiba se uma falha foi o GSUS/rede (não é
        # defeito deste programa) ou algo que precisa de suporte de
        # verdade, sem precisar abrir o log. Isolado em try/except pelo
        # mesmo motivo do snapshot acima -- registrar o diagnóstico nunca
        # pode derrubar uma execução que já terminou de verdade.
        try:
            final_counts = repo.get_run_counts(run_id)
            if run_cancelled:
                outcome, summary, needs_attention = run_diagnosis.describe_cancelled_run(
                    found=final_counts["found"], completed=final_counts["completed"],
                )
            elif gsus_unresponsive:
                outcome, summary, needs_attention = run_diagnosis.describe_gsus_unresponsive_run(
                    found=final_counts["found"], completed=final_counts["completed"],
                    consecutive_failures=consecutive_unresponsive, relogin_attempted=relogin_attempted,
                )
            else:
                outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
                    found=final_counts["found"],
                    completed=final_counts["completed"],
                    no_admission=final_counts["no_admission"],
                    patient_errors=repo.get_error_messages_for_run(run_id),
                    census_complete=census_complete,
                )
            repo.save_run_diagnostic(run_id, outcome, summary, needs_attention)
        except Exception:
            logger.exception("Falha ao registrar diagnóstico da execução -- execução segue concluída mesmo assim")

        if run_cancelled:
            report("Encerrado pelo usuário.")
            return RunResult(
                run_id=run_id, counts=repo.get_run_counts(run_id), report_path=report_output_path,
                status="CANCELLED",
            )
        if gsus_unresponsive:
            report("Interrompido: o GSUS parou de responder.")
            return RunResult(
                run_id=run_id, counts=repo.get_run_counts(run_id), report_path=report_output_path,
                status="ABORTED_GSUS",
            )
        report("Concluído.")
        return RunResult(run_id=run_id, counts=repo.get_run_counts(run_id), report_path=report_output_path)
    except Exception as exc:
        logger.exception("Execução abortada por erro não tratado")
        if run_id is not None:
            try:
                repo.finish_run(run_id, "FAILED")
            except Exception:
                logger.exception("Falha ao marcar run como FAILED após erro")
        # DIAG-001: mesma ideia do caminho de sucesso acima, mas pro caminho
        # de falha -- inclusive quando `run_id` nunca chegou a existir
        # (login ou censo falharam por completo). Esse é justamente o
        # cenário que, antes desta correção, não deixava NENHUM rastro no
        # banco (achado da auditoria de catálogo de falhas, DEC-111) --
        # `exc` marcado depois pra `update_flow.py` não duplicar o registro
        # se a mesma exceção também passar por lá.
        try:
            outcome, summary = run_diagnosis.classify_top_level_exception(exc)
            repo.save_run_diagnostic(run_id, outcome, summary, needs_attention=True)
            exc._gsus_diagnostic_written = True
        except Exception:
            logger.exception("Falha ao registrar diagnóstico da execução com erro")
        raise


def _get_db_path(repo: Repository) -> Path:
    """Caminho do arquivo do banco por trás de `repo.conn` -- usado só pra
    `_llm_phase2_worker` (DEC-109) abrir sua PRÓPRIA conexão (nunca
    compartilha `sqlite3.Connection` entre threads). Funciona igual em
    produção (arquivo real) e em teste (`tmp_path` do pytest) porque ambos
    usam um arquivo de verdade -- nunca `:memory:` (ver `database.init_db`)."""
    row = repo.conn.execute("PRAGMA database_list").fetchone()
    return Path(row["file"])


_LLM_QUEUE_DONE = object()


def _llm_phase2_worker(
    db_path: Path,
    run_id: str,
    unit: str,
    report_output_path: Path,
    llm: AnalysisEngine,
    llm_queue: "queue.Queue",
    report: ProgressCallback,
    worker_failed: threading.Event,
    cancel_event: threading.Event | None = None,
) -> None:
    """Consome `llm_queue` numa thread dedicada, em paralelo com a Fase 1
    (DEC-109) -- achado real 2026-09-02: numa execução de 182 pacientes, o
    desenho antigo (Fase 2 só começa depois que TODA a Fase 1 termina) fazia
    a IA (CPU/GPU local) ficar ociosa por mais de 1h enquanto a Fase 1
    esperava respostas lentas do GSUS, processando só 16 pacientes na Fase 2
    apesar de já ter mais de 100 prontos. Pedido explícito do usuário pra
    acelerar sem trocar hardware.

    Abre sua PRÓPRIA conexão SQLite -- nunca compartilha `sqlite3.Connection`
    entre threads (regra do módulo, ver `app/storage/repository.py`). Seguro
    porque o banco já roda em WAL (`database.get_connection`), que suporta
    um escritor + múltiplas conexões concorrentes sem exigir coordenação
    extra em código Python -- e `generate_report` já escreve com nome de
    arquivo temporário por thread (`html_report.py`), então as duas fases
    regravando o relatório ao mesmo tempo não colidem.

    Thread `daemon=True` (ver `run_once`): se a execução abortar por exceção
    não tratada antes do dreno normal da fila, o processo ainda consegue
    encerrar -- pior caso é perder a análise do paciente que estava em
    andamento naquele exato momento, que uma execução futura refaz sozinha
    (nada aqui é marcado concluído até `_run_llm_analysis` de fato terminar).

    Preço consciente desta mudança: quem chega FRESCO durante a Fase 1 é
    consumido na ordem em que a Fase 1 termina cada paciente, não mais do
    menor pro maior volume de nota (DEC-085) -- só o backlog (enfileirado
    depois que a Fase 1 termina, ver `run_once`) continua ordenado assim.

    `worker_failed` (revisão adversarial, DEC-109): setado só nos dois
    caminhos de erro genuinamente inesperado abaixo (nunca no disjuntor de
    saúde, que já se comunica via `report()` normalmente) -- é como
    `run_once` sabe, depois do `.join()`, que a Fase 2 morreu de verdade em
    vez de terminar normalmente, sem precisar inspecionar log."""
    # DEC-109 (revisão adversarial): achado real -- abrir a conexão ficava
    # FORA do try/except logo abaixo. Se `database.init_db` falhasse aqui
    # (concorrência com a Fase 1 escrevendo no mesmo arquivo, disco lento),
    # a exceção escapava da thread inteira sem passar pelo `logger` desta
    # aplicação -- vira só um traceback no stderr que ninguém vê numa
    # execução automática sem console (`--auto-update`), e a Fase 2 inteira
    # era pulada em silêncio enquanto `run_once` seguia reportando sucesso.
    try:
        worker_conn = database.init_db(db_path)
    except Exception:
        logger.exception(
            "Falha ao abrir conexão da thread de análise por IA -- Fase 2 não vai rodar nesta execução"
        )
        worker_failed.set()
        return
    worker_repo = Repository(worker_conn)
    consecutive_unhealthy = 0
    try:
        while True:
            item = llm_queue.get()
            if item is _LLM_QUEUE_DONE:
                return
            if cancel_event is not None and cancel_event.is_set():
                # UI-006: encerrado pelo usuário -- descarta o resto da fila
                # sem analisar (nada é marcado concluído; a próxima execução
                # recoloca esses pacientes via backlog, DEC-090).
                continue
            patient_id, llm_notes, window_limited = item
            # Mesmo disjuntor de saúde do llama-server já existente (achado
            # real, auditoria de resiliência + DEC-087) e o mesmo isolamento
            # de falha de bookkeeping (RESIL-011) -- só adaptados de "índice
            # numa lista pronta" pra "item consumido de uma fila viva".
            try:
                if hasattr(llm, "is_healthy") and not llm.is_healthy():
                    consecutive_unhealthy += 1
                    logger.error(
                        "llama-server não respondeu à checagem de saúde (%d na fila no momento)",
                        llm_queue.qsize(),
                    )
                    if consecutive_unhealthy >= MAX_CONSECUTIVE_UNHEALTHY_CHECKS:
                        report(
                            "Interrompendo análise por IA: llama-server não está respondendo -- "
                            "pacientes restantes na fila ficarão sem análise nesta execução."
                        )
                        logger.error("Fase 2 interrompida por disjuntor de saúde")
                        return
                    continue
                consecutive_unhealthy = 0
                report(f"Analisando paciente com IA ({llm_queue.qsize()} restante(s) na fila no momento)...")
            except Exception:
                consecutive_unhealthy += 1
                logger.exception("Falha ao checar saúde do llama-server/reportar progresso")
                if consecutive_unhealthy >= MAX_CONSECUTIVE_UNHEALTHY_CHECKS:
                    logger.error("Fase 2 interrompida -- falhas repetidas na bookkeeping, não na análise em si")
                    return
                continue
            try:
                _run_llm_analysis(worker_repo, patient_id, llm, llm_notes, window_limited=window_limited)
            except Exception:  # isolamento de falha por paciente -- RF-12
                logger.exception(
                    "Falha inesperada na análise por IA do paciente %s -- seguindo pros demais",
                    pseudonym.for_log(patient_id),
                )
            try:
                generate_report(worker_repo, run_id, unit, report_output_path)
            except Exception:
                logger.exception(
                    "Falha ao regravar relatório após paciente %s -- análise por IA segue mesmo assim",
                    pseudonym.for_log(patient_id),
                )
    except Exception:
        # Nunca deixa a thread morrer em silêncio (comportamento padrão do
        # Python pra exceção não tratada numa thread -- só imprime no
        # stderr, ninguém no thread principal fica sabendo). Acontecendo
        # isto, a Fase 2 desta execução simplesmente para de avançar --
        # seguro (nada fica marcado concluído até de fato terminar).
        logger.exception("Thread de análise por IA (Fase 2) encerrada por erro inesperado")
        worker_failed.set()
    finally:
        worker_conn.close()


def _process_patient_rules(
    repo: Repository,
    patient: Patient,
    patient_id: str,
    record_source: RecordSource,
) -> tuple[list[StructuredNote], bool] | None:
    """Coleta+normaliza notas novas e aplica RULES-001 -- rápido, sempre
    síncrono. NÃO chama o LLM (DEC-071 separou isso numa fase posterior,
    `run_once` decide se/quando chamar `_run_llm_analysis`). Devolve as
    notas já recortadas pra janela do LLM (`_limit_to_recent_window`) e se
    houve corte, ou `None` se não há nota nova pra analisar.

    Só os dias ainda não processados: a rotina automática pega o que é
    novo, não o histórico inteiro (decisão do usuário -- ver DEC-048)."""
    raw_text = record_source.get_raw_notes_text(patient, repo.get_note_days(patient_id))
    blocks = parse_note_blocks(raw_text)

    all_structured_notes: list[StructuredNote] = []
    new_structured_notes: list[StructuredNote] = []

    for block in blocks:
        normalized_text = normalize_text(block["text"])
        timestamp = normalize_datetime(block["timestamp_raw"])
        note_hash = compute_note_hash(timestamp, block["source_type"], block["specialty"], normalized_text)

        structured = StructuredNote(
            timestamp=timestamp, specialty=block["specialty"], source_type=block["source_type"], text=normalized_text
        )
        all_structured_notes.append(structured)

        note = Note(
            patient_id=patient_id,
            source_type=block["source_type"] or "Evolução",
            specialty=block["specialty"],
            timestamp=timestamp,
            text=normalized_text,
            text_hash=note_hash,
        )
        if repo.add_note(note):  # True = NEW_NOTE, False = SKIP (já visto)
            new_structured_notes.append(structured)

    previous_state_row = repo.get_patient_state(patient_id)
    previous_necessidade = previous_state_row["necessidade_hospitalar"] if previous_state_row else None

    # `apply_all_rules` roda sobre as notas desta janela a cada execução --
    # sem reconciliar contra o que já está ativo, cada rodada inseriria uma
    # pendência NOVA duplicada enquanto a condição continuasse verdadeira
    # (achado real, DEC-062). `source="RULE"` isola isso da reconciliação do
    # LLM (`_run_llm_analysis`) -- ciclos de vida independentes.
    #
    # IMPORTANTE (achado real, DEC-064): `all_structured_notes` NÃO é o
    # histórico completo contra o GSUS real -- `get_raw_notes_text(patient,
    # known_days)` pula dias já conhecidos (só reenvia os ~2 dias mais
    # recentes + novos, ver app/gsus/records.py). Por isso NUNCA resolvemos
    # uma pendência de regra só por ela "não ter aparecido" nesta janela --
    # a nota de origem pode simplesmente ter saído do recorte incremental
    # sem o exame/parecer ter sido concluído de verdade. Só resolvemos
    # quando `apply_all_rules` devolve um achado com `resolved=True` -- ou
    # seja, quando a MESMA janela de notas contém tanto a solicitação quanto
    # a conclusão, prova real de resolução (não inferência por ausência).
    active_rule_rows = [r for r in repo.get_active_pending_items(patient_id) if r["source"] == "RULE"]
    for finding in apply_all_rules(all_structured_notes):
        # Casa por descrição (determinística por construção -- RuleFinding
        # sempre gera o mesmo texto pro mesmo exame/especialidade): distingue
        # corretamente duas pendências de regra concorrentes e diferentes
        # (ex.: tomografia E ultrassom abertos ao mesmo tempo -- achado real,
        # DEC-064; categoria+subtipo sozinhos colidiam nesse caso, já que
        # RULES-001 usa um subtipo único por categoria pra todo tipo de exame).
        existing = _find_matching_pending(active_rule_rows, finding.category, finding.subcategory, finding.description)

        if finding.resolved:
            if existing is not None:
                repo.resolve_pending_item(existing["pending_id"])
            continue

        if existing is not None:
            continue

        hours_elapsed = hours_elapsed_since(finding.evidence_date)
        priority = compute_priority(finding.category, previous_necessidade, hours_elapsed)
        repo.add_pending_item(
            patient_id, finding.category, finding.description, finding.evidence, finding.evidence_date,
            subcategory=finding.subcategory, origin=finding.origin, priority=priority, is_inferred=False,
            source="RULE",
        )

    if not new_structured_notes:
        return None
    return _prepare_notes_for_llm(new_structured_notes, LLM_LOOKBACK_DAYS)


def _limit_to_recent_window(
    notes: list[StructuredNote], days: int
) -> tuple[list[StructuredNote], bool]:
    """Devolve (notas dentro da janela, True se alguma nota ficou de fora).
    Janela é relativa à nota mais recente do lote (não a "hoje") -- uma
    atualização automática que roda de madrugada não deve descartar notas só
    porque a mais nova tem algumas horas. Nota sem timestamp reconhecido é
    sempre incluída (não dá pra saber se é antiga -- nunca descarta por
    excesso de cautela, RF-08/RNF)."""
    recent = most_recent_note(notes)
    if recent is None or not recent.timestamp:
        return notes, False
    try:
        reference = datetime.fromisoformat(recent.timestamp)
    except ValueError:
        return notes, False
    cutoff = reference - timedelta(days=days)

    within: list[StructuredNote] = []
    excluded_any = False
    for note in notes:
        if not note.timestamp:
            within.append(note)
            continue
        try:
            note_dt = datetime.fromisoformat(note.timestamp)
        except ValueError:
            within.append(note)
            continue
        if note_dt >= cutoff:
            within.append(note)
        else:
            excluded_any = True
    return within, excluded_any


def _limit_to_char_budget(
    notes: list[StructuredNote], max_chars: int
) -> tuple[list[StructuredNote], bool]:
    """Segundo corte, por TAMANHO em vez de tempo (RESIL-006) -- roda DEPOIS
    de `_limit_to_recent_window`. Descarta as notas mais ANTIGAS primeiro
    (`notes` já vem em ordem cronológica ascendente, ver `Repository.
    get_notes`/`_process_patient_rules`) até caber no orçamento, ou até
    sobrar só a mais recente -- nunca devolve lista vazia se havia alguma
    nota (mesma filosofia de nunca descartar tudo por excesso de cautela,
    RF-08/RNF, só que aqui o "tudo" seria uma nota só gigante demais; melhor
    tentar com ela sozinha do que não analisar nada)."""
    total = sum(len(note.text) for note in notes)
    if total <= max_chars or len(notes) <= 1:
        return notes, False

    within = list(notes)
    while len(within) > 1 and sum(len(note.text) for note in within) > max_chars:
        within.pop(0)
    return within, True


def _prepare_notes_for_llm(
    notes: list[StructuredNote], days: int
) -> tuple[list[StructuredNote], bool]:
    """Combina os dois cortes -- por data (DEC-066) e por tamanho
    (RESIL-006) -- na ordem certa (data primeiro, tamanho por cima, já que a
    janela de dias sozinha ainda pode deixar volume grande demais pro
    contexto do LLM). `window_limited` fica True se QUALQUER um dos dois
    cortou algo -- mesmo significado de sempre pro relatório (RF-26): esta
    análise não viu o histórico completo disponível."""
    by_date, date_limited = _limit_to_recent_window(notes, days)
    by_size, size_limited = _limit_to_char_budget(by_date, MAX_NOTES_CHARS_FOR_LLM)
    return by_size, date_limited or size_limited


def _reconstruct_all_notes(repo: Repository, patient_id: str) -> list[StructuredNote]:
    """Histórico COMPLETO já extraído pro paciente (não só a nota nova de
    hoje) -- usado pelo resgate de backlog do LLM-004 (ver `run_once`).
    Paciente sem nenhuma análise bem-sucedida precisa do histórico inteiro
    pro modelo ter contexto (achado real da auditoria de resiliência: mandar
    só a última nota isolada, com `previous_state=None`, produz uma análise
    pobre mesmo quando "funciona")."""
    return [
        StructuredNote(
            timestamp=row["timestamp"], specialty=row["specialty"],
            source_type=row["source_type"], text=row["text"],
        )
        for row in repo.get_notes(patient_id)
    ]


def _safe_error_text(exc: Exception, max_length: int = 300) -> str:
    """Achado real (auditoria de resiliência, achado de PRIVACIDADE): `str(exc)`
    de um erro do Playwright pode incluir o "Call log" inteiro (passos,
    URLs) e, em erros de "strict mode violation", o HTML dos elementos que
    casaram -- podendo conter texto da própria tela do GSUS. Persistido sem
    filtro em `processing_queue.last_error` isso é um vazamento real pra
    disco (fora do escopo de "nunca chega no contexto do Claude", mas ainda
    assim indevido). Mantém só o TIPO da exceção + a PRIMEIRA LINHA da
    mensagem -- no Playwright isso é tipicamente só a operação e o motivo
    curto (ex.: "Timeout 30000ms exceeded"), sem call log nem HTML."""
    text = str(exc).strip()
    first_line = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first_line}"[:max_length]


# Categorias cujo SUBTIPO representa ESTÁGIO de um fluxo, não um tipo
# diferente de pendência (seção 4.2 da orientação técnica: "distinguir o
# fluxo: solicitado -> realizado -> conduta definida. Interconsulta
# realizada sem conduta definida pode continuar pendente"). Achado real
# (DEC-064): casar por (categoria, subtipo) exato fazia a MESMA interconsulta
# evoluindo de SOLICITADA -> REALIZADA_SEM_CONDUTA_DEFINIDA parecer uma
# pendência NOVA, perdendo a evidência/data original e criando duplicata.
# Para estas categorias, casa só por categoria -- aceito o custo de juntar
# duas pendências REALMENTE distintas da mesma categoria (raro) em troca de
# nunca quebrar o rastreamento de progresso de uma única pendência real.
STAGE_PROGRESSION_CATEGORIES = {CATEGORY_INTERCONSULTA, CATEGORY_PROCEDIMENTO_CIRURGIA}


def _find_matching_pending(active_rows, category: str, subcategory: str | None, description: str | None = None):
    """Mesma pendência real entre execuções. Duas fontes, duas heurísticas
    (achado real, revisão de conformidade DEC-064):

    - Regra (`description` informado): `RuleFinding.description` é
      DETERMINÍSTICO -- sempre o mesmo texto para o mesmo exame/especialidade
      (ex.: "Aguarda realização de tomografia"). Casar por descrição exata
      (dentro da mesma categoria+subtipo) distingue corretamente duas
      pendências de regra concorrentes e diferentes (ex.: tomografia E
      ultrassom abertos ao mesmo tempo -- antes colapsavam na mesma, porque
      RULES-001 usa um subtipo único por categoria pra todo tipo de exame).
    - LLM (`description` ausente): a descrição é texto livre do modelo, varia
      entre chamadas mesmo pra mesma pendência real -- não dá pra usar.
      Categoria+subtipo é a heurística (categoria sozinha para as
      STAGE_PROGRESSION_CATEGORIES, ver acima)."""
    for row in active_rows:
        if row["category"] != category:
            continue
        if description is not None:
            if row["description"] == description:
                return row
            continue
        if category in STAGE_PROGRESSION_CATEGORIES:
            return row
        if row["subcategory"] == subcategory:
            return row
    return None


def _run_llm_analysis(
    repo: Repository, patient_id: str, llm: AnalysisEngine, new_notes: list[StructuredNote],
    window_limited: bool = False,
) -> None:
    previous_state_row = repo.get_patient_state(patient_id)
    previous_state = (
        {"clinical_context": previous_state_row["clinical_context"], "current_status": previous_state_row["current_status"]}
        if previous_state_row
        else None
    )
    # Sem isso o LLM só veria texto livre do estado anterior, não a lista
    # estruturada do que está pendente -- não conseguiria dizer se uma
    # pendência antiga foi resolvida pelas evoluções novas. Contexto inclui
    # TODAS as pendências ativas (regra + LLM) -- só a reconciliação abaixo
    # (reiteração/resolução) precisa distinguir a origem (DEC-062).
    all_active_rows = repo.get_active_pending_items(patient_id)
    active_pending_items = [
        {
            "category": row["category"], "subcategory": row["subcategory"],
            "description": row["description"], "evidence_date": row["evidence_date"],
        }
        for row in all_active_rows
    ]
    active_rows = [r for r in all_active_rows if r["source"] == "LLM"]

    try:
        analysis = llm.analyze_patient(previous_state, new_notes, active_pending_items)
    except LLMAnalysisError:
        logger.exception(
            "LLM_ANALYSIS_ERROR para paciente %s -- mantendo estado anterior, sem inventar resultado",
            pseudonym.for_log(patient_id),
        )
        return

    if analysis.get("insufficient_information"):
        return

    # especialidade_responsavel vem de metadado JÁ estruturado (o cargo do
    # profissional na nota, extraído pelo parser -- não pedimos pro LLM
    # adivinhar algo que já temos de forma confiável, RF-07/RF-08). Usa a
    # nota mais recente -- mesmo critério de recência do prompt (DEC-058).
    last_note = most_recent_note(new_notes)
    repo.save_patient_state(
        patient_id, analysis["clinical_context"], analysis["current_status"],
        especialidade_responsavel=last_note.specialty if last_note else None,
        origem_internacao=analysis.get("origem_internacao"),
        necessidade_hospitalar=analysis["necessidade_hospitalar"],
        necessidade_hospitalar_justificativa=analysis["necessidade_hospitalar_justificativa"],
        objetivo_terapeutico=analysis["objetivo_terapeutico"],
        proximo_passo=analysis["proximo_passo"],
        edd_data=analysis.get("edd_data"),
        edd_status=analysis["edd_status"],
        dia_classificacao=analysis["dia_classificacao"],
        dia_causa=analysis.get("dia_causa"),
        model_version=getattr(llm, "model_version", None),
        analysis_window_limited=window_limited,
    )

    # Reconcilia as pendências reportadas agora contra as que já estavam
    # ativas (DEC-062): mesma categoria+subtipo -> REITERAÇÃO (evidência
    # nova acrescentada, evidence_date/description ORIGINAIS preservados --
    # seção 5 da orientação técnica exige a PRIMEIRA evidência documental,
    # nunca a mais recente). Categoria+subtipo novo -> pendência nova.
    # Pendência ativa que não foi reconfirmada nesta análise -> resolvida
    # (o prompt já pede pro LLM reavaliar cada uma -- isso só fecha o ciclo
    # que faltava).
    matched_pending_ids = set()
    for item in analysis["pending_items"]:
        existing = _find_matching_pending(active_rows, item["category"], item.get("subcategory"))
        if existing is not None:
            matched_pending_ids.add(existing["pending_id"])
            repo.add_pending_item_evidence(existing["pending_id"], item.get("evidence_date"), item["evidence"])
            repo.update_pending_item_progress(
                existing["pending_id"], confidence=item.get("confidence"), flow_status=item.get("flow_status")
            )
            continue

        hours_elapsed = hours_elapsed_since(item.get("evidence_date"))
        priority = compute_priority(item["category"], analysis["necessidade_hospitalar"], hours_elapsed)
        # Achado real (DEC-064): o LLM frequentemente devolve origin=null
        # (campo opcional no contrato) -- sem fallback, a maioria das
        # pendências do LLM nunca mostrava o selo interna/externa no
        # relatório (regras sempre têm origin via RuleFinding.origin, só o
        # caminho do LLM ficava sem default).
        origin = item.get("origin") or default_origin(item["category"])
        repo.add_pending_item(
            patient_id, item["category"], item["description"], item["evidence"], item.get("evidence_date"),
            subcategory=item.get("subcategory"), origin=origin, priority=priority,
            confidence=item.get("confidence"), is_inferred=bool(item.get("is_inferred")),
            flow_status=item.get("flow_status"), source="LLM",
        )

    for row in active_rows:
        if row["pending_id"] not in matched_pending_ids:
            repo.resolve_pending_item(row["pending_id"])
