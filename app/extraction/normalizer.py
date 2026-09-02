"""Normalização de texto extraído do GSUS (EXTRACT-002).

Objetivo: texto visualmente idêntico deve produzir sempre a mesma string
normalizada, para que o hash (INCR-001) seja estável entre execuções.
"""
from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RUN = re.compile(r"[ \t ]+")
_BLANK_LINES_RUN = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RUN.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINES_RUN.sub("\n\n", text)
    return text.strip()


def normalize_datetime(raw: str | None) -> str | None:
    """Normaliza 'DD/MM/AAAA HH:MM' (formato comum em sistemas hospitalares
    brasileiros) para ISO 8601. Retorna None se não reconhecer o formato --
    nunca inventa uma data (fail-soft: mantém o dado bruto acessível ao
    chamador, apenas não populamos evidence_date com um palpite)."""
    if not raw:
        return None
    raw = raw.strip()
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})[ T](\d{2}):(\d{2})(:(\d{2}))?$", raw)
    if not match:
        return None
    day, month, year, hour, minute = match.group(1), match.group(2), match.group(3), match.group(4), match.group(5)
    second = match.group(7) or "00"
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}"
