"""EDD-001: previsão de alta RELATIVA ("alta em 48h", "previsão de alta
amanhã") ancorada na data da evolução em que aparece.

Achado real (respostas ao pagador, 2026-09-07): 199 de 200 análises sem EDD
documentada, embora "previsão de alta" apareça em 144 evoluções -- MED/CIR
escrevem a previsão no cabeçalho ("diagnóstico descritivo") como prazo
relativo, e GO como "alta em 48h" para gestantes. O contrato do LLM só
aceita data EXPLÍCITA (nunca estimada pelo modelo), então essas previsões
viravam NAO_REGISTRADA. Aqui a conversão é DETERMINÍSTICA (regex + data da
evolução), nunca um palpite do modelo: só prazos numéricos ("48h", "2 dias",
"24-48h" -> limite superior), "amanhã"/"hoje" e datas explícitas escritas
junto de "previsão de alta". Negação ("sem previsão de alta") e alta de
SETOR ("alta da UTI em 24h") não contam.

O resultado é gravado com `edd_inferida=1` e rotulado no relatório e na
dashboard como vindo de previsão relativa -- o auditor sabe a origem.
Decisão clínica de tratar a previsão relativa como EDD documentada: pedido
do pagador (docx "DUVIDAS SOBRE PRODUTO", itens 5, 6 e 22).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable

from app.analysis.rules import StructuredNote

# "alta" seguida de "da"/"do" é alta de SETOR (UTI, CTI, unidade), não hospitalar.
_ALTA = r"\balta(?!\s+d[ao]\b)(?:\s+hospitalar)?(?:\s+(?:prevista|programada|prov[aá]vel|poss[ií]vel|estimada))?"
_QUALIFIER = r"(?:previs[aã]o\s+de\s+|prov[aá]vel\s+|poss[ií]vel\s+|programa(?:ção|cao)\s+de\s+)?"
_APPROX = r"(?:~\s*|aprox(?:\.|imadamente)?\s*|cerca\s+de\s+|mais\s+ou\s+menos\s+|\+/-\s*)?"
_AMOUNT = r"(\d{1,3})\s*(?:(?:-|–|a|/)\s*(\d{1,3}))?\s*(h|hs|hrs|horas?|d|dias?)\b"

_RELATIVE = re.compile(
    _QUALIFIER + _ALTA + r"\s+(?:em|para|dentro\s+de|daqui\s+a)\s+" + _APPROX + _AMOUNT,
    re.IGNORECASE,
)
# "previsão de alta: 48h" / "previsão de alta 2 dias" (sem preposição)
_RELATIVE_COLON = re.compile(
    r"\bprevis[aã]o\s+de\s+alta(?:\s+hospitalar)?\s*(?::|-|–|em|para|de)?\s*" + _APPROX + _AMOUNT,
    re.IGNORECASE,
)
_TOMORROW = re.compile(_QUALIFIER + _ALTA + r"\s+(?:para\s+|em\s+)?amanh[aã]\b", re.IGNORECASE)
_TODAY = re.compile(_QUALIFIER + _ALTA + r"\s+(?:para\s+)?hoje\b", re.IGNORECASE)
# Cobertura real (2026-09-08, 321 evoluções com menção a alta): a forma mais
# comum de MED/CIR é "previsão de alta: dia 30/08/26" -- dois separadores
# seguidos (":" e "dia") e ano com dois dígitos.
_EXPLICIT = re.compile(
    r"\b(?:previs[aã]o\s+de\s+alta|alta\s+(?:prevista|programada|estimada))(?:\s+hospitalar)?"
    r"(?:\s*(?::|-|–|em|para|no|o|dia|de))*\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b",
    re.IGNORECASE,
)
# "alta em 14/08" sem a palavra "previsão": só vale como previsão quando a data
# ainda não passou -- no passado é relato de história ("alta em 11/07, retornou
# em 13/07"), nunca uma EDD.
_DATED_DISCHARGE = re.compile(
    r"\balta(?!\s+d[ao]\b)(?:\s+hospitalar)?\s+(?:em|para)\s+(?:o\s+)?(?:dia\s+)?"
    r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b",
    re.IGNORECASE,
)
# Sem ano escrito e data já passada: só vira ano seguinte perto da virada do
# ano ("alta 02/01" escrita em 28/12); fora disso a data passada é mantida
# (previsão que venceu), nunca empurrada um ano pra frente.
_YEAR_ROLLOVER_MAX_DAYS = 90
_NEGATION_BEFORE = re.compile(r"\bsem\s+$", re.IGNORECASE)
_NEGATION_WINDOW = 12


def _negated(text: str, start: int) -> bool:
    return bool(_NEGATION_BEFORE.search(text[max(0, start - _NEGATION_WINDOW):start]))


def _parse_anchor(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(timestamp[:10])
    except ValueError:
        return None


def _explicit_date(match: re.Match, anchor: datetime) -> date | None:
    day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
    if year is None:
        candidate_year = anchor.year
    else:
        candidate_year = int(year) + (2000 if len(year) == 2 else 0)
    try:
        parsed = date(candidate_year, month, day)
    except ValueError:
        return None
    if year is None and parsed < anchor.date():
        try:
            rolled = date(candidate_year + 1, month, day)
        except ValueError:
            return None
        if (rolled - anchor.date()).days <= _YEAR_ROLLOVER_MAX_DAYS:
            parsed = rolled
    return parsed


def _relative_date(match: re.Match, anchor: datetime) -> date:
    first, second, unit = match.group(1), match.group(2), match.group(3).lower()
    amount = int(second) if second else int(first)  # faixa "24-48h": limite superior
    if unit.startswith("h"):
        return (anchor + timedelta(hours=amount)).date()
    return (anchor + timedelta(days=amount)).date()


def infer_edd_from_text(text: str | None, anchor: datetime) -> str | None:
    """Data prevista de alta (`AAAA-MM-DD`) escrita nesta evolução, ancorada
    em `anchor` (data/hora da evolução), ou None. Ordem: data explícita de
    previsão > "alta em dd/mm" futura > prazo numérico > amanhã > hoje.
    Nunca inventa: sem prazo, None."""
    if not text:
        return None
    for match in _EXPLICIT.finditer(text):
        if _negated(text, match.start()):
            continue
        parsed = _explicit_date(match, anchor)
        if parsed is not None:
            return parsed.isoformat()
    for match in _DATED_DISCHARGE.finditer(text):
        if _negated(text, match.start()):
            continue
        parsed = _explicit_date(match, anchor)
        if parsed is not None and parsed >= anchor.date():
            return parsed.isoformat()
    for pattern in (_RELATIVE, _RELATIVE_COLON):
        for match in pattern.finditer(text):
            if _negated(text, match.start()):
                continue
            return _relative_date(match, anchor).isoformat()
    for match in _TOMORROW.finditer(text):
        if not _negated(text, match.start()):
            return (anchor.date() + timedelta(days=1)).isoformat()
    for match in _TODAY.finditer(text):
        if not _negated(text, match.start()):
            return anchor.date().isoformat()
    return None


def infer_edd_from_notes(notes: Iterable[StructuredNote]) -> str | None:
    """Percorre as evoluções da MAIS RECENTE para a mais antiga e devolve a
    primeira previsão encontrada -- a previsão mais nova vence (a equipe
    reavalia o prazo a cada dia). Evolução sem data não serve de âncora."""
    dated = [note for note in notes if note.timestamp]
    for note in sorted(dated, key=lambda n: n.timestamp or "", reverse=True):
        anchor = _parse_anchor(note.timestamp)
        if anchor is None:
            continue
        inferred = infer_edd_from_text(note.text, anchor)
        if inferred:
            return inferred
    return None
