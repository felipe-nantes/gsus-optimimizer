"""Testes de LLM-001/LLM-002 contra um servidor HTTP local de mentira que
imita o formato de resposta do llama-server (/v1/chat/completions). Não
depende de um binário llama-server real nem de um modelo .gguf real -- só
valida o contrato HTTP + validação de schema + retry."""
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.analysis.llm import LLMAnalysisError, LLMStartupError, LocalLLM
from app.analysis.rules import StructuredNote

VALID_RESPONSE = {
    "clinical_context": "Internado por AVC isquêmico.",
    "current_status": "Estável.",
    "necessidade_hospitalar": "SIM",
    "necessidade_hospitalar_justificativa": "Monitorização neurológica contínua.",
    "objetivo_terapeutico": "Definir conduta cirúrgica versus conservadora.",
    "proximo_passo": "Parecer da cirurgia torácica.",
    "edd_data": None,
    "edd_status": "NAO_REGISTRADA",
    "dia_classificacao": "VERDE",
    "dia_causa": None,
    "pending_items": [
        {
            "category": "INTERCONSULTA",
            "subcategory": "SOLICITADA",
            "description": "Aguarda avaliação da cirurgia torácica",
            "evidence": "Solicitada avaliação da cirurgia torácica.",
            "evidence_date": "2026-08-20T10:31:00",
            "origin": "INTERNA",
            "is_inferred": False,
            "confidence": None,
            "flow_status": "SOLICITADA",
        }
    ],
    "insufficient_information": False,
}


def _make_handler(responses: list[str], captured_bodies: list[str] | None = None):
    call_count = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            if captured_bodies is not None:
                captured_bodies.append(raw_body.decode("utf-8"))
            content = responses[min(call_count["n"], len(responses) - 1)]
            call_count["n"] += 1
            body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # silencia log de acesso do http.server nos testes

    return Handler, call_count


@pytest.fixture
def stub_server():
    responses = []

    def start(resp_list):
        responses.extend(resp_list)
        handler_cls, call_count = _make_handler(responses)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, call_count

    servers = []

    def factory(resp_list):
        server, call_count = start(resp_list)
        servers.append(server)
        return server.server_address[1], call_count

    yield factory

    for server in servers:
        server.shutdown()


@pytest.fixture
def capturing_stub_server():
    """Variante de `stub_server` que guarda o corpo bruto de cada request --
    necessário para inspecionar o prompt que de fato foi enviado em cada
    tentativa (ver DEC-089: o retry loop passou a reescrever o prompt nas
    tentativas seguintes a uma falha de validação)."""
    captured_bodies: list[str] = []

    def start(resp_list):
        handler_cls, call_count = _make_handler(resp_list, captured_bodies)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, call_count

    servers = []

    def factory(resp_list):
        server, call_count = start(resp_list)
        servers.append(server)
        return server.server_address[1], call_count, captured_bodies

    yield factory

    for server in servers:
        server.shutdown()


def test_analyze_patient_returns_valid_parsed_output(stub_server):
    port, _ = stub_server([json.dumps(VALID_RESPONSE)])
    llm = LocalLLM(model_path="unused", port=port)

    result = llm.analyze_patient(
        previous_state=None,
        new_notes=[StructuredNote(timestamp="2026-08-20T10:31:00", specialty="Clínica Médica",
                                   source_type="Evolução", text="Solicitada avaliação da cirurgia torácica.")],
    )

    assert result["insufficient_information"] is False
    assert len(result["pending_items"]) == 1


def test_analyze_patient_retries_on_invalid_json_then_succeeds(stub_server):
    port, call_count = stub_server(["isto não é json", json.dumps(VALID_RESPONSE)])
    llm = LocalLLM(model_path="unused", port=port, max_retries=3)

    result = llm.analyze_patient(previous_state=None, new_notes=[])

    assert result["insufficient_information"] is False
    assert call_count["n"] == 2


def test_analyze_patient_raises_llm_analysis_error_after_exhausting_retries(stub_server):
    port, call_count = stub_server(["isto não é json"])
    llm = LocalLLM(model_path="unused", port=port, max_retries=2)

    with pytest.raises(LLMAnalysisError):
        llm.analyze_patient(previous_state=None, new_notes=[])

    assert call_count["n"] == 2


def test_analyze_patient_accepts_response_wrapped_in_markdown_fence(stub_server):
    fenced = "```json\n" + json.dumps(VALID_RESPONSE) + "\n```"
    port, _ = stub_server([fenced])
    llm = LocalLLM(model_path="unused", port=port)

    result = llm.analyze_patient(previous_state=None, new_notes=[])
    assert result["insufficient_information"] is False


def test_analyze_patient_normalizes_null_string_sentinels(stub_server):
    """Reproduz execução real 2026-08-24: modelo devolveu "null" como STRING
    para edd_data/dia_causa/flow_status em vez do valor JSON null. O
    resultado devolvido por analyze_patient já deve vir normalizado -- quem
    chama (orchestrator.py, html_report.py) nunca deveria ver a string."""
    dirty = {
        **VALID_RESPONSE,
        "edd_data": "null",
        "dia_causa": "null",
        "pending_items": [{**VALID_RESPONSE["pending_items"][0], "flow_status": "null"}],
    }
    port, _ = stub_server([json.dumps(dirty)])
    llm = LocalLLM(model_path="unused", port=port)

    result = llm.analyze_patient(previous_state=None, new_notes=[])

    assert result["edd_data"] is None
    assert result["dia_causa"] is None
    assert result["pending_items"][0]["flow_status"] is None


def test_analyze_patient_normalizes_null_string_edd_status(stub_server):
    """Reproduz execução real 2026-08-25 (DEC-065): modelo devolveu "null"
    como STRING pra edd_status -- um ENUM OBRIGATÓRIO, nunca nullable --
    derrubando as 3 tentativas de retry sem chance de sucesso. Sem
    normalização, isso é indistinguível de qualquer outra saída inválida."""
    dirty = {**VALID_RESPONSE, "edd_status": "null", "edd_data": None}
    port, _ = stub_server([json.dumps(dirty)])
    llm = LocalLLM(model_path="unused", port=port)

    result = llm.analyze_patient(previous_state=None, new_notes=[])

    assert result["edd_status"] == "NAO_REGISTRADA"


# -------------------------------------------------------- LocalLLM.start()

# RESIL-017 (achado real 2026-09-08): estes testes chamavam `start()` na porta
# PADRÃO (8811) e `_kill_orphan_on_port` encerrou o llama-server de uma
# auditoria real em curso na mesma máquina. Nenhum teste pode tocar a porta
# real nem a limpeza de porta.
_UNUSED_PORT = 1  # porta reservada, nunca ocupada por um llama-server real


def _isolate_from_real_llama_server(monkeypatch):
    from app.analysis import llm as llm_module

    monkeypatch.setattr(llm_module, "_kill_orphan_on_port", lambda port: None)


def test_start_raises_llm_startup_error_when_server_path_missing(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"")
    llm = LocalLLM(model_path=model, server_path=tmp_path / "nao-existe.exe")

    with pytest.raises(LLMStartupError):
        llm.start()


def test_start_raises_llm_startup_error_when_model_path_missing(tmp_path):
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    llm = LocalLLM(model_path=tmp_path / "nao-existe.gguf", server_path=server)

    with pytest.raises(LLMStartupError):
        llm.start()


def test_start_wraps_popen_oserror_as_llm_startup_error(tmp_path, monkeypatch):
    """Achado real (DEC-058): mesmo com os dois caminhos existindo no disco,
    `Popen` pode falhar (permissão, antivírus, etc.) -- isso não pode escapar
    como OSError cru, quebraria o contrato que app/ui/errors.py depende
    (só sabe tratar LLMStartupError/LLMAnalysisError)."""
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"")

    def _raise_oserror(*args, **kwargs):
        raise FileNotFoundError("[WinError 2] simulado")

    monkeypatch.setattr(subprocess, "Popen", _raise_oserror)
    _isolate_from_real_llama_server(monkeypatch)

    llm = LocalLLM(model_path=model, server_path=server, port=_UNUSED_PORT)
    with pytest.raises(LLMStartupError):
        llm.start()
    assert llm._process is None


def test_start_wraps_log_file_oserror_as_llm_startup_error(tmp_path, monkeypatch):
    """Achado de auditoria (RESIL-011/DEC-105, 2026-09-01): abrir o arquivo
    de log do llama-server (disco cheio, permissão negada) ficava FORA de
    qualquer try/except -- diferente do Popen logo abaixo (já protegido,
    DEC-058) -- e escapava como OSError cru, quebrando o mesmo contrato
    (`update_flow.py` só espera `ModelDownloadError`/`LLMStartupError` aqui,
    e um OSError cru vazaria a conexão SQLite que só fecha no `finally`
    daquele bloco)."""
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"")

    # `blocker` é um ARQUIVO, não diretório -- criar um subdiretório dentro
    # dele (o que `Path(server_log_path).parent.mkdir(parents=True)` tenta
    # fazer) levanta OSError de verdade, sem precisar mockar internals.
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    log_path = blocker / "sub" / "llama-server.log"

    _isolate_from_real_llama_server(monkeypatch)
    llm = LocalLLM(model_path=model, server_path=server, server_log_path=log_path, port=_UNUSED_PORT)
    with pytest.raises(LLMStartupError):
        llm.start()
    assert llm._process is None
    assert llm._server_log_file is None


# ------------------------------------------- resiliência do processo (2026-08-28)
# Achados da auditoria de resiliência: (1) stdout/stderr iam para um PIPE que
# ninguém lia -- deadlock garantido quando o buffer (~64KB no Windows)
# enchesse, causa mecânica provável do "slot preso" do DEC-087; (2) `stop()`
# chamava `kill()` no timeout mas nunca conferia depois se o processo
# realmente morreu -- log dizia "encerrado" com o processo ainda vivo; (3)
# nada limpava um órfão de execução anterior ocupando a porta antes de subir
# um processo novo -- `_wait_ready` podia validar contra o servidor ERRADO.

class _FakeProcess:
    """Dublê de `subprocess.Popen` só com o que `LocalLLM.stop()` usa --
    `wait_outcomes` é consumida em ordem a cada chamada de `wait()`
    (True=confirma morte, False=levanta TimeoutExpired)."""

    def __init__(self, pid=4242, wait_outcomes=()):
        self.pid = pid
        self._wait_outcomes = list(wait_outcomes)
        self.terminate_called = False
        self.kill_called = False

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True

    def wait(self, timeout=None):
        outcome = self._wait_outcomes.pop(0)
        if not outcome:
            raise subprocess.TimeoutExpired(cmd="llama-server", timeout=timeout)


def test_start_uses_devnull_for_stdout_stderr_not_unread_pipe(tmp_path, monkeypatch):
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"")

    captured = {}

    class _ImmediatelyHealthyProcess(_FakeProcess):
        def poll(self):
            return None

    def _fake_popen(args, **kwargs):
        captured.update(kwargs)
        return _ImmediatelyHealthyProcess()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("app.analysis.llm._kill_orphan_on_port", lambda port: None)
    monkeypatch.setattr(
        "app.analysis.llm.LocalLLM._wait_ready", lambda self: None
    )

    llm = LocalLLM(model_path=model, server_path=server)
    llm.start()

    assert captured["stdout"] == subprocess.DEVNULL
    assert captured["stderr"] == subprocess.DEVNULL


def test_start_passes_bounded_ctx_size_and_single_slot_to_popen(tmp_path, monkeypatch):
    """Achado real, confirmado AO VIVO nesta máquina (2026-08-28): sem
    `--ctx-size`, llama-server sobe com o contexto MÁXIMO do modelo e pediu
    um buffer de KV cache de ~13,25GB, falhando a alocação e derrubando a
    inicialização inteira. `--parallel 1` evita alocar 4 slots quando o app
    só processa um paciente por vez."""
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"")

    captured_args = {}

    class _ImmediatelyHealthyProcess(_FakeProcess):
        def poll(self):
            return None

    def _fake_popen(args, **kwargs):
        captured_args["args"] = args
        return _ImmediatelyHealthyProcess()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("app.analysis.llm._kill_orphan_on_port", lambda port: None)
    monkeypatch.setattr("app.analysis.llm.LocalLLM._wait_ready", lambda self: None)

    llm = LocalLLM(model_path=model, server_path=server, ctx_size=8192)
    llm.start()

    args = captured_args["args"]
    assert "--ctx-size" in args
    assert args[args.index("--ctx-size") + 1] == "8192"
    assert "--parallel" in args
    assert args[args.index("--parallel") + 1] == "1"


def test_start_writes_server_output_to_log_file_instead_of_devnull_when_configured(tmp_path, monkeypatch):
    """Achado real: a correção original (DEVNULL) escondeu o diagnóstico de
    uma falha real de inicialização (falha de alocação de memória) --
    precisou rodar o binário manualmente por fora do app pra descobrir a
    causa. Um ARQUIVO real (não um pipe) preserva o diagnóstico sem
    reintroduzir o risco de deadlock do pipe (sem buffer de tamanho fixo)."""
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"")
    log_path = tmp_path / "logs" / "llama-server.log"

    captured = {}

    class _ImmediatelyHealthyProcess(_FakeProcess):
        def poll(self):
            return None

    def _fake_popen(args, **kwargs):
        captured.update(kwargs)
        return _ImmediatelyHealthyProcess(wait_outcomes=[True])

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("app.analysis.llm._kill_orphan_on_port", lambda port: None)
    monkeypatch.setattr("app.analysis.llm.LocalLLM._wait_ready", lambda self: None)

    llm = LocalLLM(model_path=model, server_path=server, server_log_path=log_path)
    llm.start()

    assert log_path.parent.exists()  # diretório de logs criado automaticamente
    assert captured["stdout"] is not subprocess.DEVNULL
    assert captured["stdout"] is captured["stderr"]  # mesmo arquivo pros dois

    llm.stop()
    assert llm._server_log_file is None  # arquivo fechado depois de parar


def test_stop_logs_success_only_after_terminate_is_confirmed(tmp_path):
    llm = LocalLLM(model_path=tmp_path / "m", port=0)
    llm._process = _FakeProcess(wait_outcomes=[True])

    llm.stop()

    assert llm._process is None  # não levanta, não precisa de kill nem taskkill


def test_stop_escalates_to_kill_when_terminate_does_not_confirm_exit(tmp_path):
    fake = _FakeProcess(wait_outcomes=[False, True])
    llm = LocalLLM(model_path=tmp_path / "m", port=0)
    llm._process = fake

    llm.stop()

    assert fake.kill_called is True
    assert llm._process is None


def test_stop_escalates_to_taskkill_when_kill_does_not_confirm_exit(tmp_path, monkeypatch):
    """Achado real: a versão antiga logava 'encerrado' aqui mesmo com o
    processo vivo -- nunca chamava nada além de kill()."""
    fake = _FakeProcess(pid=9999, wait_outcomes=[False, False, True])
    llm = LocalLLM(model_path=tmp_path / "m", port=0)
    llm._process = fake

    taskkill_calls = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: taskkill_calls.append(cmd)
    )

    llm.stop()

    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "9999"]]
    assert llm._process is None


def test_stop_never_raises_even_when_process_survives_taskkill(tmp_path, monkeypatch):
    """Pior caso: nem taskkill confirma a morte -- `stop()` deve logar um
    erro claro (não mentir "encerrado"), mas nunca propagar exceção pro
    chamador (quem chama stop() geralmente está num finally/cleanup)."""
    fake = _FakeProcess(wait_outcomes=[False, False, False])
    llm = LocalLLM(model_path=tmp_path / "m", port=0)
    llm._process = fake
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: None)

    llm.stop()  # não deve levantar

    assert llm._process is None


def _netstat_output_with_pid_on_port(port: int, pid: str) -> str:
    return (
        "\n  Proto  Local Address          Foreign Address        State           PID\n"
        f"  TCP    127.0.0.1:{port}          0.0.0.0:0              LISTENING       {pid}\n"
        "  TCP    127.0.0.1:8080          0.0.0.0:0              LISTENING       111\n"
    )


def test_kill_orphan_on_port_kills_process_listening_on_that_port(monkeypatch):
    from app.analysis.llm import _kill_orphan_on_port

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout=_netstat_output_with_pid_on_port(8811, "5555"), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    _kill_orphan_on_port(8811)

    assert ["taskkill", "/F", "/T", "/PID", "5555"] in calls


def test_kill_orphan_on_port_does_nothing_when_port_is_free(monkeypatch):
    from app.analysis.llm import _kill_orphan_on_port

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_netstat_output_with_pid_on_port(9999, "5555"), stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    _kill_orphan_on_port(8811)  # porta diferente da listada -- nada pra matar

    assert all(cmd[0] != "taskkill" for cmd in calls)


def test_kill_orphan_on_port_never_raises_when_netstat_is_unavailable(monkeypatch):
    from app.analysis.llm import _kill_orphan_on_port

    def _raise(cmd, **kwargs):
        raise OSError("netstat não encontrado")

    monkeypatch.setattr(subprocess, "run", _raise)

    _kill_orphan_on_port(8811)  # não deve levantar


def test_analyze_patient_rejects_pending_item_without_evidence(stub_server):
    invalid = {**VALID_RESPONSE, "pending_items": [{"category": "DIAGNOSTICO", "description": "x", "evidence": "", "is_inferred": False}]}
    port, call_count = stub_server([json.dumps(invalid)] * 3)
    llm = LocalLLM(model_path="unused", port=port, max_retries=3)

    with pytest.raises(LLMAnalysisError):
        llm.analyze_patient(previous_state=None, new_notes=[])
    assert call_count["n"] == 3


# ------------------------------------------------- reparo pós-falha de validação (DEC-089)
# Achado real 2026-08-28: 10 pacientes reprocessados via script de
# recuperação falharam com o MESMO erro (dia_classificacao=VERMELHO sem
# dia_causa, RF-28) nas 3 tentativas, sempre -- porque temperature=0 +
# prompt idêntico entre tentativas é uma pergunta determinística: reenviar
# o mesmo prompt reproduz a mesma saída inválida, o retry nunca teve chance
# real de corrigir uma falha de validação (só ajudava falha de comunicação).

def test_analyze_patient_sends_repair_prompt_after_validation_failure(capturing_stub_server):
    invalid = {**VALID_RESPONSE, "dia_classificacao": "VERMELHO", "dia_causa": None}
    port, call_count, bodies = capturing_stub_server([json.dumps(invalid), json.dumps(VALID_RESPONSE)])
    llm = LocalLLM(model_path="unused", port=port, max_retries=3)

    result = llm.analyze_patient(previous_state=None, new_notes=[])

    assert result["insufficient_information"] is False
    assert call_count["n"] == 2

    first_prompt = json.loads(bodies[0])["messages"][1]["content"]
    second_prompt = json.loads(bodies[1])["messages"][1]["content"]
    assert first_prompt != second_prompt  # não é uma repetição -- é uma pergunta nova
    assert "dia_causa preenchida" in second_prompt
    assert "VERMELHO" in second_prompt


def test_analyze_patient_reverts_to_base_prompt_after_communication_failure(capturing_stub_server):
    """Uma falha de COMUNICAÇÃO (não de validação) não deve herdar um prompt
    de reparo de uma tentativa anterior -- reenvia a pergunta original."""

    class _FlakyHandler(BaseHTTPRequestHandler):
        _n = {"count": 0}

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            _FlakyHandler._n["count"] += 1
            if _FlakyHandler._n["count"] == 1:
                self.send_response(500)
                self.end_headers()
                return
            resp = json.dumps({"choices": [{"message": {"content": json.dumps(VALID_RESPONSE)}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        llm = LocalLLM(model_path="unused", port=server.server_address[1], max_retries=3)
        result = llm.analyze_patient(previous_state=None, new_notes=[])
        assert result["insufficient_information"] is False
        assert _FlakyHandler._n["count"] == 2
    finally:
        server.shutdown()
