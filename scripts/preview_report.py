r"""Gera uma captura segura do relatório HTML com dados fictícios.

Nenhum dado da instalação real é acessado: banco e relatório vivem em uma
pasta temporária, removida ao terminar. A imagem final é o único arquivo
gravado no caminho informado.
"""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.sys.path.insert(0, str(PROJECT_ROOT))

from app import config  # noqa: E402
from app.reports.html_report import generate_report  # noqa: E402
from app.storage import database  # noqa: E402
from app.storage.repository import Repository, RUN_STATUS_COMPLETED  # noqa: E402
from scripts.preview_ui import SAMPLE_PATIENTS, _seed_preview_database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Fotografa o relatório com dados fictícios.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gsus-report-preview-") as data_dir:
        os.environ["GSUS_AUDITORIA_DATA_DIR"] = data_dir
        _seed_preview_database()

        conn = database.init_db(config.get_db_path())
        repo = Repository(conn)
        run_id = repo.start_run()
        patient_ids = [patient[0] for patient in SAMPLE_PATIENTS]
        repo.enqueue_patients(run_id, patient_ids)
        for patient_id in patient_ids:
            repo.mark_processing(run_id, patient_id)
            repo.mark_done(run_id, patient_id)
        repo.finish_run(run_id, RUN_STATUS_COMPLETED)
        report_path = Path(data_dir) / "relatorio-demonstracao.html"
        generate_report(repo, run_id, "Clínica Médica", report_path)
        conn.close()

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            page.goto(report_path.as_uri(), wait_until="load")
            page.screenshot(path=str(output), full_page=False)
            browser.close()

    print(output)


if __name__ == "__main__":
    main()
