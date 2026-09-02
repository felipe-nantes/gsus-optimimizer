"""Download do modelo GGUF na primeira execução (BUILD-001).

Decisão do usuário 2026-08-25: o instalador NÃO empacota o modelo (~4,92GB
-- DEC-007) dentro do `.exe`/instalador, que ficaria enorme. Em vez disso,
o app baixa o modelo sozinho, uma vez, na primeira vez que precisar dele
(primeiro "Atualizar agora" após a instalação) -- ver DECISIONS.md.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Mesmo modelo já validado em produção (DEC-007): Meta-Llama-3.1-8B-Instruct,
# quantização Q4_K_M da comunidade (bartowski), licença Llama 3.1 Community
# License. RNF-06: trocar de modelo é só trocar este valor -- nenhum outro
# módulo sabe qual .gguf está carregado.
DEFAULT_MODEL_DOWNLOAD_URL = (
    "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/"
    "resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024  # 1MB
DOWNLOAD_TIMEOUT_SECONDS = 60  # por leitura/conexão -- não é o tempo total do download


class ModelDownloadError(Exception):
    """Não foi possível baixar o modelo. Nunca deixa um arquivo parcial no
    caminho final (RNF-07 -- LocalLLM.start() não pode confundir download
    incompleto com modelo pronto)."""


def ensure_model_downloaded(
    model_path: Path,
    url: str = DEFAULT_MODEL_DOWNLOAD_URL,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Baixa `url` para `model_path` se ainda não existir. Idempotente --
    chamado toda vez antes de `LocalLLM.start()`, mas só baixa de fato uma
    vez (primeira execução); nas seguintes, `model_path.exists()` já é
    verdadeiro e a função retorna imediatamente."""
    model_path = Path(model_path)
    if model_path.exists():
        return

    logger.info("Modelo de IA não encontrado em %s -- iniciando download", model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    # Baixa pra um arquivo temporário e só renomeia pro caminho final no
    # sucesso -- uma queda de rede no meio do download nunca deixa um
    # `model.gguf` parcial/corrompido que `LocalLLM.start()` trataria como
    # "modelo pronto" só porque o arquivo existe.
    tmp_path = model_path.with_name(model_path.name + ".part")

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "GSUSAuditoria"})
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            total_bytes = int(response.headers.get("Content-Length", 0))
            downloaded_bytes = 0
            last_reported_pct = -1
            with open(tmp_path, "wb") as tmp_file:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    tmp_file.write(chunk)
                    downloaded_bytes += len(chunk)
                    if progress and total_bytes:
                        pct = int(downloaded_bytes * 100 / total_bytes)
                        if pct != last_reported_pct:
                            last_reported_pct = pct
                            progress(
                                f"Baixando modelo de IA (primeira execução)... {pct}% "
                                f"({downloaded_bytes // (1024 * 1024)}MB de {total_bytes // (1024 * 1024)}MB)"
                            )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise ModelDownloadError(f"Falha ao baixar o modelo de IA: {exc}") from exc

    tmp_path.replace(model_path)
    logger.info("Modelo de IA baixado com sucesso em %s", model_path)
