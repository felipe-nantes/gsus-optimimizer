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
