"""Integração com o LLM local (LLM-001 / LLM-002).

O modelo roda em processo local (`llama-server`, llama.cpp) vinculado a
127.0.0.1. Este módulo NUNCA controla o navegador nem toma decisão clínica
(prompt mestre seção 21) -- ele só formata o incremento (estado anterior +
notas novas) em um prompt, chama o servidor local via HTTP e valida a saída
contra o contrato de app/analysis/schemas.py antes de devolver ao chamador.

A troca de modelo GGUF não deve exigir alterar nada fora deste módulo
(RNF-06): quem chama `LocalLLM.analyze_patient` não sabe (nem precisa saber)
qual .gguf está carregado.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.analysis.rules import StructuredNote, most_recent_note
from app.analysis.schemas import normalize_null_sentinels, validate_analysis_output
from app.analysis.taxonomy import SUBTYPES_BY_CATEGORY

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"  # nunca 0.0.0.0 -- ver PROJECT_SPEC.md RNF-05
DEFAULT_PORT = 8811
DEFAULT_MAX_RETRIES = 3
# 60s (valor original, LLM-001) já era justo com o prompt antigo, mais
# simples (~80s observados no benchmark real). O prompt do DEC-057 é bem
# maior (taxonomia inteira + schema completo embutidos no SYSTEM_PROMPT) --
# mais tokens de entrada para processar e mais campos para gerar. Em
# execução real (2026-08-24) as 3 tentativas deram timeout de comunicação
# aos 60s, mesmo com o modelo já carregado -- não era erro de validação.
# Subiu de novo pra 480s em 2026-08-25 (DEC-065): validação contra pacientes
# reais escolhidos aleatoriamente (não os mesmos de sempre) tinha admissões
# bem mais longas (~43 evoluções em vez de ~5-6) -- o incremento diário
# normal (RF-08) processa só notas NOVAS, bem menor, mas a PRIMEIRA análise
# de um paciente com histórico longo (ou depois de um hiato grande sem
# rodar) reenvia tudo de uma vez e estourava 240s repetidamente.
#
# Subiu de novo pra 1800s em 2026-08-25 (DEC-070/071): mesmo com prompt
# pequeno (~2500 tokens), benchmark isolado (texto fictício) mediu ~1,1
# token/segundo de geração no hardware alvo real (notebook Intel i5-1235U,
# 15W) -- 4-6x mais lento que a suposição usada até aqui. Não é bug de
# configuração (contexto/threads testados e descartados como causa, ver
# DEC-070). Só é seguro alargar o timeout assim porque a análise por IA
# deixou de bloquear quem clicou "Atualizar agora" (DEC-071 -- roda depois
# do relatório com regras já estar disponível).
DEFAULT_TIMEOUT_SECONDS = 1800
# DEC-070 (achado secundário): a chamada nunca limitava `max_tokens` --
# geração ficava sem teto (`--n-predict` default do llama-server é -1,
# infinito). Um teto generoso o bastante pro JSON completo do contrato
# (várias pendências com evidência) evita que uma resposta que não pare de
# forma limpa consuma o timeout inteiro sem necessidade.
DEFAULT_MAX_TOKENS = 1200
# Hardware-alvo confirmada como fraca (sem GPU, RAM apertada -- ver
# CURRENT_STATE.md): carregar um modelo de 8B em CPU pode passar de 60s
# facilmente. 5 minutos dá margem sem virar espera infinita.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 300

# Achado real, confirmado ao vivo (auditoria de resiliência 2026-08-28,
# DEC-070 já suspeitava): sem `--ctx-size`, `llama-server` sobe com o
# contexto MÁXIMO do modelo -- nesta máquina, isso pediu um buffer de KV
# cache de ~13,25GB e falhou a alocação (RAM insuficiente no momento),
# derrubando a inicialização inteira (`llama_server: exiting due to model
# loading error`, código de saída 1). 8192 tokens é generoso o bastante pro
# maior prompt real deste app (system prompt + taxonomia + até 14 dias de
# evolução já recortados pelo DEC-066 + resposta de até DEFAULT_MAX_TOKENS)
# e testado ao vivo nesta máquina sem falha de alocação.
DEFAULT_CTX_SIZE = 8192


def _image_name_of_pid(pid: str) -> str:
    """Nome do executável do processo `pid` via `tasklist` (nativo do
    Windows), em minúsculas; vazio se não der pra descobrir. Best-effort,
    nunca levanta."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.splitlines():
        if line.startswith('"'):
            return line.split('","')[0].strip('"').lower()
    return ""


def _kill_orphan_on_port(port: int) -> None:
    """Achado real (auditoria de resiliência 2026-08-28): se uma execução
    anterior deixou um `llama-server` órfão vivo (ver `stop()`), a próxima
    `start()` sobe um processo NOVO mas `_wait_ready` pode acabar validando
    o `/health` do órfão antigo (mesma porta) em vez do processo que
    acabamos de iniciar -- a execução "funciona" contra um servidor errado,
    possivelmente travado. Limpeza best-effort via `netstat`/`taskkill`
    (nativos do Windows, mesmo princípio do DEC-002) antes de subir um novo
    processo. Nunca levanta -- é só uma rede de segurança extra, nunca pode
    virar um motivo novo de falha na inicialização."""
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        pid = parts[4]
        if pid == "0":
            continue
        # RESIL-017 (achado real 2026-09-08): esta limpeza matou o llama-server
        # de uma auditoria EM CURSO quando um teste de integração chamou
        # `start()` na porta padrão a partir de outro processo. A porta ocupada
        # só é 'órfão' se o dono for um llama-server; qualquer outro programa
        # na porta fica em paz (e o start falha adiante, de forma visível).
        image = _image_name_of_pid(pid)
        if image and "llama" not in image:
            logger.warning(
                "Porta %d ocupada pelo processo %s (pid %s), que não é llama-server -- não encerrado.",
                port, image, pid,
            )
            continue
        logger.warning(
            "Porta %d já ocupada (pid %s) antes de iniciar llama-server -- "
            "encerrando processo órfão de execução anterior", port, pid,
        )
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


def _taxonomy_prompt_block() -> str:
    """Gerado a partir de `app.analysis.taxonomy` -- fonte única de
    verdade, nunca desalinha do que a validação (schemas.py) realmente
    aceita."""
    lines = []
    for category, subtypes in SUBTYPES_BY_CATEGORY.items():
        lines.append(f"- {category}: {', '.join(subtypes)}")
    return "\n".join(lines)


# Prompt alinhado à orientação técnica de auditoria hospitalar concorrente
# fornecida pelo usuário 2026-08-24 (ver DECISIONS.md DEC-057 e
# PROJECT_SPEC.md RF-20 a RF-28). Modelo de perguntas: por que o paciente
# ainda está internado, o que impede a progressão, existe ação pendente que
# poderia ter ocorrido e não ocorreu.
SYSTEM_PROMPT = (
    "Você é um assistente de auditoria hospitalar concorrente. Você organiza texto "
    "clínico já extraído de um prontuário. Você NÃO toma decisões clínicas, NÃO "
    "prescreve, e NÃO deve inventar data, horário, resultado ou causalidade que não "
    "esteja no texto fornecido.\n\n"
    "Responda, sobre o paciente HOJE: (1) por que ele ainda precisa estar internado; "
    "(2) o que impede a progressão para alta, transferência ou próxima etapa; "
    "(3) existe ação pendente que poderia ter ocorrido e ainda não ocorreu.\n\n"
    "REGRAS OBRIGATÓRIAS:\n"
    "- NUNCA declare \"internação desnecessária\". Se a necessidade de cuidado "
    "hospitalar agudo não estiver clara nos registros, use necessidade_hospitalar="
    "\"NAO_IDENTIFICADA\" e escreva \"necessidade de nível hospitalar agudo não "
    "identificada nos registros disponíveis\" -- a decisão final é sempre do auditor "
    "humano, nunca sua.\n"
    "- Toda pendência DEVE citar o trecho exato do texto que a sustenta (evidence) e, "
    "se houver, a data/hora dessa evidência (evidence_date). Sem evidência, não "
    "reporte a pendência.\n"
    "- Toda pendência usa uma das categorias fechadas abaixo, com um subtipo da mesma "
    "lista (ou null se nenhum se aplicar bem). Nunca invente categoria fora da lista.\n"
    "- Se a pendência não for uma frase explícita, mas algo que você concluiu "
    "combinando eventos (ex.: exame solicitado e nunca mais mencionado), marque "
    "is_inferred=true e informe confidence (ALTA, MEDIA ou BAIXA). Use linguagem de "
    "incerteza (\"possível pendência\", \"resultado não encontrado nos registros "
    "analisados\") -- nunca afirme causalidade não documentada (nunca \"o hospital "
    "atrasou\").\n"
    "- edd_data só é preenchida se uma data de previsão de alta estiver EXPLICITAMENTE "
    "registrada no texto -- nunca estimada por você.\n"
    "- origem_internacao só é preenchida se a origem (ex.: emergência, eletiva, "
    "transferência de outro serviço/hospital) estiver EXPLICITAMENTE registrada no "
    "texto fornecido -- normalmente só aparece na nota de admissão, então null é a "
    "resposta correta na maioria das evoluções incrementais.\n"
    "- dia_classificacao=VERMELHO exige dia_causa preenchida.\n"
    "- Se não houver informação suficiente para analisar, responda somente com "
    "insufficient_information=true.\n"
    "- Campos que aceitam ausência de valor (edd_data, dia_causa, subcategory, "
    "evidence_date, origin, confidence, flow_status) devem usar o valor JSON null "
    "quando não houver informação -- NUNCA a palavra \"null\" como texto entre "
    "aspas.\n"
    "- edd_status, necessidade_hospitalar, dia_classificacao e category NUNCA "
    "aceitam null nem a palavra \"null\" -- são obrigatórios, sempre um dos valores "
    "fechados listados para cada um.\n"
    "- necessidade_hospitalar, current_status e dia_classificacao descrevem a "
    "situação de HOJE: baseie-os SEMPRE na evolução marcada como \"EVOLUÇÃO MAIS "
    "RECENTE\", nunca em evoluções anteriores. Se a mais recente contradiz uma "
    "anterior (ex.: paciente melhorou, investigação virou ambulatorial, alta já "
    "orientada), a mais recente vence -- as anteriores são só contexto histórico "
    "de como o caso evoluiu, não a situação atual.\n\n"
    "CATEGORIAS E SUBTIPOS VÁLIDOS:\n" + _taxonomy_prompt_block() + "\n\n"
    "Responda SOMENTE com um objeto JSON no formato:\n"
    '{"clinical_context": "2 a 4 linhas: motivo da internação + problema atual + '
    'intervenções já realizadas", '
    '"current_status": "situação clínica atual, resumida", '
    '"necessidade_hospitalar": "SIM|PROVAVELMENTE_SIM|INCERTO|POSSIVELMENTE_NAO|NAO_IDENTIFICADA", '
    '"necessidade_hospitalar_justificativa": "string com evidência", '
    '"objetivo_terapeutico": "o que precisa ser resolvido/atingido antes da alta", '
    '"proximo_passo": "próximo marco necessário para a alta", '
    '"origem_internacao": "string (ex.: emergência, eletiva, transferência) ou null", '
    '"edd_data": "AAAA-MM-DD ou null", '
    '"edd_status": "REGISTRADA|NAO_REGISTRADA|VENCIDA", '
    '"dia_classificacao": "VERDE|VERMELHO", '
    '"dia_causa": "string ou null", '
    '"pending_items": [{"category": "string", "subcategory": "string ou null", '
    '"description": "string", "evidence": "string", "evidence_date": "string ou null", '
    '"origin": "INTERNA|EXTERNA ou null", "is_inferred": false, '
    '"confidence": "ALTA|MEDIA|BAIXA ou null", "flow_status": "string ou null"}], '
    '"insufficient_information": false}'
)


class LLMAnalysisError(Exception):
    """Saída do LLM inválida após todas as tentativas. Nunca inventar resultado."""


class LLMStartupError(Exception):
    """Não foi possível iniciar o processo local do LLM."""


def build_prompt(
    previous_state: dict | None,
    new_notes: list[StructuredNote],
    active_pending_items: list[dict] | None = None,
) -> str:
    """Monta o prompt a partir do incremento -- nunca reenvia o histórico
    completo desnecessariamente (RF-08).

    `active_pending_items` (pendências já registradas, ainda não resolvidas)
    é necessário para o LLM conseguir dizer se uma pendência antiga foi
    resolvida pelas evoluções novas -- sem isso ele só veria o texto livre
    de `current_status`, não a lista estruturada do que está pendente."""
    lines = []
    if previous_state:
        lines.append("CONTEXTO CLÍNICO ANTERIOR:")
        lines.append(previous_state.get("clinical_context") or "(nenhum)")
        lines.append("")
        lines.append("SITUAÇÃO ANTERIOR:")
        lines.append(previous_state.get("current_status") or "(nenhuma)")
        lines.append("")

    if active_pending_items:
        lines.append("PENDÊNCIAS AINDA ABERTAS DA ÚLTIMA ANÁLISE (reavalie se continuam):")
        for item in active_pending_items:
            date_part = f" (desde {item['evidence_date']})" if item.get("evidence_date") else ""
            # subcategory incluído (achado real, DEC-064): sem isso o LLM não
            # tinha como saber o subtipo já registrado, o que aumentava a
            # chance de reportar um subtipo diferente pra mesma pendência
            # real e quebrar o casamento entre execuções.
            subcategory_part = f" [{item['subcategory']}]" if item.get("subcategory") else ""
            lines.append(f"- [{item['category']}]{subcategory_part} {item['description']}{date_part}")
        lines.append("")

    # Achado real 2026-08-24 (DEC-058): o LLM justificou necessidade_hospitalar
    # citando uma evolução ANTIGA, ignorando que a mais recente já indicava alta
    # -- não bastava o texto ter a data, precisava de um marcador explícito.
    most_recent = most_recent_note(new_notes)

    lines.append("EVOLUÇÕES NOVAS DESDE A ÚLTIMA ANÁLISE:")
    if not new_notes:
        lines.append("(nenhuma evolução nova)")
    for note in new_notes:
        marker = " *** EVOLUÇÃO MAIS RECENTE -- priorize esta sobre as demais para necessidade_hospitalar, current_status e dia_classificacao ***" if note is most_recent else ""
        header = f"[{note.timestamp or 'sem data'} - {note.specialty or 'sem especialidade'} - {note.source_type or 'sem tipo'}]{marker}"
        lines.append(header)
        lines.append(note.text)
        lines.append("")

    return "\n".join(lines)


def _extract_json_object(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _build_repair_prompt(base_prompt: str, previous_raw_output: str, errors: list[str]) -> str:
    """Achado real (2026-08-28, resume_fase2.py em pacientes com falha
    persistente): com temperature=0 e o prompt idêntico, reenviar a mesma
    pergunta nas tentativas seguintes reproduz a MESMA saída inválida --
    visto em 10/10 pacientes com o mesmo erro RF-28 (dia_classificacao=
    VERMELHO sem dia_causa) nas 3 tentativas, sempre. Sem mudar o prompt, o
    retry loop não dava nenhuma chance real de sucesso. Aqui a próxima
    tentativa vira um pedido de correção pontual, apontando a saída anterior
    e o erro exato -- é uma entrada genuinamente diferente, não uma repetição."""
    error_list = "\n".join(f"- {err}" for err in errors)
    return (
        f"{base_prompt}\n\n"
        "Sua resposta anterior foi:\n"
        f"{previous_raw_output}\n\n"
        "Essa resposta tem os seguintes problemas de validação:\n"
        f"{error_list}\n\n"
        "Responda novamente com o JSON completo, corrigindo exatamente esses "
        "problemas e mantendo os demais campos coerentes com as notas "
        "fornecidas. Não invente informação nova para resolver o problema -- "
        "se não houver causa registrada no texto para justificar "
        "dia_classificacao=VERMELHO, use dia_classificacao=VERDE em vez disso."
    )


class LocalLLM:
    def __init__(
        self,
        model_path: Path,
        server_path: Path | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        startup_timeout_seconds: int = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        ctx_size: int = DEFAULT_CTX_SIZE,
        server_log_path: Path | None = None,
    ):
        self.model_path = model_path
        self.server_path = server_path
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.startup_timeout_seconds = startup_timeout_seconds
        self.max_tokens = max_tokens
        self.ctx_size = ctx_size
        # Achado real (auditoria de resiliência + validação ao vivo,
        # 2026-08-28): `stdout`/`stderr` iam pra um PIPE que ninguém lia --
        # deadlock garantido quando o buffer (~64KB no Windows) enchesse
        # (causa mecânica provável do "slot preso" do DEC-087). A correção
        # inicial trocou por DEVNULL, mas isso teria escondido o diagnóstico
        # de uma falha real de inicialização (`ggml_backend_cpu_buffer_
        # type_alloc_buffer: failed to allocate buffer`, descoberta rodando
        # o binário manualmente por FORA do app -- ver DEC-092) -- um
        # ARQUIVO real (não um pipe) não tem buffer de tamanho fixo pra
        # travar, então dá pra manter o log sem reintroduzir o risco de
        # deadlock. `None` mantém DEVNULL (usado pelos testes -- não cria
        # arquivo à toa em ambiente de teste).
        self.server_log_path = server_log_path
        self._process: subprocess.Popen | None = None
        self._server_log_file = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def model_version(self) -> str:
        """Identificador rastreável do modelo em uso (RF-30, "registrar
        versão do modelo") -- o nome do arquivo .gguf carregado. Não é um
        nome de versão "bonito" (não temos metadado além do caminho), mas é
        real e suficiente para correlacionar uma análise a um modelo
        específico."""
        return Path(self.model_path).name

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.server_path is None:
            raise LLMStartupError("Caminho do llama-server não configurado.")
        if not Path(self.server_path).exists():
            raise LLMStartupError(f"llama-server não encontrado em {self.server_path}")
        if not Path(self.model_path).exists():
            raise LLMStartupError(f"Modelo GGUF não encontrado em {self.model_path}")

        _kill_orphan_on_port(self.port)

        # Achado de auditoria (RESIL-011/DEC-105, 2026-09-01): diferente do
        # `Popen` logo abaixo (já protegido, DEC-058), abrir o arquivo de
        # log ficava FORA de qualquer try/except -- disco cheio/permissão
        # negada aqui levantava `OSError` cru, quebrando o contrato desta
        # função (só deveria propagar `LLMStartupError`) e escapando do
        # `except ModelDownloadError`/`except LLMStartupError` de
        # `update_flow.py`, vazando a conexão SQLite que só é fechada no
        # `finally` daquele bloco.
        if self.server_log_path is not None:
            try:
                Path(self.server_log_path).parent.mkdir(parents=True, exist_ok=True)
                self._server_log_file = open(self.server_log_path, "a", encoding="utf-8")
            except OSError as exc:
                raise LLMStartupError(f"Não foi possível abrir o log do llama-server: {exc}") from exc
            output_target = self._server_log_file
        else:
            output_target = subprocess.DEVNULL

        try:
            self._process = subprocess.Popen(
                [
                    str(self.server_path),
                    "-m", str(self.model_path),
                    "--host", self.host,
                    "--port", str(self.port),
                    "--ctx-size", str(self.ctx_size),
                    "--parallel", "1",
                ],
                stdout=output_target,
                stderr=output_target,
            )
        except OSError as exc:
            # Achado real (DEC-058): mesmo com os dois `Path.exists()` acima
            # passando, `Popen` pode falhar (permissão, antivírus, WinError 2
            # por outro motivo) -- sem isso, um OSError cru escapava deste
            # módulo, quebrando o contrato de quem chama (só espera
            # LLMStartupError daqui, ver app/ui/errors.py::friendly_message).
            self._close_server_log()
            raise LLMStartupError(f"Não foi possível iniciar o llama-server: {exc}") from exc
        logger.info("llama-server iniciado (pid=%s, %s)", self._process.pid, self.base_url)
        try:
            self._wait_ready()
        except LLMStartupError:
            # Nunca deixar o processo órfão consumindo RAM/CPU se a
            # inicialização falhar -- aconteceu de verdade em teste manual
            # (ver CURRENT_STATE.md) antes deste fix.
            self.stop()
            raise

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise LLMStartupError(
                    f"llama-server encerrou sozinho durante a inicialização (código {self._process.returncode})."
                )
            try:
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=2) as resp:
                    if resp.status == 200:
                        logger.info("llama-server pronto")
                        return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(1)
        raise LLMStartupError("llama-server não respondeu dentro do tempo esperado.")

    def is_healthy(self, timeout: float = 3) -> bool:
        """Disjuntor da Fase 2 (`app/orchestrator.py::run_once`, achado real
        da auditoria de resiliência): confirma que o processo que NÓS
        iniciamos ainda está de pé E respondendo, antes de arriscar até
        `timeout_seconds` (padrão 1800s) numa análise que pode estar fadada
        contra um servidor morto/travado. Checar só `/health` (sem
        `self._process.poll()`) já bastaria pra a maioria dos casos, mas não
        detectaria o próprio processo tendo saído -- os dois juntos cobrem
        mais."""
        if self._process is None or self._process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return False

    def stop(self) -> None:
        """Achado real (auditoria de resiliência 2026-08-28): a versão
        anterior chamava `kill()` no timeout mas nunca conferia se o
        processo realmente morreu depois disso -- `wait()` é o que de fato
        confirma a morte no Windows, e faltava um segundo `wait()` após o
        `kill()`. Resultado observado em produção: log dizia "encerrado" com
        o processo ainda vivo (órfão consumindo RAM, e ocupando a porta pra
        a próxima execução -- ver `_kill_orphan_on_port`). Agora só loga
        sucesso quando a morte é CONFIRMADA, escalando terminate -> kill ->
        `taskkill /F /T` (mata a árvore inteira, não só o PID direto)."""
        if self._process is None:
            self._close_server_log()
            return
        pid = self._process.pid
        self._process.terminate()
        if self._wait_for_exit(10):
            logger.info("llama-server (pid=%d) encerrado", pid)
            self._process = None
            self._close_server_log()
            return

        logger.warning("llama-server (pid=%d) não respondeu a terminate() em 10s -- forçando kill()", pid)
        self._process.kill()
        if self._wait_for_exit(10):
            logger.info("llama-server (pid=%d) encerrado (via kill())", pid)
            self._process = None
            self._close_server_log()
            return

        logger.warning("llama-server (pid=%d) sobreviveu a kill() -- forçando taskkill /F /T", pid)
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        if self._wait_for_exit(5):
            logger.info("llama-server (pid=%d) encerrado (via taskkill)", pid)
        else:
            logger.error(
                "llama-server (pid=%d) pode ter sobrevivido mesmo a taskkill /F /T -- "
                "verificar processo manualmente (tasklist)", pid,
            )
        self._process = None
        self._close_server_log()

    def _close_server_log(self) -> None:
        if self._server_log_file is not None:
            try:
                self._server_log_file.close()
            except OSError:
                pass
            self._server_log_file = None

    def _wait_for_exit(self, timeout_seconds: float) -> bool:
        try:
            self._process.wait(timeout=timeout_seconds)
            return True
        except subprocess.TimeoutExpired:
            return False

    def __enter__(self) -> "LocalLLM":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # --------------------------------------------------------------- inference
    def _call_completion(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": self.max_tokens,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]

    def analyze_patient(
        self,
        previous_state: dict | None,
        new_notes: list[StructuredNote],
        active_pending_items: list[dict] | None = None,
    ) -> dict:
        base_prompt = build_prompt(previous_state, new_notes, active_pending_items)
        current_prompt = base_prompt
        last_errors: list[str] = []

        for attempt in range(1, self.max_retries + 1):
            try:
                raw_content = self._call_completion(current_prompt)
            except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, ValueError) as exc:
                last_errors = [f"falha de comunicação com o LLM: {exc}"]
                logger.warning("Tentativa %d/%d falhou (%s)", attempt, self.max_retries, exc)
                current_prompt = base_prompt
                continue

            parsed = _extract_json_object(raw_content)
            if parsed is None:
                last_errors = ["saída não é um JSON válido"]
                logger.warning("Tentativa %d/%d: saída não é JSON válido", attempt, self.max_retries)
                current_prompt = base_prompt
                continue

            parsed = normalize_null_sentinels(parsed)
            errors = validate_analysis_output(parsed)
            if not errors:
                return parsed

            last_errors = errors
            logger.warning("Tentativa %d/%d: saída inválida (%s)", attempt, self.max_retries, errors)
            current_prompt = _build_repair_prompt(base_prompt, raw_content, errors)

        raise LLMAnalysisError(
            f"LLM_ANALYSIS_ERROR: saída inválida após {self.max_retries} tentativa(s): {last_errors}"
        )
