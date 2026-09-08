import pytest

from app.models import Note, Patient
from app.storage import database
from app.storage.repository import Repository, QUEUE_DONE, QUEUE_ERROR, QUEUE_PENDING


@pytest.fixture
def repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    yield Repository(conn)
    conn.close()


def _sample_patient(record_number="123456", bed="2A", unit="Clínica Médica") -> Patient:
    return Patient(record_number=record_number, bed=bed, unit=unit, admission_date="2026-08-15")


def test_upsert_patient_is_idempotent(repo):
    p1 = repo.upsert_patient(_sample_patient())
    p2 = repo.upsert_patient(_sample_patient(bed="3B"))
    assert p1 == p2

    row = repo.conn.execute("SELECT bed FROM patients WHERE patient_id = ?", (p1,)).fetchone()
    assert row["bed"] == "3B"


def test_add_note_new_then_skip(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    note = Note(patient_id=patient_id, source_type="Evolução", specialty="Clínica Médica",
                timestamp="2026-08-20T08:00:00", text="texto", text_hash="hash-1")

    assert repo.add_note(note) is True  # NEW_NOTE
    assert repo.add_note(note) is False  # SKIP (mesmo hash)


def test_queue_full_cycle_done(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])

    next_patient = repo.next_pending(run_id)
    assert next_patient == patient_id

    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)

    assert repo.next_pending(run_id) is None
    counts = repo.get_run_counts(run_id)
    assert counts == {"found": 1, "completed": 1, "failed": 0, "no_admission": 0, "awaiting_notes": 0}


def test_queue_error_does_not_block_other_patients(repo):
    p1 = repo.upsert_patient(_sample_patient(record_number="111"))
    p2 = repo.upsert_patient(_sample_patient(record_number="222"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [p1, p2])

    repo.mark_processing(run_id, p1)
    repo.mark_error(run_id, p1, "TimeoutError ao abrir prontuário")

    next_patient = repo.next_pending(run_id)
    assert next_patient == p2
    repo.mark_processing(run_id, p2)
    repo.mark_done(run_id, p2)

    counts = repo.get_run_counts(run_id)
    assert counts == {"found": 2, "completed": 1, "failed": 1, "no_admission": 0, "awaiting_notes": 0}

    failed = repo.get_failed_patients(run_id)
    assert len(failed) == 1
    assert failed[0]["patient_id"] == p1


def test_resume_reclassifies_orphan_processing(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)  # simula interrupção: nunca chegou a DONE/ERROR

    repo.resume_incomplete_runs()

    row = repo.conn.execute(
        "SELECT status, attempts FROM processing_queue WHERE run_id = ? AND patient_id = ?",
        (run_id, patient_id),
    ).fetchone()
    assert row["status"] == QUEUE_PENDING
    assert row["attempts"] == 1  # mantém a tentativa já contabilizada


def test_resume_marks_error_after_max_attempts(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_processing(run_id, patient_id)
    repo.mark_processing(run_id, patient_id)  # attempts = 3 (MAX_QUEUE_ATTEMPTS)

    repo.resume_incomplete_runs()

    row = repo.conn.execute(
        "SELECT status FROM processing_queue WHERE run_id = ? AND patient_id = ?",
        (run_id, patient_id),
    ).fetchone()
    assert row["status"] == QUEUE_ERROR


def test_pending_items_lifecycle(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    pending_id = repo.add_pending_item(
        patient_id, "EXAME", "Aguarda tomografia", "Solicitada tomografia...", "2026-08-20T08:00:00"
    )

    active = repo.get_active_pending_items(patient_id)
    assert len(active) == 1
    assert active[0]["pending_id"] == pending_id

    repo.resolve_pending_item(pending_id)
    assert repo.get_active_pending_items(patient_id) == []


def test_get_note_days_returns_distinct_iso_dates(repo):
    """Rotina automática usa isso pra não reabrir dia já processado
    (DEC-048). Dados fictícios."""
    patient_id = repo.upsert_patient(_sample_patient())
    for i, timestamp in enumerate(
        ["2026-08-20T08:00:00", "2026-08-20T14:30:00", "2026-08-21T09:15:00"]
    ):
        repo.add_note(Note(patient_id=patient_id, source_type="Evolução", specialty=None,
                           timestamp=timestamp, text=f"texto {i}", text_hash=f"hash-{i}"))

    assert repo.get_note_days(patient_id) == {"2026-08-20", "2026-08-21"}


def test_get_note_days_ignores_notes_without_timestamp(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    repo.add_note(Note(patient_id=patient_id, source_type="Evolução", specialty=None,
                       timestamp=None, text="sem data", text_hash="hash-sem-data"))

    assert repo.get_note_days(patient_id) == set()


def test_get_note_days_is_per_patient(repo):
    p1 = repo.upsert_patient(_sample_patient())
    p2 = repo.upsert_patient(_sample_patient(record_number="999999"))
    repo.add_note(Note(patient_id=p1, source_type="Evolução", specialty=None,
                       timestamp="2026-08-20T08:00:00", text="a", text_hash="h1"))
    repo.add_note(Note(patient_id=p2, source_type="Evolução", specialty=None,
                       timestamp="2026-08-19T08:00:00", text="b", text_hash="h2"))

    assert repo.get_note_days(p1) == {"2026-08-20"}
    assert repo.get_note_days(p2) == {"2026-08-19"}


def test_error_log_does_not_contain_record_number(repo, caplog):
    """patient_id É o número do prontuário (upsert_patient) -- não pode
    aparecer em texto puro no log (RNF-03 / DEC-049)."""
    import logging

    patient_id = repo.upsert_patient(_sample_patient(record_number="987654"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])

    with caplog.at_level(logging.ERROR):
        repo.mark_error(run_id, patient_id, "falha qualquer")

    assert "987654" not in caplog.text
    assert "pac-" in caplog.text  # pseudônimo presente, log ainda correlacionável


# ------------------------------------------- DEC-061: sem filtro por unit

def test_mark_patients_inactive_not_in_ignores_unit(repo):
    """Achado real 2026-08-24: a conta GSUS enxerga várias unidades ao mesmo
    tempo, e `patients.unit` é texto livre do GSUS por paciente -- nunca um
    valor controlado por este app. Filtrar por igualdade de string com
    `unit` fazia isto nunca marcar ninguém como inativo de verdade."""
    stays = repo.upsert_patient(_sample_patient(record_number="111", unit="4-Internados P.A."))
    leaves = repo.upsert_patient(_sample_patient(record_number="222", unit="ENF. MEDICO-CIRURGICA 2"))

    repo.mark_patients_inactive_not_in([stays])

    row_stays = repo.conn.execute("SELECT active FROM patients WHERE patient_id = ?", (stays,)).fetchone()
    row_leaves = repo.conn.execute("SELECT active FROM patients WHERE patient_id = ?", (leaves,)).fetchone()
    assert row_stays["active"] == 1
    assert row_leaves["active"] == 0


def test_mark_patients_inactive_not_in_empty_list_marks_everyone_inactive(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    repo.mark_patients_inactive_not_in([])
    row = repo.conn.execute("SELECT active FROM patients WHERE patient_id = ?", (patient_id,)).fetchone()
    assert row["active"] == 0


def test_get_all_active_pending_items_ignores_unit_but_respects_active_flag(repo):
    patient_a = repo.upsert_patient(_sample_patient(record_number="111", unit="4-Internados P.A."))
    patient_b = repo.upsert_patient(_sample_patient(record_number="222", unit="ENF. MEDICO-CIRURGICA 2"))
    repo.add_pending_item(patient_a, "DIAGNOSTICO", "d", "evidência a", None)
    repo.add_pending_item(patient_b, "DIAGNOSTICO", "d", "evidência b", None)

    # Duas unidades diferentes, as duas devem aparecer -- sem filtro por unit.
    items = repo.get_all_active_pending_items()
    assert {row["patient_id"] for row in items} == {patient_a, patient_b}

    # Paciente inativo (ex.: teve alta) some da lista, mesmo com pendência aberta.
    repo.mark_patients_inactive_not_in([patient_b])
    items_after = repo.get_all_active_pending_items()
    assert {row["patient_id"] for row in items_after} == {patient_b}


# ------------------------------------------------- save_patient_state (DEC-063)

def test_save_patient_state_preserves_sticky_fields_when_not_repeated(repo):
    """`origem_internacao`/`edd_data`/`edd_status` normalmente só aparecem
    numa nota específica (admissão, ou a que registrou a previsão de alta)
    -- análises incrementais posteriores (RF-08) não têm mais como vê-los.
    Sem COALESCE, a 2ª análise apagaria um dado real já confirmado. `edd_data`
    entrou aqui na revisão de conformidade (DEC-064) -- tinha a mesma razão
    de ser das outras mas ficou de fora do COALESCE por inconsistência."""
    patient_id = repo.upsert_patient(_sample_patient())
    repo.save_patient_state(
        patient_id, "Contexto 1", "Status 1",
        especialidade_responsavel="Clínica Médica", origem_internacao="Emergência",
        model_version="model-v1.gguf", edd_data="2026-08-25", edd_status="REGISTRADA",
    )
    repo.save_patient_state(
        patient_id, "Contexto 2", "Status 2",
        especialidade_responsavel=None, origem_internacao=None, model_version=None,
        edd_data=None, edd_status="NAO_REGISTRADA",
    )

    state = repo.get_patient_state(patient_id)
    assert state["clinical_context"] == "Contexto 2"  # este SEMPRE reflete a análise mais recente
    assert state["especialidade_responsavel"] == "Clínica Médica"
    assert state["origem_internacao"] == "Emergência"
    assert state["model_version"] == "model-v1.gguf"
    assert state["edd_data"] == "2026-08-25"
    assert state["edd_status"] == "REGISTRADA"


def test_save_patient_state_updates_edd_when_repeated(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    repo.save_patient_state(patient_id, "C1", "S1", edd_data="2026-08-25", edd_status="REGISTRADA")
    repo.save_patient_state(patient_id, "C2", "S2", edd_data="2026-08-27", edd_status="REGISTRADA")

    state = repo.get_patient_state(patient_id)
    assert state["edd_data"] == "2026-08-27"


def test_save_patient_state_updates_sticky_fields_when_repeated(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    repo.save_patient_state(patient_id, "Contexto 1", "Status 1", especialidade_responsavel="Clínica Médica")
    repo.save_patient_state(patient_id, "Contexto 2", "Status 2", especialidade_responsavel="Infectologia")

    assert repo.get_patient_state(patient_id)["especialidade_responsavel"] == "Infectologia"


def test_save_patient_state_always_overwrites_necessidade_hospitalar(repo):
    """Ao contrário dos campos "sticky", necessidade_hospitalar reflete a
    situação de HOJE -- nunca deve preservar um valor antigo."""
    patient_id = repo.upsert_patient(_sample_patient())
    repo.save_patient_state(patient_id, "Contexto 1", "Status 1", necessidade_hospitalar="SIM")
    repo.save_patient_state(patient_id, "Contexto 2", "Status 2", necessidade_hospitalar="POSSIVELMENTE_NAO")

    assert repo.get_patient_state(patient_id)["necessidade_hospitalar"] == "POSSIVELMENTE_NAO"


def test_save_patient_state_analysis_window_limited_is_never_sticky(repo):
    """DEC-066: reflete só a análise de AGORA -- se um dia o backlog for
    pequeno de novo, o aviso tem que sumir (ao contrário dos campos sticky
    como origem_internacao)."""
    patient_id = repo.upsert_patient(_sample_patient())
    repo.save_patient_state(patient_id, "C1", "S1", analysis_window_limited=True)
    assert repo.get_patient_state(patient_id)["analysis_window_limited"] == 1

    repo.save_patient_state(patient_id, "C2", "S2", analysis_window_limited=False)
    assert repo.get_patient_state(patient_id)["analysis_window_limited"] == 0


# ------------------------------------- purge_old_notes_for_discharged_patients (DEC-068)

def _set_last_seen_days_ago(repo, patient_id, days_ago):
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    repo.conn.execute("UPDATE patients SET last_seen_at = ? WHERE patient_id = ?", (past, patient_id))
    repo.conn.commit()


def _add_note(repo, patient_id, text_hash="h1"):
    repo.add_note(Note(
        patient_id=patient_id, source_type="Evolução", specialty="Clínica Médica",
        timestamp="2026-08-20T08:00:00", text="texto de evolução", text_hash=text_hash,
    ))


def test_purge_deletes_notes_of_patient_discharged_long_ago(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    _add_note(repo, patient_id)
    repo.mark_patients_inactive_not_in([])  # alta
    _set_last_seen_days_ago(repo, patient_id, 100)

    purged = repo.purge_old_notes_for_discharged_patients(retention_days=90)

    assert purged == 1
    assert repo.get_notes(patient_id) == []


def test_purge_keeps_notes_of_recently_discharged_patient(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    _add_note(repo, patient_id)
    repo.mark_patients_inactive_not_in([])
    _set_last_seen_days_ago(repo, patient_id, 10)

    purged = repo.purge_old_notes_for_discharged_patients(retention_days=90)

    assert purged == 0
    assert len(repo.get_notes(patient_id)) == 1


def test_purge_never_touches_active_patient_regardless_of_age(repo):
    patient_id = repo.upsert_patient(_sample_patient())
    _add_note(repo, patient_id)
    _set_last_seen_days_ago(repo, patient_id, 500)  # "velho", mas nunca teve alta

    purged = repo.purge_old_notes_for_discharged_patients(retention_days=90)

    assert purged == 0
    assert len(repo.get_notes(patient_id)) == 1


def test_purge_never_deletes_structured_summary(repo):
    """O que a seção 17 da orientação técnica precisa pra validação
    auditor×IA (resumo + evidência) nunca é apagado -- só o texto bruto."""
    patient_id = repo.upsert_patient(_sample_patient())
    _add_note(repo, patient_id)
    repo.save_patient_state(patient_id, "Contexto.", "Estável.")
    pending_id = repo.add_pending_item(patient_id, "DIAGNOSTICO", "d", "evidência", None)
    repo.mark_patients_inactive_not_in([])
    _set_last_seen_days_ago(repo, patient_id, 100)

    repo.purge_old_notes_for_discharged_patients(retention_days=90)

    assert repo.get_notes(patient_id) == []
    assert repo.get_patient_state(patient_id) is not None
    assert repo.conn.execute(
        "SELECT 1 FROM pending_items WHERE pending_id = ?", (pending_id,)
    ).fetchone() is not None


# --------------------------------------------------------- daily_snapshot
# REPORT-003 Fase 2 / DEC-099 -- histórico agregado do serviço (RF-28).

def test_save_daily_snapshot_persists_parent_and_children(repo):
    run_id = repo.start_run()

    snapshot_id = repo.save_daily_snapshot(
        run_id,
        total_active_patients=10,
        patients_with_active_pending=4,
        patients_without_edd=3,
        patients_with_edd_overdue=1,
        patients_dia_vermelho=2,
        patients_dia_verde=8,
        median_resolution_hours=12.5,
        category_counts={
            "category": {"DIAGNOSTICO": 3, "TRANSFERENCIA": 1},
            "dia_causa": {"AGUARDA_EXAME": 2},
        },
    )

    snapshots = repo.get_daily_snapshots()
    assert len(snapshots) == 1
    assert snapshots[0]["snapshot_id"] == snapshot_id
    assert snapshots[0]["run_id"] == run_id
    assert snapshots[0]["total_active_patients"] == 10
    assert snapshots[0]["patients_dia_vermelho"] == 2

    categories = repo.get_daily_snapshot_categories(snapshot_id)
    by_dimension_label = {(row["dimension"], row["label"]): row["total"] for row in categories}
    assert by_dimension_label == {
        ("category", "DIAGNOSTICO"): 3,
        ("category", "TRANSFERENCIA"): 1,
        ("dia_causa", "AGUARDA_EXAME"): 2,
    }


def test_get_daily_snapshots_returns_chronological_order(repo):
    """Gráfico de tendência lê da esquerda pra direita -- mais antigo
    primeiro, independente da ordem de inserção real.

    Achado real de auditoria adversarial: a versão anterior deste teste
    inseria run_1 antes de run_2 e conferia `[run_1, run_2]` -- isso passaria
    IGUAL mesmo se `get_daily_snapshots` perdesse o `ORDER BY created_at` e
    devolvesse só a ordem física de inserção (mutante silencioso, nunca
    pego). Aqui a linha inserida PRIMEIRO (run_2) recebe um `created_at`
    FORÇADO a ser mais NOVO que a linha inserida DEPOIS (run_1) -- ordem
    cronológica e ordem de inserção ficam deliberadamente opostas, então só
    uma ordenação real por `created_at` (não a ordem física da tabela)
    produz o resultado esperado."""
    run_1 = repo.start_run()
    run_2 = repo.start_run()

    # run_2 inserido PRIMEIRO (ficaria fisicamente antes de run_1 na tabela).
    snapshot_2 = repo.save_daily_snapshot(
        run_2, total_active_patients=2, patients_with_active_pending=0,
        patients_without_edd=0, patients_with_edd_overdue=0,
        patients_dia_vermelho=0, patients_dia_verde=2,
        median_resolution_hours=None, category_counts={},
    )
    snapshot_1 = repo.save_daily_snapshot(
        run_1, total_active_patients=1, patients_with_active_pending=0,
        patients_without_edd=0, patients_with_edd_overdue=0,
        patients_dia_vermelho=0, patients_dia_verde=1,
        median_resolution_hours=None, category_counts={},
    )
    # Mas cronologicamente run_1 é mais ANTIGO -- força isso via UPDATE
    # direto, invertendo a ordem física de inserção da ordem por data.
    repo.conn.execute("UPDATE daily_snapshot SET created_at = ? WHERE snapshot_id = ?",
                       ("2020-01-01T00:00:00+00:00", snapshot_1))
    repo.conn.execute("UPDATE daily_snapshot SET created_at = ? WHERE snapshot_id = ?",
                       ("2020-01-02T00:00:00+00:00", snapshot_2))
    repo.conn.commit()

    snapshots = repo.get_daily_snapshots()
    assert [s["run_id"] for s in snapshots] == [run_1, run_2]


def test_get_daily_snapshots_limit_keeps_the_most_recent_in_order(repo):
    run_ids = [repo.start_run() for _ in range(3)]
    for run_id in run_ids:
        repo.save_daily_snapshot(
            run_id, total_active_patients=1, patients_with_active_pending=0,
            patients_without_edd=0, patients_with_edd_overdue=0,
            patients_dia_vermelho=0, patients_dia_verde=1,
            median_resolution_hours=None, category_counts={},
        )

    snapshots = repo.get_daily_snapshots(limit=2)

    assert [s["run_id"] for s in snapshots] == run_ids[1:]  # os 2 mais recentes, em ordem


def test_daily_snapshot_with_no_categories_returns_empty_children(repo):
    run_id = repo.start_run()
    snapshot_id = repo.save_daily_snapshot(
        run_id, total_active_patients=0, patients_with_active_pending=0,
        patients_without_edd=0, patients_with_edd_overdue=0,
        patients_dia_vermelho=0, patients_dia_verde=0,
        median_resolution_hours=None, category_counts={},
    )

    assert repo.get_daily_snapshot_categories(snapshot_id) == []


def test_mark_awaiting_notes_is_counted_apart_from_errors_and_no_admission(repo):
    """RESIL-015: admitido há pouco sem card/evolução acessível é categoria
    própria -- nunca infla `failed` nem `no_admission`."""
    fresh = repo.upsert_patient(_sample_patient("111"))
    done = repo.upsert_patient(_sample_patient("222"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [fresh, done])
    repo.mark_processing(run_id, fresh)
    repo.mark_awaiting_notes(run_id, fresh)
    repo.mark_processing(run_id, done)
    repo.mark_done(run_id, done)

    counts = repo.get_run_counts(run_id)
    assert counts == {"found": 2, "completed": 1, "failed": 0, "no_admission": 0, "awaiting_notes": 1}
    assert [row["record_number"] for row in repo.get_awaiting_notes_patients(run_id)] == ["111"]
    assert repo.get_failed_patients(run_id) == []
    assert repo.get_error_messages_for_run(run_id) == []

    repo.finish_run(run_id, "COMPLETED")
    run_row = repo.conn.execute(
        "SELECT patients_awaiting_notes, patients_failed FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert (run_row["patients_awaiting_notes"], run_row["patients_failed"]) == (1, 0)



def test_resume_closes_runs_left_running_by_a_dead_process(repo):
    """RESIL-016: run cujo processo morreu no meio fica RUNNING pra sempre --
    a execução seguinte fecha como INTERRUPTED, com finished_at e as
    contagens reais da fila; a run nova, iniciada depois, não é tocada."""
    from app.storage.repository import RUN_STATUS_INTERRUPTED, RUN_STATUS_RUNNING

    done = repo.upsert_patient(_sample_patient("111"))
    orphan = repo.upsert_patient(_sample_patient("222"))
    dead_run = repo.start_run()
    repo.enqueue_patients(dead_run, [done, orphan])
    repo.mark_processing(dead_run, done)
    repo.mark_done(dead_run, done)
    repo.mark_processing(dead_run, orphan)  # processo morreu aqui

    repo.resume_incomplete_runs()
    new_run = repo.start_run()

    old = repo.conn.execute(
        "SELECT status, finished_at, patients_found, patients_completed FROM runs WHERE run_id = ?", (dead_run,)
    ).fetchone()
    assert old["status"] == RUN_STATUS_INTERRUPTED
    assert old["finished_at"] is not None
    assert (old["patients_found"], old["patients_completed"]) == (2, 1)
    fresh = repo.conn.execute("SELECT status, finished_at FROM runs WHERE run_id = ?", (new_run,)).fetchone()
    assert (fresh["status"], fresh["finished_at"]) == (RUN_STATUS_RUNNING, None)


def test_resume_leaves_finished_runs_alone(repo):
    patient = repo.upsert_patient(_sample_patient("111"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient])
    repo.mark_processing(run_id, patient)
    repo.mark_done(run_id, patient)
    repo.finish_run(run_id, "COMPLETED")
    finished_at = repo.conn.execute("SELECT finished_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]

    repo.resume_incomplete_runs()

    row = repo.conn.execute("SELECT status, finished_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert (row["status"], row["finished_at"]) == ("COMPLETED", finished_at)
