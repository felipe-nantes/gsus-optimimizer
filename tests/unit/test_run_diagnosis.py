"""DIAG-001 (2026-09-03): classificação em linguagem simples do resultado
de uma execução, pra que o auditor (sem conhecimento técnico) saiba se uma
falha foi o GSUS/máquina (não é defeito do programa) ou algo que precisa de
suporte de verdade. A correção destes testes É o produto -- uma
classificação errada nos dois sentidos é ruim: dizer "tudo certo" quando
teve um bug real esconde o problema; gritar "erro grave" a cada instabilidade
comum do GSUS cansa o auditor e ele passa a ignorar o aviso."""
from datetime import datetime, timedelta, timezone

from app.analysis import run_diagnosis


# --------------------------------------------------------- is_known_gsus_error
def test_known_gsus_patterns_recognized():
    known_examples = [
        "GSUSCensusIncompleteError: Paginação interrompida após 5 tentativas -- 40 paciente(s) coletado(s) até aqui, censo pode estar incompleto.",
        "GSUSRecordError: Nenhuma internação em andamento encontrada após 5 tentativas (marcador 'Permanece Internado' não apareceu).",
        "GSUSLoginError: modal de seleção de estabelecimento não apareceu/fechou.",
    ]
    for text in known_examples:
        assert run_diagnosis.is_known_gsus_error(text), text


def test_raw_playwright_error_text_is_never_auto_classified_as_known_gsus_cause():
    """Achado real ao implementar (verificado contra a classe de verdade,
    não suposição): `type(exc).__name__` do Playwright pra `TimeoutError`/
    `Error` é literalmente "TimeoutError"/"Error" -- nunca aparece como
    "PlaywrightTimeoutError"/"PlaywrightError" no texto persistido. Um erro
    bruto do Playwright escapando de um ponto sem retry (achado real da
    auditoria de catálogo, DEC-111) deve continuar caindo como "precisa de
    investigação" -- são justamente gaps de retry ainda não fechados, não
    instabilidade já mapeada."""
    assert not run_diagnosis.is_known_gsus_error("TimeoutError: Timeout 30000ms exceeded.")
    assert not run_diagnosis.is_known_gsus_error("Error: Frame was detached.")


def test_unrecognized_error_defaults_to_not_known():
    """Achado da auditoria de catálogo (DEC-111): erro bruto do Playwright
    que escapa de um ponto sem retry (ex.: 'Frame was detached' em
    _first_record_number) ou qualquer exceção nova (KeyError, ValueError)
    não deve ser confundido com instabilidade já conhecida do GSUS."""
    unknown_examples = [
        "KeyError: 'unidade'",
        "ValueError: could not convert string to float",
        "GSUSRecordError: O número do prontuário não foi aceito corretamente pelo campo de busca do GSUS.",
        "GSUSCensusError: Linha 3 da tabela de resultado tem 2 coluna(s), esperava pelo menos 7.",
    ]
    for text in unknown_examples:
        assert not run_diagnosis.is_known_gsus_error(text), text


def test_empty_or_none_error_is_not_known():
    assert not run_diagnosis.is_known_gsus_error(None)
    assert not run_diagnosis.is_known_gsus_error("")


# --------------------------------------------------- classify_top_level_exception
def test_login_error_classified_as_gsus_cause():
    from app.gsus.login import GSUSLoginError

    outcome, summary = run_diagnosis.classify_top_level_exception(
        GSUSLoginError("modal de seleção de estabelecimento não apareceu/fechou.")
    )
    assert outcome == run_diagnosis.OUTCOME_FALHA_GSUS
    assert "não é um defeito deste programa" in summary or "não é um defeito" in summary or "GSUS" in summary


def test_census_error_classified_as_gsus_cause():
    from app.gsus.census import GSUSCensusError

    outcome, _summary = run_diagnosis.classify_top_level_exception(GSUSCensusError("falhou de vez"))
    assert outcome == run_diagnosis.OUTCOME_FALHA_GSUS


def test_raw_playwright_error_classified_as_gsus_cause():
    from playwright.sync_api import Error as PlaywrightError

    outcome, _summary = run_diagnosis.classify_top_level_exception(PlaywrightError("Frame was detached"))
    assert outcome == run_diagnosis.OUTCOME_FALHA_GSUS


def test_missing_credential_classified_as_needs_setup_not_gsus():
    outcome, summary = run_diagnosis.classify_top_level_exception(
        RuntimeError("Credencial GSUS não configurada.")
    )
    assert outcome == run_diagnosis.OUTCOME_FALHA_INESPERADA
    assert "Configurações" in summary


def test_sqlite_error_classified_as_local_infra_not_gsus():
    import sqlite3

    outcome, summary = run_diagnosis.classify_top_level_exception(sqlite3.OperationalError("database is locked"))
    assert outcome == run_diagnosis.OUTCOME_FALHA_INESPERADA
    assert "GSUS" not in summary or "não relacionada" in summary


def test_genuinely_unknown_exception_classified_as_unexpected():
    """Uma exceção que não é nenhuma das classificadas -- ex.: um bug real
    novo introduzido por uma mudança futura -- NUNCA deve cair
    silenciosamente como 'causa do GSUS'. Default seguro: precisa de
    investigação."""
    outcome, summary = run_diagnosis.classify_top_level_exception(KeyError("campo_inesperado"))
    assert outcome == run_diagnosis.OUTCOME_FALHA_INESPERADA
    assert "suporte técnico" in summary


# ------------------------------------------------------- classify_completed_run
def test_clean_success_with_no_errors():
    outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
        found=50, completed=50, no_admission=0, patient_errors=[], census_complete=True,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO
    assert not needs_attention
    assert "50" in summary


def test_incomplete_census_is_partial_gsus_never_unexpected():
    """Mesmo com ZERO erro de paciente, um censo incompleto (paginação do
    GSUS desistiu) precisa ser sinalizado -- é o cenário exato do achado
    real de auditoria (madrugada 2026-09-01/02, DEC-108) onde pacientes
    ficaram de fora sem nenhum outro sinal de erro."""
    outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
        found=20, completed=20, no_admission=0, patient_errors=[], census_complete=False,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO_PARCIAL_GSUS
    assert not needs_attention
    assert "GSUS" in summary
    assert "não é defeito deste programa" in summary


def test_low_failure_rate_all_known_gsus_causes_is_partial_success_no_attention():
    patient_errors = ["GSUSRecordError: Nenhuma internação em andamento encontrada após 5 tentativas (marcador 'Permanece Internado' não apareceu)."] * 3
    outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
        found=100, completed=97, no_admission=0, patient_errors=patient_errors, census_complete=True,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO_PARCIAL_GSUS
    assert not needs_attention  # 3% -- abaixo do limiar, não precisa de atenção extra
    assert "3" in summary


def test_high_failure_rate_all_known_gsus_causes_suggests_manual_review():
    patient_errors = ["GSUSRecordError: Nenhuma internação em andamento encontrada após 5 tentativas (marcador 'Permanece Internado' não apareceu)."] * 60
    outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
        found=100, completed=40, no_admission=0, patient_errors=patient_errors, census_complete=True,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO_PARCIAL_GSUS
    assert needs_attention
    assert "conferência manual" in summary


def test_any_unrecognized_error_forces_unexpected_outcome_even_if_rare():
    """Achado central desta feature: mesmo 1 erro de tipo desconhecido no
    meio de 99 erros conhecidos do GSUS precisa aparecer pro auditor como
    'precisa de investigação' -- não pode ficar escondido na média."""
    patient_errors = (
        ["GSUSRecordError: Nenhuma internação em andamento encontrada após 5 tentativas (marcador 'Permanece Internado' não apareceu)."] * 5
        + ["KeyError: 'campo_novo_desconhecido'"]
    )
    outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
        found=100, completed=94, no_admission=0, patient_errors=patient_errors, census_complete=True,
    )
    assert outcome == run_diagnosis.OUTCOME_FALHA_INESPERADA
    assert needs_attention
    assert "1" in summary
    assert "suporte técnico" in summary


def test_no_admission_patients_never_counted_as_errors():
    outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
        found=10, completed=8, no_admission=2, patient_errors=[], census_complete=True,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO
    assert not needs_attention
    assert "2" in summary


def test_zero_patients_found_is_a_plain_success():
    outcome, _summary, needs_attention = run_diagnosis.classify_completed_run(
        found=0, completed=0, no_admission=0, patient_errors=[], census_complete=True,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO
    assert not needs_attention


# ------------------------------------------------------- compute_staleness_warning
def test_no_diagnostic_ever_produces_no_staleness_warning():
    """Banco novo (recém-instalado ou recém-atualizado pra esta versão) não
    deve alarmar o auditor por não ter histórico ainda."""
    warning = run_diagnosis.compute_staleness_warning(None, "00:01", datetime.now(timezone.utc))
    assert warning is None


def test_recent_diagnostic_produces_no_staleness_warning():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=5)).isoformat()
    warning = run_diagnosis.compute_staleness_warning(recent, "00:01", now)
    assert warning is None


def test_stale_diagnostic_triggers_warning_attributing_to_scheduler_not_bug():
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=40)).isoformat()
    warning = run_diagnosis.compute_staleness_warning(stale, "00:01", now)
    assert warning is not None
    assert "desligado ou hibernando" in warning
    assert "não um defeito deste programa" in warning


# ------------------------------------------------------- describe_cancelled_run (UI-006)
def test_cancelled_run_is_described_as_user_action_not_failure():
    outcome, summary, needs_attention = run_diagnosis.describe_cancelled_run(found=80, completed=3)
    assert outcome == run_diagnosis.OUTCOME_CANCELADA
    assert needs_attention is False
    assert "3 de 80" in summary
    assert "encerrada pelo usuário" in summary.lower()


# ------------------------------------------ DEC-117: GSUS sem responder a busca
def test_search_unresponsive_error_is_a_known_gsus_cause():
    assert run_diagnosis.is_known_gsus_error(
        "GSUSSearchUnresponsiveError: A tela de busca de prontuário não respondeu em 5 tentativas"
    )


def test_gsus_unresponsive_run_is_described_as_gsus_failure_needing_attention():
    outcome, summary, needs_attention = run_diagnosis.describe_gsus_unresponsive_run(
        found=183, completed=6, consecutive_failures=6, relogin_attempted=True,
    )
    assert outcome == run_diagnosis.OUTCOME_FALHA_GSUS
    assert needs_attention is True
    assert "6 paciente(s) seguido(s)" in summary
    assert "6 de 183" in summary
    assert "refazer o login" in summary


def test_awaiting_notes_patients_never_counted_as_errors():
    """RESIL-015: admitidos há pouco sem evolução acessível são categoria
    benigna -- execução limpa, sem atenção, e o resumo explica que entram
    na próxima atualização."""
    outcome, summary, needs_attention = run_diagnosis.classify_completed_run(
        found=10, completed=7, no_admission=1, patient_errors=[], census_complete=True, awaiting_notes=2,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO
    assert not needs_attention
    assert "2 admitido(s) há pouco" in summary
    assert "1 sem internação atual" in summary
    assert "próxima atualização" in summary


def test_classify_completed_run_keeps_working_without_awaiting_notes_argument():
    outcome, summary, _ = run_diagnosis.classify_completed_run(
        found=3, completed=3, no_admission=0, patient_errors=[], census_complete=True,
    )
    assert outcome == run_diagnosis.OUTCOME_SUCESSO
    assert summary.endswith("processado(s).")
