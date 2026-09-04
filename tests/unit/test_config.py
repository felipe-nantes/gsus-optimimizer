import sys
from pathlib import Path

from app import config
from app.security import credentials


def _no_saved_credential(monkeypatch):
    """`load_config` (DEC-104) sempre consulta o Credential Manager real pra
    preencher `gsus_username` -- mocado por padrão nos testes deste arquivo
    pra não depender do que porventura esteja salvo na máquina de quem roda
    a suíte (hermético) nem arriscar um CPF real aparecendo numa mensagem
    de falha de teste."""
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)


def test_load_config_missing_file_returns_defaults(tmp_path, monkeypatch):
    _no_saved_credential(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg = config.load_config(cfg_path)
    assert cfg.configured is False
    assert cfg.schedule_time == config.DEFAULT_SCHEDULE_TIME


def test_save_config_never_persists_gsus_username_to_disk(tmp_path):
    """DEC-104 (auditoria de segurança pré-entrega, 2026-09-01): `gsus_username`
    é o CPF do usuário -- dado pessoal sensível, não uma preferência de app.
    Já causou uma exposição real e documentada (inspecionar `config.json`
    pra editar outro campo imprimiu o CPF sem necessidade). Só vive no
    Windows Credential Manager (`app.security.credentials`) a partir de
    agora, nunca em texto plano no disco."""
    cfg_path = tmp_path / "config.json"
    cfg = config.AppConfig(gsus_username="12345678900", unit="Clínica Médica", configured=True)

    assert "gsus_username" not in cfg.to_dict()

    config.save_config(cfg, cfg_path)
    raw = cfg_path.read_text(encoding="utf-8")
    assert "12345678900" not in raw


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    """`gsus_username` não vem mais do JSON -- vem do Credential Manager
    (mocado aqui, ver DEC-104). Os demais campos continuam fazendo o
    round-trip normal via `config.json`."""
    monkeypatch.setattr(credentials, "get_credential", lambda name: ("jsilva", "senha-fake") if name == "gsus" else None)

    cfg_path = tmp_path / "config.json"
    cfg = config.AppConfig(gsus_username="jsilva", unit="Clínica Médica", configured=True)
    config.save_config(cfg, cfg_path)

    loaded = config.load_config(cfg_path)
    assert loaded.gsus_username == "jsilva"  # veio do Credential Manager mocado, não do JSON
    assert loaded.unit == "Clínica Médica"
    assert loaded.configured is True


def test_load_config_corrupted_file_returns_defaults(tmp_path, monkeypatch):
    _no_saved_credential(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{not valid json", encoding="utf-8")
    cfg = config.load_config(cfg_path)
    assert cfg.configured is False


def test_save_config_never_contains_password_field():
    cfg = config.AppConfig()
    assert "password" not in cfg.to_dict()


# --------------------------------------------- get_app_root/resolve_app_path (DEC-069)
# Achado real (DEC-059): caminho relativo resolvido contra o diretório de
# trabalho do PROCESSO (não a pasta de instalação) fazia `Popen` recusar o
# executável mesmo com `Path.exists()` aceitando -- `.exe` empacotado ou
# disparado pelo Task Scheduler não garante que o cwd seja a pasta certa.

def test_get_app_root_dev_mode_is_project_root(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    root = config.get_app_root()
    assert (root / "app" / "config.py").resolve() == Path(config.__file__).resolve()


def test_get_app_root_frozen_mode_is_executable_directory(monkeypatch, tmp_path):
    fake_exe = tmp_path / "GSUSAuditoria" / "gsus-auditoria.exe"
    fake_exe.parent.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert config.get_app_root() == fake_exe.parent.resolve()


def test_resolve_app_path_absolute_passes_through(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    absolute = Path("C:/some/absolute/path/model.gguf") if sys.platform == "win32" else Path("/some/absolute/model.gguf")
    assert config.resolve_app_path(str(absolute)) == absolute


def test_resolve_app_path_relative_resolves_against_app_root(monkeypatch, tmp_path):
    fake_exe = tmp_path / "gsus-auditoria.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    resolved = config.resolve_app_path("models/model.gguf")

    assert resolved == (tmp_path / "models" / "model.gguf").resolve()


# ------------------------------------------------------------- UI-006 (browser_visible)

def test_browser_visible_defaults_true_when_key_missing_in_old_config(tmp_path, monkeypatch):
    """Config gravado por versões anteriores não tem a chave -- precisa carregar
    com o padrão (navegador visível, DEC-077), nunca falhar nem virar False."""
    _no_saved_credential(monkeypatch)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"unit": "Clínica Médica", "configured": true}', encoding="utf-8")
    assert config.load_config(cfg_path).browser_visible is True


def test_browser_visible_false_roundtrips_through_config_json(tmp_path, monkeypatch):
    _no_saved_credential(monkeypatch)
    cfg_path = tmp_path / "config.json"
    config.save_config(config.AppConfig(unit="Clínica Médica", configured=True, browser_visible=False), cfg_path)
    assert '"browser_visible": false' in cfg_path.read_text(encoding="utf-8")
    assert config.load_config(cfg_path).browser_visible is False
