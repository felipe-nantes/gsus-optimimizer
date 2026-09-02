"""Parsing de evoluções: texto bruto extraído do GSUS -> campos estruturados
(EXTRACT-001).

Formato do cabeçalho de cada evolução CONFIRMADO pelo usuário em
2026-08-20 (estrutura sanitizada, ver DECISIONS.md DEC-013 -- nenhum nome
real de paciente/profissional aparece neste arquivo nem em fixtures):

    DD/MM/AAAA HH:MM - <profissional> (<cargo>)
    <corpo do texto, com campos rotulados que variam por tipo de bloco>

O texto bruto de um dia inteiro (`app.gsus.records.extract_notes`) pode
conter VÁRIOS desses cabeçalhos em sequência, sem separação confiável por
linha em branco (depende de como o `inner_text()` do DOM concatena os
blocos). Por isso o parser localiza os cabeçalhos pelo próprio padrão
(regex), não por divisão em blocos por linha vazia -- mais robusto.

O nome do profissional NUNCA é guardado em campo estruturado (minimização
de PHI -- não há necessidade clínica de saber quem escreveu, só o quê).
`source_type` recebe o cargo (ex.: "Médico residente"); `specialty` fica
None (o formato real não tem um campo de especialidade separado no
cabeçalho -- ver rules.py, que não depende mais disso).

Um trecho que não bate com o cabeçalho esperado NÃO é descartado: vira um
único bloco com timestamp/source_type = None e o texto integral preservado
(fail-soft), para nunca perder informação clínica por variação de formato.
"""
from __future__ import annotations

import logging
import re

NOTE_HEADER_PATTERN = re.compile(
    r"(?P<timestamp>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})\s*-\s*"
    r"[^\n(]+?\s*\((?P<role>[^)\n]+)\)"
)

logger = logging.getLogger(__name__)


def parse_note_blocks(raw_text: str) -> list[dict]:
    """Localiza cada cabeçalho de evolução no texto bruto e devolve um
    dict por evolução (timestamp_raw/specialty/source_type/text)."""
    if not raw_text or not raw_text.strip():
        return []

    matches = list(NOTE_HEADER_PATTERN.finditer(raw_text))
    if not matches:
        logger.warning("Nenhum cabeçalho de evolução reconhecido; mantendo texto bruto sem metadados.")
        return [{"timestamp_raw": None, "specialty": None, "source_type": None, "text": raw_text.strip()}]

    parsed = []
    for i, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        parsed.append(
            {
                "timestamp_raw": match.group("timestamp"),
                "specialty": None,
                "source_type": match.group("role").strip(),
                "text": raw_text[body_start:body_end].strip(),
            }
        )
    return parsed
