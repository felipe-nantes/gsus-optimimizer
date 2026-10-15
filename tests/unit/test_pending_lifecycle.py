"""PEND-001 (pedido do auditor, DEC-129 seções 3 e 5): estados da pendência,
histórico de eventos e migração do banco anterior à 1.6.0. Dados fictícios."""
import sqlite3

import pytest

from app.models import Patient
from app.storage import database
from app.storage.repository import (
    EVENT_CANCELLED, EVENT_CREATED, EVENT_IN_REVIEW, EVENT_REACTIVATED, EVENT_RESOLVED,
    PENDING_ACTIVE, PENDING_CANCELLED, PENDING_IN_REVIEW, PENDING_RESOLVED,
    RESOLUTION_AUDITOR, RESOLUTION_DETECTOR, RESOLUTION_LEGACY, Repository,
)


@pytest.fixture
def repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    yield Repository(conn)
    conn.close()


def _patient(repo, record_number="900001", bed="5A"):
    return repo.upsert_patient(Patient(record_number=record_number, bed=bed, unit="Clínica Médica",
                                       admission_date="2026-08-15"))


def _item(repo, patient_id, **kwargs):
    return repo.add_pending_item(
        patient_id, "INTERCONSULTA", "Aguarda avaliação da cardiologia",
        "Solicito parecer da cardiologia.", "2026-08-20T08:00:00", source="LLM", **kwargs,
    )


def _status(repo, pending_id):
    return repo.conn.execute("SELECT * FROM pending_items WHERE pending_id = ?", (pending_id,)).fetchone()


def test_creation_records_alvo_and_a_created_event(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid, alvo="CARDIOLOGIA")

    assert _status(repo, pending_id)["alvo"] == "CARDIOLOGIA"
    events = repo.get_pending_item_events(pending_id)
    assert [e["event_type"] for e in events] == [EVENT_CREATED]
    assert events[0]["to_status"] == PENDING_ACTIVE
    assert events[0]["actor"] == "LLM"
    assert events[0]["evidence"] == "Solicito parecer da cardiologia."


def test_in_review_stays_open_everywhere(repo):
    """"Em verificação" não some do relatório nem do censo -- continua aberta."""
    pid = _patient(repo)
    pending_id = _item(repo, pid)

    assert repo.set_pending_in_review(pending_id, actor="SISTEMA", note="IA deixou de citar") is True
    assert _status(repo, pending_id)["status"] == PENDING_IN_REVIEW
    assert [r["pending_id"] for r in repo.get_active_pending_items(pid)] == [pending_id]
    assert [r["pending_id"] for r in repo.get_all_active_pending_items()] == [pending_id]


def test_in_review_is_not_repeated_every_run(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    assert repo.set_pending_in_review(pending_id, actor="SISTEMA") is True
    assert repo.set_pending_in_review(pending_id, actor="SISTEMA") is False

    types = [e["event_type"] for e in repo.get_pending_item_events(pending_id)]
    assert types == [EVENT_CREATED, EVENT_IN_REVIEW]


def test_reactivation_only_from_in_review(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    assert repo.reactivate_pending_item(pending_id, actor="IA") is False  # já ativa

    repo.set_pending_in_review(pending_id, actor="SISTEMA")
    assert repo.reactivate_pending_item(pending_id, actor="IA", evidence="Segue aguardando parecer.") is True
    assert _status(repo, pending_id)["status"] == PENDING_ACTIVE
    events = repo.get_pending_item_events(pending_id)
    assert [e["event_type"] for e in events] == [EVENT_CREATED, EVENT_IN_REVIEW, EVENT_REACTIVATED]
    assert events[-1]["from_status"] == PENDING_IN_REVIEW


def test_resolve_records_who_when_and_evidence(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    repo.set_pending_in_review(pending_id, actor="SISTEMA")

    assert repo.resolve_pending_item(
        pending_id, source=RESOLUTION_DETECTOR, evidence="Avaliação da cardiologia: ...",
        evidence_date="2026-08-21T10:00:00", resolved_at="2026-08-21T13:00:00+00:00",
    ) is True

    row = _status(repo, pending_id)
    assert row["status"] == PENDING_RESOLVED
    assert row["resolution_source"] == RESOLUTION_DETECTOR
    assert row["resolution_evidence"] == "Avaliação da cardiologia: ..."
    assert row["resolution_evidence_date"] == "2026-08-21T10:00:00"
    assert row["resolved_at"] == "2026-08-21T13:00:00+00:00"
    last = repo.get_pending_item_events(pending_id)[-1]
    assert (last["event_type"], last["from_status"], last["to_status"], last["actor"]) == (
        EVENT_RESOLVED, PENDING_IN_REVIEW, PENDING_RESOLVED, RESOLUTION_DETECTOR)
    assert repo.get_active_pending_items(pid) == []


def test_closing_twice_never_rewrites_the_first_closure(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    assert repo.resolve_pending_item(pending_id, source=RESOLUTION_DETECTOR) is True
    first = _status(repo, pending_id)

    assert repo.resolve_pending_item(pending_id, source=RESOLUTION_AUDITOR, resolved_by="Auditor X") is False
    assert repo.cancel_pending_item(pending_id, source=RESOLUTION_AUDITOR) is False
    assert repo.set_pending_in_review(pending_id, actor="SISTEMA") is False

    again = _status(repo, pending_id)
    assert dict(again) == dict(first)
    assert len(repo.get_pending_item_events(pending_id)) == 2


def test_cancel_is_closed_but_not_counted_as_resolved(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    assert repo.cancel_pending_item(pending_id, source="IA", evidence="Suspenso o parecer.") is True

    assert _status(repo, pending_id)["status"] == PENDING_CANCELLED
    assert repo.get_active_pending_items(pid) == []
    assert repo.get_resolved_pending_items() == []  # fora do tempo mediano de resolução
    assert repo.get_pending_item_events(pending_id)[-1]["event_type"] == EVENT_CANCELLED


def test_recently_closed_lists_only_active_patients_since_cutoff(repo):
    pid = _patient(repo)
    discharged = _patient(repo, record_number="900002", bed="5B")
    old = _item(repo, pid)
    recent = _item(repo, pid)
    cancelled = _item(repo, pid)
    still_open = _item(repo, pid)
    other = _item(repo, discharged)
    repo.resolve_pending_item(old, source="IA", resolved_at="2026-08-10T12:00:00+00:00")
    repo.conn.execute("UPDATE pending_items SET closed_at = ? WHERE pending_id = ?",
                      ("2026-08-10T12:00:00+00:00", old))
    repo.conn.commit()
    repo.resolve_pending_item(recent, source="IA", resolved_at="2026-08-21T12:00:00+00:00")
    repo.cancel_pending_item(cancelled, source="IA")
    repo.resolve_pending_item(other, source="IA", resolved_at="2026-08-21T12:00:00+00:00")
    repo.conn.execute("UPDATE patients SET active = 0 WHERE patient_id = ?", (discharged,))
    repo.conn.commit()

    ids = {r["pending_id"] for r in repo.get_recently_closed_pending_items("2026-08-20T00:00:00+00:00")}
    assert ids == {recent, cancelled}
    assert still_open not in ids


def test_set_alvo_only_fills_empty(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    repo.set_pending_alvo(pending_id, "CARDIOLOGIA")
    repo.set_pending_alvo(pending_id, "NEFROLOGIA")
    assert _status(repo, pending_id)["alvo"] == "CARDIOLOGIA"


def test_patient_pending_items_include_closed(repo):
    pid = _patient(repo)
    a = _item(repo, pid)
    b = _item(repo, pid)
    repo.resolve_pending_item(a, source="IA")
    assert [r["pending_id"] for r in repo.get_patient_pending_items(pid)] == [a, b]


# ------------------------------------------------------------- migração


_OLD_SCHEMA = """
CREATE TABLE patients (
    patient_id TEXT PRIMARY KEY, record_number TEXT NOT NULL UNIQUE, bed TEXT, unit TEXT,
    admission_date TEXT, active INTEGER NOT NULL DEFAULT 1, last_seen_at TEXT
);
CREATE TABLE pending_items (
    pending_id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    category TEXT NOT NULL, subcategory TEXT, description TEXT NOT NULL, evidence TEXT NOT NULL,
    evidence_date TEXT, origin TEXT, priority TEXT, confidence TEXT,
    is_inferred INTEGER NOT NULL DEFAULT 0, flow_status TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL, resolved_at TEXT, source TEXT
);
INSERT INTO patients VALUES ('p1', '900001', '5A', 'Clínica Médica', '2026-08-15', 1, NULL);
INSERT INTO pending_items (pending_id, patient_id, category, description, evidence, evidence_date,
    status, created_at, resolved_at, source)
VALUES
    ('open-1', 'p1', 'EXAME', 'Aguarda TC', 'Solicitada TC.', '2026-08-20T08:00:00',
     'ACTIVE', '2026-08-20T11:00:00+00:00', NULL, 'LLM'),
    ('done-1', 'p1', 'INTERCONSULTA', 'Aguarda cardio', 'Parecer cardio.', '2026-08-19T08:00:00',
     'RESOLVED', '2026-08-19T11:00:00+00:00', '2026-08-21T11:00:00+00:00', 'LLM');
"""


def test_migration_from_pre_1_6_database_backfills_history(tmp_path):
    db_path = tmp_path / "antigo.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(_OLD_SCHEMA)
    raw.commit()
    raw.close()

    conn = database.init_db(db_path)
    repo = Repository(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(pending_items)")}
    assert {"alvo", "resolution_source", "resolution_evidence", "resolution_evidence_date",
            "resolved_by"} <= columns

    done = _status(repo, "done-1")
    assert done["resolution_source"] == RESOLUTION_LEGACY
    assert _status(repo, "open-1")["resolution_source"] is None

    assert [e["event_type"] for e in repo.get_pending_item_events("open-1")] == [EVENT_CREATED]
    done_events = repo.get_pending_item_events("done-1")
    assert [e["event_type"] for e in done_events] == [EVENT_CREATED, EVENT_RESOLVED]
    assert done_events[0]["created_at"] == "2026-08-19T11:00:00+00:00"
    assert done_events[1]["created_at"] == "2026-08-21T11:00:00+00:00"
    assert done_events[1]["actor"] == RESOLUTION_LEGACY
    conn.close()

    # Idempotente: abrir de novo não duplica nada.
    conn = database.init_db(db_path)
    total = conn.execute("SELECT COUNT(*) FROM pending_item_events").fetchone()[0]
    assert total == 3
    conn.close()


# --------------------------------------- confiabilidade (revisão da Fase 3)


def test_no_op_state_change_never_leaves_a_write_transaction_open(repo):
    """Revisão adversarial: UPDATE sem efeito (item já em verificação, o caso
    comum toda noite) abria a transação implícita e segurava a trava de
    escrita -- a outra fase e a tela davam "database is locked" após 30 s."""
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    assert repo.set_pending_in_review(pending_id, actor="DETECTOR") is True
    assert repo.set_pending_in_review(pending_id, actor="DETECTOR") is False
    assert repo.conn.in_transaction is False
    assert repo.reactivate_pending_item(pending_id, actor="IA") is True
    assert repo.reactivate_pending_item(pending_id, actor="IA") is False
    assert repo.conn.in_transaction is False
    repo.resolve_pending_item(pending_id, source="IA")
    assert repo.resolve_pending_item(pending_id, source="IA") is False
    assert repo.conn.in_transaction is False


def test_init_db_with_nothing_to_backfill_writes_nothing(tmp_path):
    """A tela chama init_db a cada 60 s enquanto a execução escreve: sem
    trabalho, a migração não pode pegar a trava de escrita."""
    db_path = tmp_path / "auditoria.db"
    conn = database.init_db(db_path)
    repo = Repository(conn)
    pid = _patient(repo)
    repo.resolve_pending_item(_item(repo, pid), source="IA")
    conn.close()

    conn = database.get_connection(db_path)
    before = conn.total_changes
    database._backfill_pending_history(conn)
    assert conn.total_changes == before
    assert conn.in_transaction is False
    conn.close()


def test_closed_at_is_when_the_system_closed_not_the_evidence_date(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    repo.resolve_pending_item(pending_id, source="DETECTOR", resolved_at="2026-08-10T12:00:00+00:00")
    row = _status(repo, pending_id)
    assert row["resolved_at"] == "2026-08-10T12:00:00+00:00"
    assert row["closed_at"] > "2026-08-10T12:00:00+00:00"
    ids = {r["pending_id"] for r in repo.get_recently_closed_pending_items("2026-08-20T00:00:00+00:00")}
    assert pending_id in ids  # fechada agora, mesmo com evidência antiga


def test_extra_evidence_is_idempotent_and_safe_for_vanished_items(repo):
    pid = _patient(repo)
    pending_id = _item(repo, pid)
    assert repo.add_pending_item_evidence(pending_id, "2026-08-21T08:00:00", "Segue aguardando.") is not None
    assert repo.add_pending_item_evidence(pending_id, "2026-08-21T08:00:00", "Segue aguardando.") is None
    # A própria evidência original não vira "Evidência 2".
    assert repo.add_pending_item_evidence(pending_id, "2026-08-20T08:00:00", "Solicito parecer da cardiologia.") is None
    assert repo.add_pending_item_evidence("nao-existe", "2026-08-21T08:00:00", "x") is None
    assert len(repo.get_pending_item_evidence(pending_id)) == 1


def test_migration_backfills_closed_at_for_items_closed_before_it_existed(tmp_path):
    db_path = tmp_path / "antigo.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(_OLD_SCHEMA)
    raw.commit()
    raw.close()
    conn = database.init_db(db_path)
    row = conn.execute("SELECT closed_at FROM pending_items WHERE pending_id = 'done-1'").fetchone()
    assert row["closed_at"] == "2026-08-21T11:00:00+00:00"
    conn.close()
