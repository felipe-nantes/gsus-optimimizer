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

PEND-001 (DEC-129): as regras só CRIAM pendência; quem fecha é o detector
(`specialty_evaluation.detect_resolutions`), que olha a internação inteira
e exige prova posterior ao pedido. Antes a regra fechava interconsulta por
"especialidade citada + palavra parecer" na mesma janela -- "Reitero
solicitação de parecer da cardiologia" fechava o próprio pedido -- e exame
por "resultado de" em QUALQUER frase da nota ("Aguarda TC. Resultado de
hemograma normal" fechava a TC). Agora tudo é por FRASE, com `alvo`
normalizado (especialidade, exame), e a evidência gravada é a frase do
pedido, não a nota inteira. A revisão adversarial da Fase 3 apertou o resto:
cada exame PEDIDO na frase (não o primeiro citado), nada de "conforme
parecer da X"/"não solicito", e data do pedido só a ligada ao verbo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.analysis import specialty_evaluation as se
from app.analysis.specialties import fold
from app.analysis.taxonomy import CATEGORY_DIAGNOSTICO, CATEGORY_INTERCONSULTA, default_origin

# Mantidos por compatibilidade com quem importava os nomes antigos --
# apontam para a taxonomia fechada (RF-22, DEC-057): "EXAME" passou a se
# chamar DIAGNOSTICO; "INTERCONSULTA" já tinha o mesmo nome.
CATEGORY_EXAM = CATEGORY_DIAGNOSTICO
CATEGORY_CONSULT = CATEGORY_INTERCONSULTA

SUBTYPE_EXAM_PENDING = "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL"
SUBTYPE_CONSULT_REQUESTED = "SOLICITADA"

# Infinitivo e presente entram (2ª revisão: listas de conduta usam "CD:
# solicitar TC de abdome; solicitar parecer da cardiologia"); a especialidade
# pedindo também ("Nefrologia solicitou USG de rins" -- 3ª revisão).
REQUEST_VERB = re.compile(
    r"\b(?:solicitad[ao]s?|solicito|solicitamos|solicitei|solicitar|solicita|solicitando|solicitarei|solicitaremos"
    r"|solicitou|solicitaram|pedid[ao]s?|peco|pedimos|pediu|pediram|reitero|reiterad[ao]s?)\b"
)
# Particípio DEPOIS do pedido: "Parecer da nefrologia solicitado", "TC de
# crânio solicitada".
_PASSIVE_REQUEST = re.compile(r"^[^,;:]{0,40}?\b(?:solicitad[ao]s?|pedid[ao]s?|reiterad[ao]s?)\b")
# "Não solicito TC", "Não foi solicitada TC", "Sem pedido de TC" -- negação
# do PRÓPRIO verbo ("Paciente não solicita alta, solicitada TC" pede a TC).
_NEGATION_BEFORE_VERB = re.compile(r"\b(?:nao|sem)\s+(?:(?:foi|foram|sera|serao|e|ha|houve)\s+)?$")
_NEGATED_NOUN_REQUEST = re.compile(r"\b(?:nao|sem)\s+(?:(?:ha|houve)\s+)?pedido\b")
# Infinitivo/futuro ("solicitar", "solicitarei") é PLANO: condicionado,
# ponderado ou negado não é pedido ("Se piora, solicitar TC de crânio", "Sem
# necessidade de solicitar TC", "Considerar solicitar ECO" -- 4ª revisão).
_PLANNED_VERB = re.compile(r"^(?:solicitar|solicitarei|solicitaremos|pedir)$")
_PLAN_HEDGE_BEFORE = re.compile(
    r"\bse\b|\bcaso\b|\bna\s+ausencia\s+de\b|\bconsiderar\b|\bavaliar\s+(?:a\s+)?(?:necessidade|indicacao|possibilidade)\b"
    r"|\bdiscutir\b|\bponderar\b|\bnecessidade\s+de\b"
    r"|\bindicacao\s+de\b|\bdesnecessari\w*|\b(?:nao|sem)\s+(?:\w+\s+){0,3}(?:de\s+)?$|\bprogramar\b|\bpossivel\w*"
)
# Quem cita uma avaliação que JÁ aconteceu não está pedindo: "Solicitado ECO
# conforme avaliação da cardiologia".
_REPORTED_HEAD_BEFORE = se.REPORTED_HEAD_BEFORE

# Exames de imagem/endoscopia que a regra acompanha -- laboratório de rotina
# ("solicitado hemograma") fica com a IA, senão vira uma pendência por dia.
RULE_EXAMS = {
    "TOMOGRAFIA", "ANGIOTOMOGRAFIA", "ANGIORRESSONÂNCIA", "RESSONÂNCIA", "RAIO-X", "ULTRASSOM", "DOPPLER",
    "DOPPLER ARTERIAL", "DOPPLER VENOSO", "ECOCARDIOGRAMA", "ECOCARDIOGRAMA TRANSESOFÁGICO", "ENDOSCOPIA",
    "COLONOSCOPIA",
}

# Pedido que já traz resultado/realização do PRÓPRIO exame não é um pedido
# em aberto ("Solicitado ECO, realizado ontem"). Só o trecho do exame pedido
# conta -- a justificativa que cita OUTRO exame ("para elucidar lesão
# evidenciada na TC") não (2ª revisão adversarial).
_ALREADY_DONE = re.compile(
    r"\brealizad[oa]s?\b|\blaudo\b|\bresultado\b|\bsem\s+alteraco\w*|\bevidenci\w*|\bmostr(?:a|ou)\b"
    r"|\bcom\s+(?:fe|fracao)\b|\bcompativel\b|\bveio\b|\bnormal\b"
)
_NOT_DONE_YET_BEFORE = re.compile(
    r"\b(?:nao|ainda\s+nao|a\s+ser|sera|serao|devera|deve|aguard\w*|apos|se|caso|sem|depender\s+d[oea])\s+"
    r"(?:\w+\s+){0,2}$"
)
_DONE_OPENS_CLAUSE = re.compile(
    r"^\s*(?:(?:que|o\s+qual|a\s+qual|sendo|com|e|ja)\s+)?(?:foi\s+|foram\s+)?(?:" + _ALREADY_DONE.pattern + r")"
)
# Onde termina o trecho do exame pedido: a justificativa.
_PURPOSE = re.compile(r"\b(?:para|p/|pra|devido|por|pois|que|apos|conforme|vista|visto|segundo)\b")
# Entre o verbo e o exame pedido: itens de uma lista ("Solicitado hemograma,
# amilase e tomografia de abdome") -- nunca uma justificativa ("parecer da
# vascular devido a TVP vista em doppler" não pede doppler), nem o resultado
# de um exame ("solicita resultado da TC").
_EXAM_LIST_STOP = re.compile(
    r"\b(?:devido|por|pois|para|p/|pra|que|vist[oa]|evidenciad\w*|mostr\w*|apos|conforme|segundo"
    r"|realizad\w*|resultados?|laudos?|com\s+(?:achado|resultado|laudo))\b"
)
_MAX_WORDS_PER_LIST_ITEM = 5
# Linha que abre uma lista de pedidos ("Exames solicitados:", "Solicito:"):
# os itens de lista abaixo dela são pedidos (3ª revisão).
_REQUEST_LIST_HEADER = re.compile(
    r"^\s*(?:exames?\s+)?(?:solicitad[oa]s?|solicito|solicitamos|solicitar|pedid[oa]s?|peco)\s*:?\s*$"
    r"|^\s*exames?\s+(?:solicitad[oa]s?|a\s+solicitar|pedid[oa]s?)\s*:?\s*$"
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d{1,2}\s*[.)-])\s*")

# Pedido que já relata a resposta ("solicitado parecer da nefro, avaliado
# ontem") é histórico, não pedido em aberto. Só logo DEPOIS da especialidade
# pedida, até a vírgula -- "ainda não avaliado", "aguarda ser avaliado",
# "paciente avaliado", "avaliado pela outra especialidade" não contam (3ª
# revisão).
_CONSULT_ALREADY_ANSWERED = re.compile(
    r"\b(?:avaliad[oa]s?|respondid[oa]s?|avaliou|respondeu|vist[oa]\s+pel[ao])\b"
    r"|\brealizad[oa]s?\s+(?:a\s+|o\s+)?(?:avaliacao|parecer|interconsulta)\b"
)
_NOT_YET_BEFORE = re.compile(
    r"\b(?:nao|ainda\s+nao|ser|a\s+ser|aguard\w*|serem|sendo|sera|serao)\s+(?:\w+\s+){0,2}$"
)
_PATIENT_EVALUATED = re.compile(r"\b(?:paciente|pcte|pct)\s+(?:ja\s+)?$")
# "Parecer da cardiologia: sem indicação de cateterismo, solicitado ECO" --
# é a resposta relatada, e o pedido é do ECO (3ª revisão).
_HEAD_REPORTS_ANSWER = re.compile(r"^\s*[:\-–]")

# Data ligada ao PEDIDO: "solicitado parecer em 20/08", "pedida dia 18/08".
# Só a data que o verbo governa direto (no máximo o nome do pedido no meio,
# nada de vírgula) -- "solicitado parecer por oligúria desde 18/08" é a data
# do sintoma, não do pedido (2ª revisão adversarial).
_REQUEST_DATE = re.compile(
    r"\b(?:solicitad[oa]s?|pedid[oa]s?|solicitei|reiterad[oa]s?)\s+"
    r"(?:(?:o\s+|a\s+)?(?:parecer|avaliacao|interconsulta|ic|exame|tc|tomografia|eco|ecocardiograma|usg|rx)"
    r"(?:\s+(?!por\b|devido\b|desde\b|apos\b|com\b|pois\b|para\b)[a-z]+){0,3}?\s+)?"
    r"(?:em|dia|no\s+dia)\s+(\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?)"
)

_SENTENCE_SPLIT = re.compile(r"(?<!\b[A-Za-z])\.(?!\d)|[;\n]+")


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
    alvo: str | None = None
    # Todas as vezes que o pedido aparece na janela, (data, frase), da mais
    # antiga pra mais nova -- `evidence`/`evidence_date` são a primeira. Quem
    # chama usa as seguintes quando a primeira já foi atendida: "reitero
    # parecer da nefrologia" DEPOIS da avaliação é um pedido novo.
    occurrences: list[tuple[str | None, str]] = field(default_factory=list)

    @property
    def origin(self) -> str:
        """Regra determinística nunca infere -- sempre explícita (RF-25)."""
        return default_origin(self.category)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def _note_sentences(text: str) -> list[tuple[str, str]]:
    """(texto para achar o pedido, frase original) de cada frase da evolução.
    Os itens debaixo de "Exames solicitados:" ganham o verbo do cabeçalho só
    para achar o pedido ("- TC de crânio" -> "solicitado TC de crânio"); a
    evidência e a checagem de cópia usam a frase como está na evolução (4ª
    revisão: a frase sintética nunca era achada e a lista copiada recriava o
    pedido já atendido)."""
    result: list[tuple[str, str]] = []
    in_request_list = False
    for line in (text or "").split("\n"):
        folded_line = fold(line)
        if not folded_line.strip():
            in_request_list = False
            continue
        if _REQUEST_LIST_HEADER.match(folded_line):
            in_request_list = True
            continue
        if in_request_list and _LIST_ITEM.match(line):
            result.extend((f"solicitado {sentence}", sentence)
                          for sentence in _sentences(_LIST_ITEM.sub("", line, count=1)))
            continue
        in_request_list = False
        result.extend((sentence, sentence) for sentence in _sentences(line))
    return result


def _request_verbs(folded: str) -> list[re.Match]:
    """Verbos de pedido que não estão negados ("não solicita alta" não pede)
    nem são plano condicionado/negado no infinitivo ("se piora, solicitar")."""
    verbs = []
    for verb in REQUEST_VERB.finditer(folded):
        if _NEGATION_BEFORE_VERB.search(folded[max(0, verb.start() - 25):verb.start()]):
            continue
        if _PLANNED_VERB.match(verb.group(0)):
            before = folded[max(0, verb.start() - 60):verb.start()]
            before = re.split(r"[.;:\n]", before)[-1]  # "CD: solicitar TC" é ordem
            if _PLAN_HEDGE_BEFORE.search(before):
                continue
        verbs.append(verb)
    return verbs


def _request_date(folded_sentence: str, note_timestamp: str | None, admission_cutoff: str | None) -> str | None:
    """Data do PEDIDO: a da evolução -- ou a data que a frase liga ao verbo
    ("solicitado parecer em 20/08", escrito dias depois), sem hora. Nunca
    uma data futura (agendamento), nunca uma fração clínica ("força 4/5",
    "PA 18/10"), nunca antes da internação atual (a revisão mostrou pedidos
    novos "datados" do ano anterior, fechados com resultado antigo ou
    apagados pela poda do DEC-119 toda noite). Frase com mais de um pedido
    fica com a data da evolução: a data de um não é a do outro ("Reitero
    parecer da cardiologia solicitado em 15/08 e solicito parecer da
    neurologia" -- 3ª revisão)."""
    if not note_timestamp:
        return note_timestamp
    try:
        note_day = date.fromisoformat(note_timestamp[:10])
    except ValueError:
        return note_timestamp
    match = _REQUEST_DATE.search(folded_sentence)
    if match is None:
        return note_timestamp
    if len(_request_verbs(folded_sentence)) > 1 or (
            len(requested_exams(folded_sentence)) + len(consult_targets_requested(folded_sentence))) > 1:
        return note_timestamp
    cited = se.cited_dates(match.group(1), note_day)
    if not cited or cited[0] >= note_day:
        return note_timestamp
    if admission_cutoff and cited[0].isoformat() < admission_cutoff:
        return note_timestamp
    return f"{cited[0].isoformat()}T00:00:00"


def _add_occurrence(findings: dict[str, RuleFinding], target: str, make, sentence: str, request_date: str | None):
    finding = findings.get(target)
    if finding is None:
        finding = findings[target] = make()
    finding.occurrences.append((request_date, sentence))


def _finalize(findings: dict[str, RuleFinding]) -> list[RuleFinding]:
    for finding in findings.values():
        finding.occurrences.sort(key=lambda occurrence: occurrence[0] or "")
        finding.evidence_date, finding.evidence = finding.occurrences[0]
    return list(findings.values())


def _chronological(notes: list[StructuredNote]) -> list[StructuredNote]:
    """A extração do GSUS entrega do dia mais novo pro mais antigo (achado do
    LLM-006) -- a PRIMEIRA evidência de um pedido tem de ser a mais antiga.
    Sem data vai pro começo (não dá pra saber; ordem estável)."""
    return sorted(notes, key=lambda n: n.timestamp or "")


def _already_done(folded: str, end: int, mentions) -> bool:
    """O exame pedido já aparece feito/com resultado logo depois dele --
    inclusive depois da justificativa cortada ("Solicitado ECO que mostrou
    FE de 30%", "solicitada TC por dispneia, evidenciando TEP" -- 3ª
    revisão). Para na vírgula/ponto e vírgula e no próximo exame citado."""
    next_exam = next((start for start, _stop, _exam, _region in mentions if start >= end), None)
    segment = folded[end:next_exam if next_exam is not None else len(folded)]
    cut = re.search(r"[;]", segment)
    if cut:
        segment = segment[:cut.start()]
    own, comma, after_comma = segment.partition(",")
    reaches_next_exam = next_exam is not None and not comma and not cut

    def done_in_own() -> bool:
        for match in _ALREADY_DONE.finditer(own):
            # "lesão evidenciada NA TC": o resultado é do exame citado logo depois.
            if reaches_next_exam and len(own) - match.end() <= 12:
                continue
            # "Solicitada TC de crânio ainda não realizada": não foi feito.
            if _NOT_DONE_YET_BEFORE.search(own[max(0, match.start() - 25):match.start()]):
                continue
            return True
        return False

    following = after_comma.split(",")[0]
    # "Solicitado ECO de controle, ÚLTIMO sem alterações": a oração seguinte
    # fala do exame anterior, não deste. E só vale o resultado que ABRE a
    # oração seguinte (", realizado ontem", ", evidenciando TEP") -- 4ª
    # revisão: ", aguardo resultado", ", a ser realizado", ", ainda não
    # realizada", ", ECG normal" derrubavam o pedido.
    about_previous = re.search(r"\b(?:ultim[oa]|anterior|previ[oa])\b", following) is not None
    return done_in_own() or (not about_previous and _DONE_OPENS_CLAUSE.match(following) is not None)


def _is_list_before(text: str) -> bool:
    """Entre o verbo e o exame só há itens de lista ("hemograma, amilase e",
    por mais longa que seja a lista de laboratório)."""
    if _EXAM_LIST_STOP.search(text):
        return False
    items = [item for item in re.split(r",|\be\b", text) if item.strip()]
    return all(len(re.findall(r"[a-z0-9]+", item)) <= _MAX_WORDS_PER_LIST_ITEM for item in items)


# Frase colada sem pontuação, com centenas de verbos e exames, ficava cúbica
# (5ª revisão: 85 s a 9.600 caracteres). O verbo mais próximo vem primeiro, e
# a lista de laboratório mais longa cabe em 300 caracteres.
_MAX_EXAM_MENTIONS = 40
_MAX_VERB_TO_EXAM_CHARS = 300


def _governing_verb(folded: str, verbs: list[re.Match], start: int) -> re.Match | None:
    for verb in reversed(verbs):
        if verb.end() > start:
            continue
        if start - verb.end() > _MAX_VERB_TO_EXAM_CHARS:
            return None
        if _is_list_before(folded[verb.end():start]):
            return verb
    return None


def requested_exams(sentence: str) -> list[str]:
    """Exames PEDIDOS numa frase: depois do verbo ("Solicito TC de crânio e
    USG de abdome" -> os dois; "Solicitado hemograma, amilase e TC de
    abdome" -> a TC) ou com o particípio logo depois ("TC de crânio
    solicitada"). Um exame citado com resultado ("TC com AVC isquêmico,
    solicito RNM", "Solicitado ECO que mostrou FE 30%"), na justificativa
    ("para elucidar lesão vista na TC"), já realizado ("Realizada TC
    solicitada pela neurologia") ou em frase de histórico é contexto, não
    pedido."""
    folded = fold(sentence)
    if _NEGATED_NOUN_REQUEST.search(folded):
        return []
    verbs = _request_verbs(folded)
    if not verbs:
        return []
    mentions = se.exam_mentions(folded)[:_MAX_EXAM_MENTIONS]
    found: list[str] = []
    for index, (start, end, exam, region) in enumerate(mentions):
        if exam not in RULE_EXAMS:
            continue
        if exam in ("ULTRASSOM", "ECOCARDIOGRAMA") and index + 1 < len(mentions) \
                and mentions[index + 1][2].startswith("DOPPLER") \
                and re.fullmatch(r"\s*(?:com\s+)?", folded[end:mentions[index + 1][0]]):
            continue  # "USG doppler venoso" é o doppler, não um ultrassom sem região (4ª revisão)
        if _already_done(folded, end, mentions):
            continue
        if re.search(r"\brealizad[oa]s?\s+(?:\w+\s+){0,2}$", folded[max(0, start - 30):start]):
            continue  # "Realizada TC de crânio solicitada pela neurologia"
        stop = mentions[index + 1][0] if index + 1 < len(mentions) else len(folded)
        if se.history_adjacent(folded[start:min(stop, end + 30)]):
            continue  # "TC de crânio anterior", "hemoculturas prévias"
        governing = _governing_verb(folded, verbs, start)
        passive = _PASSIVE_REQUEST.match(folded[end:])
        if governing is None and passive is None:
            continue
        # Histórico só quando o marcador é do PRÓPRIO pedido: antes do verbo,
        # na mesma oração ("No PS foi solicitada TC"), ou logo depois do
        # particípio ("TC solicitada na admissão"). Marcador na justificativa
        # ("por febre há 2 dias", "paciente admitido com rebaixamento",
        # "para comparar com a TC anterior") não apaga o pedido (4ª revisão).
        if governing is not None:
            clause_start = max(folded.rfind(mark, 0, governing.start()) for mark in (",", ";", ":")) + 1
            if se.history_strong(folded[clause_start:governing.start()]):
                continue
        elif se.history_strong(folded[end + passive.start():end + passive.end() + 25]):
            continue
        # Um item por região listada: "Solicito TC de crânio, tórax e abdome"
        # são três exames (3ª revisão: só o crânio virava pendência).
        regions = se.listed_regions(folded, end, stop, exam) or ([region] if region else [])
        for target in ([f"{exam} DE {each}" for each in regions] or [exam]):
            if target not in found:
                found.append(target)
    # O mesmo exame sem região na mesma frase de um com região é ele mesmo
    # ("Solicito novo RX de tórax, RX de ontem com velamento").
    return [target for target in found if " DE " in target or not any(other.startswith(f"{target} DE ")
                                                                       for other in found)]


def apply_exam_rule(notes: list[StructuredNote], admission_cutoff: str | None = None) -> list[RuleFinding]:
    """Frase com pedido + exame de imagem/endoscopia = pendência candidata
    (prompt mestre seção 20, primeiro exemplo). Uma por alvo ("TOMOGRAFIA
    DE ABDOME"), com a evidência do pedido mais antigo da janela."""
    findings: dict[str, RuleFinding] = {}
    for note in _chronological(notes):
        for sentence, original in _note_sentences(note.text):
            for target in requested_exams(sentence):
                _add_occurrence(findings, target, lambda target=target: RuleFinding(
                    category=CATEGORY_EXAM,
                    subcategory=SUBTYPE_EXAM_PENDING,
                    description=f"Aguarda realização de {target.lower()}",
                    evidence="", evidence_date=None, alvo=target,
                ), original, _request_date(fold(sentence), note.timestamp, admission_cutoff))
    return _finalize(findings)


def _answered_right_after(folded: str, list_end: int) -> bool:
    """Logo depois da especialidade pedida, até a vírgula, já diz que ela
    respondeu ("solicitado parecer da nefro, avaliado ontem" é histórico).
    "Ainda não avaliado", "aguarda ser avaliado", "paciente avaliado" e
    "avaliado pela OUTRA especialidade" não contam."""
    segment = folded[list_end:]
    segment = re.split(r"[;:]", segment, maxsplit=1)[0]
    parts = segment.split(",")
    window = ",".join(parts[:2])
    for match in _CONSULT_ALREADY_ANSWERED.finditer(window):
        before = window[max(0, match.start() - 30):match.start()]
        if _NOT_YET_BEFORE.search(before) or _PATIENT_EVALUATED.search(before):
            continue
        after = window[match.end():match.end() + 40]
        other = re.match(r"\s*pel[ao]s?\s+(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?", after)
        if other and specialties_after(after[other.end():]):
            continue  # "avaliado pela cardiologia" -- outra especialidade
        return True
    return False


def specialties_after(text: str) -> list[str]:
    return se.specialties_listed_after_head(text)


def consult_targets_requested(sentence: str) -> list[str]:
    """Especialidades PEDIDAS numa frase: verbo de pedido que GOVERNA a
    cabeça ("Solicito parecer da nefrologia"; antes dela, sem vírgula ou
    dois-pontos no meio) ou o particípio logo depois da especialidade
    ("Parecer da nefrologia solicitado"). "Parecer da cardiologia: sem
    indicação de cateterismo, solicitado ECO" é a resposta relatada e o
    pedido do ECO, não um pedido de parecer (3ª revisão)."""
    folded = fold(sentence)
    verbs = _request_verbs(folded)
    if _NEGATED_NOUN_REQUEST.search(folded) or not verbs:
        return []
    found: list[str] = []
    for head in se.CONSULT_HEAD.finditer(folded):
        if _REPORTED_HEAD_BEFORE.search(folded[max(0, head.start() - 30):head.start()]):
            continue
        rest = folded[head.end():]
        listed = se.specialties_listed_after_head(rest)
        if not listed:
            continue
        list_end = head.end() + _listed_length(rest)
        after_list = folded[list_end:]
        by_verb = any(0 <= head.start() - verb.end() <= _MAX_VERB_TO_HEAD_CHARS
                      and _verb_reaches_head(folded[verb.end():head.start()]) for verb in verbs)
        if not by_verb:
            # "Parecer da X: sem indicação..." é a resposta relatada; "Parecer
            # da X: solicitado" é o pedido (4ª revisão: com o verbo antes --
            # "Solicito parecer da X: IRA oligúrica" -- o dois-pontos não apaga).
            if _HEAD_REPORTS_ANSWER.match(after_list) and not _STATUS_REQUEST_AFTER_COLON.match(after_list):
                continue
            if _PASSIVE_REQUEST.match(after_list) is None and _STATUS_REQUEST_AFTER_COLON.match(after_list) is None:
                continue
        if not _is_rerequest(folded, head) and _answered_right_after(folded, list_end):
            continue
        for label in listed:
            if label not in found:
                found.append(label)
    return found


# Teste barato antes de fatiar e procurar (frase colada de 20 mil caracteres
# ficava quadrática -- 4ª revisão); o aparte mais longo aceito cabe nisto.
_MAX_VERB_TO_HEAD_CHARS = 120
_STATUS_REQUEST_AFTER_COLON = re.compile(r"^\s*[:\-–]\s*(?:solicitad|pedid|reiterad)\w*\b")
# Entre o verbo e a cabeça, com vírgula, só um aparte curto ("Solicito, por
# gentileza, avaliação da nefrologia"; "Solicito, devido à piora da função
# renal, parecer da nefrologia") -- nunca outro objeto ("Solicitado
# hemograma, parecer da nefrologia sem indicação de HD") -- 4ª revisão.
# Cada parte entre vírgulas é UM aparte inteiro -- conferido parte a parte,
# sem regex repetida (5ª revisão: a versão com grupo repetido levava 27 s numa
# frase de 140 caracteres com palavras repetidas).
_ASIDE = re.compile(
    r"(?:por\s+gentileza|gentilmente|por\s+favor|novamente|hoje|ainda|tambem|com\s+urgencia|urgente|nesta\s+data"
    r"|neste\s+momento|mais\s+uma\s+vez|(?:devido|em\s+virtude|diante|por\s+conta|pois)\b[^,;:]{1,60})"
)
_FINAL_ARTICLE = re.compile(r"(?:^|\s)(?:a|o|uma|um|nova|novo)$")
_MAX_ASIDES = 6


def _verb_reaches_head(between: str) -> bool:
    if re.search(r"[;:]", between):
        return False
    if "," not in between:
        return len(between.split()) <= 4
    parts = [" ".join(part.split()) for part in between.split(",")]
    if parts:
        parts[-1] = _FINAL_ARTICLE.sub("", parts[-1]).strip()
    asides = [part for part in parts if part]
    if not asides or len(asides) > _MAX_ASIDES or not all(_ASIDE.fullmatch(part) for part in asides):
        return False
    return not se.exam_mentions(between) and not se.CONSULT_HEAD.search(between)


def _is_rerequest(folded: str, head: re.Match) -> bool:
    """Reavaliação/nova avaliação: a avaliação ANTERIOR citada na frase
    ("avaliado em 18/08, sem indicação na ocasião") não responde a este
    pedido (4ª revisão)."""
    return head.group(0).startswith("reavalia") or re.search(r"\bnov[oa]s?\s+$", folded[max(0, head.start() - 8):
                                                                                         head.start()]) is not None


def _listed_length(rest: str) -> int:
    """Até onde vai a lista de especialidades logo no começo de `rest`."""
    position = 0
    while True:
        chunk = rest[position:]
        hit = se._specialty_at_start(chunk)
        if hit is None:
            stripped = se._TEAM_PREFIX.match(chunk)
            if stripped:
                inner = se._specialty_at_start(chunk[stripped.end():])
                hit = (inner[0], stripped.end() + inner[1]) if inner else None
        if hit is None:
            return position
        position += hit[1]
        glue = se.LIST_GLUE.match(rest[position:])
        if glue is None:
            return position
        next_hit = se._specialty_at_start(rest[position + glue.end():])
        if next_hit is None:
            stripped = se._TEAM_PREFIX.match(rest[position + glue.end():])
            if not (stripped and se._specialty_at_start(rest[position + glue.end() + stripped.end():])):
                return position
        position += glue.end()


def apply_consult_rule(notes: list[StructuredNote], admission_cutoff: str | None = None) -> list[RuleFinding]:
    """Frase com pedido de avaliação/parecer de uma especialidade
    reconhecida = pendência candidata (prompt mestre seção 20, segundo
    exemplo). "Solicitada avaliação do quadro" não vira mais pendência
    "Aguarda avaliação de quadro": sem especialidade reconhecida, nada."""
    findings: dict[str, RuleFinding] = {}
    for note in _chronological(notes):
        for sentence, original in _note_sentences(note.text):
            folded = fold(sentence)
            for target in consult_targets_requested(sentence):
                _add_occurrence(findings, target, lambda target=target: RuleFinding(
                    category=CATEGORY_CONSULT,
                    subcategory=SUBTYPE_CONSULT_REQUESTED,
                    description=f"Aguarda avaliação de {target.lower()}",
                    evidence="", evidence_date=None, alvo=target,
                ), original, _request_date(folded, note.timestamp, admission_cutoff))
    return _finalize(findings)


def apply_all_rules(notes: list[StructuredNote], admission_cutoff: str | None = None) -> list[RuleFinding]:
    """Pedidos encontrados nesta janela de notas. Quem chama
    (orchestrator.py) decide se cada um é pendência nova, se já está
    registrado (mesma categoria + alvo, de qualquer fonte) ou se já foi
    atendido antes (pendência encerrada depois deste pedido).
    `admission_cutoff` (AAAA-MM-DD): data de pedido citada antes disso não
    vale -- fica a da evolução."""
    return apply_exam_rule(notes, admission_cutoff) + apply_consult_rule(notes, admission_cutoff)
