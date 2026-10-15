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

import bisect
import logging
import queue
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from app.analysis import run_diagnosis
from app.analysis.llm import LLMAnalysisError
from app.analysis.priority import compute_priority, days_since_admission, hours_elapsed_since, pre_admission_cutoff_iso
from app.analysis.rules import StructuredNote, apply_all_rules, most_recent_note
from app.analysis.edd import infer_edd_from_notes
from app.analysis.sources import classify_source, is_physician
from app.analysis.schemas import (
    DECISION_CANCELLED, DECISION_RESOLVED, DECISION_UNCERTAIN, REVIEW_FIELD, normalize_review_decisions,
)
from app.analysis.specialty_evaluation import (
    ACTION_CANCEL, ACTION_RESOLVE, ACTION_REVIEW, CONSULT_CLOSABLE_SUBTYPES as _CONSULT_CLOSABLE_SUBTYPES,
    DetectorNote, DetectorSession, alvo_core_words, cited_dates, clear_caches, evidence_shows_done, is_canonical_target,
    is_history,
    kind_and_target, normalize_target, notes_blob, puncture_already_done, quote_confirms_cancel, quote_confirms_done,
    quote_in_notes, quote_is_about_item, reference_time, regions_for, request_already_handled, resolution_time_utc,
    KIND_PUNCTURE,
)
from app.analysis.specialties import fold
from app.analysis.taxonomy import (
    CATEGORY_DIAGNOSTICO, CATEGORY_INTERCONSULTA, CATEGORY_PROCEDIMENTO_CIRURGIA, default_origin,
)
from app.extraction.hashing import compute_note_hash
from app.extraction.normalizer import (
    normalize_datetime,
    normalize_iso_date,
    normalize_llm_datetime,
    normalize_text,
)
from app.extraction.parser import parse_note_blocks
from app.gsus.census import GSUSCensusIncompleteError
from app.gsus.records import (
    GSUSCurrentAdmissionNotFound,
    GSUSNoCurrentAdmissionDays,
    GSUSNoNotesToExtract,
    GSUSSearchUnresponsiveError,
)
from app.models import Note, Patient
from app.reports import dashboard_metrics
from app.reports.html_report import generate_report
from app.security import pseudonym
from app.storage import database
from app.storage.repository import (
    CLOSED_PENDING_STATUSES, PENDING_IN_REVIEW, RESOLUTION_DETECTOR, RESOLUTION_LLM, Repository,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


class _Pend001Totals:
    """Contagens do PEND-001 na execução inteira (as duas fases), pra uma
    linha de resumo no fim -- é o que a validação em produção (ACCEPT-001)
    lê no log (2ª revisão adversarial: só o detector contava, por paciente)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def reset(self) -> None:
        with self._lock:
            self._counts = {}

    def add(self, key: str, amount: int = 1) -> None:
        if amount:
            with self._lock:
                self._counts[key] = self._counts.get(key, 0) + amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


_PEND001_TOTALS = _Pend001Totals()


def _rollback_quietly(repo: Repository) -> None:
    """Depois de uma falha engolida: desfaz a transação que ficou aberta.
    Sem isto, um "database is locked" passageiro deixava a conexão da Fase 2
    presa numa transação e o resto da noite falhava na hora (2ª revisão
    adversarial)."""
    try:
        if repo.conn.in_transaction:
            repo.conn.rollback()
    except Exception:
        logger.exception("Falha ao desfazer transação depois de erro -- seguindo")

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
#
# LLM-006 (achado da revisão adversarial, medido com o /tokenize do
# llama-server em porta isolada): o prompt v2 (regras do auditor) subiu o
# system prompt de 5.132 pra 7.921 caracteres (1.585 -> 2.417 tokens). O que
# a calibração acima garantiu foi o TOTAL "system prompt + notas" (~14.132
# caracteres); mantido o total, o orçamento das notas cai pra 6.200. Um teste
# (`test_prompt_and_notes_budget_stay_within_the_calibrated_total`) impede que
# o próximo aumento do prompt coma essa margem em silêncio.
CALIBRATED_PROMPT_PLUS_NOTES_CHARS = 5132 + 9000
MAX_NOTES_CHARS_FOR_LLM = 6200
TRUNCATION_NOTICE = " [trecho final desta evolução omitido por tamanho]"

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

# RESIL-015 (achado real 2026-09-08): paciente admitido HOJE ou ONTEM cujo
# card da internação ainda não aparece na busca, ou cujo único dia ainda não
# tem evolução acessível, não é falha -- é o intervalo normal entre a
# admissão e a primeira evolução. Até esta folga (dias desde a admissão),
# esses dois casos viram AWAITING_NOTES (categoria benigna, sem alarme);
# acima dela continuam ERROR, porque aí o padrão passa a indicar GSUS
# instável ou alta não refletida no censo. Histórico: 28/60 e 32/45 das
# ocorrências caíam em 0-1 dia.
RECENT_ADMISSION_GRACE_DAYS = 1


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
        patient_context: dict | None = None,
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


def _is_recent_admission(admission_date: str | None, today: date | None = None) -> bool:
    """RESIL-015: admissão de hoje ou de ontem (`RECENT_ADMISSION_GRACE_DAYS`).
    Data ausente/irreconhecível conta como NÃO recente -- na dúvida, o caso
    segue como erro visível, nunca some numa categoria benigna."""
    days = days_since_admission(admission_date, today)
    return days is not None and 0 <= days <= RECENT_ADMISSION_GRACE_DAYS


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
        return {"found": 0, "completed": 0, "failed": 0, "no_admission": 0, "awaiting_notes": 0}

    # Achado real (auditoria de resiliência 2026-08-28): antes desta trava,
    # qualquer exceção não prevista escapando desta função (falha de
    # login/censo sem retry, erro inesperado na fila, etc.) deixava a run
    # com status RUNNING pra sempre -- ninguém nunca escrevia FAILED -- e o
    # relatório do dia anterior ficava parado, sem nenhum aviso de que a
    # auditoria de hoje não rodou. `run_id` só passa a existir a partir de
    # `repo.start_run()` (abaixo); continuar `None` cobre uma falha ainda
    # mais cedo (login/censo), quando não há run nenhuma pra marcar FAILED.
    run_id: str | None = None
    _PEND001_TOTALS.reset()
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
                except (GSUSCurrentAdmissionNotFound, GSUSNoNotesToExtract) as exc:
                    # RESIL-015: card ainda não visível na busca / único dia sem
                    # evolução acessível. Em admissão recente é o esperado
                    # (categoria benigna, retomado na próxima atualização);
                    # fora da folga continua erro visível, como antes.
                    if _is_recent_admission(patients_by_id[patient_id].admission_date):
                        repo.mark_awaiting_notes(run_id, patient_id)
                    else:
                        logger.exception(
                            "Falha ao processar paciente %s", pseudonym.for_log(patient_id),
                        )
                        repo.mark_error(run_id, patient_id, _safe_error_text(exc))
                    # O GSUS respondeu (a busca rodou): zera o disjuntor.
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
                    _rollback_quietly(repo)
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
            # LLM-006: quanto os filtros de exibição corrigiram o texto da IA
            # nesta execução -- medida da aderência do prompt v2, só contagens.
            try:
                fixes = dashboard_metrics.count_display_fixes(repo)
                logger.info(
                    "Correções de exibição do resumo clínico (LLM-006): %d paciente(s) -- rotina=%d, "
                    "sinal vital=%d, localização=%d, repetição=%d",
                    fixes["pacientes"], fixes["rotina"], fixes["sinal_vital"], fixes["localizacao"], fixes["repeticao"],
                )
            except Exception:
                logger.exception("Falha ao contar correções de exibição -- execução segue concluída mesmo assim")

        # PEND-001: uma linha com tudo o que mudou de estado nas pendências
        # nesta execução (detector, migração, decisões da IA) -- só contagens.
        totals = _PEND001_TOTALS.snapshot()
        if totals:
            logger.info("PEND-001: totais da execução -- %s",
                        ", ".join(f"{key}={value}" for key, value in sorted(totals.items())))
        clear_caches()  # caches por frase do detector: memória do PC fraco

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
                    awaiting_notes=final_counts["awaiting_notes"],
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
        clear_caches()  # a execução que falhou também libera a memória dos caches (4ª revisão)
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
                _rollback_quietly(worker_repo)
            try:
                generate_report(worker_repo, run_id, unit, report_output_path)
            except Exception:
                _rollback_quietly(worker_repo)
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

    # DEC-119 (achado real 2026-09-07): 109 dos 183 pacientes ativos tinham
    # evoluções de 2014-2025 gravadas, e 77 pendências de REGRA ativas com
    # evidência anterior à internação atual (tempos de "anos" no censo). A
    # extração deixava passar dias de episódios antigos. Segunda linha de
    # defesa, independente da tela do GSUS: evolução datada antes da
    # admissão (menos uma folga pro pronto-socorro) NÃO entra nas regras nem
    # no LLM. Continua sendo gravada -- é o que evita reabrir o dia na próxima
    # execução (DEC-048) -- só não é analisada.
    cutoff_iso = pre_admission_cutoff_iso(patient.admission_date)
    ignored_before_admission = 0

    for block in blocks:
        normalized_text = normalize_text(block["text"])
        timestamp = normalize_datetime(block["timestamp_raw"])
        note_hash = compute_note_hash(timestamp, block["source_type"], block["specialty"], normalized_text)

        structured = StructuredNote(
            timestamp=timestamp, specialty=block["specialty"], source_type=block["source_type"], text=normalized_text
        )
        belongs_to_current_admission = not (cutoff_iso and timestamp and timestamp[:10] < cutoff_iso)
        if belongs_to_current_admission:
            all_structured_notes.append(structured)
        else:
            ignored_before_admission += 1

        note = Note(
            patient_id=patient_id,
            source_type=block["source_type"] or "Evolução",
            specialty=block["specialty"],
            timestamp=timestamp,
            text=normalized_text,
            text_hash=note_hash,
        )
        if repo.add_note(note) and belongs_to_current_admission:  # True = NEW_NOTE, False = SKIP (já visto)
            new_structured_notes.append(structured)

    if ignored_before_admission:
        logger.info(
            "Ignorando %d evolução(ões) anterior(es) à internação atual (antes de %s) do paciente %s",
            ignored_before_admission, cutoff_iso, pseudonym.for_log(patient_id),
        )

    previous_state_row = repo.get_patient_state(patient_id)
    previous_necessidade = previous_state_row["necessidade_hospitalar"] if previous_state_row else None

    # `apply_all_rules` roda sobre as notas desta janela a cada execução --
    # sem reconciliar contra o que já está registrado, cada rodada inseriria
    # uma pendência NOVA duplicada enquanto o pedido continuasse na janela
    # (achado real, DEC-062). PEND-001: casa por (categoria, alvo) contra as
    # pendências ABERTAS de qualquer fonte -- regra e IA não duplicam mais uma
    # à outra -- e não recria o que já foi encerrado depois deste pedido
    # (senão o pedido de anteontem, ainda na janela, reabriria toda noite a
    # interconsulta que o detector acabou de fechar).
    #
    # IMPORTANTE (achado real, DEC-064): `all_structured_notes` NÃO é o
    # histórico completo -- `get_raw_notes_text(patient, known_days)` só
    # reenvia os ~2 dias mais recentes + novos. Por isso nada fecha por "não
    # ter aparecido" nesta janela: quem fecha é o detector logo abaixo, que
    # lê a internação inteira do banco e exige prova posterior ao pedido.
    try:
        _apply_rules_and_detector(repo, patient, patient_id, all_structured_notes, cutoff_iso, previous_necessidade)
    except Exception:
        # Confiabilidade diária: um defeito nas regras/no detector nunca pode
        # derrubar o paciente (as evoluções já foram gravadas; a IA segue).
        logger.exception("PEND-001: falha nas regras/detector do paciente %s -- seguindo", pseudonym.for_log(patient_id))
        _rollback_quietly(repo)

    if not new_structured_notes:
        return None
    return _prepare_notes_for_llm(new_structured_notes, LLM_LOOKBACK_DAYS)


def _apply_rules_and_detector(
    repo: Repository, patient: Patient, patient_id: str, all_structured_notes: list[StructuredNote],
    cutoff_iso: str | None, previous_necessidade: str | None,
) -> None:
    open_rows = _current_open_rows(_backfill_targets(repo, patient_id, patient.admission_date), patient.admission_date)
    all_rows = _current_admission_rows(repo.get_patient_pending_items(patient_id), patient.admission_date)
    session = DetectorSession(_admission_notes(repo, patient_id, patient.admission_date))
    for finding in apply_all_rules(all_structured_notes, cutoff_iso):
        if _find_by_target(_matchable_rows(open_rows, finding.subcategory), finding.category, finding.alvo) is not None:
            continue
        # O primeiro pedido da janela que ainda NÃO foi atendido -- um
        # "reitero parecer" depois da avaliação é pedido novo; o original,
        # anterior a ela, não reabre nada. A CÓPIA do pedido já atendido
        # (evolução copiada do dia anterior) também não: 2ª revisão
        # adversarial -- criava uma pendência que nunca mais fechava.
        unhandled = next(
            (occurrence for occurrence in finding.occurrences
             if _closed_after(all_rows, finding.category, finding.alvo, occurrence[0]) is None
             and not _copy_of_handled_request(session, all_rows, finding.category, finding.alvo, occurrence)),
            None,
        )
        if unhandled is None:
            continue
        evidence_date, evidence = unhandled
        hours_elapsed = hours_elapsed_since(evidence_date)
        priority = compute_priority(finding.category, previous_necessidade, hours_elapsed)
        repo.add_pending_item(
            patient_id, finding.category, finding.description, evidence, evidence_date,
            subcategory=finding.subcategory, origin=finding.origin, priority=priority, is_inferred=False,
            source="RULE", alvo=finding.alvo,
        )
        _PEND001_TOTALS.add("criadas_regra")
        open_rows = _current_open_rows(repo.get_active_pending_items(patient_id), patient.admission_date)

    _close_answered_pending_items(repo, patient_id, patient.admission_date, session=session)


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


def _sort_chronologically(notes: list[StructuredNote]) -> list[StructuredNote]:
    """Ordem cronológica crescente, por VALOR do horário. Achado da Fase 2
    (medição só de contagens no banco de dev): a extração do GSUS entrega os
    dias do MAIS NOVO pro mais antigo (2.641 trocas de dia "pra trás" contra
    39 "pra frente" na ordem de gravação) -- o corte por tamanho, que
    supunha ordem crescente, descartava justamente as evoluções mais
    recentes. Nota sem horário vai pro fim, na ordem em que veio."""
    dated = sorted((n for n in notes if n.timestamp), key=lambda n: n.timestamp)
    return dated + [n for n in notes if not n.timestamp]


def _limit_to_char_budget(
    notes: list[StructuredNote], max_chars: int
) -> tuple[list[StructuredNote], bool]:
    """Segundo corte, por TAMANHO em vez de tempo (RESIL-006) -- roda DEPOIS
    de `_limit_to_recent_window`, sobre notas em ordem cronológica crescente
    (`_sort_chronologically`). Recência primeiro, como sempre foi, com uma
    garantia do LLM-006 (pedido do auditor, DEC-129 seção 2): a evolução
    MÉDICA mais recente entra antes de qualquer outra, se couber -- ela é a
    base da situação de hoje no prompt. Depois, da mais nova pra mais
    antiga, entra o que couber; quando uma nota de uma fonte (médica ou
    demais) não cabe, as mais antigas DESSA fonte também ficam de fora --
    sem buraco no meio da linha do tempo. Nunca devolve lista vazia se havia
    alguma nota (a médica mais recente, se houver, vai sozinha) -- melhor
    tentar com ela do que não analisar nada (RF-08/RNF). Devolve em ordem
    cronológica. (Achado da revisão adversarial: uma reserva fixa de 70%
    pras médicas descartava a médica mais recente quando ela era grande e o
    evento de enfermagem de hoje em troca de médicas de 10 dias atrás.)"""
    total = sum(len(note.text) for note in notes)
    if total <= max_chars:
        return notes, False

    def truncated(note: StructuredNote) -> StructuredNote:
        if len(note.text) <= max_chars:
            return note
        keep = max_chars - len(TRUNCATION_NOTICE)
        return StructuredNote(
            timestamp=note.timestamp, specialty=note.specialty, source_type=note.source_type,
            text=note.text[:keep].rstrip() + TRUNCATION_NOTICE,
        )

    # Mais nova primeiro; nota sem horário conta como a mais antiga.
    dated = [(i, n) for i, n in enumerate(notes) if n.timestamp]
    undated = [(i, n) for i, n in enumerate(notes) if not n.timestamp]
    newest_first = list(reversed(dated)) + undated
    chosen: dict[int, StructuredNote] = {}
    used = 0
    newest_physician = next(((i, n) for i, n in newest_first if is_physician(n.source_type)), None)
    if newest_physician is not None:
        # Achado da 2ª revisão: uma evolução médica maior que o orçamento
        # sumia e a enfermagem virava a "base de hoje". Vai o começo dela
        # (diagnóstico descritivo, queixa, exame), com aviso de corte.
        index, note = newest_physician
        chosen[index] = truncated(note)
        used = len(chosen[index].text)

    blocked: set[str] = set()  # fontes cuja próxima nota (mais antiga) não coube
    for index, note in newest_first:
        group = classify_source(note.source_type)
        if index in chosen or group in blocked:
            continue
        if used + len(note.text) <= max_chars:
            chosen[index] = note
            used += len(note.text)
        elif note.timestamp and len(note.text) <= max_chars:
            # Nota que não caberia nem sozinha, ou sem horário, não bloqueia
            # as mais antigas da mesma fonte.
            blocked.add(group)
    if not chosen:
        index, note = newest_first[0]
        chosen[index] = truncated(note)
    return [chosen[i] for i in sorted(chosen)], True


def _prepare_notes_for_llm(
    notes: list[StructuredNote], days: int
) -> tuple[list[StructuredNote], bool]:
    """Combina os dois cortes -- por data (DEC-066) e por tamanho
    (RESIL-006) -- na ordem certa (data primeiro, tamanho por cima, já que a
    janela de dias sozinha ainda pode deixar volume grande demais pro
    contexto do LLM), sobre as notas já em ordem cronológica. `window_limited`
    fica True se QUALQUER um dos dois cortou algo -- mesmo significado de
    sempre pro relatório (RF-26): esta análise não viu o histórico completo
    disponível."""
    ordered = _sort_chronologically(notes)
    by_date, date_limited = _limit_to_recent_window(ordered, days)
    by_size, size_limited = _limit_to_char_budget(by_date, MAX_NOTES_CHARS_FOR_LLM)
    return by_size, date_limited or size_limited


def _latest_physician_note(
    repo: Repository, patient_id: str, new_notes: list[StructuredNote], admission_date: str | None,
) -> StructuredNote | None:
    """Evolução MÉDICA mais recente já gravada desta internação (não antes
    da admissão menos a folga, e dentro da janela do LLM em relação à nota
    nova mais recente). None se não houver."""
    cutoff_iso = pre_admission_cutoff_iso(admission_date)
    newest = most_recent_note(new_notes)
    window_start = None
    if newest is not None and newest.timestamp:
        try:
            window_start = (datetime.fromisoformat(newest.timestamp) - timedelta(days=LLM_LOOKBACK_DAYS)).isoformat()
        except ValueError:
            window_start = None
    for row in reversed(repo.get_notes(patient_id)):
        timestamp = row["timestamp"]
        if not timestamp or not is_physician(row["source_type"]):
            continue
        if cutoff_iso and timestamp[:10] < cutoff_iso:
            return None
        if window_start and timestamp < window_start:
            return None
        return StructuredNote(
            timestamp=timestamp, specialty=row["specialty"], source_type=row["source_type"], text=row["text"],
        )
    return None


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


def _find_by_target(rows, category: str, alvo: str | None):
    """PEND-001: mesma pendência = mesma categoria e mesmo alvo normalizado,
    de qualquer fonte (regra ou IA). Sem alvo, nada casa por aqui. Exame sem
    região ("TOMOGRAFIA", regra antiga) casa com o mesmo exame COM região só
    quando o próprio texto do item cita essa região (2ª revisão: senão o
    pedido antigo e o mesmo pedido relido viravam duas pendências; 3ª: "TC
    por suspeita de AVC" engolia a "TC de abdome" pedida depois, e o laudo da
    primeira fechava as duas)."""
    if not alvo:
        return None
    for row in rows:
        if row["category"] == category and row["alvo"] == alvo:
            return row
    if category == CATEGORY_DIAGNOSTICO:
        exam, _, region = alvo.partition(" DE ")
        if region:
            for row in rows:
                if row["category"] == category and row["alvo"] == exam \
                        and region in regions_for(exam, (row["description"], row["evidence"])):
                    return row
    return None


def _matchable_rows(rows, incoming_subcategory: str | None):
    """Interconsulta registrada num estágio que o detector não fecha
    ("realizada sem conduta definida") não engole um pedido NOVO de
    interconsulta à mesma especialidade (3ª revisão)."""
    if incoming_subcategory not in _CONSULT_CLOSABLE_SUBTYPES:
        return rows
    return [row for row in rows if row["category"] != CATEGORY_INTERCONSULTA
            or row["subcategory"] in _CONSULT_CLOSABLE_SUBTYPES]


def _closed_after(all_rows, category: str, alvo: str | None, request_evidence_date: str | None):
    """Pendência ENCERRADA com o mesmo alvo depois deste pedido -- o pedido
    já foi atendido (ou o auditor já decidiu). Devolve a linha, ou None."""
    if not alvo:
        return None
    for row in all_rows:
        if row["status"] not in CLOSED_PENDING_STATUSES:
            continue
        if row["category"] != category or row["alvo"] != alvo:
            continue
        if request_already_handled(request_evidence_date, row["resolution_evidence_date"], row["resolved_at"]):
            return row
    return None


def _closure_local_iso(row) -> str | None:
    """Quando a pendência encerrada foi atendida (horário local): a data da
    evidência que provou, ou -- sem ela -- a do encerramento."""
    if row["resolution_evidence_date"]:
        return row["resolution_evidence_date"]
    try:
        stamp = row["closed_at"] or row["resolved_at"]
        return datetime.fromisoformat(stamp).astimezone().replace(tzinfo=None).isoformat(timespec="seconds") \
            if stamp else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _current_admission_rows(rows, admission_date: str | None):
    """Pendências desta internação: as encerradas ANTES da admissão atual
    (internação anterior do mesmo prontuário) não dizem nada sobre um pedido
    de hoje (2ª revisão: "parecer da cardiologia" respondido em março
    descartava o pedido novo de agosto)."""
    cutoff = pre_admission_cutoff_iso(admission_date)
    if not cutoff:
        return rows
    kept = []
    for row in rows:
        if row["status"] in CLOSED_PENDING_STATUSES:
            closure = _closure_local_iso(row)
            if closure and closure[:10] < cutoff:
                continue
        kept.append(row)
    return kept


def _from_previous_admission(row, cutoff: str | None) -> bool:
    if not cutoff:
        return False
    stamp = row["evidence_date"] or row["created_at"] or ""
    return bool(stamp) and stamp[:10] < cutoff


def _current_open_rows(rows, admission_date: str | None):
    """Pendências abertas DESTA internação. A que ficou aberta de uma
    internação anterior não casa com pedido novo nem é fechada pelo detector
    com evolução de agora (3ª revisão: o pedido de agosto virava evidência
    do item de março, e uma evolução anterior ao pedido de agosto o
    fechava). Ela vai uma vez para verificação no backfill."""
    cutoff = pre_admission_cutoff_iso(admission_date)
    return [row for row in rows if not _from_previous_admission(row, cutoff)]


# Cópia de evolução: a mesma frase aparece em dias seguidos. Pedido igual que
# volta depois de dias sem aparecer é pedido NOVO (2ª revisão adversarial).
COPY_FORWARD_MAX_GAP = timedelta(days=3)


def _new_request_after(session: DetectorSession, quote: str | None, boundary_iso: str | None, *,
                       by_sentence: bool = False, until_iso: str | None = None) -> str | None:
    """O trecho volta a aparecer depois de `boundary` (horário local) como
    pedido NOVO? Devolve a data da primeira evolução nova com ele; None se
    não volta, ou se volta só como cópia. Cópia = estava nas evoluções logo
    antes e continuou sendo copiado: toda evolução MÉDICA entre a última
    aparição antes e a primeira depois também o tem (3ª revisão: o mesmo
    pedido escrito de novo 2 dias depois da resposta, com uma evolução
    médica sem ele no meio, era descartado como cópia)."""
    if not quote or not boundary_iso:
        return None
    notes = session.notes_with_sentence(quote) if by_sentence else session.notes_containing(quote)
    # `until_iso`: só as aparições até a ocorrência que se quer classificar
    # (ela é cópia ou pedido novo?) -- as de depois são outra pergunta.
    after = [note for note in notes if note.timestamp > boundary_iso and (until_iso is None or note.timestamp <= until_iso)]
    if not after:
        return None
    before = [note for note in notes if note.timestamp <= boundary_iso]
    previous = before[-1].timestamp if before else None
    containing = {id(note) for note in notes}
    physician = sorted((note.timestamp, id(note)) for note in session.notes
                       if note.timestamp and is_physician(note.role))
    # Cada reaparição em ordem, não só a primeira (4ª revisão: uma cópia no
    # dia seguinte à resposta escondia todo pedido igual escrito depois).
    for note in after:
        if previous is None:
            return note.timestamp
        try:
            gap = datetime.fromisoformat(note.timestamp) - datetime.fromisoformat(previous)
        except (ValueError, TypeError):
            return None
        if gap > COPY_FORWARD_MAX_GAP:
            return note.timestamp
        # Só as evoluções DEPOIS do atendimento contam (a própria resposta da
        # especialidade fica no meio e, claro, não repete o pedido).
        window_start = max(previous, boundary_iso)
        low = bisect.bisect_right(physician, (window_start, float("inf")))
        for stamp, note_id in physician[low:]:
            if stamp >= note.timestamp:
                break
            if note_id not in containing:
                return note.timestamp  # uma evolução médica no meio não o copiou: foi escrito de novo
        previous = note.timestamp
    return None


def _latest_closure(all_rows, category: str, alvo: str | None, before_iso: str | None):
    """A pendência encerrada (mesma categoria e alvo) mais recente cujo
    atendimento é anterior a `before_iso`."""
    if not alvo or not before_iso:
        return None
    candidates = [
        (closure, row) for row in all_rows
        if row["status"] in CLOSED_PENDING_STATUSES and row["category"] == category and row["alvo"] == alvo
        and (closure := _closure_local_iso(row)) is not None and closure < before_iso
    ]
    return max(candidates, key=lambda pair: pair[0]) if candidates else None


def _copy_of_handled_request(session: DetectorSession, all_rows, category: str, alvo: str | None, occurrence) -> bool:
    """Ocorrência de pedido (regra) que é cópia de um pedido JÁ atendido: a
    mesma frase já estava nas evoluções antes do atendimento e continuou
    sendo copiada depois dele. Compara a FRASE inteira, de qualquer tamanho
    (3ª revisão: "Peço ECO." curto escapava)."""
    request_iso, sentence = occurrence
    latest = _latest_closure(all_rows, category, alvo, request_iso)
    if latest is None:
        return False
    closure_iso, _row = latest
    notes = session.notes_with_sentence(sentence)
    if not any(note.timestamp <= closure_iso for note in notes):
        return False  # frase nova depois do atendimento: pedido novo
    return _new_request_after(session, sentence, closure_iso, by_sentence=True, until_iso=request_iso) is None


def _backfill_targets(repo: Repository, patient_id: str, admission_date: str | None = None):
    """Pendência aberta gravada antes do PEND-001 (ou sem alvo reconhecível
    na época) ganha o `alvo` que dá pra deduzir da descrição (e, pra exame,
    da evidência, quando a região é inequívoca) -- é o que deixa o detector
    fechá-la e a regra/IA casar com ela. Interconsulta criada pela regra
    ANTIGA sem especialidade reconhecível ("Aguarda avaliação de quadro", 51
    de 78 no banco de dev) nunca teria como ser fechada nem casada: vai pra
    "Em verificação pelo auditor", sem sumir. Pendência que ficou aberta de
    uma internação ANTERIOR também vai pra verificação (3ª revisão)."""
    rows = repo.get_active_pending_items(patient_id)
    filled = moved = previous = 0
    cutoff = pre_admission_cutoff_iso(admission_date)
    for row in rows:
        if _from_previous_admission(row, cutoff):
            previous += int(repo.set_pending_in_review(
                row["pending_id"], actor="SISTEMA",
                note="Pendência aberta de uma internação anterior do mesmo prontuário -- confira se ainda vale.",
            ))
            continue
        if row["alvo"]:
            continue
        if row["source"] == "LLM" or row["category"] == CATEGORY_DIAGNOSTICO:
            evidence = row["evidence"]
        else:
            evidence = None
        target = normalize_target(row["category"], None, row["description"], evidence)
        old_rule_consult = row["source"] == "RULE" and row["category"] == CATEGORY_INTERCONSULTA
        if target and not (old_rule_consult and not is_canonical_target(row["category"], target)):
            repo.set_pending_alvo(row["pending_id"], target)
            filled += 1
        elif old_rule_consult:
            moved += int(repo.set_pending_in_review(
                row["pending_id"], actor="SISTEMA",
                note="Criada por regra de versão anterior sem especialidade reconhecível -- confira se ainda vale.",
            ))
    if filled or moved or previous:
        logger.info("PEND-001: migração -- %d alvo(s) preenchido(s), %d interconsulta(s) antiga(s) em verificação, "
                    "%d de internação anterior em verificação, paciente %s",
                    filled, moved, previous, pseudonym.for_log(patient_id))
        _PEND001_TOTALS.add("alvos_preenchidos", filled)
        _PEND001_TOTALS.add("migracao_em_verificacao", moved)
        _PEND001_TOTALS.add("internacao_anterior_em_verificacao", previous)
    return repo.get_active_pending_items(patient_id) if filled or moved or previous else rows


def _admission_notes(repo: Repository, patient_id: str, admission_date: str | None) -> list[DetectorNote]:
    """Todas as evoluções desta internação já gravadas (menos a folga do
    DEC-119 antes da admissão) -- base do detector e da checagem das
    decisões da IA."""
    cutoff_iso = pre_admission_cutoff_iso(admission_date)
    return [
        DetectorNote(timestamp=row["timestamp"], role=row["source_type"], text=row["text"])
        for row in repo.get_notes(patient_id)
        if not (cutoff_iso and row["timestamp"] and row["timestamp"][:10] < cutoff_iso)
    ]


def _close_answered_pending_items(
    repo: Repository, patient_id: str, admission_date: str | None, *, session: DetectorSession | None = None,
) -> int:
    """PEND-001: roda o detector sobre TODAS as evoluções desta internação já
    gravadas (não só a janela nova) e conclui o que tem prova posterior ao
    pedido -- interconsulta avaliada, punção realizada, exame com resultado.
    Nunca derruba o paciente: falha aqui só é registrada no log."""
    try:
        return _close_answered_pending_items_unsafe(repo, patient_id, admission_date, session)
    except Exception:
        logger.exception("PEND-001: falha no detector do paciente %s -- pendências ficam como estão",
                         pseudonym.for_log(patient_id))
        _rollback_quietly(repo)
        return 0


def _close_answered_pending_items_unsafe(
    repo: Repository, patient_id: str, admission_date: str | None, session: DetectorSession | None,
) -> int:
    open_rows = _current_open_rows(repo.get_active_pending_items(patient_id), admission_date)
    if not open_rows:
        return 0
    if session is None:
        session = DetectorSession(_admission_notes(repo, patient_id, admission_date))
    counts = {ACTION_RESOLVE: 0, ACTION_CANCEL: 0, ACTION_REVIEW: 0}
    for found in session.detect(open_rows):
        if found.action == ACTION_RESOLVE:
            changed = repo.resolve_pending_item(
                found.pending_id, source=RESOLUTION_DETECTOR, evidence=found.evidence,
                evidence_date=found.evidence_date, resolved_at=found.resolved_at, note=found.note,
            )
        elif found.action == ACTION_CANCEL:
            changed = repo.cancel_pending_item(
                found.pending_id, source=RESOLUTION_DETECTOR, evidence=found.evidence,
                evidence_date=found.evidence_date, resolved_at=found.resolved_at, note=found.note,
            )
        else:
            changed = repo.set_pending_in_review(
                found.pending_id, actor=RESOLUTION_DETECTOR, note=found.note,
                evidence=found.evidence, evidence_date=found.evidence_date,
            )
        counts[found.action] += int(changed)
    if any(counts.values()):
        logger.info(
            "PEND-001: detector -- %d concluída(s), %d cancelada(s), %d em verificação, paciente %s",
            counts[ACTION_RESOLVE], counts[ACTION_CANCEL], counts[ACTION_REVIEW], pseudonym.for_log(patient_id),
        )
        _PEND001_TOTALS.add("detector_concluidas", counts[ACTION_RESOLVE])
        _PEND001_TOTALS.add("detector_canceladas", counts[ACTION_CANCEL])
        _PEND001_TOTALS.add("detector_em_verificacao", counts[ACTION_REVIEW])
    return counts[ACTION_RESOLVE] + counts[ACTION_CANCEL]


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
    # TODAS as pendências abertas (regra + LLM, ativas e em verificação).
    # PEND-001 (contrato v2): cada uma ganha um código (P1, P2...) e a IA
    # devolve uma decisão por código.
    # Pendência aberta de internação ANTERIOR fica fora (está em verificação
    # para o auditor; nem a IA nem o detector decidem por ela -- 3ª revisão).
    # A Fase 1 já a moveu; repetir aqui é idempotente e cobre a análise que
    # roda sem a Fase 1 antes.
    patient_row = repo.get_patient_by_record_number(patient_id)
    admission_date = patient_row["admission_date"] if patient_row is not None else None
    open_rows = _current_open_rows(_backfill_targets(repo, patient_id, admission_date), admission_date)
    rows_by_ref = {f"P{index}": row for index, row in enumerate(_registered_for_prompt(open_rows), start=1)}
    active_pending_items = [
        {
            "ref": ref, "category": row["category"], "subcategory": row["subcategory"],
            "description": row["description"], "evidence_date": row["evidence_date"],
            "alvo": row["alvo"], "status": row["status"],
        }
        for ref, row in rows_by_ref.items()
    ]

    # LLM-006 (pedido do auditor, DEC-129, caso 1): unidade e leito do censo
    # entram no prompt como fonte de verdade -- antes o modelo deduzia a
    # localização de evolução antiga ("leito de observação" pra paciente na UTI).
    patient_context = (
        {"unit": patient_row["unit"], "bed": patient_row["bed"], "admission_date": patient_row["admission_date"]}
        if patient_row is not None else None
    )

    # Achado da 2ª revisão do LLM-006: a execução da noite só manda o que é
    # NOVO -- se a evolução médica do dia já foi analisada mais cedo (ou no
    # fim de semana, quando só a enfermagem escreve), a anotação de
    # enfermagem virava a "base de hoje". Junta a evolução médica mais
    # recente desta internação como referência, dentro do mesmo orçamento.
    notes_for_llm = new_notes
    if new_notes and not any(is_physician(n.source_type) for n in new_notes):
        reference = _latest_physician_note(repo, patient_id, new_notes, admission_date)
        if reference is not None:
            notes_for_llm, cut = _limit_to_char_budget(
                _sort_chronologically([reference] + new_notes), MAX_NOTES_CHARS_FOR_LLM,
            )
            window_limited = window_limited or cut

    try:
        analysis = llm.analyze_patient(previous_state, notes_for_llm, active_pending_items, patient_context=patient_context)
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
    # adivinhar algo que já temos de forma confiável, RF-07/RF-08). Bug
    # corrigido no LLM-006: lia `specialty`, que o parser nunca preenche --
    # ficava sempre NULL. Agora: cargo da evolução MÉDICA mais recente.
    last_physician_note = most_recent_note([n for n in notes_for_llm if is_physician(n.source_type)])
    # DEC-119: o contrato pede "AAAA-MM-DD", mas o modelo às vezes copia
    # "DD/MM/AAAA" da evolução -- `is_edd_overdue` só lê ISO.
    edd_data = normalize_iso_date(analysis.get("edd_data"))
    edd_status = analysis["edd_status"]
    edd_inferred = False
    if edd_data is None:
        # EDD-001 (pedido do pagador, 2026-09-07): sem data explícita, uma
        # previsão RELATIVA escrita na evolução ("alta em 48h", "alta amanhã")
        # é convertida de forma determinística a partir da data da própria
        # evolução -- nunca pelo modelo -- e gravada como inferida.
        inferred = infer_edd_from_notes(new_notes)
        if inferred:
            edd_data, edd_status, edd_inferred = inferred, "REGISTRADA", True
    repo.save_patient_state(
        patient_id, analysis["clinical_context"], analysis["current_status"],
        especialidade_responsavel=last_physician_note.source_type if last_physician_note else None,
        origem_internacao=analysis.get("origem_internacao"),
        necessidade_hospitalar=analysis["necessidade_hospitalar"],
        necessidade_hospitalar_justificativa=analysis["necessidade_hospitalar_justificativa"],
        objetivo_terapeutico=analysis["objetivo_terapeutico"],
        proximo_passo=analysis["proximo_passo"],
        edd_data=edd_data,
        edd_status=edd_status,
        edd_inferred=edd_inferred,
        dia_classificacao=analysis["dia_classificacao"],
        dia_causa=analysis.get("dia_causa"),
        model_version=getattr(llm, "model_version", None),
        analysis_window_limited=window_limited,
    )

    # PEND-001 (contrato v2, DEC-129 seção 3): primeiro as DECISÕES da IA
    # sobre o que já estava registrado (de qualquer fonte), depois as
    # pendências novas, casadas por (categoria, alvo).
    # Trecho citado pela IA só vale se estiver nas evoluções que ela recebeu
    # (achado da avaliação real: ela copiou a linha da lista de pendências do
    # próprio prompt como "evidência"). As evoluções da internação são
    # pré-processadas UMA vez por paciente (2ª revisão: refazer por decisão
    # custava ~26 s em internação longa).
    blob = notes_blob(note.text for note in notes_for_llm)
    admission_date = patient_row["admission_date"] if patient_row is not None else None
    session = DetectorSession(_admission_notes(repo, patient_id, admission_date))
    sent_times = {note.timestamp for note in notes_for_llm if note.timestamp}
    decided_ids = _apply_review_decisions(repo, patient_id, rows_by_ref, analysis, blob, session, open_rows)

    for item in analysis["pending_items"]:
        try:
            _reconcile_new_item(repo, patient_id, item, analysis, blob, session, admission_date, decided_ids, sent_times)
        except Exception:
            logger.exception("PEND-001: pendência da IA ignorada por erro, paciente %s", pseudonym.for_log(patient_id))
            _rollback_quietly(repo)

    # "Sumiu da resposta" deixou de concluir (era a ÚNICA regra de
    # fechamento antes do PEND-001 -- e fechava sem evidência nenhuma).
    # Pendência que a IA não reemitiu nem decidiu fica COMO ESTÁ (plano
    # aprovado, DEC-129 decisão 3: "decisão omitida mantém o estado") -- o
    # prompt v2 pede pra não repetir o que já está registrado, então ausência
    # é o caso comum e não diz nada. Quem fecha: decisão da IA com evidência
    # verificada, o detector ou o auditor.

    # Pendência nova que a IA acabou de registrar pode já ter a prova nas
    # evoluções gravadas (ex.: "aguarda avaliação" de um pedido antigo).
    _close_answered_pending_items(repo, patient_id, admission_date, session=session)


# A IA recebe no máximo isto de pendências registradas por análise (revisão
# adversarial): a lista e a resposta cresciam com a internação e estouravam
# o limite de 1200 tokens da resposta -- o paciente deixava de ser analisado
# toda noite. As que ficam de fora mantêm o estado (decisão omitida).
MAX_REGISTERED_ITEMS_IN_PROMPT = 12


def _registered_for_prompt(open_rows) -> list:
    """Em verificação primeiro (é o que mais precisa de decisão), depois as
    ativas mais recentes."""
    in_review = [row for row in open_rows if row["status"] == PENDING_IN_REVIEW]
    active = sorted((row for row in open_rows if row["status"] != PENDING_IN_REVIEW),
                    key=lambda row: row["created_at"] or "", reverse=True)
    return (in_review + active)[:MAX_REGISTERED_ITEMS_IN_PROMPT]


def _sane_llm_date(iso: str | None, admission_date: str | None) -> str | None:
    """Data devolvida pela IA só vale dentro da internação e até amanhã --
    revisão adversarial: "15/03/1958" (data de nascimento copiada) e
    "31/12/9999" derrubavam a análise do paciente toda noite; data no futuro
    escondia pedidos novos do mesmo alvo até ela passar."""
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    floor = pre_admission_cutoff_iso(admission_date) or "2000-01-01"
    if parsed.date().isoformat() < floor or parsed > datetime.now() + timedelta(days=1):
        return None
    return iso


# Palavras de "cabeça" que não distinguem uma pendência de outra ("CIRURGIA
# ORTOPEDICA" x "CIRURGIA VASCULAR" são duas cirurgias, não uma).
_GENERIC_ALVO_HEADS = {
    "CIRURGIA", "VAGA", "PASSAGEM", "EXAME", "EXAMES", "PROCEDIMENTO", "TRANSFERENCIA", "LEITO", "LIBERACAO",
    "AUTORIZACAO", "RESULTADO", "TERAPIA", "TRATAMENTO", "AJUSTE", "INICIO", "SUSPENSAO", "CONTROLE",
}


_ALVO_ACTIONS = {"INICIO", "SUSPENSAO", "RETIRADA", "TROCA", "LIBERACAO", "AJUSTE"}


# Sinônimos comuns nos alvos de texto livre da IA.
_ALVO_SYNONYMS = {"O2": "OXIGENIO", "OXIGENOTERAPIA": "OXIGENIO", "VM": "VENTILACAO", "VMI": "VENTILACAO",
                  "TQT": "TRAQUEOSTOMIA", "HD": "HEMODIALISE", "ATB": "ANTIBIOTICO"}


def _generic_alvos_compatible(a: str | None, b: str | None) -> bool:
    """Alvos de texto livre da mesma pendência variam entre análises
    ("DESMAME OXIGENIO" x "DESMAME O2", "COLECISTECTOMIA" x "COLECISTECTOMIA
    VIDEOLAPAROSCOPICA"): iguais (com sinônimos) ou um contido no outro.
    "TRANSFUSAO HEMACIAS" x "TRANSFUSAO PLAQUETAS" não -- a 3ª revisão
    mostrou "metade das palavras em comum" juntando pedidos diferentes, e
    fechar um sumia com o outro. Alvo vazio não casa com nada."""
    if not a or not b:
        return False
    if a == b:
        return True
    # Ação diferente é pedido diferente: "LIBERACAO VANCOMICINA" x "SUSPENSAO
    # VANCOMICINA", "INICIO HEMODIALISE" x "SUSPENSAO HEMODIALISE" (4ª revisão).
    if {word for word in a.split() if word in _ALVO_ACTIONS} != {word for word in b.split() if word in _ALVO_ACTIONS}:
        return False
    words_a = {_ALVO_SYNONYMS.get(word, word) for word in a.split() if word not in _GENERIC_ALVO_HEADS}
    words_b = {_ALVO_SYNONYMS.get(word, word) for word in b.split() if word not in _GENERIC_ALVO_HEADS}
    if not words_a or not words_b:
        return False
    return words_a <= words_b or words_b <= words_a


# Reemissão sem trecho verificável de algo encerrado há menos que isto: a IA
# repetindo o pedido original; mais antigo, pode ser pedido novo (verificação).
UNVERIFIED_REEMISSION_WINDOW = timedelta(days=7)


def _unverified_request_date(item, admission_date: str | None, sent_times: set[str]) -> str | None:
    """Data de pedido com trecho que não está em evolução nenhuma: nunca
    antes da evolução mais nova que a IA recebeu -- o pedido está em algum
    ponto do que ela leu, e uma data anterior (a admissão mostrada no prompt,
    por exemplo) deixava o detector fechar o item com evidência ANTERIOR ao
    pedido (2ª revisão adversarial)."""
    ai_iso = _sane_llm_date(normalize_llm_datetime(item.get("evidence_date")), admission_date)
    candidates = [iso for iso in (ai_iso, max(sent_times) if sent_times else None) if iso]
    return max(candidates) if candidates else None


def _reconcile_new_item(
    repo, patient_id, item, analysis, blob, session: DetectorSession, admission_date, decided_ids, sent_times,
) -> None:
    open_rows = _matchable_rows(_current_open_rows(repo.get_active_pending_items(patient_id), admission_date),
                                item.get("subcategory"))
    all_rows = _current_admission_rows(repo.get_patient_pending_items(patient_id), admission_date)
    alvo = normalize_target(item["category"], item.get("alvo"), item["description"], item["evidence"])
    # Data do pedido = a da evolução MAIS ANTIGA que contém o trecho citado
    # (revisão adversarial: data errada da IA deixava evolução ANTERIOR ao
    # pedido fechá-lo) -- o que mantém a reemissão do pedido já atendido
    # reconhecida como tal. Mas se o trecho volta depois de um atendimento
    # após dias sem aparecer, é pedido NOVO com as mesmas palavras e vale a
    # data da volta (2ª revisão). Sem o trecho nas evoluções: ver
    # `_unverified_request_date`.
    quote_notes = session.notes_containing(item["evidence"])
    sent_quote_notes = [note for note in quote_notes if note.timestamp in sent_times]
    if quote_notes:
        evidence_iso = quote_notes[0].timestamp
        latest_iso = (sent_quote_notes or quote_notes)[-1].timestamp
    else:
        evidence_iso = latest_iso = _unverified_request_date(item, admission_date, sent_times)
    existing = _find_by_target(open_rows, item["category"], alvo)
    if existing is None and not is_canonical_target(item["category"], alvo):
        # Alvo de texto livre: casamento antigo por categoria+subtipo
        # (DEC-064) -- só entre pendências DA IA sem alvo canônico e com alvo
        # compatível. A revisão mostrou a regra antiga "Aguarda avaliação de
        # quadro" engolindo todo pedido sem especialidade reconhecida, e
        # "traqueostomia" virando evidência da colecistectomia.
        candidates = [
            r for r in open_rows
            if r["source"] == "LLM" and not is_canonical_target(r["category"], r["alvo"])
            and _generic_alvos_compatible(r["alvo"], alvo)
        ]
        existing = _find_matching_pending(candidates, item["category"], item.get("subcategory"))
    if existing is not None:
        # REITERAÇÃO (DEC-062): evidência nova acrescentada,
        # evidence_date/description ORIGINAIS preservados -- seção 5 da
        # orientação técnica exige a PRIMEIRA evidência documental.
        if existing["pending_id"] not in decided_ids and quote_in_notes(item["evidence"], blob):
            repo.add_pending_item_evidence(existing["pending_id"], latest_iso, item["evidence"])
            if _reactivate_if_ai_put_in_review(repo, existing, item["evidence"], latest_iso):
                _PEND001_TOTALS.add("ia_reativadas")
        repo.update_pending_item_progress(
            existing["pending_id"], confidence=item.get("confidence"), flow_status=item.get("flow_status")
        )
        return
    item_row = {
        "category": item["category"], "subcategory": item.get("subcategory"), "alvo": alvo,
        "description": item["description"], "evidence": item["evidence"], "source": "LLM",
    }
    review_note = None
    if quote_notes:
        closed = _closed_after(all_rows, item["category"], alvo, evidence_iso)
        if closed is not None:
            # O pedido citado já foi atendido (ou o auditor já decidiu) -- a
            # janela de 14 dias ainda contém o pedido original e a IA tende a
            # reemiti-lo (achado do DEC-129). Só vira pendência se as mesmas
            # palavras voltarem depois do atendimento como pedido novo.
            new_iso = _new_request_after(session, item["evidence"], _closure_local_iso(closed))
            if new_iso is None:
                return
            evidence_iso = new_iso
    elif alvo:
        latest = _latest_closure(all_rows, item["category"], alvo, "9999")
        if latest is not None:
            # Reemissão de algo já encerrado nesta internação, com trecho que
            # não está em evolução nenhuma: a IA repetindo o estado anterior
            # (revisão adversarial -- recriava a interconsulta respondida e
            # ela nunca mais fechava). Encerramento antigo (mais de uma
            # semana antes do que a IA leu) pode ser pedido novo: verificação.
            oldest_sent = min(sent_times) if sent_times else None
            if not oldest_sent or not _older_than(latest[0], oldest_sent, UNVERIFIED_REEMISSION_WINDOW):
                logger.info("PEND-001: reemissão sem trecho verificável ignorada, paciente %s",
                            pseudonym.for_log(patient_id))
                _PEND001_TOTALS.add("ia_reemissoes_ignoradas")
                return
            review_note = ("A IA registrou de novo um pedido já atendido nesta internação, sem trecho verificável "
                           "nas evoluções -- confira se é um pedido novo.")
    if evidence_shows_done(item_row):
        # Avaliação real: "aguardo avaliação da infectologia" citando "Pela
        # INFECTOLOGIA: paciente avaliado." -- a evidência desmente.
        logger.info("PEND-001: pendência da IA desmentida pela própria evidência, paciente %s",
                    pseudonym.for_log(patient_id))
        _PEND001_TOTALS.add("ia_desmentidas_pela_evidencia")
        return
    kind_target = kind_and_target(item_row)
    if kind_target is not None and kind_target[0] == KIND_PUNCTURE and any(
            puncture_already_done(item_row, item["evidence"], note, session.first_seen())
            for note in (sent_quote_notes or quote_notes[-1:])):
        # Plano do PEND-001 e caso 3 do auditor: anotação de enfermagem que
        # registra a punção JÁ FEITA não abre pendência.
        logger.info("PEND-001: pendência de punção já realizada no próprio trecho ignorada, paciente %s",
                    pseudonym.for_log(patient_id))
        _PEND001_TOTALS.add("ia_puncao_ja_feita")
        return
    if quote_notes and latest_iso and evidence_iso and latest_iso > evidence_iso:
        # Pedido que já aparecia antes e continua aparecendo: se o detector
        # o daria por atendido com uma resposta ANTERIOR à última vez que o
        # pedido aparece, e o pedido voltou depois de dias sem aparecer, é
        # pedido novo -- vale a data da volta, e só prova posterior fecha
        # (2ª revisão adversarial: a resposta de dias antes fechava o pedido
        # de hoje).
        decided = session.decide({**item_row, "pending_id": "", "evidence_date": evidence_iso,
                                  "created_at": datetime.now(timezone.utc).isoformat()})
        if decided is not None and decided.action in (ACTION_RESOLVE, ACTION_CANCEL):
            new_iso = _new_request_after(session, item["evidence"], decided.evidence_date)
            if new_iso is not None:
                evidence_iso = new_iso

    hours_elapsed = hours_elapsed_since(evidence_iso)
    priority = compute_priority(item["category"], analysis["necessidade_hospitalar"], hours_elapsed)
    # Achado real (DEC-064): o LLM frequentemente devolve origin=null
    # (campo opcional no contrato) -- sem fallback, a maioria das
    # pendências do LLM nunca mostrava o selo interna/externa no
    # relatório (regras sempre têm origin via RuleFinding.origin, só o
    # caminho do LLM ficava sem default).
    origin = item.get("origin") or default_origin(item["category"])
    pending_id = repo.add_pending_item(
        patient_id, item["category"], item["description"], item["evidence"], evidence_iso,
        subcategory=item.get("subcategory"), origin=origin, priority=priority,
        confidence=item.get("confidence"), is_inferred=bool(item.get("is_inferred")),
        flow_status=item.get("flow_status"), source="LLM", alvo=alvo,
    )
    _PEND001_TOTALS.add("criadas_ia")
    if review_note:
        repo.set_pending_in_review(pending_id, actor="SISTEMA", note=review_note)
        _PEND001_TOTALS.add("ia_em_verificacao")


def _older_than(earlier_iso: str, later_iso: str, window: timedelta) -> bool:
    try:
        return datetime.fromisoformat(later_iso) - datetime.fromisoformat(earlier_iso) > window
    except ValueError:
        return False


# Pedido/espera escrito de novo depois da prova, com o assunto da pendência:
# "Segue aguardando vaga de UTI", "Nova solicitação de vaga de UTI" (4ª revisão).
_WAIT_OR_REQUEST_CUE = re.compile(r"aguard\w*|solicit\w*|\breiter\w*|\bpendente\b|\bpedid[oa]\b|\bcobrar\b")


def _ai_closure_evidence_date(row, quote: str, action: str, session: DetectorSession, open_rows,
                              absorbed=()) -> str | None:
    """Para categorias que o detector NÃO avalia (transferência, terapêutica,
    alta, procedimento que não é punção): data (local) da evolução que
    comprova a decisão CONCLUIDA/CANCELADA da IA, ou None se o trecho não
    comprova. A evolução tem de ser posterior ao pedido; o trecho tem de
    dizer que foi feito (lista positiva, nenhuma recusa/adiamento/falha) --
    ou, para cancelar, o motivo definitivo -- e tem de ser DESTA pendência,
    não de outra aberta do paciente (2ª revisão adversarial: "vaga negada",
    "transferência adiada" e o código trocado entre duas vagas fechavam).
    3ª revisão: nunca uma frase de histórico copiada de antes do pedido
    ("recebido da UTI em 05/08"), e nunca quando a evolução que prova -- ou
    uma posterior -- ainda repete o pedido ("Aguarda vaga de UTI"). 4ª
    revisão: o pedido mais NOVO que a pendência absorveu (`absorbed`, (data,
    texto)) também vale -- prova anterior a ele não fecha -- e a espera escrita
    de outro jeito depois da prova também é conflito."""
    reference = reference_time(row["evidence_date"], row["created_at"])
    if reference is None:
        return None
    for stamp, _text in absorbed:
        newer = reference_time(stamp, None)
        if newer is not None and newer > reference:
            reference = newer
    after = []
    for note in session.notes_containing(quote):
        try:
            if datetime.fromisoformat(note.timestamp) > reference:
                after.append(note)
            else:
                return None  # o trecho já estava antes do pedido: cópia, não prova
        except (ValueError, TypeError):
            continue
    if not after:
        return None
    folded_quote = fold(quote)
    try:
        proving_time = datetime.fromisoformat(after[0].timestamp)
    except (ValueError, TypeError):
        return None
    if is_history(folded_quote) or _cites_date_before_iso(folded_quote, proving_time, reference):
        return None
    requests = list(dict.fromkeys(text for text in (row["evidence"], *(text for _stamp, text in absorbed)) if text))
    later = [note for note in session.notes if note.timestamp and note.timestamp >= after[0].timestamp]
    later_blobs = [notes_blob([note.text]) for note in later]
    if any(quote_in_notes(request, blob) for blob in later_blobs for request in requests):
        return None  # a evolução da prova (ou outra depois) ainda repete o pedido: informação conflitante
    if _waits_again(row, quote, later, lost_too=action == ACTION_RESOLVE):
        return None
    if action == ACTION_RESOLVE:
        confirmed = quote_confirms_done(quote, row)
    else:
        confirmed = quote_confirms_cancel(quote, row)
    if not confirmed or not quote_is_about_item(quote, row, open_rows):
        return None
    return after[0].timestamp


# A coisa provada se perdeu depois da prova: "vaga de UTI cancelada pela
# regulação", "vaga perdida", "transporte suspenso" (5ª revisão).
_LOST_CUE = re.compile(
    r"\bcancelad\w*|\bcancelou\b|\bnegad\w*|\bperdid\w*|\bsuspens\w*|\brecusad\w*|\bindeferid\w*|\bsem\s+vaga\b"
)


def _waits_again(row, quote: str, later_notes, *, lost_too: bool = False) -> bool:
    """Da evolução da prova em diante, uma frase (que não é o próprio trecho)
    volta a esperar/pedir com as palavras PRÓPRIAS do alvo ("aguarda leito de
    UTI", "aguarda transferência para UTI" -- sem exigir a cabeça "vaga"), ou
    diz que a coisa provada se perdeu (`lost_too`, para conclusão)."""
    core = alvo_core_words(row)
    head = {word for word in re.findall(r"[a-z0-9]+", fold(row["alvo"])) if len(word) >= 3} - core
    if not core and not head:
        return False
    quoted = notes_blob([quote])
    for note in later_notes:
        for sentence in re.split(r"[.;\n]", note.text or ""):
            folded = fold(sentence)
            if quote_in_notes(sentence, quoted):
                continue
            words = set(re.findall(r"[a-z0-9]+", folded))
            if core and _WAIT_OR_REQUEST_CUE.search(folded) and core <= words:
                return True
            if lost_too and _LOST_CUE.search(folded) and (words & core or words & head):
                return True
    return False


def _cites_date_before_iso(folded: str, note_time: datetime, reference: datetime) -> bool:
    return any(cited < reference.date() for cited in cited_dates(folded, note_time.date()))


def _apply_review_decisions(
    repo: Repository, patient_id: str, rows_by_ref: dict, analysis: dict, blob: str, session: DetectorSession,
    open_rows,
) -> set[str]:
    """Aplica `revisao_pendencias` (contrato v2). Devolve os ids decididos.
    Código desconhecido ou repetido é ignorado; decisão omitida mantém o
    estado. CONCLUIDA/CANCELADA chegam aqui sempre com evidência (sem ela,
    `normalize_review_decisions` já rebaixou pra INCERTA) -- e só valem se o
    trecho estiver nas evoluções enviadas E comprovar o encerramento; senão
    viram "em verificação" (o auditor confere, nada fecha com evidência que
    não dá pra verificar). A data gravada é sempre a da evolução citada,
    nunca a que a IA escreveu."""
    decided: set[str] = set()
    outcomes: dict[str, int] = {}
    for decision in normalize_review_decisions(analysis.get(REVIEW_FIELD)):
        row = rows_by_ref.get(decision["ref"])
        if row is None or row["pending_id"] in decided:
            continue
        decided.add(row["pending_id"])
        try:
            outcome = _apply_one_decision(repo, row, decision, blob, session, open_rows)
        except Exception:
            logger.exception("PEND-001: decisão da IA ignorada por erro, pendência mantida")
            _rollback_quietly(repo)
            continue
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            _PEND001_TOTALS.add(outcome)
    if outcomes:
        logger.info("PEND-001: decisões da IA -- %s, paciente %s",
                    ", ".join(f"{key}={value}" for key, value in sorted(outcomes.items())),
                    pseudonym.for_log(patient_id))
    return decided


def _apply_one_decision(
    repo: Repository, row, decision: dict, blob: str, session: DetectorSession, open_rows,
) -> str | None:
    """Aplica uma decisão; devolve o que de fato mudou (pra contagem no log)."""
    verdict = decision["decisao"]
    evidence = decision["evidence"] if quote_in_notes(decision["evidence"], blob) else None
    quoted = session.notes_containing(evidence) if evidence else []
    evidence_iso = quoted[-1].timestamp if quoted else None
    if verdict in (DECISION_RESOLVED, DECISION_CANCELLED):
        action = ACTION_RESOLVE if verdict == DECISION_RESOLVED else ACTION_CANCEL
        agrees = session.agrees(row, action)
        if agrees:
            # Interconsulta/exame/punção: o detector, olhando a internação
            # inteira (com a checagem de conflito), prova o mesmo -- ele
            # encerra no fim desta análise, com a evidência dele.
            return "ia_confirmada_pelo_detector"
        closure_iso = None
        if agrees is None and evidence:
            absorbed = [(item["timestamp"], item["text"]) for item in repo.get_pending_item_evidence(row["pending_id"])]
            closure_iso = _ai_closure_evidence_date(row, evidence, action, session, open_rows, absorbed)
        if closure_iso is None:
            # Item já em verificação fica como está (quem pôs continua
            # registrado -- a IA não tira do detector o que ele segurou).
            changed = repo.set_pending_in_review(
                row["pending_id"], actor=RESOLUTION_LLM, evidence=evidence,
                note="A IA indicou que foi encerrada, mas o trecho citado não comprova -- confira.",
            )
            return "ia_em_verificacao" if changed else None
        if verdict == DECISION_RESOLVED:
            changed = repo.resolve_pending_item(
                row["pending_id"], source=RESOLUTION_LLM, evidence=evidence, evidence_date=closure_iso,
                resolved_at=resolution_time_utc(closure_iso, row["created_at"]),
                note="Concluída automaticamente pela IA, com o trecho citado como evidência.",
            )
            return "ia_concluidas" if changed else None
        changed = repo.cancel_pending_item(
            row["pending_id"], source=RESOLUTION_LLM, evidence=evidence, evidence_date=closure_iso,
            resolved_at=resolution_time_utc(closure_iso, row["created_at"]),
            note="Cancelada pela IA: a equipe retirou a indicação, conforme o trecho citado.",
        )
        return "ia_canceladas" if changed else None
    if verdict == DECISION_UNCERTAIN:
        changed = repo.set_pending_in_review(
            row["pending_id"], actor=RESOLUTION_LLM, evidence=evidence, evidence_date=evidence_iso,
            note="A IA não encontrou registro suficiente para decidir -- confira.",
        )
        return "ia_em_verificacao" if changed else None
    # MANTIDA
    if evidence:
        repo.add_pending_item_evidence(row["pending_id"], evidence_iso, evidence)
    return "ia_reativadas" if _reactivate_if_ai_put_in_review(repo, row, evidence, evidence_iso) else None


def _reactivate_if_ai_put_in_review(repo: Repository, row, evidence: str | None, evidence_iso: str | None) -> bool:
    """A IA volta a confirmar uma pendência que ELA mesma tinha deixado em
    verificação (incerta, ou encerramento com trecho que não comprovou) ->
    ativa de novo. Em verificação por
    outro motivo (detector achou "exame realizado" sem laudo; regra antiga
    sem especialidade) fica como está -- senão alternaria a cada execução."""
    if row["status"] != PENDING_IN_REVIEW or repo.last_review_actor(row["pending_id"]) != RESOLUTION_LLM:
        return False
    return repo.reactivate_pending_item(
        row["pending_id"], actor=RESOLUTION_LLM, evidence=evidence, evidence_date=evidence_iso,
        note="A IA voltou a confirmar que a pendência segue aberta.",
    )
