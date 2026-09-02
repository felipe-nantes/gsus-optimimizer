"""Utilitário compartilhado pelos scripts de verificação manual
(check_login.py, check_census.py, check_notes.py).

Sem isso, qualquer `logger.warning`/`logger.error` do app (ex.:
`app.gsus.census`) usa o handler padrão do Python, que imprime no
terminal -- e algumas dessas mensagens podem citar identificador de
paciente (prontuário). Aconteceu de verdade (ver DECISIONS.md DEC-016).
Configurar um handler de arquivo aqui garante que essas mensagens vão pro
log local, nunca pro terminal que o usuário poderia colar de volta.
"""
from __future__ import annotations

import logging
from pathlib import Path


def setup_file_logging() -> Path:
    log_path = Path(__file__).resolve().parent.parent / "logs" / "check_scripts.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()  # remove qualquer handler padrão (ex.: stderr)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)

    return log_path
