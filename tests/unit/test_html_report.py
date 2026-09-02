from app.models import Patient
from app.reports.html_report import generate_report
from app.storage import database
from app.storage.repository import Repository


def _setup_repo(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    return Repository(conn)


def test_report_shows_summary_counts_and_failures(tmp_path):
    repo = _setup_repo(tmp_path)
    ok_patient = repo.upsert_patient(Patient(record_number="111", bed="2A", unit="Clínica Médica"))
    failed_patient = repo.upsert_patient(Patient(record_number="222", bed="2B", unit="Clínica Médica"))

    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [ok_patient, failed_patient])
    repo.mark_processing(run_id, ok_patient)
    repo.mark_done(run_id, ok_patient)
    repo.mark_processing(run_id, failed_patient)
    repo.mark_error(run_id, failed_patient, "TimeoutError ao abrir prontuário")

    repo.save_patient_state(ok_patient, "Internado por AVC isquêmico.", "Estável.")
    repo.add_pending_item(
        ok_patient, "INTERCONSULTA", "Aguarda avaliação da cirurgia torácica",
        "Solicitada avaliação da cirurgia torácica...", "2026-08-20T10:31:00",
    )

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "Pacientes encontrados" in content
    assert ">2<" in content  # found = 2
    assert "LEITO 2A" in content
    assert "cirurgia torácica" in content
    assert "prontuário 222" in content  # falha visível
    assert "Solicitada avaliação da cirurgia torácica" in content


def test_report_shows_pending_item_when_no_patient_state_exists_yet(tmp_path):
    """Gap apontado na revisão de conformidade (DEC-064): uma pendência de
    REGRA (RULES-001) pode existir antes de qualquer análise por IA rodar
    (ex.: llm=None, ou notas já vistas mas regras reavaliadas mesmo assim)
    -- generate_report precisa lidar com isso sem quebrar nem esconder a
    pendência (caminho `by_patient[...]` quando `state_by_patient` não tem
    o paciente)."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="112", bed="2C", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    # De propósito: NUNCA chama repo.save_patient_state para este paciente.
    repo.add_pending_item(
        patient_id, "DIAGNOSTICO", "Aguarda tomografia", "Solicitada tomografia de tórax.",
        "2026-08-20T08:00:00", source="RULE",
    )

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "LEITO 2C" in content
    assert "tomografia" in content.lower()


def test_report_shows_patient_processed_by_rules_only_with_no_findings_yet(tmp_path):
    """Achado real E2E-001 (2026-08-27): DEC-071 separou regras (Fase 1,
    sempre roda, síncrona) da análise por IA (Fase 2, pode levar dias num
    censo grande em hardware fraco -- DEC-070). Um paciente processado com
    sucesso pela Fase 1 sem nenhuma pendência de regra encontrada, e que a
    Fase 2 ainda não alcançou, não tem `pending_items` nem `patient_state`
    -- antes desta correção, ficava INVISÍVEL no relatório inteiro (nem
    censo, nem individual), mesmo `mark_done` com sucesso. Gravíssimo pra
    uma ferramenta de auditoria: paciente ativo silenciosamente ausente do
    censo diário, sem nenhum aviso."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="1000", bed="10A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    # De propósito: nem save_patient_state, nem add_pending_item -- Fase 1
    # (regras) rodou e concluiu, mas a Fase 2 (IA) ainda não chegou nele.

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "LEITO 10A" in content
    assert "Prontuário 1000" in content
    assert "Sem pendências identificadas" in content
    assert "não avaliada ainda" in content


def test_report_shows_no_pending_message_when_clean(tmp_path):
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="333", bed="3A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "Contexto qualquer.", "Estável, sem pendências.")

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "Sem pendências identificadas" in content
    assert "prontuário 333" not in content  # não houve falha


def test_report_shows_patients_regardless_of_scraped_unit_label(tmp_path):
    """Reproduz achado real 2026-08-24 (DEC-061): a conta GSUS enxerga várias
    unidades ao mesmo tempo (censo já é o escopo de auditoria), e
    `patients.unit` é texto livre que o GSUS mostra por paciente -- nunca
    igual ao 'setor' configurado no app. O relatório não pode filtrar por
    essa igualdade, senão vem vazio mesmo com pacientes processados."""
    repo = _setup_repo(tmp_path)
    patient_a = repo.upsert_patient(Patient(record_number="555", bed="5A", unit="4-Internados P.A."))
    patient_b = repo.upsert_patient(Patient(record_number="666", bed="6B", unit="ENF. MEDICO-CIRURGICA 2"))

    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_a, patient_b])
    repo.mark_processing(run_id, patient_a)
    repo.mark_done(run_id, patient_a)
    repo.mark_processing(run_id, patient_b)
    repo.mark_done(run_id, patient_b)
    repo.save_patient_state(patient_a, "Contexto A.", "Estável.")
    repo.save_patient_state(patient_b, "Contexto B.", "Estável.")

    # "Auditoria" não bate com NENHUM dos dois `unit` reais acima -- de
    # propósito, é exatamente o cenário real que expôs o bug.
    output = generate_report(repo, run_id, "Auditoria", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "Nenhum paciente processado com sucesso" not in content
    assert "LEITO 5A" in content
    assert "LEITO 6B" in content


def test_report_shows_unit_per_patient(tmp_path):
    """DEC-063: desde que o filtro por unit foi removido (DEC-061), o
    relatório precisa indicar de qual unidade real é cada paciente -- sem
    isso, dois "LEITO 2A" de setores diferentes ficam indistinguíveis."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="777", bed="2A", unit="4-Internados P.A."))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "Contexto.", "Estável.")

    output = generate_report(repo, run_id, "Auditoria", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "4-Internados P.A." in content
    assert "<th>Unidade</th>" in content


def test_report_shows_origin_confidence_and_sla(tmp_path):
    """Seção 13 (interna/externa) e seção 15 (confiança) da orientação
    técnica: dado já existia no banco, mas nunca aparecia no HTML (achado
    da revisão de conformidade, DEC-063)."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="888", bed="8A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "Contexto.", "Estável.")
    repo.add_pending_item(
        patient_id, "DIAGNOSTICO", "Aguarda tomografia", "Solicitada tomografia de tórax.",
        "2026-08-20T08:00:00", origin="INTERNA", confidence="ALTA", is_inferred=True, source="LLM",
    )

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "barreira interna" in content
    assert "confiança alta" in content
    assert "SLA institucional: 48h" in content
    assert "inferido" in content  # is_inferred=True acima -- gap apontado na revisão de conformidade


def test_report_shows_external_origin_medium_low_confidence_and_flow_status(tmp_path):
    """Complementa o teste acima: só INTERNA/ALTA eram exercitados antes
    (gap apontado na revisão de conformidade, DEC-064)."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="889", bed="8B", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "Contexto.", "Estável.")
    repo.add_pending_item(
        patient_id, "TRANSFERENCIA", "Aguarda vaga em UTI", "Regulação acionada para vaga de UTI.",
        "2026-08-20T08:00:00", origin="EXTERNA", confidence="BAIXA", flow_status="SOLICITADA", source="LLM",
    )

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "barreira externa" in content
    assert "confiança baixa" in content
    assert "fluxo: SOLICITADA" in content


def test_report_shows_multiple_evidences_as_reiterations(tmp_path):
    """Seção 15 da orientação técnica: "EVIDÊNCIA 1 / EVIDÊNCIA 2" pra
    mesma pendência -- ficou possível de verdade só depois do DEC-062
    (reconciliação de pendências entre execuções)."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="999", bed="9A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "Contexto.", "Estável.")
    pending_id = repo.add_pending_item(
        patient_id, "DIAGNOSTICO", "Aguarda RM", "Solicito RM de coluna lombar.", "2026-08-20T09:32:00",
    )
    repo.add_pending_item_evidence(pending_id, "2026-08-21T08:10:00", "Mantém aguardo de RM.")

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "Evidência 1" in content
    assert "Evidência 2" in content
    assert "Solicito RM de coluna lombar." in content
    assert "Mantém aguardo de RM." in content


def test_report_escapes_html_in_evidence_text(tmp_path):
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="444", bed="4A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "x", "y")
    repo.add_pending_item(patient_id, "EXAME", "d", "<script>alert(1)</script>", None)

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;" in content


def test_report_shows_window_limited_warning(tmp_path):
    """Achado real 2026-08-25 (DEC-066): quando o LLM só viu as evoluções
    mais recentes (backlog grande demais pro hardware), o relatório precisa
    avisar -- nunca esconder que a análise é parcial (RF-26)."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="333", bed="3A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "Contexto.", "Estável.", analysis_window_limited=True)

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert '<div class="window-limited-warning">' in content
    assert "GSUS" in content


def test_report_write_is_atomic_and_leaves_no_leftover_tmp_file(tmp_path):
    """Achado real (auditoria de resiliência 2026-08-28): `generate_report`
    roda dezenas de vezes por execução (uma por paciente na Fase 2) --
    escrever direto no arquivo final deixava uma janela onde uma
    interrupção no meio destruía o relatório do dia anterior sem deixar
    nada utilizável no lugar. Agora escreve num `.tmp` e troca via
    `os.replace` (atômico) só quando a escrita termina por completo."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="335", bed="3C", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    report_path = tmp_path / "relatorio.html"

    output = generate_report(repo, run_id, "Clínica Médica", report_path)

    assert output == report_path
    assert report_path.exists()
    assert not report_path.with_name(report_path.name + ".tmp").exists()


def test_report_does_not_show_window_limited_warning_by_default(tmp_path):
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="334", bed="3B", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(patient_id, "Contexto.", "Estável.")

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert '<div class="window-limited-warning">' not in content


# --------------------------------------------------- RF-29 (auditoria de certificação, 2026-09-01)
# Achado real: os indicadores agregados do serviço (% com pendência ativa,
# tempo mediano por categoria, % sem EDD, interno×externo) já existiam
# desde a Fase 1 da dashboard (dashboard_metrics.py), mas nunca apareciam
# no arquivo HTML de verdade -- só na tela Tkinter. "Abrir Relatório" abre
# justamente este arquivo, então o indicador exigido pelo RF-29 nunca
# chegava ao auditor que só usa o relatório (não a tela).

def test_report_includes_service_indicators_section(tmp_path):
    repo = _setup_repo(tmp_path)
    p1 = repo.upsert_patient(Patient(record_number="501", bed="1A", unit="UTI"))
    p2 = repo.upsert_patient(Patient(record_number="502", bed="1B", unit="UTI"))
    p3 = repo.upsert_patient(Patient(record_number="503", bed="2A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [p1, p2, p3])
    for pid in (p1, p2, p3):
        repo.mark_processing(run_id, pid)
        repo.mark_done(run_id, pid)

    repo.add_pending_item(p1, "DIAGNOSTICO", "Aguarda TC", "evid", "2026-08-20T08:00:00", origin="INTERNA")
    repo.add_pending_item(p2, "PROCEDIMENTO_CIRURGIA", "Aguarda cirurgia", "evid", "2026-08-20T08:00:00", origin="INTERNA")

    output = generate_report(repo, run_id, "Auditoria", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert '<div class="indicators">' in content
    assert "Indicadores do serviço" in content
    assert "Com pendência ativa" in content
    assert "Sem EDD documentada" in content
    assert "Tempo mediano de resolução" in content
    assert "DIAGNOSTICO" in content
    assert "PROCEDIMENTO_CIRURGIA" in content
    # censo agregado por unidade -- diferente da tabela por-paciente já existente
    assert "Censo agregado por unidade" in content
    assert "UTI" in content
    assert "Clínica Médica" in content


def test_report_service_indicators_never_include_dia_causa_free_text(tmp_path):
    """Mesmo limite de segurança já aplicado em daily_snapshot_category
    (DEC-101): `dia_causa` é texto livre do LLM, sem taxonomia fechada.

    A seção AGREGADA (`<div class="indicators">`) nunca deve mostrar esse
    texto -- só a CONTAGEM agregada (sempre segura) de dias vermelhos. Isto
    é DIFERENTE de mostrar a causa no relatório INDIVIDUAL do próprio
    paciente (`_render_bed`/RF-28) -- isso continua correto e obrigatório,
    é a mesma pessoa vendo sua própria evidência, não uma agregação
    cross-paciente sem taxonomia fechada."""
    repo = _setup_repo(tmp_path)
    patient_id = repo.upsert_patient(Patient(record_number="504", bed="3A", unit="Clínica Médica"))
    run_id = repo.start_run()
    repo.enqueue_patients(run_id, [patient_id])
    repo.mark_processing(run_id, patient_id)
    repo.mark_done(run_id, patient_id)
    repo.save_patient_state(
        patient_id, "Contexto.", "Estável.",
        dia_classificacao="VERMELHO",
        dia_causa="aguardando parecer da neurocirurgia sobre paciente com sequela pos-AVC",
    )

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert "Dia vermelho hoje" in content  # a CONTAGEM aparece na seção agregada
    # A causa É esperada no relatório INDIVIDUAL do próprio paciente (RF-28)
    assert "neurocirurgia" in content

    indicators_section = content[content.index('<div class="indicators">'):content.index('<div class="census">')]
    assert "neurocirurgia" not in indicators_section  # mas NUNCA na seção agregada
    assert "sequela" not in indicators_section


def test_report_service_indicators_safe_on_empty_database(tmp_path):
    """Primeira execução (banco vazio) não pode quebrar o relatório inteiro
    -- mesmo raciocínio de compute_service_indicators com banco vazio."""
    repo = _setup_repo(tmp_path)
    run_id = repo.start_run()
    repo.finish_run(run_id, "COMPLETED")

    output = generate_report(repo, run_id, "Clínica Médica", tmp_path / "relatorio.html")
    content = output.read_text(encoding="utf-8")

    assert '<div class="indicators">' in content
    assert "Nenhum paciente ativo nesta execução." in content
