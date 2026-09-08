from app.storage import database


def test_init_db_creates_all_tables(tmp_path):
    db_path = tmp_path / "auditoria.db"
    conn = database.init_db(db_path)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "runs",
            "patients",
            "notes",
            "processing_queue",
            "patient_state",
            "pending_items",
        }
        assert expected.issubset(tables)
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "auditoria.db"
    conn1 = database.init_db(db_path)
    conn1.close()
    conn2 = database.init_db(db_path)  # não deve levantar erro na segunda vez
    conn2.close()


def test_notes_unique_constraint_prevents_duplicate_hash(tmp_path):
    db_path = tmp_path / "auditoria.db"
    conn = database.init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO patients (patient_id, record_number, bed, unit, active) "
            "VALUES ('p1', '123456', '2A', 'Clínica Médica', 1)"
        )
        conn.execute(
            "INSERT INTO notes (note_id, patient_id, source_type, specialty, timestamp, "
            "text, text_hash, created_at) VALUES "
            "('n1', 'p1', 'evolucao', 'clinica', '2026-08-20T10:00:00', 'texto', 'hash1', '2026-08-20T10:00:00')"
        )
        conn.commit()

        import sqlite3

        try:
            conn.execute(
                "INSERT INTO notes (note_id, patient_id, source_type, specialty, timestamp, "
                "text, text_hash, created_at) VALUES "
                "('n2', 'p1', 'evolucao', 'clinica', '2026-08-20T10:00:00', 'texto', 'hash1', '2026-08-20T10:00:00')"
            )
            conn.commit()
            assert False, "deveria ter levantado IntegrityError para text_hash duplicado"
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()


# ------------------------------------------------------------- DEC-119 (dados já gravados)

def test_migration_normalizes_br_evidence_dates_and_purges_rule_items_before_admission(tmp_path):
    from app.models import Patient
    from app.storage.repository import Repository

    db_path = tmp_path / "auditoria.db"
    conn = database.init_db(db_path)
    repo = Repository(conn)
    patient_id = repo.upsert_patient(Patient(record_number="100", bed="2A", unit="U", admission_date="06/09/2026"))
    llm_item = repo.add_pending_item(
        patient_id, "DIAGNOSTICO", "Aguarda TC", "TC solicitada.", "20/08/2026 08:00", source="LLM",
    )
    repo.add_pending_item_evidence(llm_item, "21/08/2026", "Reiterado.")
    old_rule_item = repo.add_pending_item(
        patient_id, "INTERCONSULTA", "Aguarda parecer", "Parecer.", "2022-02-14T10:00:00", source="RULE",
    )
    repo.add_pending_item_evidence(old_rule_item, "2022-02-15T10:00:00", "Reiterado em 2022.")
    recent_rule_item = repo.add_pending_item(
        patient_id, "DIAGNOSTICO", "Aguarda RM", "RM solicitada.", "2026-09-02T10:00:00", source="RULE",
    )
    conn.commit()
    conn.close()

    conn = database.init_db(db_path)  # migração roda de novo sobre o banco existente
    try:
        rows = {r["pending_id"]: r for r in conn.execute("SELECT pending_id, evidence_date, source FROM pending_items")}
        assert rows[llm_item]["evidence_date"] == "2026-08-20T08:00:00"
        assert old_rule_item not in rows  # evidência de 2022 numa admissão de 2026: lixo removido
        assert recent_rule_item in rows  # 4 dias antes da admissão: dentro da folga, fica
        extra = conn.execute("SELECT timestamp FROM pending_item_evidence WHERE pending_id = ?", (llm_item,)).fetchall()
        assert [r["timestamp"] for r in extra] == ["2026-08-21T00:00:00"]
        leftovers = conn.execute(
            "SELECT COUNT(*) FROM pending_item_evidence WHERE pending_id = ?", (old_rule_item,)
        ).fetchone()[0]
        assert leftovers == 0
    finally:
        conn.close()

    conn = database.init_db(db_path)  # idempotente
    try:
        assert conn.execute("SELECT COUNT(*) FROM pending_items").fetchone()[0] == 2
    finally:
        conn.close()
