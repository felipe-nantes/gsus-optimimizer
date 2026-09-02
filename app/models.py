"""Estruturas de dados compartilhadas entre gsus/, extraction/ e storage/."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Patient:
    record_number: str
    bed: str
    unit: str
    admission_date: str | None = None


@dataclass
class Note:
    patient_id: str
    source_type: str
    specialty: str | None
    timestamp: str | None
    text: str
    text_hash: str = ""
