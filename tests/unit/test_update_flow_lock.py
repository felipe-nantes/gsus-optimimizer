"""Testa o lock de instância única do "Atualizar agora" (achado real,
DEC-086/088, 2026-08-28): a tarefa agendada disparou em cima de um clique
manual já em andamento -- as duas tentaram usar o mesmo GSUS, a mesma porta
do LLM e o mesmo banco ao mesmo tempo, sem nenhuma exclusão mútua. A
segunda instância ficou travada por horas, sem nunca falhar nem terminar
sozinha. Lock via `msvcrt.locking` (API nativa do Windows) -- sem
dependência nova, mesmo princípio do DEC-002."""
from app import config
from app.security import credentials
from app.update_flow import UpdateAlreadyRunningError, _InstanceLock, run_update


def test_instance_lock_second_acquire_fails_while_first_holds_it(tmp_path):
    lock_path = tmp_path / "update.lock"
    first = _InstanceLock(lock_path)
    second = _InstanceLock(lock_path)

    assert first.acquire() is True
    assert second.acquire() is False  # já travado pelo primeiro

    first.release()


def test_instance_lock_can_reacquire_after_release(tmp_path):
    lock_path = tmp_path / "update.lock"
    first = _InstanceLock(lock_path)
    assert first.acquire() is True
    first.release()

    second = _InstanceLock(lock_path)
    assert second.acquire() is True  # livre de novo -- release() funcionou de verdade
    second.release()


def test_run_update_raises_when_another_update_already_holds_the_lock(tmp_path, monkeypatch):
    """O lock é a PRIMEIRA coisa checada em `run_update` -- nem chega a
    olhar credencial/GSUS se outra atualização já estiver rodando."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        credentials, "get_credential", lambda name: (_ for _ in ()).throw(AssertionError("não deveria ser chamado"))
    )

    held_by_outra_execucao = _InstanceLock(config.get_app_data_dir() / "update.lock")
    assert held_by_outra_execucao.acquire() is True
    try:
        cfg = config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True)
        try:
            run_update(cfg, tmp_path / "relatorio.html")
            assert False, "deveria ter levantado UpdateAlreadyRunningError"
        except UpdateAlreadyRunningError:
            pass
    finally:
        held_by_outra_execucao.release()


def test_run_update_releases_lock_after_finishing_so_next_call_can_acquire(tmp_path, monkeypatch):
    """Achado real (DEC-088): o lock não pode ficar preso pra sempre depois
    de uma execução terminar (com sucesso ou falha) -- senão a PRÓXIMA
    tentativa legítima (clique manual de amanhã, ou a tarefa agendada)
    ficaria bloqueada por engano."""
    monkeypatch.setenv("GSUS_AUDITORIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "get_credential", lambda name: None)  # falha rápida, sem GSUS/LLM

    cfg = config.AppConfig(gsus_username="11122233344", unit="Clínica Médica", configured=True)
    try:
        run_update(cfg, tmp_path / "relatorio.html")
    except RuntimeError:
        pass  # esperado -- sem credencial configurada, mas o lock deve ter sido liberado mesmo assim

    # Lock livre de novo -- uma nova tentativa consegue adquirir.
    probe = _InstanceLock(config.get_app_data_dir() / "update.lock")
    assert probe.acquire() is True
    probe.release()
