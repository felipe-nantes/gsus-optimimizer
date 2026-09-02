"""Schema e conexão SQLite. Única camada que abre conexão com auditoria.db.

Tabelas conforme PROJECT_SPEC.md secao 6 (RF-11) / prompt mestre secao 17.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id                  TEXT PRIMARY KEY,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    status                  TEXT NOT NULL,
    patients_found          INTEGER NOT NULL DEFAULT 0,
    patients_completed      INTEGER NOT NULL DEFAULT 0,
    patients_failed         INTEGER NOT NULL DEFAULT 0,
    patients_no_admission   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id      TEXT PRIMARY KEY,
    record_number   TEXT NOT NULL UNIQUE,
    bed             TEXT,
    unit            TEXT,
    admission_date  TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    last_seen_at    TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    note_id      TEXT PRIMARY KEY,
    patient_id   TEXT NOT NULL REFERENCES patients(patient_id),
    source_type  TEXT NOT NULL,
    specialty    TEXT,
    timestamp    TEXT,
    text         TEXT NOT NULL,
    text_hash    TEXT NOT NULL,
    processed    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE (patient_id, text_hash)
);

CREATE TABLE IF NOT EXISTS processing_queue (
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    patient_id   TEXT NOT NULL REFERENCES patients(patient_id),
    status       TEXT NOT NULL DEFAULT 'PENDING',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    PRIMARY KEY (run_id, patient_id)
);

-- Campos abaixo de `especialidade_responsavel` em diante: modelo de
-- "registro diário padronizado" da orientação técnica de auditoria
-- concorrente fornecida pelo usuário 2026-08-24 (ver DECISIONS.md DEC-057).
-- Todos nullable -- um paciente sem análise de IA ainda tem linha aqui
-- (contexto/status determinístico), os campos de inferência ficam vazios
-- até a primeira análise do LLM.
CREATE TABLE IF NOT EXISTS patient_state (
    patient_id                              TEXT PRIMARY KEY REFERENCES patients(patient_id),
    clinical_context                        TEXT,
    current_status                          TEXT,
    last_analysis_at                        TEXT,
    especialidade_responsavel               TEXT,
    origem_internacao                       TEXT,
    necessidade_hospitalar                  TEXT,
    necessidade_hospitalar_justificativa    TEXT,
    objetivo_terapeutico                    TEXT,
    proximo_passo                           TEXT,
    edd_data                                TEXT,
    edd_status                              TEXT,
    dia_classificacao                       TEXT,
    dia_causa                               TEXT,
    model_version                           TEXT,
    analysis_window_limited                 INTEGER NOT NULL DEFAULT 0
);

-- `category`/`evidence`/`evidence_date` são a categoria e a evidência
-- PRINCIPAL (primeira) -- mantidos por compatibilidade com o que já existia.
-- Evidências ADICIONAIS (reiterações) vão em `pending_item_evidence`.
-- `priority` é calculada por REGRA (app/analysis/priority.py), nunca pelo
-- LLM livremente -- RF-24.
CREATE TABLE IF NOT EXISTS pending_items (
    pending_id     TEXT PRIMARY KEY,
    patient_id     TEXT NOT NULL REFERENCES patients(patient_id),
    category       TEXT NOT NULL,
    subcategory    TEXT,
    description    TEXT NOT NULL,
    evidence       TEXT NOT NULL,
    evidence_date  TEXT,
    origin         TEXT,
    priority       TEXT,
    confidence     TEXT,
    is_inferred    INTEGER NOT NULL DEFAULT 0,
    flow_status    TEXT,
    status         TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at     TEXT NOT NULL,
    resolved_at    TEXT,
    source         TEXT
);

-- Evidências adicionais de uma pendência já registrada (ex.: "solicitado em
-- 19/08 e reiterado em 20 e 21/08" -- seção 15 da orientação). A primeira
-- evidência continua em `pending_items.evidence`/`evidence_date`.
CREATE TABLE IF NOT EXISTS pending_item_evidence (
    evidence_id  TEXT PRIMARY KEY,
    pending_id   TEXT NOT NULL REFERENCES pending_items(pending_id),
    timestamp    TEXT,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Rollup agregado do serviço, gravado ao fim de cada execução bem-sucedida
-- (REPORT-003 Fase 2, DEC-099) -- fecha a lacuna real do RF-28 ("manter
-- histórico de dias vermelhos por causa"): `patient_state` é UPSERT puro,
-- sobrescrito a cada análise, então sem esta tabela não existe NENHUM jeito
-- de saber como o serviço estava há 1 semana, só como está agora. Aditiva --
-- não toca `patient_state`/`pending_items`.
CREATE TABLE IF NOT EXISTS daily_snapshot (
    snapshot_id                    TEXT PRIMARY KEY,
    run_id                         TEXT NOT NULL REFERENCES runs(run_id),
    created_at                     TEXT NOT NULL,
    total_active_patients          INTEGER NOT NULL,
    patients_with_active_pending   INTEGER NOT NULL,
    patients_without_edd           INTEGER NOT NULL,
    patients_with_edd_overdue      INTEGER NOT NULL,
    patients_dia_vermelho          INTEGER NOT NULL,
    patients_dia_verde             INTEGER NOT NULL,
    median_resolution_hours        REAL
);

-- Detalhe do rollup acima, 1-N. `dimension` distingue o que está sendo
-- contado ('category' | 'origin' | 'priority' | 'dia_causa' -- este último
-- só entre os pacientes classificados VERMELHO no snapshot, é o que
-- historiza a CAUSA dos dias vermelhos exigida pelo RF-28). Uma tabela só
-- pras 4 dimensões evita 4 tabelas quase idênticas para o mesmo propósito.
CREATE TABLE IF NOT EXISTS daily_snapshot_category (
    row_id       TEXT PRIMARY KEY,
    snapshot_id  TEXT NOT NULL REFERENCES daily_snapshot(snapshot_id),
    dimension    TEXT NOT NULL,
    label        TEXT NOT NULL,
    total        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_patient ON notes(patient_id);
CREATE INDEX IF NOT EXISTS idx_queue_run_status ON processing_queue(run_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_patient_status ON pending_items(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_evidence_pending ON pending_item_evidence(pending_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_created ON daily_snapshot(created_at);
CREATE INDEX IF NOT EXISTS idx_snapshot_category_snapshot ON daily_snapshot_category(snapshot_id);
"""

VALID_QUEUE_STATUSES = {"PENDING", "PROCESSING", "DONE", "ERROR", "NO_ADMISSION"}
VALID_PENDING_STATUSES = {"ACTIVE", "RESOLVED", "UNKNOWN"}


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # DEC-109 (revisão adversarial, 2026-09-02): antes da Fase 2 (IA) rodar
    # numa thread própria com sua PRÓPRIA conexão, nunca havia duas conexões
    # escrevendo neste arquivo ao mesmo tempo -- o timeout padrão do módulo
    # `sqlite3` (5s) nunca importava na prática. Agora importa: sob
    # contenção (duas escritas coincidindo por acaso), 5s pode não bastar no
    # hardware fraco confirmado do alvo (DEC-070/092), fazendo uma escrita de
    # bookkeeping (ex.: `finish_run`) levantar `OperationalError: database is
    # locked` -- 30s dá bastante margem sem risco de travar de verdade (WAL
    # só serializa escritores entre si, nunca bloqueia leitores).
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Adiciona coluna nova a um banco JÁ existente -- `CREATE TABLE IF NOT
    EXISTS` só cria a tabela quando ela ainda não existe, não adiciona
    coluna numa tabela criada por uma versão anterior do schema (ver
    DECISIONS.md DEC-054)."""
    runs_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "patients_no_admission" not in runs_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN patients_no_admission INTEGER NOT NULL DEFAULT 0")

    # DEC-057: novos campos do modelo de auditoria concorrente.
    state_new_columns = {
        "especialidade_responsavel": "TEXT", "origem_internacao": "TEXT",
        "necessidade_hospitalar": "TEXT", "necessidade_hospitalar_justificativa": "TEXT",
        "objetivo_terapeutico": "TEXT", "proximo_passo": "TEXT",
        "edd_data": "TEXT", "edd_status": "TEXT",
        "dia_classificacao": "TEXT", "dia_causa": "TEXT", "model_version": "TEXT",
        # DEC-066: true quando o LLM analisou só as últimas LLM_LOOKBACK_DAYS
        # (orchestrator.py) por causa de backlog grande -- relatório precisa
        # avisar que a análise é parcial (achado real, hardware fraco).
        "analysis_window_limited": "INTEGER NOT NULL DEFAULT 0",
    }
    existing_state_columns = {row["name"] for row in conn.execute("PRAGMA table_info(patient_state)")}
    for column, sql_type in state_new_columns.items():
        if column not in existing_state_columns:
            conn.execute(f"ALTER TABLE patient_state ADD COLUMN {column} {sql_type}")

    pending_new_columns = {
        "subcategory": "TEXT", "origin": "TEXT", "priority": "TEXT", "confidence": "TEXT",
        "is_inferred": "INTEGER NOT NULL DEFAULT 0", "flow_status": "TEXT",
        # DEC-062: distingue pendência criada por regra determinística (RULES-001)
        # de pendência do LLM -- sem isso, resolver automaticamente uma pendência
        # do LLM não reconfirmada also resolvia (errado) pendências de regra, que
        # têm ciclo de vida próprio (reavaliadas a cada rodada de apply_all_rules).
        # NULL em linha já existente (banco anterior a este campo) = nunca entra
        # na reconciliação automática -- mais seguro que assumir a origem.
        "source": "TEXT",
    }
    existing_pending_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pending_items)")}
    for column, sql_type in pending_new_columns.items():
        if column not in existing_pending_columns:
            conn.execute(f"ALTER TABLE pending_items ADD COLUMN {column} {sql_type}")
