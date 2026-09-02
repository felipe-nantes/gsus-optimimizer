"""Regras determinísticas (RULES-001).

Operam sobre notas já estruturadas e normalizadas (não sobre DOM/HTML).
Rodam SEMPRE antes do LLM (prompt mestre seção 20/21): o que uma regra aqui
resolve, o LLM não precisa reinterpretar.

IMPORTANTE (RF-07 / prompt mestre seção 20): as palavras-chave abaixo são
CANDIDATAS. Nenhum tempo clínico arbitrário é usado -- as regras comparam
apenas ordem cronológica e presença/ausência textual, nunca "X horas depois
disso é uma pendência". Antes de uso em produção real, a lista de palavras-
chave deve ser revisada e aprovada por alguém com critério clínico (ver
PROJECT_SPEC.md RF-07).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.analysis.taxonomy import CATEGORY_DIAGNOSTICO, CATEGORY_INTERCONSULTA, default_origin

# Mantidos por compatibilidade com quem importava os nomes antigos --
# apontam para a taxonomia fechada (RF-22, DEC-057): "EXAME" passou a se
# chamar DIAGNOSTICO; "INTERCONSULTA" já tinha o mesmo nome.
CATEGORY_EXAM = CATEGORY_DIAGNOSTICO
CATEGORY_CONSULT = CATEGORY_INTERCONSULTA

SUBTYPE_EXAM_PENDING = "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL"
SUBTYPE_CONSULT_REQUESTED = "SOLICITADA"

REQUEST_VERB = r"solicitad[ao]|pedid[ao]"
COMPLETION_VERB = r"realizad[ao]|resultado\s+d[eoa]|laudo"

EXAM_KEYWORDS = [
    "tomografia",
    "ressonância magnética",
    "ressonancia magnetica",
    "raio-x",
    "raio x",
    "ultrassom",
    "ecocardiograma",
    "endoscopia",
    "colonoscopia",
]

CONSULT_TRIGGER = re.compile(
    rf"(?:{REQUEST_VERB})\s+(?:a\s+)?avalia[cç][aã]o\s+(?:d[ao]\s+)?(?P<specialty>[^.,;\n]+)",
    re.IGNORECASE,
)
CONSULT_COMPLETION_MARKERS = ("parecer",)

# Achado real (revisão de conformidade, DEC-064): "aguardando laudo"/"aguarda
# parecer" citam a palavra que marca conclusão ("laudo", "parecer") mas
# significam exatamente o oposto -- ainda NÃO chegou. Sem essa exclusão,
# apply_exam_rule/apply_consult_rule resolviam pendências reais só porque a
# palavra apareceu, mesmo précedida por "aguard-" na mesma frase (a
# orientação técnica, seção 4, trata "exame realizado, aguardando laudo"
# como estado PENDENTE distinto, nunca concluído).
WAITING_MARKER = "aguard"


def _has_unwaited_marker(text_lower: str, marker_pattern: str) -> bool:
    """True só se alguma frase da nota citar `marker_pattern` SEM também
    conter `WAITING_MARKER` -- nível de frase, não de nota inteira, pra não
    deixar uma menção genuinamente concluída em outra frase ser mascarada
    por um "aguarda" sobre outro assunto na mesma nota."""
    for sentence in re.split(r"[.\n;]", text_lower):
        if re.search(marker_pattern, sentence) and WAITING_MARKER not in sentence:
            return True
    return False


@dataclass
class StructuredNote:
    timestamp: str | None
    specialty: str | None
    source_type: str | None
    text: str


def most_recent_note(notes: list[StructuredNote]) -> StructuredNote | None:
    """Nota com o maior `timestamp` (comparação de string ISO 8601, sempre
    válida pois `normalize_datetime` produz esse formato) -- por VALOR,
    nunca por posição na lista, já que a ordem de extração do GSUS não é
    garantida por este módulo (achado real, DEC-058). None se nenhuma nota
    tiver timestamp reconhecido."""
    most_recent: StructuredNote | None = None
    for note in notes:
        if note.timestamp and (most_recent is None or note.timestamp > most_recent.timestamp):
            most_recent = note
    return most_recent


@dataclass
class RuleFinding:
    category: str
    description: str
    evidence: str
    evidence_date: str | None
    subcategory: str | None = None
    resolved: bool = False
    resolves_description: str | None = None

    @property
    def origin(self) -> str:
        """Regra determinística nunca infere -- sempre explícita (RF-25)."""
        return default_origin(self.category)


def _find_exam_keyword(text_lower: str) -> str | None:
    for keyword in EXAM_KEYWORDS:
        if keyword in text_lower:
            return keyword
    return None


def apply_exam_rule(notes: list[StructuredNote]) -> list[RuleFinding]:
    """Exame solicitado + nenhum resultado posterior = pendência candidata
    (prompt mestre seção 20, primeiro exemplo)."""
    findings: list[RuleFinding] = []
    open_exams: dict[str, RuleFinding] = {}

    for note in notes:
        text_lower = note.text.lower()
        keyword = _find_exam_keyword(text_lower)
        if keyword is None:
            continue

        requested = re.search(REQUEST_VERB, text_lower) is not None
        completed = _has_unwaited_marker(text_lower, COMPLETION_VERB)

        if completed and keyword in open_exams:
            open_exams.pop(keyword).resolved = True
            continue

        if requested and keyword not in open_exams:
            finding = RuleFinding(
                category=CATEGORY_EXAM,
                subcategory=SUBTYPE_EXAM_PENDING,
                description=f"Aguarda realização de {keyword}",
                evidence=note.text,
                evidence_date=note.timestamp,
            )
            open_exams[keyword] = finding
            findings.append(finding)

    # Devolve TAMBÉM os achados resolvidos (`resolved=True`), não só os
    # ainda ativos -- achado real (revisão de conformidade, DEC-064): quem
    # chama (orchestrator.py) precisa distinguir "resolvido de verdade
    # dentro desta janela" (ambos os sinais -- solicitação e conclusão --
    # presentes nas mesmas notas) de "simplesmente não apareceu nesta
    # janela" (a nota de origem pode ter saído do recorte incremental do
    # GSUS real sem o exame ter sido de fato concluído). Filtrar aqui
    # apagaria essa distinção -- ver `apply_all_rules`.
    return findings


def apply_consult_rule(notes: list[StructuredNote]) -> list[RuleFinding]:
    """Interconsulta solicitada + nenhum parecer posterior = pendência
    candidata (prompt mestre seção 20, segundo exemplo).

    Resolução por conteúdo do texto (menção à especialidade + marcador de
    parecer na mesma nota), não por `note.specialty` -- o cabeçalho real do
    GSUS não tem um campo de especialidade estruturado, só cargo do
    profissional (ver DECISIONS.md DEC-013 / app/extraction/parser.py)."""
    findings: list[RuleFinding] = []
    open_consults: dict[str, RuleFinding] = {}

    for note in notes:
        text_lower = note.text.lower()

        consult_marker_pattern = "|".join(re.escape(marker) for marker in CONSULT_COMPLETION_MARKERS)
        for specialty in list(open_consults):
            mentions_specialty = specialty in text_lower
            has_opinion_marker = _has_unwaited_marker(text_lower, consult_marker_pattern)
            if mentions_specialty and has_opinion_marker:
                open_consults.pop(specialty).resolved = True

        match = CONSULT_TRIGGER.search(text_lower)
        if match:
            specialty = match.group("specialty").strip()
            if specialty not in open_consults:
                finding = RuleFinding(
                    category=CATEGORY_CONSULT,
                    subcategory=SUBTYPE_CONSULT_REQUESTED,
                    description=f"Aguarda avaliação de {specialty}",
                    evidence=note.text,
                    evidence_date=note.timestamp,
                )
                open_consults[specialty] = finding
                findings.append(finding)

    # Devolve também os resolvidos -- mesmo motivo de apply_exam_rule.
    return findings


def apply_all_rules(notes: list[StructuredNote]) -> list[RuleFinding]:
    """Inclui achados com `resolved=True` -- quem chama (orchestrator.py)
    decide o que fazer com cada um (criar/reconhecer pendência ativa, ou
    marcar uma pendência já registrada como resolvida). Nunca filtrar aqui:
    a ausência de um achado nesta lista não significa resolução, só que
    nenhum sinal (aberto OU fechado) apareceu nas notas desta janela."""
    return apply_exam_rule(notes) + apply_consult_rule(notes)
