r"""Abre e fotografa a interface com dados totalmente ficticios.

Uso (a partir da raiz do projeto):
    .venv\Scripts\python.exe scripts\preview_ui.py --output caminho.png

O banco usado pela previa vive em uma pasta temporaria e e apagado quando
o processo termina. Nenhum dado da instalacao real e lido ou alterado.
"""
from __future__ import annotations

import argparse
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PIL import ImageGrab

# Permite executar este arquivo diretamente de scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.sys.path.insert(0, str(PROJECT_ROOT))

from app import config  # noqa: E402
from app.analysis.taxonomy import (  # noqa: E402
    CATEGORY_ADMINISTRATIVA_LOGISTICA,
    CATEGORY_ALTA_BARREIRA,
    CATEGORY_DIAGNOSTICO,
    CATEGORY_INTERCONSULTA,
    CATEGORY_PROCEDIMENTO_CIRURGIA,
    CATEGORY_TRANSFERENCIA,
)
from app.models import Patient  # noqa: E402
from app.storage import database  # noqa: E402
from app.storage.repository import Repository  # noqa: E402
from app.ui.icons import apply_window_icon  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


SAMPLE_PATIENTS = (
    ("DEMO-1001", "01A", 8, "Pneumonia em melhora clinica, mantendo oxigenio.", "Aguarda tomografia de controle", CATEGORY_DIAGNOSTICO, "INTERNA", "ALTA", "VERMELHO"),
    ("DEMO-1002", "02B", 5, "Pos-operatorio sem intercorrencias.", "Aguarda parecer da cardiologia", CATEGORY_INTERCONSULTA, "INTERNA", "MEDIA", "VERMELHO"),
    ("DEMO-1003", "03A", 12, "Fratura de femur com indicacao cirurgica.", "Aguarda disponibilidade do centro cirurgico", CATEGORY_PROCEDIMENTO_CIRURGIA, "INTERNA", "ALTA", "VERMELHO"),
    ("DEMO-1004", "04C", 3, "Estavel, criterios clinicos para alta.", "Aguarda transporte familiar", CATEGORY_ALTA_BARREIRA, "EXTERNA", "MONITORAMENTO", "VERDE"),
    ("DEMO-1005", "05A", 7, "Necessita continuidade do cuidado em unidade de apoio.", "Aguarda vaga para transferencia", CATEGORY_TRANSFERENCIA, "EXTERNA", "MEDIA", "VERMELHO"),
    ("DEMO-1006", "06B", 2, "Em observacao apos ajuste terapeutico.", "Aguarda liberacao de medicamento", CATEGORY_ADMINISTRATIVA_LOGISTICA, "INTERNA", "MONITORAMENTO", "VERDE"),
    ("DEMO-1007", "07A", 1, "Quadro clinico estavel, sem novas intercorrencias.", None, None, None, "MONITORAMENTO", "VERDE"),
)


def _seed_preview_database() -> None:
    conn = database.init_db(config.get_db_path())
    repo = Repository(conn)
    now = datetime.now(timezone.utc)

    for index, (record, bed, days, context, description, category, origin, priority, dia) in enumerate(SAMPLE_PATIENTS):
        patient_id = repo.upsert_patient(
            Patient(
                record_number=record,
                bed=bed,
                unit="Clinica Medica",
                admission_date=(date.today() - timedelta(days=days)).isoformat(),
            )
        )
        repo.save_patient_state(
            patient_id,
            context,
            "Estavel",
            origem_internacao=origin,
            edd_data=(date.today() - timedelta(days=1)).isoformat() if index in (0, 2) else None,
            edd_status="REGISTRADA" if index in (0, 2, 3) else "NAO_REGISTRADA",
            dia_classificacao=dia,
        )
        if description and category:
            repo.add_pending_item(
                patient_id,
                category,
                description,
                "Evidencia clinica ficticia para demonstracao visual.",
                (now - timedelta(hours=18 + index * 11)).isoformat(),
                origin=origin,
                priority=priority,
                source="LLM",
            )

    repo.save_run_diagnostic(
        None,
        "SUCESSO",
        "Atualizacao concluida com dados de demonstracao.",
        False,
    )
    conn.close()


def _capture(output: Path, screen: str, width: int | None = None, height: int | None = None) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("GSUS Auditoria - PREVIA COM DADOS FICTICIOS")
    apply_window_icon(root)
    app_config = config.AppConfig(
        configured=screen != "setup", unit="Clinica Medica" if screen != "setup" else "", schedule_time="06:00",
    )
    if screen == "main":
        MainWindow(root, app_config)
    elif screen == "setup":
        from app.security import credentials
        from app.ui.setup_window import SetupWindow

        credentials.get_credential = lambda _name: None
        SetupWindow(root, app_config)
    else:
        from app.ui.lookup_window import LookupWindow

        LookupWindow(root, app_config, on_back=lambda: None)
    root.update_idletasks()
    if width is not None and height is not None:
        root.geometry(f"{width}x{height}")
        root.update_idletasks()
    root.geometry(f"{root.winfo_width()}x{root.winfo_height()}+0+0")
    root.update()
    root.lift()
    root.attributes("-topmost", True)
    root.after(250, lambda: root.attributes("-topmost", False))
    root.update()

    output.parent.mkdir(parents=True, exist_ok=True)
    # Captura pelo identificador da janela: funciona mesmo quando outro
    # programa esta na frente e respeita a escala de tela do Windows.
    ImageGrab.grab(window=root.winfo_id()).save(output)
    root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera uma previa segura da interface.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screen", choices=("main", "setup", "lookup"), default="main")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    args = parser.parse_args()
    if (args.width is None) != (args.height is None):
        parser.error("--width e --height devem ser informados juntos")

    with tempfile.TemporaryDirectory(prefix="gsus-ui-preview-") as data_dir:
        os.environ["GSUS_AUDITORIA_DATA_DIR"] = data_dir
        _seed_preview_database()
        _capture(args.output.resolve(), args.screen, args.width, args.height)

    print(args.output.resolve())


if __name__ == "__main__":
    main()
