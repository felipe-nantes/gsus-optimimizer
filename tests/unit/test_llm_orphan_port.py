"""RESIL-017: a limpeza de porta antes de subir o llama-server só pode encerrar
um processo que seja llama-server -- achado real (2026-09-08): um teste de
integração chamou `LocalLLM.start()` na porta padrão e `_kill_orphan_on_port`
matou o llama-server da auditoria que rodava na máquina. Sem processos reais:
`subprocess.run` é substituído por um fake que devolve netstat/tasklist."""
import subprocess

from app.analysis import llm


class _FakeRun:
    def __init__(self, image_name: str):
        self.image_name = image_name
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[0] == "netstat":
            stdout = (
                "  Proto  Local Address          Foreign Address        State           PID\n"
                "  TCP    127.0.0.1:8811         0.0.0.0:0              LISTENING       4242\n"
                "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1000\n"
            )
        elif args[0] == "tasklist":
            stdout = f'"{self.image_name}","4242","Console","1","1.234 K"\n'
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def test_orphan_llama_server_on_the_port_is_killed(monkeypatch):
    fake = _FakeRun("llama-server.exe")
    monkeypatch.setattr(llm.subprocess, "run", fake)

    llm._kill_orphan_on_port(8811)

    kills = [c for c in fake.calls if c[0] == "taskkill"]
    assert kills == [["taskkill", "/F", "/T", "/PID", "4242"]]


def test_process_that_is_not_llama_server_is_never_killed(monkeypatch):
    fake = _FakeRun("python.exe")
    monkeypatch.setattr(llm.subprocess, "run", fake)

    llm._kill_orphan_on_port(8811)

    assert not [c for c in fake.calls if c[0] == "taskkill"]


def test_unknown_image_name_keeps_the_old_behaviour(monkeypatch):
    """tasklist indisponível/sem resposta: continua limpando (rede de
    segurança original, DEC-088), pra não regredir o caso do órfão real."""
    fake = _FakeRun("")
    monkeypatch.setattr(llm.subprocess, "run", fake)

    llm._kill_orphan_on_port(8811)

    assert [c for c in fake.calls if c[0] == "taskkill"] == [["taskkill", "/F", "/T", "/PID", "4242"]]
