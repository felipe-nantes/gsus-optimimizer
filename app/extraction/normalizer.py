"""Normalização de texto extraído do GSUS (EXTRACT-002).

Objetivo: texto visualmente idêntico deve produzir sempre a mesma string
normalizada, para que o hash (INCR-001) seja estável entre execuções.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

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


_BR_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def normalize_llm_datetime(raw: str | None) -> str | None:
    """Data/hora devolvida pelo LLM (ou copiada de uma evolução) -> ISO 8601
    `AAAA-MM-DDTHH:MM:SS`, ou None. Achado real 2026-09-07 (DEC-119): o modelo
    devolve `evidence_date` no formato em que a evolução está escrita
    ("DD/MM/AAAA HH:MM" ou só "DD/MM/AAAA") -- 111 de 204 pendências ativas
    tinham a data assim, e `hours_elapsed_since` (que só lê ISO) mostrava
    "tempo indeterminado" onde havia data. Aceita: ISO (validado), "DD/MM/AAAA
    HH:MM[:SS]" e "DD/MM/AAAA" (meia-noite). Nunca inventa: formato
    desconhecido vira None."""
    if not raw:
        return None
    raw = str(raw).strip()
    iso = normalize_datetime(raw)
    if iso:
        return iso
    match = _BR_DATE_RE.match(raw)
    if match:
        day, month, year = match.groups()
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            return None
        return f"{year}-{month}-{day}T00:00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Regime "ingênuo"/local de `evidence_date` (DEC-065): descarta fuso se vier.
    return parsed.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def normalize_iso_date(raw: str | None) -> str | None:
    """Data (EDD do LLM, "Data de Internação" do censo) -> `AAAA-MM-DD`, ou
    None. Aceita `AAAA-MM-DD`, ISO com hora e "DD/MM/AAAA[ HH:MM]" (formato
    real do GSUS, DEC-099)."""
    if not raw:
        return None
    raw = str(raw).strip()
    match = _BR_DATE_RE.match(raw[:10])
    if match and (len(raw) == 10 or raw[10] in " T"):
        day, month, year = match.groups()
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


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
