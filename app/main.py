"""Ponto de entrada do GSUS Auditoria (APP-002 — application shell)."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import tkinter as tk
from pathlib import Path

if __package__ in (None, ""):
    # Permite `python app/main.py` (o pacote `app` não está em sys.path
    # quando o script é executado diretamente, só quando importado como
    # módulo via `python -m app.main`).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.security import credentials
from app.storage import database
from app.ui.lookup_window import LookupWindow
from app.ui.main_window import MainWindow
from app.ui.setup_window import SetupWindow


def setup_logging() -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    handler = logging.handlers.RotatingFileHandler(
        config.get_logs_dir() / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Cópia do log dentro do próprio repositório, só em execução a partir do
    # código-fonte (nunca no executável empacotado). Motivo: o log de
    # produção fica em %LOCALAPPDATA%, que pode ser virtualizado/redirecionado
    # por sandbox de aplicativo -- durante o desenvolvimento isso deixou o
    # log efetivamente ilegível para quem estava depurando (DEC-046).
    if not getattr(sys, "frozen", False):
        dev_logs_dir = Path(__file__).resolve().parent.parent / "logs"
        if dev_logs_dir.is_dir():
            dev_handler = logging.handlers.RotatingFileHandler(
                dev_logs_dir / "app.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
            )
            dev_handler.setFormatter(formatter)
            root_logger.addHandler(dev_handler)


def is_configured(app_config: config.AppConfig) -> bool:
    if not app_config.configured or not app_config.gsus_username or not app_config.unit:
        return False
    return credentials.get_credential("gsus") is not None


def render(root: tk.Tk, app_config: config.AppConfig) -> None:
    """(Re)desenha o conteúdo da janela raiz conforme o estado de configuração.
    Nunca cria um segundo tk.Tk() -- só troca os widgets da janela existente,
    para que a transição configuração -> tela principal seja imediata."""
    for widget in root.winfo_children():
        widget.destroy()

    if is_configured(app_config):
        MainWindow(
            root,
            app_config,
            on_settings=lambda: _show_setup(root, app_config),
            on_lookup=lambda: _show_lookup(root, app_config),
        )
    else:
        SetupWindow(root, app_config, on_complete=lambda: render(root, app_config))


def _show_setup(root: tk.Tk, app_config: config.AppConfig) -> None:
    for widget in root.winfo_children():
        widget.destroy()
    SetupWindow(root, app_config, on_complete=lambda: render(root, app_config))


def _show_lookup(root: tk.Tk, app_config: config.AppConfig) -> None:
    for widget in root.winfo_children():
        widget.destroy()
    LookupWindow(root, app_config, on_back=lambda: render(root, app_config))


def run_auto_update() -> None:
    """SCHEDULE-001: a tarefa agendada chama o `.exe` com `--auto-update`
    (`app/scheduling.py::register_daily_task`) -- roda a MESMA atualização
    real do clique manual (`app/update_flow.py`, única fonte de verdade),
    sem nenhuma interface, e sai. Achado real (2026-08-26): sem essa flag,
    a tarefa agendada só abriria a janela do app e ficaria parada esperando
    alguém clicar "Atualizar agora" -- não existe humano numa execução de
    madrugada."""
    from app.update_flow import UpdateAlreadyRunningError, run_update

    log = logging.getLogger(__name__)
    app_config = config.load_config()
    if not is_configured(app_config):
        log.warning("Execução automática pulada -- app ainda não configurado")
        return

    report_path = config.get_app_data_dir() / "relatorio.html"
    try:
        result = run_update(app_config, report_path, progress=log.info)
        log.info("Atualização automática concluída: %s", result.counts)
    except UpdateAlreadyRunningError:
        # Achado real (DEC-086): a tarefa agendada pode disparar em cima de
        # um "Atualizar agora" manual (ou de outra execução automática) já
        # em andamento -- pular graciosamente é o comportamento certo, não
        # uma falha (mesmo padrão do "app não configurado" acima).
        log.warning("Execução automática pulada -- já existe outra atualização em andamento")
    except Exception:
        log.exception("Atualização automática falhou")


def build_app() -> tk.Tk:
    """Constrói a janela raiz e decide qual tela mostrar. Não inicia o mainloop."""
    app_config = config.load_config()
    database.init_db(config.get_db_path())

    root = tk.Tk()
    root.title("GSUS Auditoria")
    root.resizable(False, False)

    render(root, app_config)

    return root


def main() -> None:
    # Achado real (suíte completa, DEC-073): isso NÃO pode ficar em nível de
    # módulo -- `test_app_shell.py` importa `app.main` só pra testar
    # `build_app()`/`render()`, e um efeito colateral no import vazaria pro
    # resto do processo de teste (sem `monkeypatch`, `os.environ` não
    # reverte sozinho) -- quebrou `test_census_parser.py` (usa Chromium do
    # cache global, que passou a não ser mais encontrado depois do redirect).
    config.configure_playwright_browsers_path()
    setup_logging()
    logging.getLogger(__name__).info("Iniciando GSUS Auditoria")

    if "--auto-update" in sys.argv[1:]:
        run_auto_update()
        return

    root = build_app()
    root.mainloop()


if __name__ == "__main__":
    main()
