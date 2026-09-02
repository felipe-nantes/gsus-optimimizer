"""Valida o pipeline completo ponta a ponta contra o GSUS real + LLM local
real: censo -> extração -> regras -> LLM -> persistência -> relatório,
usando `orchestrator.run_once` de verdade (o MESMO caminho que
app/ui/main_window.py usa, com o LLM agora realmente plugado -- ver
DECISIONS.md DEC-058/DEC-059).

Usa um banco e relatório ISOLADOS (nunca toca auditoria.db/relatorio.html
reais). Necessário porque a amostra é limitada a poucos pacientes
(MAX_PATIENTS) para não rodar o LLM contra o censo inteiro numa primeira
validação -- e `mark_patients_inactive_not_in` marcaria como inativo
qualquer paciente real de fora da amostra se isso rodasse contra o banco
real (dado de produção corrompido por um script de teste).

PHI: só imprime contagens agregadas, tempo e TIPO de erro (nunca nome,
prontuário, leito ou texto clínico). Todo log do app (incluindo qualquer
traceback que pudesse citar algo sensível) vai para arquivo via
scripts/_common.py, nunca para o terminal -- mesmo princípio de
check_notes.py/check_census.py (ver DECISIONS.md DEC-016). O relatório
HTML final TEM dado real -- fica só na máquina, abra você mesmo.

Não interativo (sem input()) -- pensado para rodar em background.

Uso:
    .venv\\Scripts\\python.exe scripts\\check_full_pipeline.py
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import setup_file_logging
from app import config
from app.analysis.llm import LLMStartupError, LocalLLM
from app.gsus.adapter import GSUSAdapter
from app.gsus.client import GSUSClient
from app.orchestrator import run_once
from app.security import credentials
from app.storage import database
from app.storage.repository import Repository
from app.ui.errors import friendly_message

MAX_PATIENTS = 2  # reduzida de 5 pra 2 em 2026-08-25 -- pedido do usuário pra validar
# especificamente a arquitetura em duas fases do DEC-071 (relatório com regras disponível
# antes da IA começar, timeout de 1800s) contra poucos casos reais, não pra medir carga.


class SampledCensus:
    """Envolve o adapter real só para limitar a amostra -- nunca reimplementa
    extração (GSUS-001..005 são FROZEN, ver TASKS.md). Amostra ALEATÓRIA
    (não os N primeiros) -- pedido do usuário 2026-08-25, pra checar casos
    diferentes a cada rodada de validação em vez de repetir sempre os
    mesmos pacientes."""

    def __init__(self, adapter: GSUSAdapter, max_patients: int):
        self._adapter = adapter
        self._max_patients = max_patients

    def get_census(self):
        patients = self._adapter.get_census()
        if len(patients) <= self._max_patients:
            return patients
        return random.sample(patients, self._max_patients)

    def get_raw_notes_text(self, patient, known_days=frozenset()):
        return self._adapter.get_raw_notes_text(patient, known_days)


def main() -> None:
    log_path = setup_file_logging()
    config.configure_playwright_browsers_path()

    app_config = config.load_config()

    cred = credentials.get_credential("gsus")
    if cred is None:
        print("RESUMO_JSON: " + json.dumps({"falha": "credencial_gsus_ausente"}))
        return
    username, password = cred

    # Banco e relatório ISOLADOS -- nunca os reais (ver docstring do módulo).
    # Nome com sufixo de versão pra não misturar com a rodada anterior
    # (código mudou bastante entre uma e outra -- DEC-062/063/064).
    data_dir = config.get_app_data_dir()
    db_path = data_dir / "validacao_pipeline_v2.db"
    report_path = data_dir / "validacao_pipeline_v2.html"

    conn = database.init_db(db_path)
    repo = Repository(conn)

    # config.resolve_app_path (DEC-069): caminho relativo default falha ao
    # lançar o processo no Windows mesmo quando existe (achado real,
    # DECISIONS.md DEC-059) -- Popen não resolve executável relativo contra
    # o diretório de trabalho do jeito que Path.exists() resolve. Mesma
    # função usada por main_window.py -- uma só fonte de verdade.
    llm = LocalLLM(
        model_path=config.resolve_app_path(app_config.model_path),
        server_path=config.resolve_app_path(app_config.llm_server_path),
        host=app_config.llm_host,
        port=app_config.llm_port,
    )

    summary: dict = {"amostra_max_pacientes": MAX_PATIENTS, "log_path": str(log_path)}
    t0 = time.monotonic()

    print("Iniciando LLM local (pode levar minutos nesta máquina)...", flush=True)
    try:
        llm.start()
    except LLMStartupError as exc:
        summary["falha"] = "llm_nao_iniciou"
        summary["mensagem_amigavel"] = friendly_message(exc)
        summary["elapsed_seconds"] = round(time.monotonic() - t0, 1)
        print("RESUMO_JSON: " + json.dumps(summary, ensure_ascii=False))
        conn.close()
        return

    summary["llm_carga_seconds"] = round(time.monotonic() - t0, 1)
    print(f"LLM pronto ({summary['llm_carga_seconds']}s). Acessando GSUS real...", flush=True)

    try:
        with GSUSClient(app_config.gsus_base_url) as client:
            client.goto()
            real_adapter = GSUSAdapter(client.page, username, password, app_config.unit, base_url=client.base_url)
            sampled = SampledCensus(real_adapter, MAX_PATIENTS)
            result = run_once(
                repo,
                sampled,
                sampled,
                app_config.unit,
                report_path,
                llm=llm,
                progress=lambda msg: print(f"[progresso] {msg}", flush=True),
            )
        summary["counts"] = result.counts
        summary["report_path"] = str(result.report_path)
    except Exception as exc:
        summary["falha"] = type(exc).__name__
        summary["mensagem_amigavel"] = friendly_message(exc)
    finally:
        llm.stop()
        conn.close()
        summary["elapsed_seconds_total"] = round(time.monotonic() - t0, 1)

    print("RESUMO_JSON: " + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
