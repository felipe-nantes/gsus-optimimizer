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

QUEUE_PENDING = "PENDING"
QUEUE_PROCESSING = "PROCESSING"
QUEUE_DONE = "DONE"
QUEUE_ERROR = "ERROR"
QUEUE_NO_ADMISSION = "NO_ADMISSION"

PENDING_ACTIVE = "ACTIVE"
PENDING_RESOLVED = "RESOLVED"
PENDING_UNKNOWN = "UNKNOWN"

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
            "patients_completed = ?, patients_failed = ?, patients_no_admission = ? WHERE run_id = ?",
            (_now(), status, counts["found"], counts["completed"], counts["failed"],
             counts["no_admission"], run_id),
        )
        self.conn.commit()
        logger.info("Run finalizada run_id=%s status=%s", run_id, status)

    def get_run_counts(self, run_id: str) -> dict:
        row = self.conn.execute(
            "SELECT "
            "  COUNT(*) AS found, "
            "  SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END) AS completed, "
            "  SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS failed, "
            "  SUM(CASE WHEN status = 'NO_ADMISSION' THEN 1 ELSE 0 END) AS no_admission "
            "FROM processing_queue WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return {
            "found": row["found"] or 0,
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "no_admission": row["no_admission"] or 0,
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
        self.conn.commit()
        return orphan_runs

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
        completo de qualquer forma -- isto só afeta a fila da IA)."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT p.patient_id
            FROM patients p
            JOIN notes n ON n.patient_id = p.patient_id
            WHERE p.active = 1
              AND p.patient_id NOT IN (SELECT patient_id FROM patient_state)
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
            "  dia_classificacao, dia_causa, model_version, analysis_window_limited"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
            "  dia_classificacao = excluded.dia_classificacao,"
            "  dia_causa = excluded.dia_causa,"
            "  model_version = COALESCE(excluded.model_version, patient_state.model_version)",
            (
                patient_id, clinical_context, current_status, _now(),
                especialidade_responsavel, origem_internacao,
                necessidade_hospitalar, necessidade_hospitalar_justificativa,
                objetivo_terapeutico, proximo_passo, edd_data, edd_status,
                dia_classificacao, dia_causa, model_version, int(analysis_window_limited),
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
    ) -> str:
        """Campos a partir de `subcategory` são do modelo de auditoria
        concorrente (RF-22 a RF-25, DEC-057) -- todos opcionais para que
        regras determinísticas simples (RULES-001) continuem funcionando
        sem fornecê-los. `priority` é só um SNAPSHOT do momento da inserção
        -- o relatório sempre recalcula na leitura (tempo decorrido muda
        mesmo sem nova execução), ver app/analysis/priority.py.

        `source` ("RULE" ou "LLM", DEC-062) marca quem criou a pendência --
        só pendências "LLM" participam da reconciliação automática em
        `orchestrator._run_llm_analysis` (reiteração/resolução); pendências
        de regra têm ciclo de vida próprio, reavaliadas a cada rodada de
        `apply_all_rules`."""
        pending_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO pending_items ("
            "  pending_id, patient_id, category, subcategory, description,"
            "  evidence, evidence_date, origin, priority, confidence, is_inferred,"
            "  flow_status, status, created_at, source"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pending_id, patient_id, category, subcategory, description,
                evidence, evidence_date, origin, priority, confidence, int(is_inferred),
                flow_status, PENDING_ACTIVE, _now(), source,
            ),
        )
        self.conn.commit()
        return pending_id

    def add_pending_item_evidence(self, pending_id: str, timestamp: str | None, text: str) -> str:
        """Evidência ADICIONAL de uma pendência já registrada (ex.:
        reiteração -- "solicitado em 19/08 e reiterado em 20 e 21/08",
        seção 15 da orientação técnica). A primeira evidência já fica em
        `pending_items.evidence`/`evidence_date`."""
        evidence_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO pending_item_evidence (evidence_id, pending_id, timestamp, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (evidence_id, pending_id, timestamp, text, _now()),
        )
        self.conn.commit()
        return evidence_id

    def get_pending_item_evidence(self, pending_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pending_item_evidence WHERE pending_id = ? ORDER BY timestamp ASC",
            (pending_id,),
        ).fetchall()

    def get_active_pending_items(self, patient_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pending_items WHERE patient_id = ? AND status = ?",
            (patient_id, PENDING_ACTIVE),
        ).fetchall()

    def resolve_pending_item(self, pending_id: str) -> None:
        self.conn.execute(
            "UPDATE pending_items SET status = ?, resolved_at = ? WHERE pending_id = ?",
            (PENDING_RESOLVED, _now(), pending_id),
        )
        self.conn.commit()

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
        """Pendências ativas de TODOS os pacientes ativos, sem filtro por
        `unit` -- ver `mark_patients_inactive_not_in` sobre o motivo (DEC-061).
        `unit` aqui é só texto exibido, nunca um critério de filtro."""
        return self.conn.execute(
            "SELECT pi.*, p.record_number, p.bed, p.unit FROM pending_items pi "
            "JOIN patients p ON p.patient_id = pi.patient_id "
            "WHERE p.active = 1 AND pi.status = ? ORDER BY p.bed",
            (PENDING_ACTIVE,),
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
