"""Configuração não sensível da aplicação (JSON local). Nunca guarda credenciais."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "GSUSAuditoria"

DEFAULT_GSUS_BASE_URL = "https://gsus.pr.gov.br"
DEFAULT_SCHEDULE_TIME = "00:01"
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_LLM_HOST = "127.0.0.1"
DEFAULT_LLM_PORT = 8811
# RETENTION-001/DEC-068: texto bruto de evolução de paciente com alta é
# purgado depois desse prazo -- resumo estruturado (patient_state/
# pending_items/pending_item_evidence, com citações curtas de evidência)
# fica indefinidamente, é o que a seção 17 da orientação técnica precisa
# pra validação auditor×IA. 90 dias é ponto de partida, não validado
# institucionalmente -- mesmo princípio do RULES-001/RF-07.
DEFAULT_RAW_NOTES_RETENTION_DAYS = 90


def get_app_root() -> Path:
    """Diretório-base pra resolver caminhos relativos configurados
    (`model_path`/`llm_server_path`) -- NUNCA o diretório de trabalho do
    processo. Achado real (DEC-059/069): `Popen` recusa executável relativo
    resolvido contra um `cwd` que não é garantido ser a pasta de instalação
    -- um `.exe` empacotado (PyInstaller) ou disparado pelo Windows Task
    Scheduler (SCHEDULE-001) pode rodar com `cwd` completamente diferente.

    Modo frozen (`sys.frozen`, definido pelo PyInstaller): pasta do próprio
    executável -- é onde `runtime/`/`models/`/`firefox/` ficam, per
    ARCHITECTURE.md seção 6 (estrutura de distribuição).
    Modo dev: raiz do projeto (pai de `app/`) -- funciona com `python -m
    app.main` de qualquer diretório."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_app_path(path_str: str | Path) -> Path:
    """Resolve um caminho configurado (`model_path`/`llm_server_path`) --
    absoluto passa direto; relativo é resolvido contra `get_app_root()`,
    nunca contra `Path.exists()`/`Popen`'s próprias regras de resolução de
    diretório de trabalho (que divergem uma da outra -- achado real, DEC-059:
    `Path.exists()` aceita um relativo que `Popen` recusa)."""
    path = Path(path_str)
    return path if path.is_absolute() else (get_app_root() / path)


DEFAULT_PLAYWRIGHT_BROWSERS_DIR = "playwright-browsers"


def configure_playwright_browsers_path() -> None:
    """Aponta o Playwright pra uma pasta DENTRO da instalação (BUILD-001/
    DEC-073) em vez do cache global do usuário (`%LOCALAPPDATA%\\ms-playwright`)
    -- decisão do usuário 2026-08-25: o Firefox real usado contra o GSUS
    (DEC-010) precisa ir dentro do instalador pra funcionar numa máquina
    limpa (E2E-001), sem depender de `playwright install` rodar de novo lá.

    Deve ser chamada ANTES de qualquer `sync_playwright()`/`firefox.launch()`
    -- o Playwright lê `PLAYWRIGHT_BROWSERS_PATH` do ambiente em tempo de
    uso, não import, mas todo ponto de entrada real (`app/main.py` e os
    scripts `check_*.py`) chama isso logo no início por segurança."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(resolve_app_path(DEFAULT_PLAYWRIGHT_BROWSERS_DIR))


def get_app_data_dir() -> Path:
    """Diretório gravável sem elevação para dados da aplicação (ver DEC-003)."""
    override = os.environ.get("GSUS_AUDITORIA_DATA_DIR")
    if override:
        path = Path(override)
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            path = Path(local_app_data) / APP_NAME
        else:
            # Fallback para ambientes sem LOCALAPPDATA (ex.: dev fora do Windows).
            path = Path.cwd() / f".{APP_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path() -> Path:
    return get_app_data_dir() / "auditoria.db"


def get_config_path() -> Path:
    return get_app_data_dir() / "config.json"


def get_logs_dir() -> Path:
    logs_dir = get_app_data_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


@dataclass
class AppConfig:
    gsus_username: str = ""
    gsus_base_url: str = DEFAULT_GSUS_BASE_URL
    unit: str = ""
    schedule_time: str = DEFAULT_SCHEDULE_TIME
    model_path: str = "models/model.gguf"
    llm_server_path: str = "runtime/llama-server.exe"
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    llm_host: str = DEFAULT_LLM_HOST
    llm_port: int = DEFAULT_LLM_PORT
    raw_notes_retention_days: int = DEFAULT_RAW_NOTES_RETENTION_DAYS
    # Achado real (2026-09-01, pedido do usuário): acelera um catch-up
    # pontual do banco -- limita a extração de evolução aos N dias mais
    # recentes por paciente, mesmo pra quem nunca foi extraído antes (sem
    # isso, o primeiro contato com uma internação longa abre TODOS os dias
    # dela). `None` (padrão) mantém o comportamento normal, sem limite --
    # ver app/gsus/records.py::extract_notes/_days_beyond_cap.
    max_days_per_patient: int | None = None
    # UI-006 (2026-09-04, pedido do usuário): interruptor "Navegador visível"
    # x "Segundo plano" na tela principal. True (padrão) = Firefox aberto na
    # tela durante a atualização, único modo comprovado contra o GSUS real
    # (DEC-077: o login travava 100% em headless numa máquina limpa). False =
    # `headless=True` no Playwright -- o Firefox não aparece. Persistido aqui
    # pra valer também na execução agendada (`--auto-update`). Configs antigas
    # sem a chave carregam o padrão (`from_dict` ignora chave ausente).
    browser_visible: bool = True
    configured: bool = False

    def to_dict(self) -> dict:
        # DEC-104 (auditoria de segurança pré-entrega, 2026-09-01): `gsus_username`
        # é o CPF do usuário -- dado pessoal sensível, não uma preferência de
        # app. NUNCA persistido aqui, apesar do módulo dizer "nunca guarda
        # credenciais" (linha 1) -- ele já é gravado com segurança no Windows
        # Credential Manager (`app.security.credentials`, junto da senha); tê-lo
        # TAMBÉM em texto plano em `config.json` violava esse princípio e já
        # causou uma exposição real e documentada (ver DECISIONS.md DEC-104 --
        # inspecionar o JSON pra editar outro campo imprimiu o CPF sem
        # necessidade). Continua existindo como campo do dataclass (uso em
        # memória durante a sessão, ex. pré-preencher o campo de usuário ao
        # reabrir Configurações) -- só não sai pro disco.
        data = asdict(self)
        del data["gsus_username"]
        return data

    @staticmethod
    def from_dict(data: dict) -> "AppConfig":
        known_fields = {f for f in AppConfig.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return AppConfig(**filtered)


def _load_username_from_credential_store() -> str:
    """CPF nunca vem do `config.json` (DEC-104) -- sempre do Credential
    Manager, onde já é gravado com segurança junto da senha
    (`app.security.credentials.save_credential`). Import tardio evita
    custo/acoplamento pra quem só usa `AppConfig` sem tocar credenciais
    (ex.: testes que constroem `AppConfig()` direto)."""
    from app.security import credentials

    cred = credentials.get_credential("gsus")
    return cred[0] if cred is not None else ""


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or get_config_path()
    if not config_path.exists():
        cfg = AppConfig()
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = AppConfig()
        else:
            cfg = AppConfig.from_dict(data)
    cfg.gsus_username = _load_username_from_credential_store()
    return cfg


def save_config(config: AppConfig, path: Path | None = None) -> None:
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
    tmp_path.replace(config_path)
