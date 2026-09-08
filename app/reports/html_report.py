"""Relatório HTML estático, por setor/leito (REPORT-001), no modelo de
"registro diário padronizado" da orientação técnica de auditoria hospitalar
concorrente fornecida pelo usuário 2026-08-24 (ver DECISIONS.md DEC-057 e
PROJECT_SPEC.md RF-20 a RF-29).

HTML + CSS inline, sem framework JS, sem servidor -- abre direto no
navegador padrão (prompt mestre seção 27). Falhas de processamento são
sempre visíveis (seção 28): o relatório nunca finge estar completo.

Prioridade e tempo decorrido são recalculados AQUI, na leitura, não lidos
como valor fixo gravado no banco -- ambos dependem de "agora" e ficariam
desatualizados entre uma execução e a próxima (ver app/analysis/priority.py).
"""
from __future__ import annotations

import html
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from app.analysis.priority import (
    PRIORITY_ALTA, PRIORITY_MEDIA, compute_priority, days_since_admission, hours_elapsed_since, is_edd_overdue,
)
from app.analysis.sla_config import get_sla_hours
from app.reports import dashboard_metrics
from app.storage.repository import Repository

ORIGIN_LABELS = {"INTERNA": "interna", "EXTERNA": "externa"}
CONFIDENCE_LABELS = {"ALTA": "alta", "MEDIA": "média", "BAIXA": "baixa"}

CSS = """
:root { color-scheme: light; --canvas: #e9e9ec; --page: #f5f6f7; --card: #fff; --text: #111315; --muted: #71717a; --line: #eceef0; --accent: #ff4f0a; --accent-soft: #fff0e9; --success: #208442; }
* { box-sizing: border-box; }
html { background: var(--canvas); }
body { max-width: 1440px; min-height: calc(100vh - 48px); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; margin: 24px auto; padding: 34px; color: var(--text); background: var(--page); border: 1px solid #dedfe2; border-radius: 24px; box-shadow: 0 14px 40px rgba(20, 20, 22, .07); }
h1 { display: flex; align-items: center; gap: 12px; font-size: 28px; letter-spacing: -.03em; margin: 0 0 5px; }
h1::before { content: ""; width: 22px; height: 22px; flex: 0 0 22px; border-radius: 6px; background: conic-gradient(from 90deg, var(--text) 0 25%, var(--accent) 0 50%, var(--text) 0 100%); box-shadow: inset 0 0 0 3px var(--page); }
h2 { letter-spacing: -.015em; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 25px; }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
.summary .stat { min-height: 104px; background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 18px 20px; color: var(--muted); font-size: 12px; }
.summary .stat .n { color: var(--text); font-size: 34px; line-height: 1; letter-spacing: -.04em; font-weight: 650; display: block; margin-bottom: 10px; }
.summary .stat.compact .n { font-size: 23px; line-height: 1.12; overflow-wrap: anywhere; }
.summary .stat.failed .n { color: #b3261e; }
.failures, .no-admission { border-radius: 16px; padding: 15px 18px; margin-bottom: 20px; }
.failures { background: #fff3f2; border: 1px solid #f2c9c5; }
.no-admission { background: #f1f2f3; border: 1px solid var(--line); }
.failures h2, .no-admission h2 { font-size: 14px; margin: 0 0 8px; }
.failures h2 { color: #b3261e; }
.no-admission h2 { color: var(--text); }
.failures li, .no-admission li { margin-bottom: 4px; }
.census, .indicators { margin-bottom: 30px; overflow-x: auto; }
.census h2, .indicators h2 { font-size: 18px; margin: 0 0 12px; }
.census table, .indicators table { border-collapse: separate; border-spacing: 0; width: 100%; background: var(--card); border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
.census th, .census td, .indicators th, .indicators td { border: 0; border-bottom: 1px solid var(--line); padding: 10px 12px; text-align: left; font-size: 12px; }
.census tr:last-child td, .indicators tr:last-child td { border-bottom: 0; }
.census th, .indicators th { background: #f0f1f2; color: #4c4d52; font-size: 10px; letter-spacing: .04em; text-transform: uppercase; }
.prio-ALTA { color: #b3261e; font-weight: bold; }
.prio-MEDIA { color: #9b6b00; font-weight: bold; }
.prio-MONITORAMENTO { color: var(--success); }
/* UI-008: cor nos números de dia (pedido do pagador) e censo por unidade em destaque */
.summary .stat.dia-vermelho .n { color: #b3261e; }
.summary .stat.dia-verde .n { color: var(--success); }
.summary .stat small { display: block; margin-top: 4px; font-size: 11px; color: var(--muted); }
.unit-census { background: var(--card); border: 2px solid var(--accent); border-radius: 16px; padding: 14px 16px 6px; margin-bottom: 18px; }
.unit-census h3 { margin: 0 0 10px; font-size: 16px; }
.unit-census table { border: 0; }
.unit-census th, .unit-census td { font-size: 13px; }
.unit-census tr.total td { font-weight: 650; background: #f7f7f8; }
.unit { margin-bottom: 30px; }
.unit > h2 { font-size: 18px; border: 0; border-left: 4px solid var(--accent); padding: 2px 0 2px 12px; margin-bottom: 14px; }
.bed { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 18px; margin-bottom: 12px; box-shadow: 0 5px 18px rgba(20, 20, 22, .03); }
.bed h3 { margin: 0 0 5px; font-size: 15px; }
.bed .day-badge { display: inline-block; font-size: 10px; font-weight: bold; padding: 3px 9px; border-radius: 999px; margin-left: 8px; vertical-align: middle; }
.day-badge.VERDE { background: #e7f5eb; color: var(--success); }
.day-badge.VERMELHO { background: #fce8e6; color: #b3261e; }
.section-label { font-weight: bold; font-size: 10px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); margin-top: 14px; }
.pending { border-left: 4px solid var(--accent); border-radius: 0 12px 12px 0; padding: 10px 13px; margin: 9px 0; background: var(--accent-soft); }
.pending .category { font-weight: 650; }
.pending .evidence { font-style: italic; color: #44464b; margin-top: 5px; }
.pending .evidence-date { color: var(--muted); font-size: 11px; }
.no-pending { color: var(--success); }
.inferred-tag, .origin-tag { display: inline-block; font-size: 10px; border-radius: 999px; padding: 2px 7px; margin-left: 6px; }
.inferred-tag { color: #8a5e00; background: #fff3cd; }
.origin-tag { color: #4c4d52; background: #eeeff1; }
.pending .meta-line { color: var(--muted); font-size: 11px; margin-top: 3px; }
.bed h3 .unit-label { font-size: 12px; font-weight: normal; color: var(--muted); }
.window-limited-warning { background: #fff3f2; border: 1px solid #f2c9c5; border-radius: 12px; padding: 10px 13px; margin: 9px 0; color: #7a2620; font-size: 12px; }
.indicators-breakdown { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin-bottom: 16px; }
.indicators-breakdown .block { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 16px 18px; min-width: 0; }
.indicators-breakdown .block h3 { font-size: 12px; margin: 0 0 8px; color: var(--muted); }
.indicators-breakdown .block ul { margin: 0; padding-left: 18px; font-size: 12px; }
.indicators-breakdown .block li { margin-bottom: 3px; }
@media (max-width: 760px) { body { margin: 0; padding: 20px; border: 0; border-radius: 0; } h1 { font-size: 23px; } .summary { grid-template-columns: 1fr 1fr; } }
"""

NECESSIDADE_LABELS = {
    "SIM": "SIM — permanece necessitando tratamento hospitalar",
    "PROVAVELMENTE_SIM": "PROVAVELMENTE SIM",
    "INCERTO": "INCERTO — documentação insuficiente",
    "POSSIVELMENTE_NAO": "POSSIVELMENTE NÃO — existem apenas pendências",
    "NAO_IDENTIFICADA": "NÃO IDENTIFICADA nos registros disponíveis",
}


def _dih(admission_date: str | None) -> int | None:
    """Dias de internação (DIH) -- ver `priority.days_since_admission`
    (extraído de lá, RF-29, compartilhado com `dashboard_metrics.py`)."""
    return days_since_admission(admission_date)


def _format_datetime_br(value: str | None) -> str:
    """ISO `AAAA-MM-DDTHH:MM:SS` -> "DD/MM/AAAA HH:MM" (pedido do pagador,
    DEC-119). Data sem hora (meia-noite exata vinda de "DD/MM/AAAA") sai só
    como "DD/MM/AAAA". Valor não reconhecido volta como está -- nunca some."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        return parsed.strftime("%d/%m/%Y")
    return parsed.strftime("%d/%m/%Y %H:%M")


# UI-008 (pedido do pagador, 2026-09-07): paciente que a IA ainda não
# analisou dizia só um traço/"sem contextualização" -- agora diz o motivo.
AWAITING_AI_TEXT = "Aguardando análise de IA"


def _format_median_hours(hours: float | None) -> str:
    """UI-008: o pagador leu "tempo indeterminado" na mediana como defeito.
    Sem pendência resolvida ainda não há mediana -- diz isso, não um traço."""
    if hours is None:
        return "sem histórico ainda"
    return _format_hours(hours)


def _format_hours(hours: float | None) -> str:
    if hours is None:
        return "tempo indeterminado"
    if hours < 24:
        return f"~{round(hours)}h"
    days, remainder_hours = divmod(hours, 24)
    return f"~{int(days)}d {round(remainder_hours)}h"


def _format_edd(edd_status: str | None, edd_data: str | None) -> str:
    # `is_edd_overdue` (app/analysis/priority.py) decide o QUE conta como
    # vencida -- compartilhado com o indicador agregado (REPORT-003) pra
    # nunca divergir do que este relatório individual mostra pro mesmo
    # paciente. Aqui só resta formatar a string de exibição.
    if edd_status == "REGISTRADA" and edd_data:
        try:
            parsed = date.fromisoformat(edd_data[:10])
        except ValueError:
            return html.escape(edd_data)
        if is_edd_overdue(edd_status, edd_data):
            diff = (date.today() - parsed).days
            return f"{parsed.strftime('%d/%m')} — VENCIDA há {diff}d"
        return parsed.strftime("%d/%m/%Y")
    if edd_status == "VENCIDA" and edd_data:
        return f"{html.escape(edd_data)} — VENCIDA"
    return "não documentada"


def _annotate_priority(pending_row: dict, necessidade_hospitalar: str | None, repo: Repository) -> dict:
    """Recalcula tempo decorrido e prioridade agora, na leitura -- não usa
    o snapshot gravado no momento da criação (ver módulo docstring). Também
    busca reiterações (DEC-062, seção 15 da orientação: "EVIDÊNCIA 1/
    EVIDÊNCIA 2") -- a primeira evidência já fica no próprio `pending_row`."""
    hours = hours_elapsed_since(pending_row["evidence_date"])
    priority = compute_priority(pending_row["category"], necessidade_hospitalar, hours)
    extra_evidence = repo.get_pending_item_evidence(pending_row["pending_id"])
    return {**pending_row, "_hours_elapsed": hours, "_priority": priority, "_extra_evidence": extra_evidence}


_PRIORITY_ORDER = {PRIORITY_ALTA: 0, PRIORITY_MEDIA: 1}


def _sort_key(item: dict):
    return (_PRIORITY_ORDER.get(item["_priority"], 2), -(item["_hours_elapsed"] or 0))


def generate_report(repo: Repository, run_id: str, unit: str, output_path: Path) -> Path:
    counts = repo.get_run_counts(run_id)
    failed = repo.get_failed_patients(run_id)
    no_admission = repo.get_no_admission_patients(run_id)
    awaiting_notes = repo.get_awaiting_notes_patients(run_id)  # RESIL-015
    # Sem filtro por `unit` abaixo -- achado real 2026-08-24 (DEC-061): a
    # conta GSUS enxerga várias unidades ao mesmo tempo (censo já é o escopo
    # de auditoria, confirmado pelo usuário), e `patients.unit` é texto que o
    # GSUS mostra por paciente, não um valor controlado por este app. `unit`
    # aqui (parâmetro da função) só rotula o título do relatório.
    #
    # `get_all_active_patients()` (não mais só quem tem pendência ativa OU
    # `patient_state`) é a fonte da verdade de QUEM entra no relatório --
    # achado real E2E-001 (2026-08-27): com a Fase 1/Fase 2 do DEC-071, um
    # paciente processado com sucesso pelas regras mas sem nenhum achado, e
    # que a Fase 2 (IA, pode levar dias num censo grande em hardware fraco --
    # DEC-070) ainda não alcançou, não tem nem pendência nem `patient_state`
    # -- ficava INVISÍVEL no relatório inteiro. Um censo de auditoria nunca
    # pode omitir paciente ativo em silêncio (RF-20).
    all_patients = repo.get_all_active_patients()
    pending_rows = repo.get_all_active_pending_items()

    by_patient: dict[str, list[dict]] = {}
    for row in pending_rows:
        by_patient.setdefault(row["patient_id"], []).append(dict(row))

    state_by_patient = {
        row["patient_id"]: row for row in repo.conn.execute("SELECT * FROM patient_state").fetchall()
    }

    # Anota prioridade/tempo em cada pendência de cada paciente ANTES de
    # montar as duas visões (censo + individual) -- mesmo cálculo para as
    # duas, uma só vez por paciente.
    annotated_by_patient: dict[str, list[dict]] = {}
    for patient in all_patients:
        patient_id = patient["patient_id"]
        state = state_by_patient.get(patient_id)
        necessidade = state["necessidade_hospitalar"] if state else None
        items = by_patient.get(patient_id, [])
        annotated_by_patient[patient_id] = sorted(
            (_annotate_priority(item, necessidade, repo) for item in items), key=_sort_key
        )

    census_rows = []
    beds_html = []
    for patient in sorted(all_patients, key=lambda p: p["bed"] or ""):
        patient_id = patient["patient_id"]
        state = state_by_patient.get(patient_id)
        bed = patient["bed"] or "?"
        record_number = patient["record_number"] or "?"
        patient_unit = patient["unit"] or "?"
        admission_date = patient["admission_date"]
        annotated_items = annotated_by_patient[patient_id]

        census_rows.append(_render_census_row(bed, patient_unit, admission_date, state, annotated_items))
        beds_html.append(_render_bed(bed, record_number, patient_unit, admission_date, state, annotated_items))

    # RF-29 (auditoria de certificação pré-entrega, 2026-09-01): os
    # indicadores agregados do serviço existiam desde a Fase 1 da dashboard
    # (`dashboard_metrics.py`), mas nunca chegavam ao arquivo HTML de
    # verdade -- só à tela Tkinter. Reusa o mesmo cálculo, nunca uma segunda
    # fonte de verdade sobre o que conta como "com pendência ativa"/"dia
    # vermelho"/etc.
    service_indicators = dashboard_metrics.compute_service_indicators(repo)
    unit_census = dashboard_metrics.compute_unit_census(repo)

    html_content = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Auditoria GSUS — {html.escape(unit)}</title>
<style>{CSS}</style>
</head>
<body>
<h1>AUDITORIA CONCORRENTE</h1>
<div class="meta">{html.escape(unit)} — gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>

<div class="summary">
  <div class="stat"><span class="n">{counts['found']}</span>Pacientes encontrados</div>
  <div class="stat"><span class="n">{counts['completed']}</span>Processados</div>
  <div class="stat failed"><span class="n">{counts['failed']}</span>Falhas</div>
  <div class="stat"><span class="n">{counts['no_admission']}</span>Sem internação atual</div>
  <div class="stat"><span class="n">{counts['awaiting_notes']}</span>Aguardando 1ª evolução</div>
</div>

{_render_failures(failed)}
{_render_no_admission(no_admission)}
{_render_awaiting_notes(awaiting_notes)}

{_render_service_indicators(service_indicators, unit_census)}

<div class="census">
  <h2>Censo de pendências — {html.escape(unit)}</h2>
  {_render_census_table(census_rows)}
</div>

<div class="unit">
  <h2>Relatório individual — {html.escape(unit)}</h2>
  {''.join(beds_html) if beds_html else '<p>Nenhum paciente processado com sucesso nesta execução.</p>'}
</div>

</body>
</html>
"""

    # Achado real (auditoria de resiliência 2026-08-28): esta função roda
    # DEZENAS de vezes por execução (uma vez por paciente na Fase 2) --
    # escrever direto em `output_path` deixa uma janela onde uma
    # interrupção no meio (kill, queda de energia, falha de disco) destrói o
    # relatório do dia anterior, que ainda era válido, sem deixar nenhum
    # relatório utilizável no lugar. Escreve num arquivo temporário no MESMO
    # diretório (mesma unidade -- necessário pra `os.replace` ser atômico) e
    # só troca o arquivo final quando a escrita já terminou por completo.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Sufixo por thread (não só por chamada): a Fase 2 (IA) agora pode rodar
    # numa thread separada, em paralelo com a Fase 1 -- sem isto, duas
    # chamadas concorrentes (uma de cada thread) escreveriam no MESMO
    # `relatorio.html.tmp`, arriscando corromper o conteúdo antes do
    # `os.replace` atômico. Cada thread tem seu próprio nome de tmp; a
    # última a chamar `os.replace` "vence" -- inofensivo, é sempre uma
    # regravação idempotente do mesmo relatório.
    tmp_path = output_path.with_name(f"{output_path.name}.{threading.get_ident()}.tmp")
    tmp_path.write_text(html_content, encoding="utf-8")
    os.replace(tmp_path, output_path)
    return output_path


class PatientLookupResult:
    NOT_FOUND = "NOT_FOUND"
    DISCHARGED = "DISCHARGED"


def generate_patient_report(repo: Repository, record_number: str) -> str | PatientLookupResult:
    """Gera o HTML do relatório individual de UM paciente a partir do que já
    está no banco -- nunca navega no GSUS ao vivo (redesenho de "Localizar
    Paciente", DEC-067: pedido do usuário pra buscar/gerar o relatório já
    processado em vez do histórico bruto ao vivo). Devolve
    `PatientLookupResult.NOT_FOUND`/`DISCHARGED` quando não há relatório pra
    mostrar -- quem chama decide a mensagem."""
    patient_row = repo.get_patient_by_record_number(record_number)
    if patient_row is None:
        return PatientLookupResult.NOT_FOUND
    if not patient_row["active"]:
        return PatientLookupResult.DISCHARGED

    patient_id = patient_row["patient_id"]
    state = repo.get_patient_state(patient_id)
    necessidade = state["necessidade_hospitalar"] if state else None
    pending_rows = repo.get_active_pending_items(patient_id)
    annotated_items = sorted(
        (_annotate_priority(dict(row), necessidade, repo) for row in pending_rows), key=_sort_key
    )

    bed_html = _render_bed(
        patient_row["bed"] or "?", record_number, patient_row["unit"] or "?",
        patient_row["admission_date"], state, annotated_items,
    )

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Auditoria GSUS — Prontuário {html.escape(record_number)}</title>
<style>{CSS}</style>
</head>
<body>
<h1>RELATÓRIO INDIVIDUAL</h1>
<div class="meta">Consulta gerada em {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>
{bed_html}
</body>
</html>
"""


def _render_failures(failed_rows) -> str:
    if not failed_rows:
        return ""
    items = "".join(
        f"<li>Leito {html.escape(row['bed'] or '?')} — prontuário {html.escape(row['record_number'])}</li>"
        for row in failed_rows
    )
    return f"""<div class="failures">
  <h2>Prontuários não processados ({len(failed_rows)})</h2>
  <ul>{items}</ul>
</div>"""


def _render_no_admission(rows) -> str:
    """Categoria separada de falha (DEC-054): paciente ainda listado, mas
    sem internação atual na tela -- provável alta recente. Não é um
    problema a investigar, por isso estilo neutro (não vermelho de erro)."""
    if not rows:
        return ""
    items = "".join(
        f"<li>Leito {html.escape(row['bed'] or '?')} — prontuário {html.escape(row['record_number'])}</li>"
        for row in rows
    )
    return f"""<div class="no-admission">
  <h2>Sem internação atual, prováveis altas recentes ({len(rows)})</h2>
  <ul>{items}</ul>
</div>"""


def _render_awaiting_notes(rows) -> str:
    """RESIL-015: admitidos há pouco (hoje/ontem) cujo card ou evolução o
    GSUS ainda não mostra. Não é falha nem alta -- estilo neutro, com a
    data de admissão pra o auditor reconhecer o caso de vista."""
    if not rows:
        return ""
    items = "".join(
        f"<li>Leito {html.escape(row['bed'] or '?')} — prontuário {html.escape(row['record_number'])}"
        f" — admitido em {html.escape(row['admission_date'] or '?')}</li>"
        for row in rows
    )
    return f"""<div class="no-admission">
  <h2>Admitidos há pouco, ainda sem evolução acessível ({len(rows)})</h2>
  <p>O GSUS ainda não mostra a internação ou a primeira evolução desses pacientes -- entram na próxima atualização automática.</p>
  <ul>{items}</ul>
</div>"""


def _render_census_row(bed: str, unit: str, admission_date: str | None, state, annotated_items: list[dict]) -> str:
    """Uma linha por paciente: leito, DIH, contexto resumido e a pendência
    de MAIOR prioridade -- é a visão que o auditor abre no início do dia
    (seção 11 da orientação), não uma lista de todas as pendências.

    Coluna "Unidade" (DEC-063): desde o DEC-061 uma execução pode misturar
    pacientes de unidades reais diferentes -- sem indicar de qual unidade é
    cada linha, dois leitos com o mesmo rótulo (ex. "2A") em setores
    diferentes ficam indistinguíveis."""
    dih = _dih(admission_date)
    context = (state["clinical_context"] if state else None) or (
        AWAITING_AI_TEXT if state is None else "(sem contextualização)"
    )
    context_short = (context[:80] + "…") if len(context) > 80 else context

    if annotated_items:
        main = annotated_items[0]
        pending_desc = main["description"]
        category = main["category"]
        priority = main["_priority"]
        time_label = _format_hours(main["_hours_elapsed"])
    else:
        pending_desc, category, priority, time_label = "Sem pendências identificadas", "—", "MONITORAMENTO", "—"

    return (
        f"<tr><td>{html.escape(unit)}</td><td>{html.escape(bed)}</td><td>{dih if dih is not None else '?'}</td>"
        f"<td>{html.escape(context_short)}</td><td>{html.escape(pending_desc)}</td>"
        f"<td>{html.escape(time_label)}</td><td>{html.escape(category)}</td>"
        f"<td class=\"prio-{html.escape(priority)}\">{html.escape(priority)}</td></tr>"
    )


def _render_census_table(rows: list[str]) -> str:
    if not rows:
        return "<p>Nenhum paciente processado com sucesso nesta execução.</p>"
    header = (
        "<tr><th>Unidade</th><th>Leito</th><th>DIH</th><th>Contexto</th><th>Pendência principal</th>"
        "<th>Tempo</th><th>Categoria</th><th>Prioridade</th></tr>"
    )
    return f"<table>{header}{''.join(rows)}</table>"


CATEGORY_DISPLAY = {"NAO_DEFINIDA": "não definida"}
PRIORITY_DISPLAY_ORDER = ["ALTA", "MEDIA", "MONITORAMENTO"]


def _render_breakdown_block(title: str, counts: dict[str, int | str], order: list[str] | None = None) -> str:
    if not counts:
        return f'<div class="block"><h3>{html.escape(title)}</h3><p class="no-pending">Sem dado.</p></div>'
    keys = order if order else sorted(counts.keys())
    items = "".join(
        f"<li>{html.escape(CATEGORY_DISPLAY.get(key, key))}: <strong>{counts[key]}</strong></li>"
        for key in keys if key in counts
    )
    return f'<div class="block"><h3>{html.escape(title)}</h3><ul>{items}</ul></div>'


def _render_service_indicators(
    indicators: "dashboard_metrics.ServiceIndicators", unit_census: list["dashboard_metrics.UnitCensusRow"]
) -> str:
    """Indicadores agregados do serviço (RF-29, seção 12 da orientação
    técnica) -- até esta correção (auditoria de certificação pré-entrega,
    2026-09-01), esses números só existiam na tela Tkinter
    (`app/ui/main_window.py`), nunca no arquivo HTML que "Abrir Relatório"
    de fato abre/compartilha/imprime. Reusa `dashboard_metrics` (mesmo
    cálculo da dashboard, nunca uma segunda fonte de verdade).

    Deliberadamente NÃO inclui a distribuição de CAUSAS de dia vermelho
    (`indicators.dia_causa_counts`) -- é texto livre do LLM, sem taxonomia
    fechada (mesmo motivo do DEC-101 já ter excluído isso de
    `daily_snapshot_category`). Este arquivo HTML é um artefato em disco
    tão ou mais persistente/compartilhável quanto aquela tabela -- só a
    CONTAGEM agregada (sempre segura) de dias vermelhos/verdes é exibida."""
    pct = indicators
    summary = f"""<div class="summary">
  <div class="stat"><span class="n">{pct.pct_patients_with_active_pending}%</span>Com pendência ativa</div>
  <div class="stat"><span class="n">{pct.pct_patients_without_edd}%</span>Sem EDD documentada</div>
  <div class="stat"><span class="n">{pct.pct_patients_with_edd_overdue}%</span>EDD vencida</div>
  <div class="stat compact"><span class="n">{_format_median_hours(pct.median_resolution_hours)}</span>Tempo mediano de resolução<small>horas entre o registro da pendência pela auditoria e sua resolução nas evoluções</small></div>
  <div class="stat dia-vermelho"><span class="n">{pct.patients_dia_vermelho}</span>Dia vermelho hoje</div>
  <div class="stat dia-verde"><span class="n">{pct.patients_dia_verde}</span>Dia verde hoje</div>
</div>"""

    by_category_hours = {
        cat: _format_hours(hours) for cat, hours in indicators.median_resolution_hours_by_category.items()
    }
    blocks = (
        _render_breakdown_block("Pendências por categoria", indicators.by_category)
        + _render_breakdown_block("Pendências por prioridade", indicators.by_priority, order=PRIORITY_DISPLAY_ORDER)
        + _render_breakdown_block(
            "Interno × externo",
            {ORIGIN_LABELS.get(k, k): v for k, v in indicators.by_origin.items()},
        )
        + _render_breakdown_block("Tempo mediano de resolução por categoria", by_category_hours)
    )

    if not unit_census:
        census_html = "<p>Nenhum paciente ativo nesta execução.</p>"
    else:
        rows = "".join(
            f"<tr><td>{html.escape(row.unit)}</td><td>{row.patient_count}</td>"
            f"<td>{row.patients_with_active_pending}</td><td>{row.active_pending_count}</td>"
            f"<td class=\"prio-ALTA\">{row.by_priority.get('ALTA', 0)}</td>"
            f"<td class=\"prio-MEDIA\">{row.by_priority.get('MEDIA', 0)}</td>"
            f"<td class=\"prio-MONITORAMENTO\">{row.by_priority.get('MONITORAMENTO', 0)}</td></tr>"
            for row in unit_census
        )
        # UI-008: linha de total -- a visão "em poucos segundos" do dia inteiro.
        total_row = (
            f"<tr class=\"total\"><td>Total</td><td>{sum(r.patient_count for r in unit_census)}</td>"
            f"<td>{sum(r.patients_with_active_pending for r in unit_census)}</td>"
            f"<td>{sum(r.active_pending_count for r in unit_census)}</td>"
            f"<td class=\"prio-ALTA\">{sum(r.by_priority.get('ALTA', 0) for r in unit_census)}</td>"
            f"<td class=\"prio-MEDIA\">{sum(r.by_priority.get('MEDIA', 0) for r in unit_census)}</td>"
            f"<td class=\"prio-MONITORAMENTO\">{sum(r.by_priority.get('MONITORAMENTO', 0) for r in unit_census)}</td></tr>"
        )
        census_html = (
            "<table><tr><th>Unidade</th><th>Pacientes</th><th>Com pendência ativa</th>"
            "<th>Pendências ativas</th><th>Alta</th><th>Média</th><th>Monitoramento</th></tr>"
            f"{rows}{total_row}</table>"
        )

    # UI-008 (pedido do pagador: "censo por unidade: muito importante"): em
    # destaque, ANTES dos demais indicadores, com moldura própria.
    return f"""<div class="indicators">
  <h2>Indicadores do serviço</h2>
  <div class="unit-census">
    <h3>Censo agregado por unidade</h3>
    {census_html}
  </div>
  {summary}
  <div class="indicators-breakdown">{blocks}</div>
</div>"""


def _render_bed(
    bed: str, record_number: str, unit: str, admission_date: str | None, state, annotated_items: list[dict]
) -> str:
    dih = _dih(admission_date)
    dih_label = f"{dih}º DIH" if dih is not None else "DIH não determinado"

    if state and state["clinical_context"]:
        clinical_context = html.escape(state["clinical_context"])
    else:
        clinical_context = AWAITING_AI_TEXT if state is None else "(sem contextualização disponível)"

    necessidade_code = state["necessidade_hospitalar"] if state else None
    necessidade_label = NECESSIDADE_LABELS.get(necessidade_code, "não avaliada ainda")
    justificativa = state["necessidade_hospitalar_justificativa"] if state else None
    necessidade_html = html.escape(necessidade_label)
    if justificativa:
        necessidade_html += f" — {html.escape(justificativa)}"

    objetivo = html.escape(state["objetivo_terapeutico"]) if state and state["objetivo_terapeutico"] else "(não definido)"
    proximo_passo = html.escape(state["proximo_passo"]) if state and state["proximo_passo"] else "(não definido)"
    edd_html = _format_edd(state["edd_status"] if state else None, state["edd_data"] if state else None)

    dia = state["dia_classificacao"] if state else None
    day_badge = f'<span class="day-badge {html.escape(dia)}">DIA {html.escape(dia)}</span>' if dia else ""
    dia_causa = state["dia_causa"] if state else None
    dia_line = ""
    if dia:
        dia_line = f"<p><strong>Classificação:</strong> DIA {html.escape(dia)}"
        if dia_causa:
            dia_line += f" — {html.escape(dia_causa)}"
        dia_line += "</p>"

    # A evidência de CADA pendência (incluindo a principal) fica sempre
    # visível via `_render_pending` -- resumo em destaque não substitui
    # auditabilidade completa (RF-26: "o auditor deve conseguir clicar em
    # 'de onde veio?'"). Bug real corrigido aqui: a 1ª versão só mostrava a
    # evidência das pendências SECUNDÁRIAS, deixando o caso comum (uma
    # única pendência) sem evidência nenhuma visível.
    main_pending_html = ""
    all_pending_html = ""
    if annotated_items:
        main = annotated_items[0]
        main_pending_html = (
            f"<p><strong>Pendência principal:</strong> {html.escape(main['description'])} "
            f"({html.escape(main['_priority'])}, {html.escape(_format_hours(main['_hours_elapsed']))})</p>"
        )
        pending_label = "Pendências" if len(annotated_items) > 1 else "Evidência"
        all_pending_html = (
            f'<div class="section-label">{pending_label}</div>'
            + "".join(_render_pending(item) for item in annotated_items)
        )
    else:
        main_pending_html = '<p class="no-pending">Sem pendências identificadas.</p>'

    # Identificação operacional (seção 2A da orientação técnica): só exibe
    # se pelo menos um dos dois for conhecido -- dado que nem toda análise
    # (nem toda análise anterior a este campo existir, DEC-063) vai ter isso.
    especialidade = state["especialidade_responsavel"] if state else None
    origem = state["origem_internacao"] if state else None
    identificacao_parts = []
    if especialidade:
        identificacao_parts.append(f"Especialidade responsável: {html.escape(especialidade)}")
    if origem:
        identificacao_parts.append(f"Origem da internação: {html.escape(origem)}")
    identificacao_html = f'<p class="meta-line">{" · ".join(identificacao_parts)}</p>' if identificacao_parts else ""

    # Trilha de auditoria mínima (RF-30, "Princípios de segurança" da
    # orientação): versão do modelo + horário da última análise por IA.
    last_analysis_at = state["last_analysis_at"] if state else None
    model_version = state["model_version"] if state else None
    audit_parts = []
    if model_version:
        audit_parts.append(f"modelo: {html.escape(model_version)}")
    if last_analysis_at:
        audit_parts.append(f"última análise por IA: {html.escape(last_analysis_at)}")
    audit_trail_html = f'<p class="meta-line">{" · ".join(audit_parts)}</p>' if audit_parts else ""

    # Achado real 2026-08-25 (DEC-066): paciente com muito backlog acumulado
    # manda pro LLM só as últimas LLM_LOOKBACK_DAYS (orchestrator.py) --
    # hardware fraco não dá conta do histórico inteiro numa chamada só
    # (timeout e estouro de contexto observados de verdade). Auditor precisa
    # saber que a análise é PARCIAL e que existe histórico mais antigo não
    # revisado por IA -- nunca esconder essa limitação (RF-26).
    # "Consulte o GSUS diretamente" (não mais "Localizar Paciente") desde o
    # DEC-067 -- Localizar passou a mostrar só o que já está neste mesmo
    # banco (mesma janela de 2 semanas), então não ajudaria a ver o
    # histórico mais antigo que ficou de fora desta análise.
    window_limited = bool(state["analysis_window_limited"]) if state else False
    window_limited_html = (
        '<div class="window-limited-warning">⚠ Esta análise considera só as evoluções mais '
        "recentes (histórico acumulado grande demais pra uma única análise por IA nesta "
        "máquina). Pode haver contexto clínico mais antigo não revisado -- consulte o "
        "prontuário diretamente no GSUS se precisar do histórico integral.</div>"
        if window_limited else ""
    )

    return f"""<div class="bed">
  <h3>LEITO {html.escape(bed)} — Prontuário {html.escape(record_number)} — {dih_label}{day_badge}
    <span class="unit-label">({html.escape(unit)})</span></h3>
  {window_limited_html}
  {identificacao_html}
  <div class="section-label">Contexto</div>
  <p>{clinical_context}</p>
  <div class="section-label">Necessidade hospitalar</div>
  <p>{necessidade_html}</p>
  <div class="section-label">Objetivo</div>
  <p>{objetivo}</p>
  {main_pending_html}
  <div class="section-label">Próximo passo</div>
  <p>{proximo_passo}</p>
  <div class="section-label">Previsão de alta</div>
  <p>{edd_html}</p>
  {dia_line}
  {all_pending_html}
  {audit_trail_html}
</div>"""


def _format_sla(category: str) -> str:
    limit = get_sla_hours(category)
    return f"SLA institucional: {limit}h" if limit is not None else "sem SLA definido"


def _render_pending(item: dict) -> str:
    extra_evidence = item.get("_extra_evidence") or []
    has_multiple = bool(extra_evidence)

    evidence_date = item["evidence_date"]
    first_label = "Evidência 1" if has_multiple else "Evidência"
    date_suffix = f" ({html.escape(_format_hours(item['_hours_elapsed']))})" if evidence_date else ""
    first_evidence_html = (
        f'<div class="evidence">{html.escape(first_label)}'
        f'{f": {html.escape(_format_datetime_br(evidence_date))}" if evidence_date else ""} — '
        f'&ldquo;{html.escape(item["evidence"])}&rdquo;{date_suffix}</div>'
    )
    extra_evidence_html = "".join(
        f'<div class="evidence">Evidência {i + 2}'
        f'{f": {html.escape(_format_datetime_br(row["timestamp"]))}" if row["timestamp"] else ""} — '
        f'&ldquo;{html.escape(row["text"])}&rdquo;</div>'
        for i, row in enumerate(extra_evidence)
    )

    inferred_tag = '<span class="inferred-tag">inferido</span>' if item.get("is_inferred") else ""
    origin_label = ORIGIN_LABELS.get(item.get("origin"))
    origin_tag = f'<span class="origin-tag">barreira {html.escape(origin_label)}</span>' if origin_label else ""

    meta_parts = [_format_sla(item["category"])]
    confidence_label = CONFIDENCE_LABELS.get(item.get("confidence"))
    if confidence_label:
        meta_parts.append(f"confiança {confidence_label}")
    if item.get("flow_status"):
        meta_parts.append(f"fluxo: {item['flow_status']}")
    meta_line = f'<div class="meta-line">{html.escape(" · ".join(meta_parts))}</div>'

    return f"""<div class="pending">
  <div class="category">{html.escape(item['category'])} — {html.escape(item['description'])}{inferred_tag}{origin_tag}</div>
  {first_evidence_html}
  {extra_evidence_html}
  {meta_line}
</div>"""
