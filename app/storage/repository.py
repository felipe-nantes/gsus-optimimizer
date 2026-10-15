"""Única camada de acesso a dados. Nenhum outro módulo abre conexão SQLite
diretamente nem monta SQL fora daqui (QUEUE-001 / ROBUST-001 / INCR-001).
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import Note, Patient
from app.security import pseudonym

logger = logging.getLogger(__name__)

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETED = "COMPLETED"
RUN_STATUS_FAILED = "FAILED"
# UI-006 (2026-09-04): encerrada pelo usuário no meio (botão "Encerrar") --
# Fase 1 parou entre um paciente e outro, nada ficou meio-gravado.
RUN_STATUS_CANCELLED = "CANCELLED"
# RESIL-016 (2026-09-08): o processo morreu no meio (máquina desligada,
# encerramento pelo sistema, sessão do terminal fechada) sem nunca escrever
# um status final -- a run seguinte fecha essas linhas ao começar. Achado
# real: 9 runs RUNNING acumuladas em 5 dias, todas de processos interrompidos.
RUN_STATUS_INTERRUPTED = "INTERRUPTED"

QUEUE_PENDING = "PENDING"
QUEUE_PROCESSING = "PROCESSING"
QUEUE_DONE = "DONE"
QUEUE_ERROR = "ERROR"
QUEUE_NO_ADMISSION = "NO_ADMISSION"
# RESIL-015 (2026-09-08): admitido há pouco (hoje/ontem) e o GSUS ainda não
# mostra o card da internação ou nenhuma evolução acessível -- não é falha,
# entra na próxima atualização.
QUEUE_AWAITING_NOTES = "AWAITING_NOTES"

PENDING_ACTIVE = "ACTIVE"
PENDING_RESOLVED = "RESOLVED"
PENDING_UNKNOWN = "UNKNOWN"
# PEND-001 (pedido do auditor, DEC-129 seção 3): "Em verificação pelo auditor"
# -- informação incompleta ou conflitante: a IA disse INCERTA; a IA disse
# concluída/cancelada com trecho que não comprova; o detector achou "exame
# realizado" sem resultado, resposta relatada por outra equipe ou evidência
# posterior conflitante; interconsulta de regra antiga sem especialidade.
# Continua ABERTA (no censo, no relatório), só sinalizada pra conferência.
# Decisão OMITIDA pela IA não muda nada (DEC-129 decisão 3).
PENDING_IN_REVIEW = "IN_REVIEW"
# A equipe retirou a indicação (ex.: "suspensa a interconsulta") -- encerrada,
# mas não conta como resolvida no tempo mediano de resolução.
PENDING_CANCELLED = "CANCELLED"
OPEN_PENDING_STATUSES = (PENDING_ACTIVE, PENDING_IN_REVIEW)
CLOSED_PENDING_STATUSES = (PENDING_RESOLVED, PENDING_CANCELLED)

# Quem encerrou a pendência (`resolution_source`) -- o relatório diferencia
# "Concluída automaticamente" (IA/REGRA/DETECTOR) de "Concluída pelo auditor".
RESOLUTION_LLM = "IA"
RESOLUTION_RULE = "REGRA"
RESOLUTION_DETECTOR = "DETECTOR"
RESOLUTION_AUDITOR = "AUDITOR"
RESOLUTION_LEGACY = "LEGADO"  # encerrada antes do histórico existir (< 1.6.0)

# Tipos de evento do histórico (`pending_item_events`).
EVENT_CREATED = "CRIADA"
EVENT_RESOLVED = "CONCLUIDA"
EVENT_CANCELLED = "CANCELADA"
EVENT_IN_REVIEW = "EM_VERIFICACAO"
EVENT_REACTIVATED = "REATIVADA"

MAX_QUEUE_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class QueueItem:
    run_id: str
    patient_id: str
    status: str
    attempts: int


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ runs
    def start_run(self) -> str:
        run_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO runs (run_id, started_at, status, patients_found, "
            "patients_completed, patients_failed) VALUES (?, ?, ?, 0, 0, 0)",
            (run_id, _now(), RUN_STATUS_RUNNING),
        )
        self.conn.commit()
        logger.info("Run iniciada run_id=%s", run_id)
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        counts = self.get_run_counts(run_id)
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, patients_found = ?, "
            "patients_completed = ?, patients_failed = ?, patients_no_admission = ?, "
            "patients_awaiting_notes = ? WHERE run_id = ?",
            (_now(), status, counts["found"], counts["completed"], counts["failed"],
             counts["no_admission"], counts["awaiting_notes"], run_id),
        )
        self.conn.commit()
        logger.info("Run finalizada run_id=%s status=%s", run_id, status)

    def get_run_counts(self, run_id: str) -> dict:
        row = self.conn.execute(
            "SELECT "
            "  COUNT(*) AS found, "
            "  SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END) AS completed, "
            "  SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS failed, "
            "  SUM(CASE WHEN status = 'NO_ADMISSION' THEN 1 ELSE 0 END) AS no_admission, "
            "  SUM(CASE WHEN status = 'AWAITING_NOTES' THEN 1 ELSE 0 END) AS awaiting_notes "
            "FROM processing_queue WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return {
            "found": row["found"] or 0,
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "no_admission": row["no_admission"] or 0,
            "awaiting_notes": row["awaiting_notes"] or 0,
        }

    # ------------------------------------------------------ diagnóstico (DIAG-001)
    def save_run_diagnostic(self, run_id: str | None, outcome: str, summary: str, needs_attention: bool) -> None:
        """Registra, em linguagem simples, o resultado de UMA tentativa de
        execução (sucesso, sucesso parcial por instabilidade do GSUS, ou
        falha) -- pedido explícito do usuário: o auditor (sem conhecimento
        técnico) precisa saber se um problema foi causado pelo GSUS/rede/
        máquina (não é defeito do programa) ou é algo que precisa de
        suporte de verdade, sem precisar abrir o log técnico.

        `run_id` fica `None` quando a execução nem chegou a criar uma run
        (login ou censo falharam por completo, antes de `start_run()`) --
        esse é justamente o cenário que hoje não deixava nenhum rastro no
        banco (achado da auditoria de catálogo de falhas, DEC-111)."""
        self.conn.execute(
            "INSERT INTO run_diagnostics (diagnostic_id, run_id, created_at, outcome, summary, needs_attention) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), run_id, _now(), outcome, summary, 1 if needs_attention else 0),
        )
        self.conn.commit()
        logger.info("Diagnóstico de execução registrado outcome=%s needs_attention=%s", outcome, needs_attention)

    def annotate_latest_run_diagnostic(self, note: str) -> bool:
        """SCHEDULE-002 (DEC-129): acrescenta uma frase ao diagnóstico mais
        recente -- usado quando a opção "desligar ao terminar" estava ligada
        e o computador NÃO desligou, pra o auditor ver o motivo na faixa da
        tela principal (não só no log técnico). False se não há diagnóstico."""
        row = self.conn.execute(
            "SELECT diagnostic_id, summary FROM run_diagnostics ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE run_diagnostics SET summary = ? WHERE diagnostic_id = ?",
            (f"{row['summary']} {note}".strip(), row["diagnostic_id"]),
        )
        self.conn.commit()
        return True

    def get_latest_run_diagnostic(self) -> dict | None:
        row = self.conn.execute(
            "SELECT run_id, created_at, outcome, summary, needs_attention "
            "FROM run_diagnostics ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "outcome": row["outcome"],
            "summary": row["summary"],
            "needs_attention": bool(row["needs_attention"]),
        }

    def get_error_messages_for_run(self, run_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT last_error FROM processing_queue WHERE run_id = ? AND status = ?",
            (run_id, QUEUE_ERROR),
        ).fetchall()
        return [row["last_error"] for row in rows if row["last_error"]]

    def resume_incomplete_runs(self) -> list[str]:
        """Reclassifica PROCESSING órfão de runs interrompidas como PENDING
        (ou ERROR se já esgotou tentativas). Deve ser chamado no início de
        cada execução, antes de processar a fila (ROBUST-001 / resume)."""
        orphan_runs = [
            row["run_id"]
            for row in self.conn.execute(
                "SELECT DISTINCT run_id FROM processing_queue WHERE status = ?",
                (QUEUE_PROCESSING,),
            ).fetchall()
        ]
        for run_id in orphan_runs:
            rows = self.conn.execute(
                "SELECT patient_id, attempts FROM processing_queue "
                "WHERE run_id = ? AND status = ?",
                (run_id, QUEUE_PROCESSING),
            ).fetchall()
            for row in rows:
                if row["attempts"] >= MAX_QUEUE_ATTEMPTS:
                    self.conn.execute(
                        "UPDATE processing_queue SET status = ?, finished_at = ?, "
                        "last_error = ? WHERE run_id = ? AND patient_id = ?",
                        (QUEUE_ERROR, _now(), "Execução interrompida (máximo de tentativas)", run_id, row["patient_id"]),
                    )
                else:
                    self.conn.execute(
                        "UPDATE processing_queue SET status = ?, started_at = NULL "
                        "WHERE run_id = ? AND patient_id = ?",
                        (QUEUE_PENDING, run_id, row["patient_id"]),
                    )
            logger.warning("Resume: %d item(ns) órfão(s) reclassificado(s) na run %s", len(rows), run_id)
        self._close_abandoned_runs()
        self.conn.commit()
        return orphan_runs

    def _close_abandoned_runs(self) -> int:
        """RESIL-016: toda run ainda RUNNING neste ponto (início de uma execução
        nova, antes de `start_run`, sob o lock de execução única) pertence a
        um processo que já não existe -- marca INTERRUPTED com `finished_at`
        e as contagens que a fila dela realmente alcançou. Os itens PENDING
        dela não são retomados: cada execução monta a própria fila a partir
        do censo do dia (o que faltou entra de novo por lá)."""
        abandoned = self.conn.execute(
            "SELECT run_id FROM runs WHERE status = ?", (RUN_STATUS_RUNNING,)
        ).fetchall()
        for row in abandoned:
            counts = self.get_run_counts(row["run_id"])
            self.conn.execute(
                "UPDATE runs SET status = ?, finished_at = COALESCE(finished_at, ?), "
                "patients_found = ?, patients_completed = ?, patients_failed = ?, "
                "patients_no_admission = ?, patients_awaiting_notes = ? WHERE run_id = ?",
                (
                    RUN_STATUS_INTERRUPTED, _now(), counts["found"], counts["completed"],
                    counts["failed"], counts["no_admission"], counts["awaiting_notes"], row["run_id"],
                ),
            )
        if abandoned:
            logger.warning(
                "%d execução(ões) anterior(es) ficaram como RUNNING (processo interrompido no meio) "
                "-- marcadas como INTERRUPTED.", len(abandoned),
            )
        return len(abandoned)

    # -------------------------------------------------------------- patients
    def upsert_patient(self, patient: Patient) -> str:
        """Identidade estável = record_number (prontuário). Retorna patient_id."""
        patient_id = patient.record_number
        existing = self.conn.execute(
            "SELECT patient_id FROM patients WHERE patient_id = ?", (patient_id,)
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO patients (patient_id, record_number, bed, unit, "
                "admission_date, active, last_seen_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (patient_id, patient.record_number, patient.bed, patient.unit, patient.admission_date, _now()),
            )
        else:
            self.conn.execute(
                "UPDATE patients SET bed = ?, unit = ?, admission_date = ?, "
                "active = 1, last_seen_at = ? WHERE patient_id = ?",
                (patient.bed, patient.unit, patient.admission_date, _now(), patient_id),
            )
        self.conn.commit()
        return patient_id

    def get_patient_by_record_number(self, record_number: str) -> sqlite3.Row | None:
        """`patient_id` == `record_number` por construção (`upsert_patient`) --
        usado por "Localizar Paciente" reformulado (DEC-067): busca local no
        banco, nunca ao vivo no GSUS."""
        return self.conn.execute(
            "SELECT * FROM patients WHERE patient_id = ?", (record_number,)
        ).fetchone()

    def get_all_active_patients(self) -> list[sqlite3.Row]:
        """Identidade básica de TODOS os pacientes ativos no censo mais
        recente -- independente de terem alguma pendência ou análise por IA
        já salva. Usado pelo relatório (achado real E2E-001, 2026-08-27):
        antes disso, `generate_report` só enxergava paciente com pendência
        ativa OU `patient_state` já gravado -- um paciente processado com
        sucesso pela Fase 1 (regras, DEC-071) sem nenhum achado e ainda não
        alcançado pela Fase 2 (IA) não tinha nenhuma das duas coisas, e
        ficava INVISÍVEL no relatório inteiro (censo e individual), mesmo
        ativo e corretamente processado -- grave pra uma ferramenta de
        auditoria (censo incompleto sem aviso nenhum, RF-20)."""
        return self.conn.execute(
            "SELECT patient_id, record_number, bed, unit, admission_date FROM patients WHERE active = 1"
        ).fetchall()

    def get_active_patients_pending_ai_analysis(self) -> list[str]:
        """Paciente ativo com pelo menos uma evolução gravada, mas SEM
        NENHUMA análise de IA bem-sucedida ainda (`patient_state` nunca
        gravado) -- achado real (LLM-004, DEC-090, confirmado pela auditoria
        de resiliência de 2026-08-28): o sinal de "nota nova" é consumido e
        destruído na Fase 1 (hash de `add_note`) antes da Fase 2 sequer
        existir, então um paciente cuja análise falhou (ou cuja vez nunca
        chegou numa Fase 2 interrompida) NUNCA mais entra sozinho em
        `llm_tasks` -- só uma evolução genuinamente nova o resgata, o que
        pode levar dias ou nunca acontecer. `run_once` usa isto para
        recolocar esses pacientes na fila de IA em TODA execução, além dos
        que já entram por nota nova (RULES-001 continua vendo o histórico
        completo de qualquer forma -- isto só afeta a fila da IA).

        RESIL-018 (achado real 2026-09-08): a Fase 2 foi interrompida pelo
        disjuntor de saúde com 40 pacientes na fila -- a maioria JÁ tinha
        `patient_state` de dias anteriores, então o critério antigo (só quem
        nunca foi analisado) não os resgatava: ficavam com análise velha até
        surgir uma evolução genuinamente nova. Agora também entra quem tem
        evolução GRAVADA (`notes.created_at`) depois da última análise
        (`patient_state.last_analysis_at`) -- os dois carimbos vêm de
        `_now()` (UTC), comparáveis como texto."""
        rows = self.conn.execute(
            """
            SELECT p.patient_id
            FROM patients p
            JOIN notes n ON n.patient_id = p.patient_id
            LEFT JOIN patient_state s ON s.patient_id = p.patient_id
            WHERE p.active = 1
            GROUP BY p.patient_id
            HAVING MAX(s.patient_id) IS NULL
                OR MAX(n.created_at) > MAX(COALESCE(s.last_analysis_at, ''))
            """
        ).fetchall()
        return [row["patient_id"] for row in rows]

    def mark_patients_inactive_not_in(self, active_patient_ids: list[str]) -> None:
        """Paciente que saiu do censo (alta/transferência) deixa de ser 'active'.

        Sem filtro por `unit`: achado real 2026-08-24 (DEC-061) -- a conta
        GSUS enxerga várias unidades ao mesmo tempo (o censo em si já é o
        escopo de auditoria, confirmado pelo usuário), e `patients.unit` é o
        texto que o GSUS mostra por paciente, não um valor controlado por
        este app -- filtrar por igualdade de string com isso deixava
        pacientes de fora nunca sendo marcados inativos."""
        placeholders = ",".join("?" for _ in active_patient_ids) or "''"
        query = f"UPDATE patients SET active = 0 WHERE patient_id NOT IN ({placeholders})"
        self.conn.execute(query, tuple(active_patient_ids))
        self.conn.commit()

    # ----------------------------------------------------------------- notes
    def add_note(self, note: Note) -> bool:
        """Insere a evolução se o hash for inédito. Retorna True se NEW_NOTE,
        False se já existia (SKIP) -- ver INCR-001 / prompt mestre seção 18."""
        try:
            self.conn.execute(
                "INSERT INTO notes (note_id, patient_id, source_type, specialty, "
                "timestamp, text, text_hash, processed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    str(uuid.uuid4()),
                    note.patient_id,
                    note.source_type,
                    note.specialty,
                    note.timestamp,
                    note.text,
                    note.text_hash,
                    _now(),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False

    def get_note_days(self, patient_id: str) -> set[str]:
        """Datas (AAAA-MM-DD) que já têm evolução gravada para o paciente.

        Usado pela rotina automática para NÃO reabrir dias já extraídos --
        abrir dia é a operação cara da extração (ver DECISIONS.md DEC-048).
        `timestamp` é gravado em ISO (`AAAA-MM-DDTHH:MM:SS`), então os 10
        primeiros caracteres são a data. Evolução sem timestamp
        reconhecido (`NULL`) é ignorada aqui -- não identifica um dia."""
        rows = self.conn.execute(
            "SELECT DISTINCT substr(timestamp, 1, 10) AS day FROM notes "
            "WHERE patient_id = ? AND timestamp IS NOT NULL",
            (patient_id,),
        ).fetchall()
        return {row["day"] for row in rows if row["day"]}

    def get_notes(self, patient_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notes WHERE patient_id = ? ORDER BY timestamp ASC", (patient_id,)
        ).fetchall()

    def purge_old_notes_for_discharged_patients(self, retention_days: int) -> int:
        """RETENTION-001/DEC-068: apaga o TEXTO BRUTO de evolução (`notes`,
        o grosso do espaço em disco) de paciente com alta (`active=0`) há
        mais de `retention_days` -- usa `last_seen_at` (última vez visto no
        censo, nunca atualizado depois da alta -- `mark_patients_inactive_
        not_in`) como referência de "quando" a alta provavelmente ocorreu.

        NUNCA apaga `patient_state`/`pending_items`/`pending_item_evidence`
        -- o resumo estruturado com citações curtas de evidência é o que a
        seção 17 da orientação técnica precisa pra validação auditor×IA, e
        fica retido indefinidamente. Devolve quantas evoluções foram
        apagadas (só pra log agregado -- nunca conteúdo)."""
        cursor = self.conn.execute(
            "DELETE FROM notes WHERE patient_id IN ("
            "  SELECT patient_id FROM patients"
            "  WHERE active = 0 AND last_seen_at IS NOT NULL"
            "    AND datetime(last_seen_at) < datetime('now', ?)"
            ")",
            (f"-{int(retention_days)} days",),
        )
        self.conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------ queue
    def enqueue_patients(self, run_id: str, patient_ids: list[str]) -> None:
        self.conn.executemany(
            "INSERT INTO processing_queue (run_id, patient_id, status, attempts) "
            "VALUES (?, ?, ?, 0)",
            [(run_id, pid, QUEUE_PENDING) for pid in patient_ids],
        )
        self.conn.commit()

    def next_pending(self, run_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT patient_id FROM processing_queue WHERE run_id = ? AND status = ? "
            "ORDER BY rowid LIMIT 1",
            (run_id, QUEUE_PENDING),
        ).fetchone()
        return row["patient_id"] if row else None

    def mark_processing(self, run_id: str, patient_id: str) -> None:
        self.conn.execute(
            "UPDATE processing_queue SET status = ?, started_at = ?, "
            "attempts = attempts + 1 WHERE run_id = ? AND patient_id = ?",
            (QUEUE_PROCESSING, _now(), run_id, patient_id),
        )
        self.conn.commit()

    def mark_done(self, run_id: str, patient_id: str) -> None:
        self.conn.execute(
            "UPDATE processing_queue SET status = ?, finished_at = ? "
            "WHERE run_id = ? AND patient_id = ?",
            (QUEUE_DONE, _now(), run_id, patient_id),
        )
        self.conn.commit()

    def mark_error(self, run_id: str, patient_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE processing_queue SET status = ?, finished_at = ?, last_error = ? "
            "WHERE run_id = ? AND patient_id = ?",
            (QUEUE_ERROR, _now(), error[:500], run_id, patient_id),
        )
        self.conn.commit()
        logger.error("Falha ao processar paciente %s run_id=%s: %s", pseudonym.for_log(patient_id), run_id, error)

    def mark_no_admission(self, run_id: str, patient_id: str) -> None:
        """Paciente sem internação atual na tela (só episódios antigos) --
        não é falha técnica, categoria separada no relatório (decisão do
        usuário, ver DECISIONS.md DEC-054). `logger.info`, não `.error`:
        não é um problema a investigar."""
        self.conn.execute(
            "UPDATE processing_queue SET status = ?, finished_at = ? "
            "WHERE run_id = ? AND patient_id = ?",
            (QUEUE_NO_ADMISSION, _now(), run_id, patient_id),
        )
        self.conn.commit()
        logger.info("Paciente %s sem internação atual -- provável alta recente.", pseudonym.for_log(patient_id))

    def get_failed_patients(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT q.patient_id, q.last_error, p.record_number, p.bed, p.unit "
            "FROM processing_queue q JOIN patients p ON p.patient_id = q.patient_id "
            "WHERE q.run_id = ? AND q.status = ?",
            (run_id, QUEUE_ERROR),
        ).fetchall()

    def get_no_admission_patients(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT q.patient_id, p.record_number, p.bed, p.unit "
            "FROM processing_queue q JOIN patients p ON p.patient_id = q.patient_id "
            "WHERE q.run_id = ? AND q.status = ?",
            (run_id, QUEUE_NO_ADMISSION),
        ).fetchall()

    def mark_awaiting_notes(self, run_id: str, patient_id: str) -> None:
        """RESIL-015: admitido há pouco, card/evolução ainda não acessível no
        GSUS -- categoria benigna, separada de ERROR e de NO_ADMISSION.
        `logger.info`: não é um problema a investigar."""
        self.conn.execute(
            "UPDATE processing_queue SET status = ?, finished_at = ? "
            "WHERE run_id = ? AND patient_id = ?",
            (QUEUE_AWAITING_NOTES, _now(), run_id, patient_id),
        )
        self.conn.commit()
        logger.info(
            "Paciente %s admitido há pouco, ainda sem evolução acessível -- entra na próxima atualização.",
            pseudonym.for_log(patient_id),
        )

    def get_awaiting_notes_patients(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT q.patient_id, p.record_number, p.bed, p.unit, p.admission_date "
            "FROM processing_queue q JOIN patients p ON p.patient_id = q.patient_id "
            "WHERE q.run_id = ? AND q.status = ?",
            (run_id, QUEUE_AWAITING_NOTES),
        ).fetchall()

    # ------------------------------------------------------------- state/IA
    def get_patient_state(self, patient_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM patient_state WHERE patient_id = ?", (patient_id,)
        ).fetchone()

    def save_patient_state(
        self,
        patient_id: str,
        clinical_context: str,
        current_status: str,
        *,
        especialidade_responsavel: str | None = None,
        origem_internacao: str | None = None,
        necessidade_hospitalar: str | None = None,
        necessidade_hospitalar_justificativa: str | None = None,
        objetivo_terapeutico: str | None = None,
        proximo_passo: str | None = None,
        edd_data: str | None = None,
        edd_status: str | None = None,
        dia_classificacao: str | None = None,
        dia_causa: str | None = None,
        model_version: str | None = None,
        analysis_window_limited: bool = False,
        edd_inferred: bool = False,
    ) -> None:
        """Campos a partir de `especialidade_responsavel` são do modelo de
        auditoria concorrente (RF-20 a RF-28, DEC-057) -- todos opcionais
        para não quebrar chamadores que só têm contexto/status básico.

        `especialidade_responsavel`/`origem_internacao`/`model_version`/
        `edd_data`/`edd_status` usam `COALESCE(excluded.x, patient_state.x)`:
        são fatos que, uma vez conhecidos, devem persistir. `edd_data`
        entrou aqui na revisão de conformidade (DEC-064) -- tinha a MESMA
        razão de ser do `origem_internacao` (só é mencionado explicitamente
        de novo às vezes; a maioria das evoluções incrementais não repete a
        data prevista de alta) mas ficava de fora do COALESCE por
        inconsistência, apagando uma EDD real já confirmada sempre que uma
        análise seguinte não a repetisse -- exatamente o texto que
        `app/analysis/schemas.py` já dizia (incorretamente) que este método
        fazia. `_format_edd` (html_report.py) já recalcula "VENCIDA" na
        leitura a partir da data preservada, então manter `edd_status`
        também "pegajoso" continua correto. Os demais campos (ex.:
        `necessidade_hospitalar`) representam a situação de HOJE e devem
        sempre refletir a análise mais recente, nunca um valor antigo."""
        self.conn.execute(
            "INSERT INTO patient_state ("
            "  patient_id, clinical_context, current_status, last_analysis_at,"
            "  especialidade_responsavel, origem_internacao,"
            "  necessidade_hospitalar, necessidade_hospitalar_justificativa,"
            "  objetivo_terapeutico, proximo_passo, edd_data, edd_status,"
            "  dia_classificacao, dia_causa, model_version, analysis_window_limited, edd_inferida"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(patient_id) DO UPDATE SET "
            "  clinical_context = excluded.clinical_context,"
            "  current_status = excluded.current_status,"
            "  last_analysis_at = excluded.last_analysis_at,"
            # Sempre sobrescreve (nunca sticky) -- descreve só a análise de
            # AGORA; se um dia a mesma paciente for reanalisada com backlog
            # pequeno, o aviso deve sumir do relatório.
            "  analysis_window_limited = excluded.analysis_window_limited,"
            "  especialidade_responsavel = COALESCE(excluded.especialidade_responsavel, patient_state.especialidade_responsavel),"
            "  origem_internacao = COALESCE(excluded.origem_internacao, patient_state.origem_internacao),"
            "  necessidade_hospitalar = excluded.necessidade_hospitalar,"
            "  necessidade_hospitalar_justificativa = excluded.necessidade_hospitalar_justificativa,"
            "  objetivo_terapeutico = excluded.objetivo_terapeutico,"
            "  proximo_passo = excluded.proximo_passo,"
            "  edd_data = COALESCE(excluded.edd_data, patient_state.edd_data),"
            # `edd_status` quase nunca vem null do chamador real (campo
            # OBRIGATÓRIO no contrato do LLM -- schemas.py) -- um COALESCE
            # simples nele não fazia nada na prática. O que precisa ficar
            # "pegajoso" junto com `edd_data` preservada é o STATUS que
            # descrevia aquela data (REGISTRADA) -- sem isso, uma análise que
            # não menciona EDD de novo (edd_data=null + edd_status=
            # NAO_REGISTRADA, única combinação válida por RF-27 quando não
            # há data) sobrescrevia REGISTRADA -> NAO_REGISTRADA mesmo com a
            # data preservada, escondendo-a de `_format_edd` (achado real,
            # revisão de conformidade DEC-064). Amarra o status à mesma
            # condição que decide se `edd_data` foi ou não preservada.
            "  edd_status = CASE WHEN excluded.edd_data IS NULL "
            "    THEN COALESCE(patient_state.edd_status, excluded.edd_status) "
            "    ELSE excluded.edd_status END,"
            # EDD-001: a origem (inferida de previsão relativa ou não) acompanha
            # a MESMA regra de preservação da data.
            "  edd_inferida = CASE WHEN excluded.edd_data IS NULL "
            "    THEN patient_state.edd_inferida ELSE excluded.edd_inferida END,"
            "  dia_classificacao = excluded.dia_classificacao,"
            "  dia_causa = excluded.dia_causa,"
            "  model_version = COALESCE(excluded.model_version, patient_state.model_version)",
            (
                patient_id, clinical_context, current_status, _now(),
                especialidade_responsavel, origem_internacao,
                necessidade_hospitalar, necessidade_hospitalar_justificativa,
                objetivo_terapeutico, proximo_passo, edd_data, edd_status,
                dia_classificacao, dia_causa, model_version, int(analysis_window_limited),
                int(edd_inferred),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------ pendencies
    def add_pending_item(
        self,
        patient_id: str,
        category: str,
        description: str,
        evidence: str,
        evidence_date: str | None,
        *,
        subcategory: str | None = None,
        origin: str | None = None,
        priority: str | None = None,
        confidence: str | None = None,
        is_inferred: bool = False,
        flow_status: str | None = None,
        source: str | None = None,
        alvo: str | None = None,
    ) -> str:
        """Campos a partir de `subcategory` são do modelo de auditoria
        concorrente (RF-22 a RF-25, DEC-057) -- todos opcionais para que
        regras determinísticas simples (RULES-001) continuem funcionando
        sem fornecê-los. `priority` é só um SNAPSHOT do momento da inserção
        -- o relatório sempre recalcula na leitura (tempo decorrido muda
        mesmo sem nova execução), ver app/analysis/priority.py.

        `source` ("RULE" ou "LLM", DEC-062; NULL nas gravadas antes disso)
        marca quem CRIOU a pendência. Desde o PEND-001 (DEC-129) as regras
        apenas criam; toda pendência aberta desta internação, de qualquer
        fonte, casa por (categoria, alvo) com as reemissões e é avaliada pelo
        detector; a IA recebe no máximo 12 por análise (em verificação
        primeiro, depois as ativas mais recentes) -- as outras mantêm o
        estado. Quem encerra é o detector, uma decisão da IA com evidência
        verificada ou o auditor. A fonte ainda decide três coisas: a poda do
        DEC-119 (só pendência de REGRA anterior à internação é apagada), a
        interconsulta de regra ANTIGA sem especialidade reconhecível (vai para
        verificação no backfill) e o casamento antigo por categoria+subtipo
        (só entre pendências da IA sem alvo canônico).

        `alvo` (PEND-001) é o que a pendência espera, já normalizado
        (especialidade canônica, exame, procedimento) -- chave de casamento
        entre execuções e entre fontes (regra x IA)."""
        pending_id = str(uuid.uuid4())
        created_at = _now()
        self.conn.execute(
            "INSERT INTO pending_items ("
            "  pending_id, patient_id, category, subcategory, description,"
            "  evidence, evidence_date, origin, priority, confidence, is_inferred,"
            "  flow_status, status, created_at, source, alvo"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pending_id, patient_id, category, subcategory, description,
                evidence, evidence_date, origin, priority, confidence, int(is_inferred),
                flow_status, PENDING_ACTIVE, created_at, source, alvo,
            ),
        )
        self._insert_event(
            pending_id, EVENT_CREATED, None, PENDING_ACTIVE, source or "SISTEMA",
            evidence=evidence, evidence_date=evidence_date, created_at=created_at,
        )
        self.conn.commit()
        return pending_id

    def _insert_event(
        self,
        pending_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        actor: str,
        *,
        evidence: str | None = None,
        evidence_date: str | None = None,
        note: str | None = None,
        created_at: str | None = None,
    ) -> None:
        """Sem commit -- quem chama grava o evento na MESMA transação da
        mudança de estado (nunca estado novo sem histórico, nem o contrário)."""
        self.conn.execute(
            "INSERT INTO pending_item_events (event_id, pending_id, created_at, event_type, "
            "from_status, to_status, actor, evidence, evidence_date, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), pending_id, created_at or _now(), event_type,
                from_status, to_status, actor, evidence, evidence_date, note,
            ),
        )

    def _open_status(self, pending_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM pending_items WHERE pending_id = ?", (pending_id,)
        ).fetchone()
        if row is None or row["status"] not in OPEN_PENDING_STATUSES:
            return None
        return row["status"]

    def add_pending_item_evidence(self, pending_id: str, timestamp: str | None, text: str) -> str | None:
        """Evidência ADICIONAL de uma pendência já registrada (ex.:
        reiteração -- "solicitado em 19/08 e reiterado em 20 e 21/08",
        seção 15 da orientação técnica). A primeira evidência já fica em
        `pending_items.evidence`/`evidence_date`.

        Idempotente (revisão adversarial do PEND-001): a mesma evidência
        (data + texto) não é gravada de novo -- a evolução médica de
        referência é reenviada toda noite só de enfermagem -- e pendência que
        deixou de existir no meio da análise (poda do DEC-119 rodando em
        outra conexão) não derruba o paciente com erro de chave estrangeira.
        Devolve None quando nada foi gravado."""
        evidence_id = str(uuid.uuid4())
        cursor = self.conn.execute(
            "INSERT INTO pending_item_evidence (evidence_id, pending_id, timestamp, text, created_at) "
            "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM pending_items WHERE pending_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM pending_item_evidence WHERE pending_id = ? AND timestamp IS ? AND text = ?) "
            "AND NOT EXISTS (SELECT 1 FROM pending_items WHERE pending_id = ? AND evidence = ? AND evidence_date IS ?)",
            (evidence_id, pending_id, timestamp, text, _now(), pending_id, pending_id, timestamp, text,
             pending_id, text, timestamp),
        )
        self.conn.commit()
        return evidence_id if cursor.rowcount == 1 else None

    def get_pending_item_evidence(self, pending_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pending_item_evidence WHERE pending_id = ? ORDER BY timestamp ASC",
            (pending_id,),
        ).fetchall()

    def get_active_pending_items(self, patient_id: str) -> list[sqlite3.Row]:
        """Pendências ABERTAS do paciente -- ativas e "em verificação pelo
        auditor" (PEND-001): as duas continuam no relatório e no censo, e
        as duas entram na reconciliação da próxima análise."""
        return self.conn.execute(
            "SELECT * FROM pending_items WHERE patient_id = ? AND status IN (?, ?) ORDER BY created_at",
            (patient_id, *OPEN_PENDING_STATUSES),
        ).fetchall()

    def get_patient_pending_items(self, patient_id: str) -> list[sqlite3.Row]:
        """Todas as pendências do paciente, abertas ou encerradas, mais antiga
        primeiro -- o detector de PEND-001 precisa das encerradas pra não
        recriar o que já foi concluído."""
        return self.conn.execute(
            "SELECT * FROM pending_items WHERE patient_id = ? ORDER BY created_at",
            (patient_id,),
        ).fetchall()

    def _close_pending_item(
        self,
        pending_id: str,
        to_status: str,
        event_type: str,
        *,
        source: str | None,
        evidence: str | None,
        evidence_date: str | None,
        resolved_by: str | None,
        resolved_at: str | None,
        note: str | None,
    ) -> bool:
        from_status = self._open_status(pending_id)
        if from_status is None:
            return False
        now = _now()
        cursor = self.conn.execute(
            "UPDATE pending_items SET status = ?, resolved_at = ?, closed_at = ?, resolution_source = ?, "
            "resolution_evidence = ?, resolution_evidence_date = ?, resolved_by = ? "
            "WHERE pending_id = ? AND status = ?",
            (to_status, resolved_at or now, now, source, evidence, evidence_date, resolved_by, pending_id, from_status),
        )
        if cursor.rowcount != 1:
            self.conn.commit()  # encerra a transação implícita: UPDATE sem efeito também segura a trava
            return False
        self._insert_event(
            pending_id, event_type, from_status, to_status, resolved_by or source or "SISTEMA",
            evidence=evidence, evidence_date=evidence_date, note=note,
        )
        self.conn.commit()
        return True

    def resolve_pending_item(
        self,
        pending_id: str,
        *,
        source: str | None = None,
        evidence: str | None = None,
        evidence_date: str | None = None,
        resolved_by: str | None = None,
        resolved_at: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Conclui uma pendência ABERTA (ativa ou em verificação) e registra o
        evento. Devolve False sem tocar em nada se ela já estava encerrada --
        nunca reescreve quem concluiu nem quando (PEND-001).

        `resolved_at` (ISO UTC) é a data da EVIDÊNCIA de conclusão quando ela
        é conhecida (ex.: detector achou a evolução do especialista) -- o
        tempo mediano de resolução fica fiel ao prontuário, não ao horário
        em que a ferramenta rodou. Sem ela, agora."""
        return self._close_pending_item(
            pending_id, PENDING_RESOLVED, EVENT_RESOLVED,
            source=source, evidence=evidence, evidence_date=evidence_date,
            resolved_by=resolved_by, resolved_at=resolved_at, note=note,
        )

    def cancel_pending_item(
        self,
        pending_id: str,
        *,
        source: str | None = None,
        evidence: str | None = None,
        evidence_date: str | None = None,
        resolved_by: str | None = None,
        resolved_at: str | None = None,
        note: str | None = None,
    ) -> bool:
        """A equipe retirou a indicação -- encerrada como CANCELADA (fora do
        tempo mediano de resolução). Mesmas garantias de `resolve_pending_item`."""
        return self._close_pending_item(
            pending_id, PENDING_CANCELLED, EVENT_CANCELLED,
            source=source, evidence=evidence, evidence_date=evidence_date,
            resolved_by=resolved_by, resolved_at=resolved_at, note=note,
        )

    def set_pending_in_review(
        self,
        pending_id: str,
        *,
        actor: str,
        note: str | None = None,
        evidence: str | None = None,
        evidence_date: str | None = None,
    ) -> bool:
        """ATIVA -> EM VERIFICAÇÃO PELO AUDITOR. Só a partir de ATIVA: item já
        em verificação não ganha evento repetido a cada execução."""
        return self._move_status(
            pending_id, PENDING_ACTIVE, PENDING_IN_REVIEW, EVENT_IN_REVIEW, actor,
            evidence=evidence, evidence_date=evidence_date, note=note,
        )

    def _move_status(
        self,
        pending_id: str,
        from_status: str,
        to_status: str,
        event_type: str,
        actor: str,
        *,
        evidence: str | None,
        evidence_date: str | None,
        note: str | None,
    ) -> bool:
        """Troca de estado condicional + evento, numa transação. Achado da
        revisão adversarial do PEND-001: um UPDATE que não muda nada (item
        já em verificação, o caso comum toda noite) ABRE a transação
        implícita do sqlite3 e segura a trava de escrita até o próximo
        commit dessa conexão -- a outra fase da execução e a tela esperavam
        30 s e falhavam com "database is locked". Por isso: lê antes (SELECT
        não trava) e, se mesmo assim o UPDATE não pegar, encerra a transação."""
        current = self.conn.execute(
            "SELECT status FROM pending_items WHERE pending_id = ?", (pending_id,)
        ).fetchone()
        if current is None or current["status"] != from_status:
            return False
        cursor = self.conn.execute(
            "UPDATE pending_items SET status = ? WHERE pending_id = ? AND status = ?",
            (to_status, pending_id, from_status),
        )
        if cursor.rowcount != 1:
            self.conn.commit()
            return False
        self._insert_event(
            pending_id, event_type, from_status, to_status, actor,
            evidence=evidence, evidence_date=evidence_date, note=note,
        )
        self.conn.commit()
        return True

    def reactivate_pending_item(
        self,
        pending_id: str,
        *,
        actor: str,
        evidence: str | None = None,
        evidence_date: str | None = None,
        note: str | None = None,
    ) -> bool:
        """EM VERIFICAÇÃO -> ATIVA, quando uma análise nova volta a confirmar
        que o item segue pendente."""
        return self._move_status(
            pending_id, PENDING_IN_REVIEW, PENDING_ACTIVE, EVENT_REACTIVATED, actor,
            evidence=evidence, evidence_date=evidence_date, note=note,
        )

    def set_pending_alvo(self, pending_id: str, alvo: str) -> None:
        """Preenche o `alvo` de pendência gravada antes do PEND-001 (ou sem
        alvo reconhecível na época) -- só quando ainda está vazio."""
        self.conn.execute(
            "UPDATE pending_items SET alvo = ? WHERE pending_id = ? AND alvo IS NULL",
            (alvo, pending_id),
        )
        self.conn.commit()

    def get_last_review_event(self, pending_id: str) -> sqlite3.Row | None:
        """Último "em verificação" da pendência: quem pôs e por quê (o
        relatório mostra o motivo ao lado do selo)."""
        return self.conn.execute(
            "SELECT * FROM pending_item_events WHERE pending_id = ? AND event_type = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (pending_id, EVENT_IN_REVIEW),
        ).fetchone()

    def last_review_actor(self, pending_id: str) -> str | None:
        """Quem pôs a pendência "em verificação" pela última vez (IA, detector,
        sistema...) -- decide se uma confirmação nova da IA pode reativá-la."""
        row = self.get_last_review_event(pending_id)
        return row["actor"] if row else None

    def get_pending_item_events(self, pending_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pending_item_events WHERE pending_id = ? ORDER BY created_at, rowid",
            (pending_id,),
        ).fetchall()

    def get_recently_closed_pending_items(self, since_iso: str) -> list[sqlite3.Row]:
        """Pendências encerradas (concluídas ou canceladas) a partir de
        `since_iso`, de pacientes ainda internados -- bloco "Concluídas
        recentemente" do relatório (PEND-001, pergunta 3c ao auditor).
        Filtra por `closed_at`, a hora em que o SISTEMA encerrou -- não por
        `resolved_at`, que é a data da evidência: fechamento de hoje com
        evidência de semana passada sumia sem rastro (revisão adversarial)."""
        return self.conn.execute(
            "SELECT pi.*, p.record_number, p.bed, p.unit FROM pending_items pi "
            "JOIN patients p ON p.patient_id = pi.patient_id "
            "WHERE p.active = 1 AND pi.status IN (?, ?) AND pi.closed_at >= ? "
            "ORDER BY p.bed, pi.closed_at DESC",
            (*CLOSED_PENDING_STATUSES, since_iso),
        ).fetchall()

    def update_pending_item_progress(
        self, pending_id: str, *, confidence: str | None = None, flow_status: str | None = None
    ) -> None:
        """Atualiza só o que reflete PROGRESSO real de uma pendência já
        registrada (ex.: interconsulta SOLICITADA -> CONDUTA_DEFINIDA, seção
        4.2 da orientação técnica) -- nunca `description`/`evidence`/
        `evidence_date` originais, que devem continuar apontando pra
        PRIMEIRA evidência documental (seção 5, DEC-062). `COALESCE` evita
        que uma reavaliação que não repetiu o campo apague um valor já
        conhecido."""
        self.conn.execute(
            "UPDATE pending_items SET confidence = COALESCE(?, confidence), "
            "flow_status = COALESCE(?, flow_status) WHERE pending_id = ?",
            (confidence, flow_status, pending_id),
        )
        self.conn.commit()

    def get_all_active_pending_items(self) -> list[sqlite3.Row]:
        """Pendências ABERTAS (ativas e em verificação, PEND-001) de TODOS os
        pacientes ativos, sem filtro por `unit` -- ver
        `mark_patients_inactive_not_in` sobre o motivo (DEC-061). `unit` aqui
        é só texto exibido, nunca um critério de filtro."""
        return self.conn.execute(
            "SELECT pi.*, p.record_number, p.bed, p.unit FROM pending_items pi "
            "JOIN patients p ON p.patient_id = pi.patient_id "
            "WHERE p.active = 1 AND pi.status IN (?, ?) ORDER BY p.bed, pi.created_at",
            OPEN_PENDING_STATUSES,
        ).fetchall()

    def get_resolved_pending_items(self) -> list[sqlite3.Row]:
        """Pendências já RESOLVIDAS, de qualquer paciente (ativo ou com alta
        -- o tempo de resolução de quem já saiu continua sendo dado real
        pro indicador "tempo mediano de resolução", REPORT-003/DEC-099).
        `created_at`/`resolved_at` já existem desde a criação da pendência
        (`add_pending_item`/`resolve_pending_item`) -- aqui só devolve as
        linhas, o cálculo da duração fica em app/reports/dashboard_metrics.py
        (mesma separação SQL-puro/lógica de negócio de `html_report.py`)."""
        return self.conn.execute(
            "SELECT * FROM pending_items WHERE status = ? AND resolved_at IS NOT NULL",
            (PENDING_RESOLVED,),
        ).fetchall()

    # ------------------------------------------------------- daily_snapshot
    def save_daily_snapshot(
        self,
        run_id: str,
        *,
        total_active_patients: int,
        patients_with_active_pending: int,
        patients_without_edd: int,
        patients_with_edd_overdue: int,
        patients_dia_vermelho: int,
        patients_dia_verde: int,
        median_resolution_hours: float | None,
        category_counts: dict[str, dict[str, int]],
    ) -> str:
        """Grava um rollup agregado do serviço (REPORT-003 Fase 2, DEC-099).
        `category_counts` é `{dimension: {label: total}}` -- ex.
        `{"category": {...}, "origin": {...}, "priority": {...},
        "dia_causa": {...}}` -- monta a tabela filha `daily_snapshot_category`
        sem precisar de 4 parâmetros quase idênticos. Chamado UMA vez ao fim
        de cada execução bem-sucedida (app/orchestrator.py), nunca por
        paciente -- é um retrato do serviço inteiro, não individual."""
        snapshot_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO daily_snapshot ("
            "  snapshot_id, run_id, created_at, total_active_patients,"
            "  patients_with_active_pending, patients_without_edd, patients_with_edd_overdue,"
            "  patients_dia_vermelho, patients_dia_verde, median_resolution_hours"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id, run_id, _now(), total_active_patients,
                patients_with_active_pending, patients_without_edd, patients_with_edd_overdue,
                patients_dia_vermelho, patients_dia_verde, median_resolution_hours,
            ),
        )
        for dimension, counts in category_counts.items():
            for label, total in counts.items():
                self.conn.execute(
                    "INSERT INTO daily_snapshot_category (row_id, snapshot_id, dimension, label, total) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), snapshot_id, dimension, label, total),
                )
        self.conn.commit()
        logger.info("Snapshot diário gravado run_id=%s snapshot_id=%s", run_id, snapshot_id)
        return snapshot_id

    def get_daily_snapshots(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Histórico de snapshots em ordem CRONOLÓGICA (mais antigo primeiro
        -- é o que os gráficos de tendência precisam pra plotar da esquerda
        pra direita). `limit` pega só os N mais recentes (ex. "últimos 30
        dias") sem perder a ordem cronológica no resultado devolvido."""
        if limit is None:
            return self.conn.execute("SELECT * FROM daily_snapshot ORDER BY created_at").fetchall()
        recent_desc = self.conn.execute(
            "SELECT * FROM daily_snapshot ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed(recent_desc))

    def get_daily_snapshot_categories(self, snapshot_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM daily_snapshot_category WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchall()
