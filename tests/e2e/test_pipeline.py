"""Teste E2E principal (prompt mestre seção 37):

fixture GSUS -> lista pacientes -> fila -> extrai evoluções -> normaliza ->
regras -> LLM mock -> persiste -> gera relatório.

Usa apenas dados sintéticos (fixtures/notes/*.txt, prontuários fictícios).
Não depende de Playwright, GSUS real, nem de um llama-server real.
"""
import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.analysis import run_diagnosis
from app.analysis.llm import LLMAnalysisError
from app.gsus.census import GSUSCensusError, GSUSCensusIncompleteError
from app.gsus.records import GSUSNoCurrentAdmissionDays
from app.models import Patient
from app.orchestrator import run_once
from app.storage import database
from app.storage.repository import Repository

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "notes"
UNIT = "Clínica Médica"


class FixtureCensusSource:
    """Simula app.gsus.census.get_census() com pacientes sintéticos."""

    def __init__(self, patients: list[Patient]):
        self._patients = patients

    def get_census(self) -> list[Patient]:
        return self._patients


class PartialCensusSource:
    """Simula app.gsus.census.get_census() desistindo da paginação no meio
    (ver DECISIONS.md DEC-080) -- devolve só `visible_patients` via
    `GSUSCensusIncompleteError`, nunca a lista completa passada."""

    def __init__(self, visible_patients: list[Patient]):
        self._visible_patients = visible_patients

    def get_census(self) -> list[Patient]:
        raise GSUSCensusIncompleteError(
            "simulado: paginação instável", self._visible_patients
        )


class TotalCensusFailureSource:
    """DIAG-001: simula o censo falhando por completo (nem uma página --
    achado real de auditoria de catálogo, DEC-111: este é um dos caminhos
    que, antes do diagnóstico, não deixava NENHUM rastro no banco, já que
    `run_id` nunca chega a ser criado)."""

    def get_census(self) -> list[Patient]:
        raise GSUSCensusError("simulado: tela de censo não abriu de jeito nenhum")


class FixtureRecordSource:
    """Simula app.gsus.records.extract_notes() lendo fixtures/notes/*.txt."""

    def __init__(self, fixture_by_record_number: dict[str, str]):
        self._fixture_by_record_number = fixture_by_record_number

    def get_raw_notes_text(self, patient: Patient, known_days=frozenset()) -> str:
        fixture_name = self._fixture_by_record_number[patient.record_number]
        return (FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")


class FailingRecordSource(FixtureRecordSource):
    """Paciente '999' sempre falha -- valida isolamento de falha (RF-12)."""

    def get_raw_notes_text(self, patient: Patient, known_days=frozenset()) -> str:
        if patient.record_number == "999":
            raise TimeoutError("simulado: GSUS não respondeu para este prontuário")
        return super().get_raw_notes_text(patient)


class NoCurrentAdmissionRecordSource(FixtureRecordSource):
    """Paciente '888' só tem episódios antigos na tela (provável alta
    recente) -- valida a categoria separada de não-falha (DEC-054)."""

    def get_raw_notes_text(self, patient: Patient, known_days=frozenset()) -> str:
        if patient.record_number == "888":
            raise GSUSNoCurrentAdmissionDays("simulado: só internação antiga na tela")
        return super().get_raw_notes_text(patient)


class StubLLM:
    """Substitui LocalLLM real: devolve uma análise válida e fixa para
    qualquer paciente com notas novas, sem precisar de llama-server/modelo."""

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None):
        return {
            "clinical_context": "Contexto clínico sintetizado a partir das evoluções.",
            "current_status": "Situação clínica sintetizada a partir das evoluções.",
            "necessidade_hospitalar": "SIM",
            "necessidade_hospitalar_justificativa": "Justificativa sintetizada a partir das evoluções.",
            "objetivo_terapeutico": "Objetivo sintetizado a partir das evoluções.",
            "proximo_passo": "Próximo passo sintetizado a partir das evoluções.",
            "edd_data": None,
            "edd_status": "NAO_REGISTRADA",
            "dia_classificacao": "VERDE",
            "dia_causa": None,
            "pending_items": [],
            "insufficient_information": False,
        }


def _patients() -> list[Patient]:
    return [
        Patient(record_number="100", bed="2A", unit=UNIT),
        Patient(record_number="200", bed="2B", unit=UNIT),
    ]


def test_pipeline_end_to_end_persists_and_generates_report(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    census = FixtureCensusSource(_patients())
    records = FixtureRecordSource({"100": "awaiting_exam.txt", "200": "resolved_consult.txt"})
    report_path = tmp_path / "relatorio.html"

    result = run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    assert result.counts == {"found": 2, "completed": 2, "failed": 0, "no_admission": 0}
    assert report_path.exists()

    content = report_path.read_text(encoding="utf-8")
    assert "LEITO 2A" in content
    assert "LEITO 2B" in content
    assert "tomografia" in content.lower()  # pendência determinística de awaiting_exam
    assert "Contexto clínico sintetizado" in content  # veio do StubLLM
    conn.close()


def test_pipeline_isolates_patient_failure_without_stopping_batch(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    patients = _patients() + [Patient(record_number="999", bed="3A", unit=UNIT)]
    census = FixtureCensusSource(patients)
    records = FailingRecordSource({"100": "awaiting_exam.txt", "200": "resolved_consult.txt"})
    report_path = tmp_path / "relatorio.html"

    result = run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    assert result.counts == {"found": 3, "completed": 2, "failed": 1, "no_admission": 0}
    content = report_path.read_text(encoding="utf-8")
    assert "prontuário 999" in content  # falha visível no relatório (RF-14)
    conn.close()


def test_pipeline_no_current_admission_is_not_counted_as_failure(tmp_path):
    """Paciente sem internação atual (só episódios antigos) não é falha
    técnica -- categoria separada, decisão do usuário (DEC-054)."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    patients = _patients() + [Patient(record_number="888", bed="4A", unit=UNIT)]
    census = FixtureCensusSource(patients)
    records = NoCurrentAdmissionRecordSource({"100": "awaiting_exam.txt", "200": "resolved_consult.txt"})
    report_path = tmp_path / "relatorio.html"

    result = run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    assert result.counts == {"found": 3, "completed": 2, "failed": 0, "no_admission": 1}
    content = report_path.read_text(encoding="utf-8")
    assert "Sem internação atual" in content
    assert "prontuário 888" in content
    # não deve aparecer na seção de falhas reais
    assert "Prontuários não processados" not in content
    conn.close()


def test_pipeline_partial_census_never_marks_real_patient_as_inactive(tmp_path):
    """Achado real E2E-001 (2026-08-27, DEC-080): quando a paginação do
    censo desiste no meio (GSUS instável), a lista devolvida é só uma
    FATIA do censo real -- `run_once` não pode tratar isso como "censo
    completo" e marcar quem ficou de fora como inativo/alta. Antes desta
    correção, um censo parcial de verdade (60 de ~177 pacientes) marcou
    134 pacientes reais como inativos numa execução real."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    records = FixtureRecordSource({"100": "awaiting_exam.txt", "200": "resolved_consult.txt"})
    report_path = tmp_path / "relatorio.html"

    # Execução 1: censo completo, os dois pacientes aparecem e ficam ativos.
    run_once(repo, FixtureCensusSource(_patients()), records, UNIT, report_path, llm=StubLLM())
    assert repo.get_patient_by_record_number("100")["active"] == 1
    assert repo.get_patient_by_record_number("200")["active"] == 1

    # Execução 2: paginação desiste depois de só achar o paciente "100" --
    # "200" continua internado de verdade, só não foi alcançado desta vez.
    result = run_once(
        repo, PartialCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)]),
        records, UNIT, report_path, llm=StubLLM(),
    )

    assert result.counts["found"] == 1  # só o que a paginação parcial trouxe
    assert repo.get_patient_by_record_number("100")["active"] == 1
    assert repo.get_patient_by_record_number("200")["active"] == 1  # NUNCA marcado inativo
    conn.close()


class FlakyMarkProcessingRepository(Repository):
    """`mark_processing` explode pra UM paciente específico -- simula uma
    falha de escrita SQLite (lock momentâneo, disco lento) na bookkeeping do
    loop principal, não na extração/análise em si."""

    def __init__(self, conn, flaky_record_number: str):
        super().__init__(conn)
        self._flaky_record_number = flaky_record_number

    def mark_processing(self, run_id, patient_id):
        if patient_id == self._flaky_record_number:
            raise RuntimeError("simulado: SQLite momentaneamente indisponível")
        return super().mark_processing(run_id, patient_id)


def test_pipeline_isolates_bookkeeping_failure_without_aborting_the_whole_batch(tmp_path):
    """Achado real de auditoria (RESIL-011, 2026-09-01, confirmado no banco
    de produção real): `repo.mark_processing`/`report()` rodavam FORA do
    try/except por paciente -- uma falha aí (não na extração/análise, na
    própria bookkeeping do loop) abortava a run inteira de uma vez,
    deixando todo mundo depois do paciente com problema como PENDING pra
    sempre, sem nenhum `ERROR` registrado (RF-12 furado). Este teste prova
    que uma falha de bookkeeping em UM paciente agora isola só esse
    paciente -- os demais continuam sendo processados normalmente."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = FlakyMarkProcessingRepository(conn, flaky_record_number="200")

    patients = _patients() + [Patient(record_number="300", bed="3C", unit=UNIT)]
    census = FixtureCensusSource(patients)
    records = FixtureRecordSource({
        "100": "awaiting_exam.txt", "200": "resolved_consult.txt", "300": "awaiting_exam.txt",
    })
    report_path = tmp_path / "relatorio.html"

    result = run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    # Achado real: SEM o fix, "300" nunca seria alcançado (a run abortava
    # inteira na falha de "200") -- teria found=3, completed=0, failed=0,
    # e "300" ficaria PENDING pra sempre, sem nenhum ERROR correspondente.
    assert result.counts == {"found": 3, "completed": 2, "failed": 1, "no_admission": 0}
    failed = repo.get_failed_patients(result.run_id)
    assert len(failed) == 1
    assert failed[0]["patient_id"] == "200"
    conn.close()


class OrderRecordingLLM(StubLLM):
    """Registra, na ordem real de chamada, o tamanho total do texto de
    evolução enviado -- usado pra confirmar a priorização do DEC-085."""

    def __init__(self):
        self.call_sizes: list[int] = []

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None):
        self.call_sizes.append(sum(len(note.text) for note in new_notes))
        return super().analyze_patient(previous_state, new_notes, active_pending_items)


def test_pipeline_processes_fresh_patients_in_fase1_completion_order(tmp_path):
    """DEC-109 (2026-09-02) muda o comportamento que esta suíte testava até
    aqui: antes, a Fase 2 só começava depois que a Fase 1 processava TODO
    mundo, então dava pra ordenar a fila inteira do menor pro maior volume
    de texto (DEC-085) sem custo nenhum. Agora a Fase 2 roda numa thread
    dedicada que consome cada paciente assim que a Fase 1 libera ele --
    exatamente pra não deixar a IA ociosa esperando o lote inteiro (achado
    real: só 16 de 182 pacientes entravam na Fase 2 depois de mais de 1h de
    Fase 1). Ordenar globalmente exigiria esperar a Fase 1 terminar primeiro,
    o que anularia o ganho. Pacientes FRESCOS (nota nova nesta execução) são
    processados na ordem em que a Fase 1 (sequencial, sem concorrência
    interna) os libera -- aqui, a ordem do censo. Só o BACKLOG (pacientes
    sem análise nova nesta execução, ver teste seguinte) continua ordenado
    do menor pro maior, preservando a intenção original do DEC-085 onde
    ainda é possível sem sacrificar o paralelismo."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    patients = [
        Patient(record_number="100", bed="2A", unit=UNIT),  # long_admission.txt -- maior
        Patient(record_number="200", bed="2B", unit=UNIT),  # awaiting_consult.txt -- menor
        Patient(record_number="300", bed="2C", unit=UNIT),  # resolved_consult.txt -- médio
    ]
    census = FixtureCensusSource(patients)
    records = FixtureRecordSource({
        "100": "long_admission.txt",
        "200": "awaiting_consult.txt",
        "300": "resolved_consult.txt",
    })
    llm = OrderRecordingLLM()
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=llm)

    # Determinístico (não é uma corrida): a Fase 1 é sequencial e a fila é
    # FIFO -- a ordem de chegada na Fase 2 é sempre a ordem do censo,
    # independente de quando a thread da Fase 2 acorda pra consumir.
    assert len(llm.call_sizes) == 3
    assert llm.call_sizes != sorted(llm.call_sizes)  # documenta a mudança de comportamento
    conn.close()


def test_pipeline_processes_backlog_smaller_notes_first(tmp_path):
    """DEC-085 continua valendo pra fatia do BACKLOG (pacientes sem nota
    nova nesta execução, resgatados via `get_active_patients_pending_ai_analysis`
    -- ver LLM-004/DEC-090): esses só entram na fila DEPOIS que a Fase 1
    inteira termina (`run_once`), então ordenar globalmente aqui não custa
    nada ao paralelismo introduzido pelo DEC-109."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    patients = [
        Patient(record_number="100", bed="2A", unit=UNIT),  # long_admission.txt -- maior
        Patient(record_number="200", bed="2B", unit=UNIT),  # awaiting_consult.txt -- menor
    ]
    census = FixtureCensusSource(patients)
    records = FixtureRecordSource({
        "100": "long_admission.txt",
        "200": "awaiting_consult.txt",
    })
    report_path = tmp_path / "relatorio.html"

    # 1ª execução: IA falha pra ambos -- nenhum fica com patient_state.
    run_once(repo, census, records, UNIT, report_path, llm=AlwaysFailingLLM())

    # 2ª execução: mesmas notas (nada novo) -- a Fase 1 não gera tarefa
    # fresca pra ninguém; os dois só entram na fila via reconstrução de
    # backlog, que deve ordenar do menor pro maior volume de texto.
    llm = OrderRecordingLLM()
    run_once(repo, census, records, UNIT, report_path, llm=llm)

    assert llm.call_sizes == sorted(llm.call_sizes)
    assert len(llm.call_sizes) == 2
    conn.close()


def test_pipeline_second_run_does_not_reprocess_known_notes(tmp_path):
    """Incrementalidade (RF-06/INCR-001): a segunda execução não deve gerar
    pendência duplicada a partir da mesma evolução já vista."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=StubLLM())
    notes_after_first_run = repo.get_notes("100")

    run_once(repo, census, records, UNIT, report_path, llm=StubLLM())
    notes_after_second_run = repo.get_notes("100")

    assert len(notes_after_first_run) == len(notes_after_second_run) == 2
    conn.close()


class ExamThenResolvedRecordSource(FixtureRecordSource):
    """1ª chamada devolve o texto original (exame pendente); a partir da 2ª,
    devolve o texto com uma evolução extra de resultado -- simula uma nova
    evolução real chegando entre execuções."""

    RESOLVED_TEXT = (
        "20/08/2026 08:00 - Profissional Teste Um (Medico clinico)\n"
        "Paciente Teste 001, prontuario 123456, leito 2A. Internado para investigacao "
        "de dor abdominal. Solicitada tomografia de abdome.\n\n"
        "20/08/2026 14:00 - Profissional Teste Um (Medico clinico)\n"
        "Paciente estavel, aguardando realizacao da tomografia de abdome solicitada.\n\n"
        "21/08/2026 09:00 - Profissional Teste Um (Medico clinico)\n"
        "Tomografia de abdome realizada, sem alteracoes significativas.\n"
    )

    def __init__(self, fixture_by_record_number):
        super().__init__(fixture_by_record_number)
        self._calls = 0

    def get_raw_notes_text(self, patient, known_days=frozenset()) -> str:
        self._calls += 1
        if self._calls == 1:
            return super().get_raw_notes_text(patient, known_days)
        return self.RESOLVED_TEXT


def test_pipeline_second_run_does_not_duplicate_rule_based_pending_item(tmp_path):
    """Achado real (DEC-062): `apply_all_rules` roda sobre todo o histórico
    a cada execução -- sem reconciliar contra o que já está ativo, a
    2ª execução criava uma pendência DUPLICADA enquanto a condição
    (exame ainda pendente) continuasse verdadeira."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=None)
    patient_id = repo.conn.execute("SELECT patient_id FROM patients WHERE record_number = ?", ("100",)).fetchone()["patient_id"]
    active_after_first = repo.get_active_pending_items(patient_id)

    run_once(repo, census, records, UNIT, report_path, llm=None)
    active_after_second = repo.get_active_pending_items(patient_id)

    assert len(active_after_first) == 1
    assert len(active_after_second) == 1
    assert active_after_first[0]["pending_id"] == active_after_second[0]["pending_id"]
    conn.close()


def test_pipeline_rule_based_pending_item_resolves_when_exam_completed(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = ExamThenResolvedRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=None)
    patient_id = repo.conn.execute("SELECT patient_id FROM patients WHERE record_number = ?", ("100",)).fetchone()["patient_id"]
    assert len(repo.get_active_pending_items(patient_id)) == 1

    run_once(repo, census, records, UNIT, report_path, llm=None)

    assert repo.get_active_pending_items(patient_id) == []
    conn.close()


def test_pipeline_without_llm_still_applies_deterministic_rules(tmp_path):
    """RULES-001 roda mesmo sem LLM configurado (ex.: modelo ainda não instalado)."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)

    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    result = run_once(repo, census, records, UNIT, report_path, llm=None)

    assert result.counts == {"found": 1, "completed": 1, "failed": 0, "no_admission": 0}
    content = report_path.read_text(encoding="utf-8")
    assert "tomografia" in content.lower()
    conn.close()


def test_pipeline_purges_notes_of_long_discharged_patients_when_retention_configured(tmp_path):
    """RETENTION-001/DEC-068: `run_once` só purga quando `raw_notes_
    retention_days` é passado explicitamente -- None (padrão) mantém o
    comportamento antigo, pra não surpreender testes/chamadores que não
    têm opinião sobre retenção."""
    from datetime import datetime, timedelta, timezone

    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    # 1ª rodada: paciente aparece no censo, evolução extraída.
    run_once(repo, census, records, UNIT, report_path, llm=None)
    assert len(repo.get_notes("100")) == 2

    # 2ª rodada: censo vazio (paciente teve alta) -- simula alta antiga
    # ajustando last_seen_at direto, já que "agora" não avança nos testes.
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    repo.conn.execute("UPDATE patients SET last_seen_at = ? WHERE patient_id = ?", (old, "100"))
    repo.conn.commit()

    run_once(
        repo, FixtureCensusSource([]), records, UNIT, report_path, llm=None,
        raw_notes_retention_days=90,
    )

    assert repo.get_notes("100") == []
    conn.close()


# ------------------------------------- resiliência da execução (2026-08-28)
# Achados da auditoria de resiliência (DEC-090/091): (1) LLM-004 -- paciente
# cuja análise de IA falhou nunca era retomado sozinho numa execução
# seguinte sem nota genuinamente nova; (2) disjuntor de saúde -- sem ele,
# cada paciente restante queimaria o timeout inteiro contra um llama-server
# morto/travado; (3) uma falha ao regravar o relatório no meio da Fase 2
# abortava toda a análise restante; (4) uma falha catastrófica não prevista
# deixava a run presa em RUNNING pra sempre, sem nenhum sinal de que a
# auditoria do dia falhou.

class AlwaysFailingLLM(StubLLM):
    """Simula uma falha de validação persistente (ex.: DEC-089/RF-28) --
    `_run_llm_analysis` absorve isso e mantém o estado anterior (aqui,
    nenhum -- paciente nunca teve análise bem-sucedida)."""

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None):
        raise LLMAnalysisError("simulado: falha de validação persistente")


def test_pipeline_retries_never_analyzed_patient_on_next_run_without_new_notes(tmp_path):
    """LLM-004/DEC-090: paciente cuja IA falhou (sem NENHUM patient_state)
    precisa ser resgatado automaticamente numa execução seguinte, mesmo
    quando não há nenhuma evolução genuinamente nova -- o sinal de "nota
    nova" já foi consumido pela Fase 1 da primeira execução."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=AlwaysFailingLLM())
    patient_id = repo.conn.execute(
        "SELECT patient_id FROM patients WHERE record_number = ?", ("100",)
    ).fetchone()["patient_id"]
    assert repo.get_patient_state(patient_id) is None  # falhou, sem inventar nada (fail-closed)

    # Mesmo censo/registros -- nenhuma nota genuinamente nova -- mas o
    # paciente segue sem NENHUMA análise de IA bem-sucedida.
    run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    assert repo.get_patient_state(patient_id) is not None
    conn.close()


class UnhealthyLLM(StubLLM):
    def __init__(self):
        self.analyze_calls = 0

    def is_healthy(self):
        return False

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None):
        self.analyze_calls += 1
        return super().analyze_patient(previous_state, new_notes, active_pending_items)


def test_pipeline_health_circuit_breaker_skips_analysis_when_llm_is_unresponsive(tmp_path):
    """Sem o disjuntor, cada paciente restante da Fase 2 queimaria seu
    timeout inteiro (até 90min, DEC-070) contra um llama-server morto ou com
    slot preso (DEC-087) -- aqui simulado via `is_healthy() == False`."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource(_patients())
    records = FixtureRecordSource({"100": "awaiting_exam.txt", "200": "resolved_consult.txt"})
    report_path = tmp_path / "relatorio.html"

    llm = UnhealthyLLM()
    run_once(repo, census, records, UNIT, report_path, llm=llm)

    assert llm.analyze_calls == 0  # disjuntor impediu qualquer tentativa contra servidor morto
    conn.close()


def test_pipeline_survives_report_write_failure_in_the_middle_of_fase2(tmp_path, monkeypatch):
    """C3 (auditoria de resiliência): antes desta correção, `generate_report`
    dentro do loop da Fase 2 estava fora do try/except por paciente -- uma
    falha de escrita (disco cheio, permissão) abortava a análise de IA de
    TODOS os pacientes restantes, mesmo com o llama-server saudável."""
    import app.orchestrator as orchestrator_module

    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource(_patients())
    records = FixtureRecordSource({"100": "awaiting_exam.txt", "200": "resolved_consult.txt"})
    report_path = tmp_path / "relatorio.html"

    original_generate_report = orchestrator_module.generate_report
    call_count = {"n": 0}

    def _flaky_generate_report(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:  # primeira regravação da Fase 2 (após o 1º paciente)
            raise OSError("simulado: disco cheio")
        return original_generate_report(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "generate_report", _flaky_generate_report)

    run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    patient_id_100 = repo.conn.execute("SELECT patient_id FROM patients WHERE record_number = ?", ("100",)).fetchone()["patient_id"]
    patient_id_200 = repo.conn.execute("SELECT patient_id FROM patients WHERE record_number = ?", ("200",)).fetchone()["patient_id"]
    # os dois pacientes tiveram análise por IA persistida, apesar da falha
    # de escrita do relatório no meio do caminho.
    assert repo.get_patient_state(patient_id_100) is not None
    assert repo.get_patient_state(patient_id_200) is not None
    conn.close()


class _RaisingCensusAfterUpsert:
    """Simula uma falha catastrófica DEPOIS que a run já existe (`run_id`
    criado) -- diferente de GSUSCensusIncompleteError (tratada
    explicitamente), este é um erro genuinamente não previsto."""

    def get_census(self):
        return [Patient(record_number="100", bed="2A", unit=UNIT)]


def test_pipeline_marks_run_as_failed_instead_of_stuck_running_on_uncaught_error(tmp_path, monkeypatch):
    """Achado real (auditoria de resiliência): antes desta correção, uma
    exceção não prevista escapando de `run_once` deixava a run com status
    RUNNING pra sempre -- nada nunca escrevia FAILED, e não havia nenhum
    jeito de saber, só olhando o banco, que a auditoria daquele dia não
    terminou. `run_once` ainda deve LEVANTAR a exceção (contrato existente
    com `main.run_auto_update`, que já trata isso) -- só passa a também
    marcar a run como FAILED antes de propagar."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    def _raise(*args, **kwargs):
        raise RuntimeError("simulado: falha inesperada após a run já existir")

    monkeypatch.setattr(repo, "get_active_patients_pending_ai_analysis", _raise)

    with pytest.raises(RuntimeError):
        run_once(repo, _RaisingCensusAfterUpsert(), records, UNIT, report_path, llm=StubLLM())

    run_row = repo.conn.execute("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert run_row["status"] == "FAILED"


def test_pipeline_does_not_leak_llm_worker_thread_when_backlog_step_raises(tmp_path, monkeypatch):
    """DEC-109 (revisão adversarial, 2026-09-02): achado real e reproduzido
    -- antes desta correção, uma exceção no passo de backlog (o MESMO
    cenário do teste anterior, agora que a Fase 2 roda numa thread própria)
    nunca enfileirava o sentinela `_LLM_QUEUE_DONE` nem chamava `.join()`,
    porque esse código ficava DEPOIS do ponto onde a exceção escapava pro
    `except` mais externo. A thread `llm-phase2-worker` ficava bloqueada
    pra sempre em `llm_queue.get()`, vazando ela e a conexão SQLite dela
    pelo resto da vida do processo -- grave de verdade no botão "Atualizar
    agora" da UI (processo de vida longa, clique seguinte cria mais uma
    thread zumbi). Corrigido com um `finally` que garante sentinela+join em
    qualquer caminho de saída. Este teste é o que faltava (identificado pela
    própria revisão): o teste irmão só checava `status == FAILED`, nunca o
    estado da thread -- por isso passava mesmo com o vazamento presente."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    def _raise(*args, **kwargs):
        raise RuntimeError("simulado: falha inesperada após a run já existir")

    monkeypatch.setattr(repo, "get_active_patients_pending_ai_analysis", _raise)

    with pytest.raises(RuntimeError):
        run_once(repo, _RaisingCensusAfterUpsert(), records, UNIT, report_path, llm=StubLLM())

    time.sleep(0.5)  # margem pra thread realmente terminar, se for terminar
    worker_threads = [t for t in threading.enumerate() if t.name == "llm-phase2-worker"]
    assert worker_threads == [], (
        "thread da Fase 2 (IA) vazou -- ficou viva/bloqueada depois de run_once levantar a exceção"
    )
    conn.close()


def test_pipeline_logs_and_continues_when_llm_worker_connection_fails(tmp_path, monkeypatch, caplog):
    """DEC-109 (revisão adversarial): achado real -- abrir a conexão da
    thread da Fase 2 (`database.init_db` dentro de `_llm_phase2_worker`)
    ficava FORA do try/except dela. Uma falha ali escapava da thread inteira
    sem nunca passar pelo `logger` da aplicação (só um traceback perdido no
    stderr, invisível numa execução `--auto-update` sem console) -- e
    `run_once` seguia reportando sucesso com a Fase 2 inteira pulada em
    silêncio, sem nenhum rastro em lugar nenhum. Corrigido: a abertura da
    conexão agora está dentro de um try/except que loga via `logger.exception`
    e sinaliza `worker_failed` -- Fase 1 continua completando normalmente."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    def _raise(*args, **kwargs):
        raise sqlite3.OperationalError("simulado: banco bloqueado ao abrir conexão da Fase 2")

    monkeypatch.setattr(database, "init_db", _raise)

    with caplog.at_level(logging.ERROR):
        result = run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    assert result.run_id  # Fase 1 completa normalmente, apesar da Fase 2 não rodar
    assert "Falha ao abrir conexão da thread de análise por IA" in caplog.text
    conn.close()
    conn.close()


def test_pipeline_does_not_purge_when_retention_not_configured(tmp_path):
    from datetime import datetime, timedelta, timezone

    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=None)
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    repo.conn.execute("UPDATE patients SET last_seen_at = ? WHERE patient_id = ?", (old, "100"))
    repo.conn.commit()

    run_once(repo, FixtureCensusSource([]), records, UNIT, report_path, llm=None)  # sem raw_notes_retention_days

    assert len(repo.get_notes("100")) == 2
    conn.close()


# --------------------------------------------------------------- DIAG-001
# Achado de auditoria de catálogo de falhas (2026-09-03, DEC-111): antes
# desta correção, várias falhas (censo total, login) não deixavam NENHUM
# rastro persistente -- só no log técnico que o auditor (sem conhecimento
# técnico, PROJECT_SPEC.md seção 2) nunca abre. Estes testes confirmam que
# `run_once` agora sempre registra um diagnóstico em linguagem simples,
# nos três formatos de desfecho possíveis.

def test_clean_run_writes_success_diagnostic(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    diagnostic = repo.get_latest_run_diagnostic()
    assert diagnostic is not None
    assert diagnostic["run_id"] is not None
    assert diagnostic["outcome"] == run_diagnosis.OUTCOME_SUCESSO
    assert not diagnostic["needs_attention"]
    conn.close()


def test_incomplete_census_writes_partial_gsus_diagnostic_tied_to_the_run(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = PartialCensusSource([Patient(record_number="100", bed="2A", unit=UNIT)])
    records = FixtureRecordSource({"100": "awaiting_exam.txt"})
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    diagnostic = repo.get_latest_run_diagnostic()
    assert diagnostic is not None
    assert diagnostic["run_id"] is not None  # censo parcial ainda processa quem foi coletado -- run existe
    assert diagnostic["outcome"] == run_diagnosis.OUTCOME_SUCESSO_PARCIAL_GSUS
    assert "GSUS" in diagnostic["summary"]
    conn.close()


def test_total_census_failure_writes_diagnostic_with_no_run_id(tmp_path):
    """O cenário exato que era invisível antes desta correção -- censo
    falha por completo, `run_id` nunca chega a existir, mas o auditor
    precisa saber que a execução de hoje simplesmente não rodou, e por quê."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    records = FixtureRecordSource({})
    report_path = tmp_path / "relatorio.html"

    with pytest.raises(GSUSCensusError):
        run_once(repo, TotalCensusFailureSource(), records, UNIT, report_path, llm=StubLLM())

    diagnostic = repo.get_latest_run_diagnostic()
    assert diagnostic is not None
    assert diagnostic["run_id"] is None  # achado central: run_id nunca existiu, mas o diagnóstico existe mesmo assim
    assert diagnostic["outcome"] == run_diagnosis.OUTCOME_FALHA_GSUS
    assert diagnostic["needs_attention"]
    conn.close()


def test_unexpected_per_patient_error_type_flags_diagnostic_for_investigation(tmp_path):
    """Um tipo de erro que o classificador não reconhece como causa
    conhecida do GSUS precisa aparecer como FALHA_INESPERADA, mesmo que a
    run em si tenha terminado 'normalmente' (RF-12, falha isolada por
    paciente) -- é assim que um bug real de verdade não fica escondido."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    census = FixtureCensusSource([Patient(record_number="999", bed="2A", unit=UNIT)])
    records = FailingRecordSource({})  # paciente "999" sempre levanta TimeoutError genérico
    report_path = tmp_path / "relatorio.html"

    run_once(repo, census, records, UNIT, report_path, llm=StubLLM())

    diagnostic = repo.get_latest_run_diagnostic()
    assert diagnostic is not None
    assert diagnostic["outcome"] == run_diagnosis.OUTCOME_FALHA_INESPERADA
    assert diagnostic["needs_attention"]
    conn.close()


# ------------------------------------------------------------ UI-006 (Encerrar)

class CancelDuringFirstPatientRecordSource(FixtureRecordSource):
    """Simula o auditor clicando "Encerrar" enquanto o 1o paciente ainda esta
    sendo coletado: seta o evento DURANTE a extracao -- o paciente atual
    precisa terminar inteiro e o laco parar ANTES do proximo."""

    def __init__(self, fixture_by_record_number, cancel_event):
        super().__init__(fixture_by_record_number)
        self.cancel_event = cancel_event
        self.calls = 0

    def get_raw_notes_text(self, patient: Patient, known_days=frozenset()) -> str:
        self.calls += 1
        self.cancel_event.set()
        return super().get_raw_notes_text(patient, known_days)


class StoppableStubLLM(StubLLM):
    """StubLLM com `stop()` observavel -- `run_once` deve derrubar a IA na hora
    quando o usuario encerra, em vez de esperar a analise atual terminar."""

    def __init__(self):
        self.stopped = False
        self.analyze_calls = 0

    def analyze_patient(self, previous_state, new_notes, active_pending_items=None):
        self.analyze_calls += 1
        return super().analyze_patient(previous_state, new_notes, active_pending_items)

    def stop(self):
        self.stopped = True


def test_pipeline_cancel_event_stops_after_current_patient_and_marks_run_cancelled(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    cancel_event = threading.Event()
    patients = _patients() + [Patient(record_number="300", bed="2C", unit=UNIT)]
    records = CancelDuringFirstPatientRecordSource(
        {"100": "awaiting_exam.txt", "200": "resolved_consult.txt", "300": "awaiting_exam.txt"},
        cancel_event,
    )
    llm = StoppableStubLLM()
    report_path = tmp_path / "relatorio.html"

    result = run_once(
        repo, FixtureCensusSource(patients), records, UNIT, report_path, llm=llm, cancel_event=cancel_event,
    )

    assert result.status == "CANCELLED"
    assert records.calls == 1  # so o paciente em andamento terminou; o proximo nem comecou
    assert result.counts["found"] == 3
    assert result.counts["completed"] == 1
    assert result.counts["failed"] == 0
    run_row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (result.run_id,)).fetchone()
    assert run_row["status"] == "CANCELLED"
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM processing_queue WHERE run_id = ? AND status = 'PENDING'",
        (result.run_id,),
    ).fetchone()["n"]
    assert pending == 2  # os dois restantes continuam PENDING, nunca ERROR
    assert llm.stopped is True  # analise em andamento derrubada na hora
    assert llm.analyze_calls == 0  # fila da IA descartada, nada marcado como analisado
    assert not report_path.exists()  # relatorio do dia NAO e regravado com fatia parcial
    assert conn.execute("SELECT COUNT(*) AS n FROM daily_snapshot").fetchone()["n"] == 0
    diagnostic = repo.get_latest_run_diagnostic()
    assert diagnostic["outcome"] == run_diagnosis.OUTCOME_CANCELADA
    assert not diagnostic["needs_attention"]
    assert "1 de 3" in diagnostic["summary"]
    assert not any(t.name == "llm-phase2-worker" and t.is_alive() for t in threading.enumerate())
    conn.close()


def test_pipeline_cancel_before_start_never_touches_the_census(tmp_path):
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    cancel_event = threading.Event()
    cancel_event.set()

    class ExplodingCensusSource:
        def get_census(self) -> list[Patient]:
            raise AssertionError("o censo nao deveria ser consultado depois de encerrar")

    result = run_once(
        repo, ExplodingCensusSource(), FixtureRecordSource({}), UNIT, tmp_path / "relatorio.html",
        cancel_event=cancel_event,
    )

    assert result.status == "CANCELLED"
    assert result.counts == {"found": 0, "completed": 0, "failed": 0, "no_admission": 0}
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
    conn.close()


def test_pipeline_without_cancel_event_still_completes_normally(tmp_path):
    """Regressao: quem nao passa `cancel_event` (execucao agendada, testes
    antigos) continua com exatamente o comportamento anterior."""
    conn = database.init_db(tmp_path / "auditoria.db")
    repo = Repository(conn)
    records = FixtureRecordSource({"100": "awaiting_exam.txt", "200": "resolved_consult.txt"})

    result = run_once(repo, FixtureCensusSource(_patients()), records, UNIT, tmp_path / "relatorio.html")

    assert result.status == "COMPLETED"
    assert result.counts["completed"] == 2
    conn.close()
