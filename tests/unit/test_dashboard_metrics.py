"""Testes de app/reports/dashboard_metrics.py (Fase 1 do plano da dashboard
unificada -- DECISIONS.md DEC-099, TASKS.md REPORT-003). Dados 100%
fictícios, sem nenhuma dependência de GSUS/LLM real."""
from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.taxonomy import (
    CATEGORY_DIAGNOSTICO,
    CATEGORY_PROCEDIMENTO_CIRURGIA,
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
)
from app.models import Patient
from app.reports.dashboard_metrics import (
    compute_patient_census_rows,
    compute_service_indicators,
    compute_unit_census,
    save_snapshot,
)
from app.storage import database
from app.storage.repository import Repository


@pytest.fixture
def repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    yield Repository(conn)
    conn.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_no_active_patients_returns_zeroed_indicators_not_a_crash(repo):
    """Banco vazio (app recém-instalado, antes da 1ª execução) não pode
    quebrar a dashboard -- tudo zerado, nenhuma divisão por zero."""
    result = compute_service_indicators(repo)

    assert result.total_active_patients == 0
    assert result.pct_patients_with_active_pending == 0.0
    assert result.median_resolution_hours is None
    assert compute_unit_census(repo) == []


def test_patient_without_any_pending_or_state_counts_as_active_but_not_covered(repo):
    """Paciente processado pela Fase 1 mas ainda sem achado/análise (achado
    real E2E-001, mesma lógica de `get_all_active_patients`) tem que
    aparecer no total, mas não em nenhuma distribuição de pendência, e conta
    como "sem EDD documentada" (nunca teve análise nenhuma)."""
    repo.upsert_patient(Patient(record_number="100", bed="3A", unit="2B"))

    result = compute_service_indicators(repo)

    assert result.total_active_patients == 1
    assert result.patients_with_active_pending == 0
    assert result.pct_patients_with_active_pending == 0.0
    assert result.patients_without_edd == 1
    assert result.pct_patients_without_edd == 100.0
    assert result.by_category == {}


def test_pending_item_priority_is_recalculated_not_read_from_snapshot(repo):
    """Mesmo princípio do relatório individual (DEC-057/priority.py): nunca
    confia no `priority` gravado no momento da criação -- recalcula com o
    tempo decorrido ATUAL. Categoria sempre-alta (cirurgia) tem que aparecer
    como ALTA mesmo que o snapshot tenha gravado outra coisa."""
    patient_id = repo.upsert_patient(Patient(record_number="200", bed="1A", unit="2A"))
    old_evidence = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    repo.add_pending_item(
        patient_id, CATEGORY_PROCEDIMENTO_CIRURGIA, "Aguarda cirurgia", "evid", old_evidence,
        priority="MONITORAMENTO",  # snapshot propositalmente ERRADO -- deve ser ignorado
    )

    result = compute_service_indicators(repo)

    assert result.by_priority == {"ALTA": 1}
    assert result.by_category == {CATEGORY_PROCEDIMENTO_CIRURGIA: 1}


def test_distribution_by_category_priority_and_origin(repo):
    patient_a = repo.upsert_patient(Patient(record_number="300", bed="1A", unit="2A"))
    patient_b = repo.upsert_patient(Patient(record_number="301", bed="1B", unit="2A"))
    recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))

    repo.add_pending_item(patient_a, CATEGORY_DIAGNOSTICO, "Aguarda TC", "evid", recent, origin=ORIGIN_INTERNAL)
    repo.add_pending_item(patient_b, CATEGORY_PROCEDIMENTO_CIRURGIA, "Aguarda cirurgia", "evid", recent, origin=ORIGIN_EXTERNAL)

    result = compute_service_indicators(repo)

    assert result.total_active_patients == 2
    assert result.patients_with_active_pending == 2
    assert result.pct_patients_with_active_pending == 100.0
    assert result.by_category == {CATEGORY_DIAGNOSTICO: 1, CATEGORY_PROCEDIMENTO_CIRURGIA: 1}
    assert result.by_priority == {"MEDIA": 1, "ALTA": 1}  # diagnóstico dentro do SLA -> MEDIA
    assert result.by_origin == {ORIGIN_INTERNAL: 1, ORIGIN_EXTERNAL: 1}


def test_pending_item_without_origin_counts_as_not_defined(repo):
    """`origin` é opcional na criação -- não pode quebrar a agregação nem
    ficar invisível (mesmo raciocínio de nunca esconder dado incompleto,
    RF-20)."""
    patient_id = repo.upsert_patient(Patient(record_number="400", bed="1A", unit="2A"))
    repo.add_pending_item(patient_id, CATEGORY_DIAGNOSTICO, "Aguarda TC", "evid", None, origin=None)

    result = compute_service_indicators(repo)

    assert result.by_origin == {"NAO_DEFINIDA": 1}


def test_edd_indicators_use_the_same_overdue_rule_as_the_individual_report(repo):
    """Reusa `is_edd_overdue` (app/analysis/priority.py) -- precisa
    concordar com o que `html_report.py::_format_edd` mostra pro mesmo
    paciente (achado real, DEC-099)."""
    no_edd = repo.upsert_patient(Patient(record_number="500", bed="1A", unit="2A"))
    edd_ok = repo.upsert_patient(Patient(record_number="501", bed="1B", unit="2A"))
    edd_overdue = repo.upsert_patient(Patient(record_number="502", bed="1C", unit="2A"))

    repo.save_patient_state(no_edd, "ctx", "status", edd_status="NAO_REGISTRADA")
    repo.save_patient_state(edd_ok, "ctx", "status", edd_status="REGISTRADA", edd_data="2099-01-01")
    repo.save_patient_state(edd_overdue, "ctx", "status", edd_status="REGISTRADA", edd_data="2020-01-01")

    result = compute_service_indicators(repo)

    assert result.total_active_patients == 3
    # "sem previsão documentada" é só NAO_REGISTRADA -- edd_overdue TEVE uma
    # data real documentada (só que já passou), não conta como "sem EDD".
    assert result.patients_without_edd == 1
    assert result.patients_with_edd_overdue == 1
    assert result.pct_patients_with_edd_overdue == round(100 / 3, 1)


def test_median_resolution_hours_overall_and_by_category(repo):
    patient_id = repo.upsert_patient(Patient(record_number="600", bed="1A", unit="2A"))
    created = datetime.now(timezone.utc) - timedelta(hours=100)

    id_a = repo.add_pending_item(patient_id, CATEGORY_DIAGNOSTICO, "Aguarda TC", "evid", None)
    id_b = repo.add_pending_item(patient_id, CATEGORY_DIAGNOSTICO, "Aguarda RM", "evid", None)
    repo.conn.execute("UPDATE pending_items SET created_at = ? WHERE pending_id IN (?, ?)",
                       (created.isoformat(), id_a, id_b))
    repo.conn.commit()
    repo.resolve_pending_item(id_a)  # resolve "agora" -- ~100h depois do created_at forçado

    result = compute_service_indicators(repo)

    assert result.median_resolution_hours is not None
    assert 99 < result.median_resolution_hours < 101
    assert CATEGORY_DIAGNOSTICO in result.median_resolution_hours_by_category


def test_median_resolution_hours_ignores_row_with_unparseable_timestamp(repo):
    """Achado real de auditoria adversarial: `_resolution_hours` tem um
    `except ValueError: return None` pra dado fora do padrão (edição manual
    do banco, migração parcial) -- nenhum teste antes deste forçava esse
    caminho. Uma linha com `created_at` corrompido não pode derrubar
    `compute_service_indicators` (chamada a cada refresh da dashboard e ao
    fim de toda execução via `save_snapshot`) -- só precisa ser ignorada."""
    patient_id = repo.upsert_patient(Patient(record_number="601", bed="1A", unit="2A"))
    good_id = repo.add_pending_item(patient_id, CATEGORY_DIAGNOSTICO, "Aguarda TC", "evid", None)
    bad_id = repo.add_pending_item(patient_id, CATEGORY_DIAGNOSTICO, "Aguarda RM", "evid", None)
    repo.conn.execute("UPDATE pending_items SET created_at = ? WHERE pending_id = ?",
                       ("data-invalida-nao-iso", bad_id))
    repo.conn.commit()
    repo.resolve_pending_item(good_id)
    repo.resolve_pending_item(bad_id)

    result = compute_service_indicators(repo)  # não pode lançar exceção

    assert result.median_resolution_hours is not None  # a linha boa ainda conta
    assert result.median_resolution_hours_by_category[CATEGORY_DIAGNOSTICO] is not None


def test_unit_census_aggregates_by_unit_not_by_patient(repo):
    """Diferente da tabela `.census` de html_report.py (uma linha por
    paciente) -- aqui cada linha é uma UNIDADE, com subtotais."""
    p1 = repo.upsert_patient(Patient(record_number="700", bed="1A", unit="2A"))
    p2 = repo.upsert_patient(Patient(record_number="701", bed="1B", unit="2A"))
    p3 = repo.upsert_patient(Patient(record_number="702", bed="3A", unit="3B"))
    recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))

    repo.add_pending_item(p1, CATEGORY_PROCEDIMENTO_CIRURGIA, "Aguarda cirurgia", "evid", recent)
    repo.add_pending_item(p2, CATEGORY_DIAGNOSTICO, "Aguarda TC", "evid", recent)
    # p3 sem nenhuma pendência ativa

    rows = compute_unit_census(repo)

    assert [r.unit for r in rows] == ["2A", "3B"]  # ordenado por nome de unidade
    unit_2a = rows[0]
    assert unit_2a.patient_count == 2
    assert unit_2a.patients_with_active_pending == 2
    assert unit_2a.active_pending_count == 2
    assert unit_2a.by_priority == {"ALTA": 1, "MEDIA": 1}

    unit_3b = rows[1]
    assert unit_3b.patient_count == 1
    assert unit_3b.patients_with_active_pending == 0
    assert unit_3b.active_pending_count == 0
    assert unit_3b.by_priority == {}


def test_unit_census_never_crashes_on_patient_without_unit(repo):
    """`unit` pode vir vazio/None do GSUS -- nunca deve fazer a agregação
    quebrar nem esconder o paciente (agrupa como "?")."""
    repo.upsert_patient(Patient(record_number="800", bed="1A", unit=None))

    rows = compute_unit_census(repo)

    assert len(rows) == 1
    assert rows[0].unit == "?"
    assert rows[0].patient_count == 1


# ------------------------------------------------ dia_classificacao (RF-28)
# REPORT-003 Fase 2 / DEC-099.

def test_dia_classificacao_counts_are_distinct_from_edd(repo):
    """`dia_vermelho`/`dia_verde` (RF-28) é um conceito TOTALMENTE separado
    de EDD (RF-27) -- um paciente pode ser dia verde sem EDD documentada, ou
    dia vermelho com EDD em dia. Não pode haver acoplamento acidental entre
    os dois cálculos."""
    vermelho = repo.upsert_patient(Patient(record_number="900", bed="1A", unit="2A"))
    verde = repo.upsert_patient(Patient(record_number="901", bed="1B", unit="2A"))
    sem_analise = repo.upsert_patient(Patient(record_number="902", bed="1C", unit="2A"))

    repo.save_patient_state(vermelho, "ctx", "status", dia_classificacao="VERMELHO", dia_causa="AGUARDA_EXAME")
    repo.save_patient_state(verde, "ctx", "status", dia_classificacao="VERDE")
    # sem_analise nunca teve `save_patient_state` chamado -- não pode contar
    # como verde nem vermelho, só fica de fora dos dois.

    result = compute_service_indicators(repo)

    assert result.patients_dia_vermelho == 1
    assert result.patients_dia_verde == 1
    assert result.dia_causa_counts == {"AGUARDA_EXAME": 1}


def test_dia_causa_counts_group_by_cause_across_patients(repo):
    p1 = repo.upsert_patient(Patient(record_number="910", bed="1A", unit="2A"))
    p2 = repo.upsert_patient(Patient(record_number="911", bed="1B", unit="2A"))
    p3 = repo.upsert_patient(Patient(record_number="912", bed="1C", unit="2A"))

    repo.save_patient_state(p1, "ctx", "status", dia_classificacao="VERMELHO", dia_causa="AGUARDA_EXAME")
    repo.save_patient_state(p2, "ctx", "status", dia_classificacao="VERMELHO", dia_causa="AGUARDA_EXAME")
    repo.save_patient_state(p3, "ctx", "status", dia_classificacao="VERMELHO", dia_causa="AGUARDA_TRANSFERENCIA")

    result = compute_service_indicators(repo)

    assert result.patients_dia_vermelho == 3
    assert result.dia_causa_counts == {"AGUARDA_EXAME": 2, "AGUARDA_TRANSFERENCIA": 1}


# ------------------------------------------------------------ save_snapshot
# REPORT-003 Fase 2 / DEC-099 -- ponte entre o instantâneo e o histórico.

def test_save_snapshot_persists_the_same_numbers_computed_now(repo):
    """`save_snapshot` não pode ter lógica própria de contagem -- tem que
    gravar exatamente o que `compute_service_indicators` já calculou, senão
    o histórico e o instantâneo podem divergir pro mesmo momento."""
    patient_id = repo.upsert_patient(Patient(record_number="920", bed="1A", unit="2A"))
    repo.save_patient_state(patient_id, "ctx", "status", dia_classificacao="VERMELHO", dia_causa="AGUARDA_EXAME")
    repo.add_pending_item(patient_id, CATEGORY_DIAGNOSTICO, "Aguarda TC", "evid",
                           _iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    run_id = repo.start_run()

    indicators = compute_service_indicators(repo)
    snapshot_id = save_snapshot(repo, run_id)

    snapshots = repo.get_daily_snapshots()
    assert len(snapshots) == 1
    row = snapshots[0]
    assert row["snapshot_id"] == snapshot_id
    assert row["run_id"] == run_id
    assert row["total_active_patients"] == indicators.total_active_patients
    assert row["patients_dia_vermelho"] == indicators.patients_dia_vermelho == 1

    categories = repo.get_daily_snapshot_categories(snapshot_id)
    by_dimension_label = {(r["dimension"], r["label"]): r["total"] for r in categories}
    assert by_dimension_label[("category", CATEGORY_DIAGNOSTICO)] == 1


def test_save_snapshot_never_persists_dia_causa_free_text(repo):
    """Achado real de auditoria adversarial (DEC-101): `dia_causa` é texto
    livre escrito pelo LLM (sem taxonomia fechada, ao contrário de
    category/origin/priority) -- normalmente uma frase específica de UM
    paciente. `daily_snapshot_category` não tem rotina de expurgo (diferente
    de `notes`, RETENTION-001), então gravar isso ali vazaria texto clínico
    potencialmente reidentificável PRA SEMPRE. `patients_dia_vermelho` (só a
    CONTAGEM, sempre segura) continua indo pro `daily_snapshot` -- só a
    quebra POR CAUSA fica de fora até `dia_causa` ganhar uma taxonomia
    fechada como `category` já tem."""
    patient_id = repo.upsert_patient(Patient(record_number="930", bed="1A", unit="2A"))
    repo.save_patient_state(
        patient_id, "ctx", "status",
        dia_classificacao="VERMELHO",
        dia_causa="aguardando parecer da neurocirurgia sobre paciente com sequela pos-AVC",
    )
    run_id = repo.start_run()

    snapshot_id = save_snapshot(repo, run_id)

    snapshots = repo.get_daily_snapshots()
    assert snapshots[0]["patients_dia_vermelho"] == 1  # a contagem agregada continua indo

    categories = repo.get_daily_snapshot_categories(snapshot_id)
    assert all(row["dimension"] != "dia_causa" for row in categories)  # mas o texto livre, nunca
    all_labels = [row["label"] for row in categories]
    assert not any("neurocirurgia" in label or "AVC" in label for label in all_labels)


# ------------------------------------------------- compute_patient_census_rows
# REPORT-004 / DEC-099 -- tabela clicável da tela única.

def test_patient_census_row_without_pending_uses_monitoramento_default(repo):
    repo.upsert_patient(Patient(record_number="940", bed="1A", unit="2A"))

    rows = compute_patient_census_rows(repo)

    assert len(rows) == 1
    row = rows[0]
    assert row.record_number == "940"
    assert row.active_pending_count == 0
    assert row.main_category is None
    assert row.main_priority == "MONITORAMENTO"
    assert row.edd_overdue is False


def test_patient_census_row_picks_highest_priority_pending_as_main(repo):
    """Paciente com mais de uma pendência ativa -- a linha reflete a de
    MAIOR prioridade (mesmo critério de `html_report.py::_render_census_row`),
    não a primeira inserida."""
    patient_id = repo.upsert_patient(Patient(record_number="950", bed="1A", unit="2A"))
    recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    repo.add_pending_item(patient_id, CATEGORY_DIAGNOSTICO, "Aguarda TC", "evid", recent)
    repo.add_pending_item(patient_id, CATEGORY_PROCEDIMENTO_CIRURGIA, "Aguarda cirurgia", "evid", recent)

    rows = compute_patient_census_rows(repo)

    assert len(rows) == 1
    assert rows[0].active_pending_count == 2
    assert rows[0].main_category == CATEGORY_PROCEDIMENTO_CIRURGIA
    assert rows[0].main_priority == "ALTA"


def test_patient_census_row_carries_edd_and_dia_classificacao(repo):
    patient_id = repo.upsert_patient(Patient(record_number="960", bed="1A", unit="2A"))
    repo.save_patient_state(
        patient_id, "ctx", "status",
        edd_status="VENCIDA", edd_data="2020-01-01",
        dia_classificacao="VERMELHO", dia_causa="AGUARDA_EXAME",
    )

    rows = compute_patient_census_rows(repo)

    assert rows[0].edd_status == "VENCIDA"
    assert rows[0].edd_overdue is True
    assert rows[0].dia_classificacao == "VERMELHO"


def test_patient_census_row_carries_dih_context_and_main_description(repo):
    """Achado de auditoria de certificação pré-entrega (2026-09-01, RF-29):
    a tabela de censo da dashboard não tinha DIH nem Contexto, e
    "pendência principal" mostrava só a categoria (taxonomia fechada), não
    a descrição real da pendência -- RF-29 pede as duas colunas
    separadas."""
    patient_id = repo.upsert_patient(
        Patient(record_number="970", bed="1A", unit="2A", admission_date="2026-08-20")
    )
    repo.save_patient_state(patient_id, "Paciente em pós-operatório de colecistectomia.", "Estável.")
    repo.add_pending_item(
        patient_id, CATEGORY_DIAGNOSTICO, "Aguarda resultado de tomografia de tórax", "evid",
        _iso(datetime.now(timezone.utc) - timedelta(hours=1)),
    )

    rows = compute_patient_census_rows(repo)

    assert rows[0].dih is not None and rows[0].dih >= 0
    assert rows[0].clinical_context == "Paciente em pós-operatório de colecistectomia."
    assert rows[0].main_description == "Aguarda resultado de tomografia de tórax"
    assert rows[0].main_category == CATEGORY_DIAGNOSTICO  # continua disponível separadamente


def test_patient_census_row_without_admission_date_or_state_has_none_dih_and_context(repo):
    repo.upsert_patient(Patient(record_number="971", bed="1B", unit="2A"))  # sem admission_date, sem state

    rows = compute_patient_census_rows(repo)

    assert rows[0].dih is None
    assert rows[0].clinical_context is None
    assert rows[0].main_description is None


def test_save_snapshot_on_empty_database_does_not_crash(repo):
    """Primeira execução do app (banco vazio) tem que conseguir gravar um
    snapshot zerado -- mesmo raciocínio de `compute_service_indicators` com
    banco vazio (nenhuma divisão por zero, nenhuma exceção)."""
    run_id = repo.start_run()

    snapshot_id = save_snapshot(repo, run_id)

    snapshots = repo.get_daily_snapshots()
    assert snapshots[0]["snapshot_id"] == snapshot_id
    assert snapshots[0]["total_active_patients"] == 0
    assert snapshots[0]["median_resolution_hours"] is None
