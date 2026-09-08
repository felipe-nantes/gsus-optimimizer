"""Indicadores agregados do serviço (RF-28/RF-29, seção 12 da orientação
técnica de auditoria hospitalar concorrente) -- Fase 1 do plano da dashboard
unificada (DECISIONS.md DEC-099, TASKS.md REPORT-003).

Mesma separação de responsabilidade que `app/reports/html_report.py` já
usa: `Repository` fica só SQL puro, e é AQUI que se combina com
`app.analysis.priority` pra recalcular prioridade/vencimento na LEITURA
(nunca confiar num valor gravado como snapshot -- teria o mesmo problema já
resolvido no relatório individual, ver módulo docstring de html_report.py).

Indicadores do instantâneo de AGORA não mudam schema -- usam só o que
`Repository` já expõe (`get_all_active_patients`, `get_all_active_pending_items`,
`get_resolved_pending_items`, `patient_state`).

`save_snapshot` (Fase 2, DEC-099) é a ponte pro histórico: reusa
`compute_service_indicators` pra montar o retrato de HOJE e grava em
`daily_snapshot`/`daily_snapshot_category` (Repository), chamada uma vez ao
fim de cada execução bem-sucedida (app/orchestrator.py). Sem isso os
indicadores de TENDÊNCIA (% dias vermelhos ao longo do tempo, barreiras por
100 pacientes-dia) nunca teriam dado histórico pra mostrar, não importa
quanto tempo passe -- fecha a lacuna real do RF-28.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.analysis.priority import (
    PRIORITY_ALTA,
    PRIORITY_MEDIA,
    PRIORITY_MONITORAMENTO,
    compute_priority,
    days_since_admission,
    hours_elapsed_since,
    is_edd_overdue,
)
from app.storage.repository import Repository

# Ordem de gravidade (RF-24) -- reusado tanto aqui (pendência principal por
# paciente) quanto pela tabela de censo da tela única (REPORT-004, ordenação
# da coluna "Prioridade") pra nunca ter dois critérios de ordenação diferentes
# pro mesmo conceito.
PRIORITY_RANK = {PRIORITY_ALTA: 3, PRIORITY_MEDIA: 2, PRIORITY_MONITORAMENTO: 1}


@dataclass
class ServiceIndicators:
    total_active_patients: int
    patients_with_active_pending: int
    pct_patients_with_active_pending: float
    by_category: dict[str, int] = field(default_factory=dict)
    by_priority: dict[str, int] = field(default_factory=dict)
    by_origin: dict[str, int] = field(default_factory=dict)
    patients_without_edd: int = 0
    pct_patients_without_edd: float = 0.0
    patients_with_edd_overdue: int = 0
    pct_patients_with_edd_overdue: float = 0.0
    median_resolution_hours: float | None = None
    median_resolution_hours_by_category: dict[str, float | None] = field(default_factory=dict)
    patients_dia_vermelho: int = 0
    patients_dia_verde: int = 0
    dia_causa_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class UnitCensusRow:
    unit: str
    patient_count: int
    patients_with_active_pending: int
    active_pending_count: int
    by_priority: dict[str, int] = field(default_factory=dict)


@dataclass
class PatientCensusRow:
    """Uma linha por paciente ativo -- alimenta a tabela clicável da tela
    única (REPORT-004): diferente de `UnitCensusRow` (rollup por unidade,
    sem identidade de paciente), aqui cada linha aponta pra UM prontuário,
    pra permitir abrir o relatório individual dele a partir da tabela
    (mesmo `record_number` usado por "Localizar Paciente", DEC-067).
    `main_*`/`hours_elapsed` são da pendência de MAIOR prioridade do
    paciente -- mesmo critério de `html_report.py::_render_census_row`.
    `dih`/`clinical_context`/`main_description` adicionados na auditoria de
    certificação pré-entrega (2026-09-01, RF-29) -- a tabela da dashboard
    não tinha essas 3 colunas que o requisito pede explicitamente."""
    patient_id: str
    record_number: str
    bed: str
    unit: str
    admission_date: str | None
    dih: int | None
    clinical_context: str | None
    active_pending_count: int
    main_category: str | None
    main_description: str | None
    main_priority: str
    hours_elapsed: float | None
    edd_status: str | None
    edd_overdue: bool
    dia_classificacao: str | None
    # UI-008 (pedido do pagador): False enquanto a IA ainda não analisou o
    # paciente -- a tela diz "Aguardando análise de IA" em vez de um traço.
    analyzed: bool = False


def compute_patient_census_rows(repo: Repository) -> list[PatientCensusRow]:
    """Reusa `_annotated_active_items` (mesma prioridade recalculada na
    leitura que os outros indicadores desta tela) -- nunca duplica a lógica
    de anotação de pendência já usada por `compute_service_indicators`."""
    all_patients = repo.get_all_active_patients()
    annotated_items, state_by_patient = _annotated_active_items(repo)

    items_by_patient: dict[str, list[dict]] = {}
    for item in annotated_items:
        items_by_patient.setdefault(item["patient_id"], []).append(item)

    rows: list[PatientCensusRow] = []
    for patient in all_patients:
        patient_id = patient["patient_id"]
        state = state_by_patient.get(patient_id)
        items = sorted(
            items_by_patient.get(patient_id, []),
            key=lambda it: (-PRIORITY_RANK.get(it["_priority"], 0), -(it["_hours_elapsed"] or 0)),
        )
        main = items[0] if items else None
        edd_status = state["edd_status"] if state else None
        edd_data = state["edd_data"] if state else None
        rows.append(PatientCensusRow(
            patient_id=patient_id,
            record_number=patient["record_number"],
            bed=patient["bed"] or "?",
            unit=patient["unit"] or "?",
            admission_date=patient["admission_date"],
            dih=days_since_admission(patient["admission_date"]),
            clinical_context=state["clinical_context"] if state else None,
            active_pending_count=len(items),
            main_category=main["category"] if main else None,
            main_description=main["description"] if main else None,
            main_priority=main["_priority"] if main else PRIORITY_MONITORAMENTO,
            hours_elapsed=main["_hours_elapsed"] if main else None,
            edd_status=edd_status,
            edd_overdue=is_edd_overdue(edd_status, edd_data),
            dia_classificacao=state["dia_classificacao"] if state else None,
            analyzed=state is not None,
        ))
    return rows


def _annotated_active_items(repo: Repository) -> tuple[list[dict], dict[str, dict]]:
    """Mesmo padrão de `html_report.py::generate_report` -- busca pacientes
    ativos + pendências ativas + patient_state, e recalcula prioridade por
    item (nunca lê o snapshot gravado na criação, ver DEC-057/priority.py).
    Devolve (itens anotados, patient_state por patient_id) pra reuso pelas
    duas funções públicas deste módulo sem repetir as mesmas 3 consultas."""
    pending_rows = repo.get_all_active_pending_items()
    state_by_patient = {
        row["patient_id"]: row for row in repo.conn.execute("SELECT * FROM patient_state").fetchall()
    }

    annotated: list[dict] = []
    for row in pending_rows:
        state = state_by_patient.get(row["patient_id"])
        necessidade = state["necessidade_hospitalar"] if state else None
        hours = hours_elapsed_since(row["evidence_date"])
        priority = compute_priority(row["category"], necessidade, hours)
        annotated.append({**dict(row), "_priority": priority, "_hours_elapsed": hours})

    return annotated, state_by_patient


def compute_service_indicators(repo: Repository) -> ServiceIndicators:
    """Indicadores do serviço (seção 12) calculáveis HOJE, sem histórico --
    % com barreira ativa, distribuição por categoria/prioridade/origem, %
    sem EDD documentada, % EDD vencida, tempo mediano de resolução (geral e
    por categoria). Nunca lê dado de paciente individual pra fora deste
    processo -- só contagens agregadas, seguro pra exibir/logar."""
    all_patients = repo.get_all_active_patients()
    total_active = len(all_patients)

    annotated_items, state_by_patient = _annotated_active_items(repo)

    patients_with_pending = {item["patient_id"] for item in annotated_items}

    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    for item in annotated_items:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
        by_priority[item["_priority"]] = by_priority.get(item["_priority"], 0) + 1
        origin_key = item["origin"] or "NAO_DEFINIDA"
        by_origin[origin_key] = by_origin.get(origin_key, 0) + 1

    patients_without_edd = 0
    patients_with_edd_overdue = 0
    patients_dia_vermelho = 0
    patients_dia_verde = 0
    dia_causa_counts: dict[str, int] = {}
    for patient in all_patients:
        state = state_by_patient.get(patient["patient_id"])
        edd_status = state["edd_status"] if state else None
        edd_data = state["edd_data"] if state else None
        # "Sem previsão documentada" é só NAO_REGISTRADA (ou nunca analisado)
        # -- achado real (teste): VENCIDA e REGISTRADA-mas-já-passada TIVERAM
        # uma data real documentada em algum momento, não são a mesma coisa
        # que "nunca documentou nada" (seção 8/12 da orientação técnica
        # tratam os dois como conceitos distintos, não sobrepostos).
        if edd_status is None or edd_status == "NAO_REGISTRADA":
            patients_without_edd += 1
        if is_edd_overdue(edd_status, edd_data):
            patients_with_edd_overdue += 1

        # `dia_classificacao`/`dia_causa` (RF-28) são gravados em
        # `patient_state`, mas UPSERT puro -- sem `daily_snapshot` (Fase 2)
        # o valor de HOJE é tudo que já existiria amanhã. Só entra na
        # distribuição de causa quem é VERMELHO (schemas.py exige dia_causa
        # preenchida nesse caso -- "NAO_ESPECIFICADA" é só um fail-safe pra
        # dado antigo/incompleto, nunca deveria realmente ocorrer).
        classificacao = state["dia_classificacao"] if state else None
        if classificacao == "VERMELHO":
            patients_dia_vermelho += 1
            causa = (state["dia_causa"] if state else None) or "NAO_ESPECIFICADA"
            dia_causa_counts[causa] = dia_causa_counts.get(causa, 0) + 1
        elif classificacao == "VERDE":
            patients_dia_verde += 1

    resolved_rows = repo.get_resolved_pending_items()
    all_hours = [h for h in (_resolution_hours(r) for r in resolved_rows) if h is not None]
    hours_by_category: dict[str, list[float]] = {}
    for row in resolved_rows:
        h = _resolution_hours(row)
        if h is not None:
            hours_by_category.setdefault(row["category"], []).append(h)

    def pct(count: int) -> float:
        return round(100 * count / total_active, 1) if total_active else 0.0

    return ServiceIndicators(
        total_active_patients=total_active,
        patients_with_active_pending=len(patients_with_pending),
        pct_patients_with_active_pending=pct(len(patients_with_pending)),
        by_category=by_category,
        by_priority=by_priority,
        by_origin=by_origin,
        patients_without_edd=patients_without_edd,
        pct_patients_without_edd=pct(patients_without_edd),
        patients_with_edd_overdue=patients_with_edd_overdue,
        pct_patients_with_edd_overdue=pct(patients_with_edd_overdue),
        median_resolution_hours=_median(all_hours),
        median_resolution_hours_by_category={
            category: _median(hours) for category, hours in hours_by_category.items()
        },
        patients_dia_vermelho=patients_dia_vermelho,
        patients_dia_verde=patients_dia_verde,
        dia_causa_counts=dia_causa_counts,
    )


def compute_unit_census(repo: Repository) -> list[UnitCensusRow]:
    """Censo agregado POR UNIDADE (seção 11) -- diferente da tabela `.census`
    de `html_report.py`, que é uma linha por paciente/leito. Aqui cada linha
    é uma unidade, com subtotais -- a visão que a orientação técnica descreve
    como "legível em poucos segundos" pro início do dia. Ordenado por nome
    de unidade pra saída estável/testável."""
    all_patients = repo.get_all_active_patients()
    annotated_items, _ = _annotated_active_items(repo)

    items_by_unit: dict[str, list[dict]] = {}
    for item in annotated_items:
        items_by_unit.setdefault(item["unit"] or "?", []).append(item)

    patients_by_unit: dict[str, list] = {}
    for patient in all_patients:
        patients_by_unit.setdefault(patient["unit"] or "?", []).append(patient)

    rows: list[UnitCensusRow] = []
    for unit in sorted(patients_by_unit):
        unit_patients = patients_by_unit[unit]
        unit_items = items_by_unit.get(unit, [])
        by_priority: dict[str, int] = {}
        for item in unit_items:
            by_priority[item["_priority"]] = by_priority.get(item["_priority"], 0) + 1
        rows.append(UnitCensusRow(
            unit=unit,
            patient_count=len(unit_patients),
            patients_with_active_pending=len({item["patient_id"] for item in unit_items}),
            active_pending_count=len(unit_items),
            by_priority=by_priority,
        ))
    return rows


def _resolution_hours(pending_row) -> float | None:
    """Horas entre `created_at` e `resolved_at` -- os dois em UTC-aware
    (`Repository._now()`), diferente de `evidence_date` (naive/local,
    DEC-065) -- nunca misturar os dois regimes de fuso."""
    if not pending_row["resolved_at"] or not pending_row["created_at"]:
        return None
    try:
        created = datetime.fromisoformat(pending_row["created_at"])
        resolved = datetime.fromisoformat(pending_row["resolved_at"])
    except ValueError:
        return None
    return max((resolved - created).total_seconds() / 3600, 0.0)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def save_snapshot(repo: Repository, run_id: str) -> str:
    """Grava o retrato de HOJE em `daily_snapshot`/`daily_snapshot_category`
    (REPORT-003 Fase 2, DEC-099). Reusa `compute_service_indicators` -- o
    mesmo cálculo que já alimenta a visão instantânea da dashboard -- pra
    nunca ter duas fontes de verdade sobre o que conta como "sem EDD" ou
    "dia vermelho". Chamar UMA vez ao fim de cada execução bem-sucedida
    (app/orchestrator.py), depois que a Fase 2 (IA) já rodou pra esta run --
    é o retrato final do dia, não um intermediário."""
    indicators = compute_service_indicators(repo)
    # DEC-101 (achado real de auditoria adversarial, RESIL-007): `dia_causa`
    # NUNCA entra em `category_counts` -- ao contrário de category/origin/
    # priority (vocabulário fechado, `app/analysis/taxonomy.py`), `dia_causa`
    # é texto livre escrito pelo LLM (sem validação de enum, ver
    # `app/analysis/schemas.py` -- só exige não-vazio quando VERMELHO) e
    # normalmente é uma frase específica de UM paciente (ex.: "aguardando
    # parecer da neurocirurgia..."). Gravar isso como se fosse um rótulo
    # agregado seguro em `daily_snapshot_category` (tabela sem expurgo,
    # diferente de `notes`/RETENTION-001) vazaria texto clínico
    # potencialmente reidentificável pra sempre. `patients_dia_vermelho`/
    # `patients_dia_verde` (contagem agregada, sempre segura) já ficam em
    # `daily_snapshot` -- a quebra POR CAUSA (RF-28) fica pendente até
    # `dia_causa` ganhar uma taxonomia fechada como `category` já tem
    # (ver TASKS.md).
    category_counts = {
        "category": indicators.by_category,
        "origin": indicators.by_origin,
        "priority": indicators.by_priority,
    }
    return repo.save_daily_snapshot(
        run_id,
        total_active_patients=indicators.total_active_patients,
        patients_with_active_pending=indicators.patients_with_active_pending,
        patients_without_edd=indicators.patients_without_edd,
        patients_with_edd_overdue=indicators.patients_with_edd_overdue,
        patients_dia_vermelho=indicators.patients_dia_vermelho,
        patients_dia_verde=indicators.patients_dia_verde,
        median_resolution_hours=indicators.median_resolution_hours,
        category_counts=category_counts,
    )
