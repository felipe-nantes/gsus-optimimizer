"""Hash determinístico de evolução para detecção de novidade (INCR-001).

Ver PROJECT_SPEC.md RF-06 / prompt mestre seção 18: mesma combinação de
timestamp + tipo + especialidade + texto normalizado deve sempre produzir o
mesmo hash, para que uma evolução já vista seja identificada como SKIP.
"""
from __future__ import annotations

import hashlib


def compute_note_hash(timestamp: str | None, source_type: str, specialty: str | None, normalized_text: str) -> str:
    parts = [timestamp or "", source_type or "", specialty or "", normalized_text]
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
