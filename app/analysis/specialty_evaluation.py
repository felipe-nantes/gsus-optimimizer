"""Detector determinístico de pendência já atendida (PEND-001, pedido do
auditor, DEC-129 seção 4): interconsulta avaliada, punção realizada, exame
com resultado.

Achado que motivou (medição só agregada no banco de dev): das 27
interconsultas ativas com especialidade reconhecível, 15 (56%) já tinham
evolução POSTERIOR da própria especialidade -- e o modelo de 8B, testado com
o prompt antigo e o novo, MANTÉM "aguarda avaliação da infectologia" depois
de uma evolução "PELA INFECTOLOGIA". O fechamento não pode depender do modelo.

Princípios (endurecidos pela revisão adversarial da Fase 3, que provou 51
casos em que a primeira versão fechava pendência ainda aberta):
- Fechar sem estar resolvido é o PIOR erro. Na dúvida, "Em verificação pelo
  auditor" (ACTION_REVIEW) ou nada -- nunca "Concluída".
- Interconsulta só fecha com os dois sinais do plano aprovado (DEC-129
  decisão 4) e da regra do auditor: evolução ASSINADA pela especialidade ou
  linha iniciada por "PELA <ESPECIALIDADE>" -- e nenhuma das duas pode dizer
  que o paciente não foi visto. Frase de outra equipe RELATANDO a resposta
  ("Parecer da X: ...") vai no máximo pra verificação.
- Exame só fecha com RESULTADO na frase do próprio exame, na mesma região.
  Lista de pendências, "sem resultado", "cobrar laudo" nunca fecham. "Exame
  realizado" sem resultado é verificação (tabela de estados do auditor).
- Punção só fecha com sucesso afirmativo, no passado, do tipo pedido.
- Só conta evidência POSTERIOR ao pedido, e que não seja histórico: frase
  com data anterior, de admissão/internação prévia/ambulatório, ou cópia de
  uma frase que já estava numa evolução anterior ao pedido.
- Evidência nova posterior que volta a pedir ou esperar ("Reitero parecer",
  "ainda não avaliado") torna a informação conflitante: verificação.
- Varre o histórico da internação inteira (banco), não só a janela nova.

Funções puras: nada de banco aqui (o orchestrator lê e grava).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache

from app.analysis import specialties
from app.analysis.sources import is_physician
from app.analysis.specialties import fold, label_spans
from app.analysis.taxonomy import (
    CATEGORY_ADMINISTRATIVA_LOGISTICA, CATEGORY_DIAGNOSTICO, CATEGORY_INTERCONSULTA,
    CATEGORY_PROCEDIMENTO_CIRURGIA, CATEGORY_TERAPEUTICA,
)

KIND_CONSULT = "INTERCONSULTA"
KIND_EXAM = "EXAME"
KIND_PUNCTURE = "PUNCAO"

# O que o detector conclui sobre a pendência (tabela de estados do auditor,
# DEC-129 seção 3): prova de execução -> concluída; "TC suspensa" ->
# cancelada; informação incompleta/conflitante -> em verificação.
ACTION_RESOLVE = "RESOLVE"
ACTION_CANCEL = "CANCEL"
ACTION_REVIEW = "REVIEW"

# Subtipos em que "a especialidade escreveu depois" encerra a pendência.
# REALIZADA_SEM_CONDUTA_DEFINIDA fica de fora: a avaliação já aconteceu e o
# que se espera é a conduta -- outra evolução do especialista não prova isso.
_CONSULT_CLOSABLE_SUBTYPES = {None, "SOLICITADA", "OUTRO"}
CONSULT_CLOSABLE_SUBTYPES = frozenset(_CONSULT_CLOSABLE_SUBTYPES)
_EXAM_CLOSABLE_SUBTYPES = {
    None, "AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL", "EXAME_REALIZADO_AGUARDANDO_LAUDO",
    "AGUARDA_ANATOMOPATOLOGICO_OU_CULTURA",
}
_PUNCTURE_CATEGORIES = {
    CATEGORY_PROCEDIMENTO_CIRURGIA, CATEGORY_TERAPEUTICA, CATEGORY_ADMINISTRATIVA_LOGISTICA,
}

_EVIDENCE_MAX_CHARS = 300

# ================================================================== alvo

# "avaliação da X", "parecer p/ X", "interconsulta com a X", "IC para X",
# "avaliação e conduta da X" -- a especialidade que vem LOGO depois é o alvo.
_CONSULT_HEAD = re.compile(
    r"\b(?:reavaliacao|reavaliar|avaliacao|avaliar|parecer|interconsulta|resposta|visita|ic)\s+"
    r"(?:(?:e\s+(?:conduta|acompanhamento|seguimento)|com\s+urgencia|urgente|de\s+urgencia)\s+)?"
    r"(?:(?:d[aeo]s?|com|para|p/|pel[ao]s?|a[os]?)\s+)?(?:(?:a|o)\s+)?"
)
CONSULT_HEAD = _CONSULT_HEAD
_TEAM_PREFIX = re.compile(r"^(?:equipe|servico|time|grupo)\s+(?:d[aeo]s?\s+)?")
# Entre duas especialidades de uma lista: "da nefrologia e da cardiologia",
# "nefro/cardio", "nefro, cardio e infecto".
LIST_GLUE = re.compile(r"^\s*(?:,|/|\be\b|\+)\s*(?:(?:d[aeo]s?|a|o)\s+)?")
# Quem PEDIU não é o alvo: "solicitada pela nefrologia", "conforme pneumologia".
_REQUESTER_BEFORE = re.compile(
    r"(?:solicitad[oa]s?|pedid[oa]s?|indicad[oa]s?|encaminhad[oa]s?)\s+(?:pel[ao]s?|por)\s+(?:(?:a|o)\s+)?"
    r"(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$"
    r"|\b(?:conforme|segundo|de\s+acordo\s+com)\s+(?:(?:a|o)\s+)?(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$"
)


_specialty_at_start = specialties.specialty_span_at_start


def specialties_listed_after_head(rest: str) -> list[str]:
    """Especialidade(s) logo no começo de `rest` (texto dobrado, depois de um
    "avaliação/parecer..."), incluindo listas "da nefrologia e da
    cardiologia". "Serviço social" é tentado antes de tirar "serviço de".
    Especialidade qualificada por subgrupo ("cardiologia intervencionista")
    não entra: não é a especialidade inteira."""
    found: list[str] = []
    position = 0
    while True:
        chunk = rest[position:]
        hit = _specialty_at_start(chunk)
        if hit is None:
            stripped = _TEAM_PREFIX.match(chunk)
            if stripped:
                inner = _specialty_at_start(chunk[stripped.end():])
                hit = (inner[0], stripped.end() + inner[1]) if inner else None
        if hit is None:
            break
        label, length = hit
        if label not in found:
            found.append(label)
        position += length
        glue = LIST_GLUE.match(rest[position:])
        if glue is None:
            break
        position += glue.end()
    return found


# Cabeça que CITA uma avaliação já feita, não a que se espera: "após
# avaliação da nefrologia, aguardamos a da cardiologia" (2ª revisão: o alvo
# virava a nefrologia, e a evolução seguinte dela fechava o pedido).
REPORTED_HEAD_BEFORE = re.compile(r"\b(?:conforme|segundo|de\s+acordo\s+com|apos|pos|com\s+base\s+n[ao])\s+$")
_WAITING_VERB_BEFORE = re.compile(
    r"\b(?:aguard\w*|solicit\w*|pedid\w*|peco|pedimos|reiter\w*|necessit\w*|encaminh\w*|acionad\w*)\s+"
    r"(?:(?:a|o|nova|novo|novamente|uma)\s+)*$"
)


def consult_target(text: str | None, *, require_head: bool = False) -> str | None:
    """Especialidade que a interconsulta espera.
    - Com "avaliação/parecer/interconsulta/IC": a especialidade LOGO depois.
      Duas ou mais listadas ("da cardiologia e da nefrologia") = ambíguo,
      None -- fechar quando só uma responde esconderia a outra. Cabeça
      precedida de "após/conforme/segundo" é a avaliação já feita, não conta;
      cabeças diferentes com especialidades diferentes: vale a que vem depois
      de um verbo de espera/pedido, senão None.
    - Se houve âncora mas nada reconhecido depois dela ("avaliação
      pré-anestésica para cirurgia", "parecer da hepatologia"): None. Nunca
      cai pra "a única especialidade citada", que costuma ser quem PEDIU.
    - Sem âncora nenhuma: a única especialidade citada, se não for a de quem
      pediu (é o caso do `alvo` já canônico, "INFECTOLOGIA")."""
    folded = fold(text)
    head_seen = False
    candidates: list[tuple[str, bool]] = []  # (especialidade, depois de verbo de espera/pedido)
    for head in _CONSULT_HEAD.finditer(folded):
        head_seen = True
        before = folded[max(0, head.start() - 30):head.start()]
        if REPORTED_HEAD_BEFORE.search(before):
            continue
        listed = specialties_listed_after_head(folded[head.end():])
        if len(listed) > 1:
            return None
        if listed:
            candidates.append((listed[0], _WAITING_VERB_BEFORE.search(before) is not None))
    distinct = {label for label, _waiting in candidates}
    if len(distinct) == 1:
        return candidates[0][0]
    if len(distinct) > 1:
        waited = {label for label, waiting in candidates if waiting}
        return waited.pop() if len(waited) == 1 else None
    if head_seen or require_head:
        return None
    spans = specialties.specialty_spans(folded)
    labels = {label for _start, _end, label in spans}
    if len(labels) != 1:
        return None
    start = spans[0][0]
    if _REQUESTER_BEFORE.search(folded[max(0, start - 45):start]):
        return None
    return spans[0][2]


# Exames: rótulo canônico + padrão (sobre texto dobrado). Sobreposição
# resolvida como nas especialidades: começa antes e, empatado, mais longo
# ("eco doppler" é DOPPLER, nunca ecocardiograma; "angio-TC" nunca é TC).
# Modalidade conta: ETE não é ETT; Doppler arterial não é venoso.
_EXAMS: tuple[tuple[str, str], ...] = (
    ("ANGIOTOMOGRAFIA", r"\bangiotomografia\w*|\bangio\s*-?\s*tc\b|\bangiotc\b"),
    ("TOMOGRAFIA", r"\btomografia\w*|\btcs?\b|\btcc\b|\btcar\b"),
    # Angio-RM não é RM (2ª revisão: a RM simples fechava o pedido de angio-RM).
    ("ANGIORRESSONÂNCIA", r"\bangiorressonancia\w*|\bangio\s*-?\s*(?:rm|rnm|ressonancia\w*)\b"),
    ("RESSONÂNCIA", r"\bressonancia\w*|\brnm\b|\brm\b"),
    ("RAIO-X", r"\braio\s*-?\s*x\b|\brx\b|\bradiografia\w*"),
    ("DOPPLER ARTERIAL", r"\b(?:eco\s*-?\s*)?doppler\w*\s+arteria\w*|\barteriodoppler\w*"),
    ("DOPPLER VENOSO", r"\b(?:eco\s*-?\s*)?doppler\w*\s+venos\w*|\bvenodoppler\w*"),
    # "ecocardiograma com doppler" é ecocardiograma -- o "com" não abre outro exame.
    ("DOPPLER", r"\beco\s*-?\s*doppler\w*|(?<!com\s)\bdoppler\w*|\bduplex\w*"),
    # "ECO TE" é o transesofágico; "ECO TT" o transtorácico (2ª revisão: "ECO
    # TE" caía em ecocardiograma e o ETT fechava o pedido de ETE).
    ("ECOCARDIOGRAMA TRANSESOFÁGICO", r"\bete\b|\beco(?:cardiograma)?\s+transesofag\w*|\beco\s*-?\s*te\b|\becote\b"),
    ("ECOCARDIOGRAMA", r"\becocardiogra\w*|\beco\b|\bett\b|\beco\s+transtorac\w*|\beco\s*-?\s*tt\b|\becott\b"),
    ("ULTRASSOM", r"\bultrass\w*|\bultra\s*-?\s*som\w*|\busg\b|\bus\b(?=\s+(?:de\s+|do\s+|da\s+)?[a-z]{4})"),
    ("ENDOSCOPIA", r"\bendoscopia\b|\beda\b"),
    ("COLONOSCOPIA", r"\bcolonoscopia\b"),
    ("BRONCOSCOPIA", r"\bbroncoscopia\b"),
    # Modalidade conta (3ª revisão: a cintilografia óssea fechava a miocárdica).
    ("CINTILOGRAFIA ÓSSEA", r"\bcintilografia\s+ossea\b|\bmapeamento\s+osseo\b"),
    ("CINTILOGRAFIA MIOCÁRDICA", r"\bcintilografia\s+(?:de\s+perfusao\s+)?miocardica\b|\bcintilografia\s+de\s+perfusao\b"),
    ("CINTILOGRAFIA PULMONAR", r"\bcintilografia\s+(?:pulmonar|de\s+ventilacao|v\s*/\s*q)\b"),
    ("CINTILOGRAFIA RENAL", r"\bcintilografia\s+renal\b|\bdmsa\b|\bdtpa\b"),
    ("CINTILOGRAFIA TIREOIDIANA", r"\bcintilografia\s+(?:de\s+|da\s+)?tireoide\b"),
    ("CINTILOGRAFIA", r"\bcintilografia\b"),
    ("ELETROENCEFALOGRAMA", r"\beletroencefalogra\w*|\beeg\b"),
    ("HOLTER", r"\bholter\b"),
    # Cateterismo VESICAL não é o cardíaco (3ª revisão).
    ("CATETERISMO", r"\bcateterismo\b(?!\s+(?:vesical|uretral|intermitente|de\s+alivio|de\s+demora|umbilical))"
                    r"|\bcineangiocoronariografia\b"),
    ("ARTERIOGRAFIA", r"\barteriografia\b|\bangiografia\b"),
    ("MAMOGRAFIA", r"\bmamografia\b"),
    ("HEMOCULTURA", r"\bhemoculturas?\b|\bhmc\b"),
    ("UROCULTURA", r"\buroculturas?\b|\burc\b|\buroc\b"),
    ("CULTURA", r"\bculturas?\b"),
    ("ANATOMOPATOLÓGICO", r"\banatomopatolog\w*|\bbiopsias?\b|\bhistopatolog\w*|\b(?:resultado|laudo)\s+d[oa]\s+ap\b"),
)
_EXAM_COMPILED = tuple((label, re.compile(p)) for label, p in _EXAMS)
_CULTURE_EXAMS = {"HEMOCULTURA", "UROCULTURA", "CULTURA"}
_AP_EXAM = "ANATOMOPATOLÓGICO"
_SCINTIGRAPHIES = {
    "CINTILOGRAFIA ÓSSEA", "CINTILOGRAFIA MIOCÁRDICA", "CINTILOGRAFIA PULMONAR", "CINTILOGRAFIA RENAL",
    "CINTILOGRAFIA TIREOIDIANA", "CINTILOGRAFIA",
}
# CULTURA (sem material no nome) tem "região": o material ("cultura de aspirado
# traqueal", "de ponta de CVC") -- 3ª revisão: a cultura de um material
# fechava o pedido de outro.
_EXAMS_WITHOUT_REGION = {"HEMOCULTURA", "UROCULTURA"} | _SCINTIGRAPHIES | {
    _AP_EXAM, "ECOCARDIOGRAMA", "ECOCARDIOGRAMA TRANSESOFÁGICO", "MAMOGRAFIA", "HOLTER", "ELETROENCEFALOGRAMA",
    "CATETERISMO", "ENDOSCOPIA", "COLONOSCOPIA", "BRONCOSCOPIA",
}

_UPPER_LIMB = r"(?:ombros?|cotovelos?|antebracos?|punhos?|maos?|bracos?|umeros?|claviculas?)"
_LOWER_LIMB = r"(?:quadril|quadris|femur|joelhos?|pernas?|tornozelos?|pes?|tibias?|coxas?)"
_REGIONS: tuple[tuple[str, str], ...] = (
    ("CRÂNIO", r"\bcranio\b|\bcranian\w*|\bencefalo\b|\bcerebr\w*|\btcc\b"),
    ("FACE", r"\bface\b|\bseios?\s+d[ae]\s+face\b|\bseios\s+paranasais\b|\borbitas?\b|\bmastoide\w*|\bsela\b"),
    ("PESCOÇO", r"\bpescoco\b|\bcervical\b|\bcarotid\w*|\btireoide\b|\btiroide\b"),
    ("TÓRAX", r"\btorax\b|\btoracic[ao]\b|\bpulm\w*|\btcar\b"),
    ("ABDOME", r"\babdom\w*"),
    ("RINS E VIAS URINÁRIAS", r"\brins?\b|\brenal\b|\bvias\s+urinarias\b|\bbexiga\b|\bprostata\b"),
    ("PELVE", r"\bpelv\w*|\bbacia\b|\btransvaginal\b|\bobstetric\w*|\bbolsa\s+escrotal\b|\bescrot\w*|\btesticul\w*"),
    # Segmento da coluna e membro superior x inferior (com o lado) contam
    # (3ª revisão: a TC de coluna lombar fechava a cervical; o RX de ombro, o
    # de joelho).
    ("COLUNA CERVICAL", r"\bcoluna\s+cervical\b"),
    ("COLUNA TORÁCICA", r"\bcoluna\s+(?:toracica|dorsal)\b"),
    ("COLUNA LOMBAR", r"\bcoluna\s+(?:lombar|lombossacra)\b|\blombossacr\w*"),
    ("COLUNA", r"\bcoluna\b"),
    ("PARTES MOLES", r"\bpartes\s+moles\b"),
    ("MEMBRO SUPERIOR DIREITO", r"\bmsd\b|\bmembro\s+superior\s+direito\b|\b" + _UPPER_LIMB + r"\s+direit[oa]\b"),
    ("MEMBRO SUPERIOR ESQUERDO", r"\bmse\b|\bmembro\s+superior\s+esquerdo\b|\b" + _UPPER_LIMB + r"\s+esquerd[oa]\b"),
    ("MEMBROS SUPERIORES", r"\bmmss\b|\bmembros?\s+superior\w*|\b" + _UPPER_LIMB + r"\b"),
    ("MEMBRO INFERIOR DIREITO", r"\bmid\b|\bmembro\s+inferior\s+direito\b|\b" + _LOWER_LIMB + r"\s+direit[oa]\b"),
    ("MEMBRO INFERIOR ESQUERDO", r"\bmie\b|\bmembro\s+inferior\s+esquerdo\b|\b" + _LOWER_LIMB + r"\s+esquerd[oa]\b"),
    ("MEMBROS INFERIORES", r"\bmmii\b|\bmembros?\s+inferior\w*|\b" + _LOWER_LIMB + r"\b"),
    ("MEMBROS", r"\bmembros?\b"),
)
_REGION_COMPILED = tuple((label, re.compile(p)) for label, p in _REGIONS)
# Região mais ampla de cada uma: pedido de um lado e resultado sem lado (ou
# o contrário) vai no máximo para verificação, nunca conclui.
_REGION_PARENT = {
    "COLUNA CERVICAL": "COLUNA", "COLUNA TORÁCICA": "COLUNA", "COLUNA LOMBAR": "COLUNA",
    "MEMBRO SUPERIOR DIREITO": "MEMBROS SUPERIORES", "MEMBRO SUPERIOR ESQUERDO": "MEMBROS SUPERIORES",
    "MEMBRO INFERIOR DIREITO": "MEMBROS INFERIORES", "MEMBRO INFERIOR ESQUERDO": "MEMBROS INFERIORES",
    "MEMBROS SUPERIORES": "MEMBROS", "MEMBROS INFERIORES": "MEMBROS",
}
# Cada material é o seu (4ª revisão: osso, swab de ferida e partes moles; lavado,
# escarro e aspirado; pleural e ascítico eram um grupo só -- e o rótulo
# "LÍQUIDO CAVITÁRIO" nem se reconhecia de volta).
_SPECIMENS: tuple[tuple[str, str], ...] = (
    ("ASPIRADO TRAQUEAL", r"\baspirado\s+traqueal\b|\bsecrecao\s+traqueal\b|\bast\b"),
    ("LAVADO BRONCOALVEOLAR", r"\blavado\s+bronco\w*|\blba\b|\bmini\s*-?\s*bal\b"),
    ("ESCARRO", r"\bescarro\b"),
    ("PONTA DE CATETER", r"\bponta\s+de\s+(?:cvc|cateter|picc|shilley)\b"),
    ("LÍQUOR", r"\bliquor\w*|\blcr\b|\bliquido\s+cefalorraquidiano\b"),
    ("LÍQUIDO PLEURAL", r"\bliquido\s+pleural\b"),
    ("LÍQUIDO ASCÍTICO", r"\bliquido\s+(?:ascitico|peritoneal)\b"),
    ("LÍQUIDO SINOVIAL", r"\bliquido\s+(?:sinovial|articular)\b"),
    ("FRAGMENTO ÓSSEO", r"\bfragmento\s+osseo\b|\bosso\b|\bossea\b"),
    ("TECIDO", r"\btecidos?\b|\bpartes\s+moles\b"),
    ("ABSCESSO", r"\babscesso\w*"),
    ("SECREÇÃO DE DRENO", r"\bsecrecao\s+de\s+dreno\b|\bdreno\b"),
    ("FERIDA", r"\bferida\w*|\bsecrecao\s+purulenta\b"),
    ("VIGILÂNCIA", r"\bvigilancia\b|\bswab\s+(?:retal|nasal|anal|perianal|axilar)\b"),
)
_SPECIMEN_COMPILED = tuple((label, re.compile(p)) for label, p in _SPECIMENS)


def _region_patterns(exam: str):
    return _SPECIMEN_COMPILED if exam == "CULTURA" else _REGION_COMPILED


def _ancestors(region: str) -> set[str]:
    found = set()
    while region in _REGION_PARENT:
        region = _REGION_PARENT[region]
        found.add(region)
    return found


def _labels(compiled, folded: str) -> list[str]:
    seen: list[str] = []
    for _start, _end, label in label_spans(compiled, folded):
        if label not in seen:
            seen.append(label)
    return seen


def _exam_with_region(folded: str) -> list[tuple[str, str | None]]:
    """(exame, região) de cada exame citado, a região sendo a primeira que
    vem depois dele e antes do próximo exame."""
    spans = label_spans(_EXAM_COMPILED, folded)
    result = []
    for index, (start, end, exam) in enumerate(spans):
        if exam in _EXAMS_WITHOUT_REGION:
            result.append((exam, None))
            continue
        stop = spans[index + 1][0] if index + 1 < len(spans) else len(folded)
        regions = _labels(_region_patterns(exam), folded[end:stop])
        result.append((exam, regions[0] if regions else None))
    return result


def exam_mentions(folded: str) -> list[tuple[int, int, str, str | None]]:
    """(início, fim, exame, região) de cada exame citado num texto dobrado --
    usado pelas regras pra achar TODOS os exames pedidos numa frase."""
    spans = label_spans(_EXAM_COMPILED, folded)
    return [(start, end, exam, region) for (start, end, exam), (_e, region) in zip(spans, _exam_with_region(folded))]


def exam_target(text: str | None) -> str | None:
    """"TOMOGRAFIA DE CRÂNIO", "ECOCARDIOGRAMA", "HEMOCULTURA"... ou None."""
    found = _exam_with_region(fold(text))
    if not found:
        return None
    exam, region = found[0]
    return f"{exam} DE {region}" if region else exam


def _split_exam_target(target: str) -> tuple[str, str | None]:
    exam, _, region = target.partition(" DE ")
    return exam, (region or None)


def regions_for(exam: str, texts) -> set[str]:
    """Regiões com que `exam` aparece em outros textos (descrição, evidência)."""
    regions = set()
    for text in texts:
        for found_exam, region in _exam_with_region(fold(text)):
            if found_exam == exam and region:
                regions.add(region)
    return regions


def _region_for(exam: str, texts) -> str | None:
    """Região de `exam` em outros textos (descrição, evidência), só quando
    inequívoca: o mesmo exame citado com UMA região só."""
    regions = regions_for(exam, texts)
    return regions.pop() if len(regions) == 1 else None


# "PAI" (pressão arterial invasiva) só com lugar/dispositivo ao lado -- "pai
# do paciente" é o pai (2ª revisão adversarial).
_PAI = r"\bpai\b(?=\s*(?:em|na|no|radial|femoral|braquial|pedios\w*|direit\w*|esquerd\w*|mse|msd|\(|$|[.,;]))"
_PUNCTURE_ITEM = re.compile(
    r"\bpunc(?:ao|oes|ionar|ionad\w*)\b|\bacesso\s+(?:venoso|central|periferico|arterial)\b|\bcvc\b|\bpicc\b"
    r"|\bcateter\s+(?:venoso\s+)?central\b|\bparacentese\b|\btoracocentese\b|\bavp\b|" + _PAI +
    r"|\bshilley\b|\bpermcath\b|\bcateter\s+(?:de\s+)?(?:hemo)?dialise\b|\bduplo\s*-?\s*lumen\b"
    r"|\bpl\b|\bliquor\b"
)
# Tipos, nesta ordem (o mais específico antes). A 2ª revisão mostrou o CVC
# fechando pedido de Shilley e de PICC, e qualquer punção fechando "punção
# liquórica": cateter de diálise e PICC têm rótulo próprio, e punção venosa
# sem "central" é a periférica.
_HD_CATHETER = (
    r"\bshilley\b|\bpermcath\b|\bmahurkar\b|\bcateter\s+(?:de\s+)?(?:hemo)?dialise\b"
    r"|\bcateter\s+(?:de\s+|para\s+)?hd\b|\bduplo\s*-?\s*lumen\s+(?:de\s+|para\s+)?(?:hd|hemodialise|dialise)\b"
    r"|\b(?:cvc|acesso|cateter)\s+(?:venoso\s+)?(?:central\s+)?(?:de|para)\s+(?:hd|hemodialise|dialise)\b"
)
_CENTRAL_LINE = (
    r"\bcvc\b|\bcateter\s+venoso\s+central\b|\bacesso\s+(?:venoso\s+)?central\b|\bjugular\b|\bvj[ie]?[de]?\b"
    r"|\bsubclavi\w*|\bvsc\w*|\bfemoral\b|\bvf[de]?\b|\bduplo\s*-?\s*lumen\b|\bintracath\b"
)
_PERIPHERAL = r"\bperiferic[ao]s?\b|\bavp\b|\bjelco\b|\babocath\b|\bscalp\b|\bpuncao\s+venosa\b(?!\s+central)"
_PUNCTURE_TYPES: tuple[tuple[str, str], ...] = (
    ("CATETER DE HEMODIÁLISE", _HD_CATHETER),
    ("PICC", r"\bpicc\b"),
    ("PUNÇÃO LOMBAR", r"\blombar\b|\bliquor\w*|\bliquoric[ao]\b|\blcr\b|\bpl\b"),
    ("PARACENTESE", r"\bparacentese\b"),
    ("TORACOCENTESE", r"\btoracocentese\b|\bpuncao\s+(?:de\s+)?(?:derrame\s+)?pleural\b"),
    ("ACESSO VENOSO CENTRAL", _CENTRAL_LINE + r"|\bcentral\b"),
    ("ACESSO ARTERIAL", _PAI + r"|\bcateter\s+arterial\b|\blinha\s+arterial\b|\bpressao\s+arterial\s+invasiva\b"
                        r"|\bacesso\s+arterial\b"),
    ("ACESSO VENOSO PERIFÉRICO", _PERIPHERAL),
)
_PUNCTURE_TYPE_COMPILED = tuple((label, re.compile(p)) for label, p in _PUNCTURE_TYPES)
PUNCTURE_GENERIC = "PUNÇÃO"
PUNCTURE_LABELS = {label for label, _ in _PUNCTURE_TYPES} | {PUNCTURE_GENERIC}
# Na frase de SUCESSO, o tipo tem de aparecer de verdade: central exige o
# nome de cateter central ou o sítio (nunca "cateter" ou "venoso" soltos --
# "cateter nasal de O2" e "punção venosa periférica" fechavam CVC); arterial
# exige a linha arterial, não "arterial".
_PUNCTURE_SUCCESS_TYPE = {
    "CATETER DE HEMODIÁLISE": re.compile(_HD_CATHETER + r"|\b(?:para|de)\s+(?:hd|hemodialise|dialise)\b"),
    "PICC": re.compile(r"\bpicc\b"),
    "ACESSO VENOSO CENTRAL": re.compile(_CENTRAL_LINE + r"|\bpicc\b"),
    "ACESSO ARTERIAL": re.compile(_PAI + r"|\b(?:passad|instalad|inserid|puncionad|realizad)[oa]s?\s+(?:a\s+)?pai\b"
                                  r"|\bcateter\s+arterial\b|\blinha\s+arterial\b|\bpressao\s+arterial\s+invasiva\b"),
    "PUNÇÃO LOMBAR": re.compile(r"\blombar\b|\bliquor\w*|\bliquoric[ao]\b|\bpl\b|\blcr\b"),
    "PARACENTESE": re.compile(r"\bparacentese\b"),
    "TORACOCENTESE": re.compile(r"\btoracocentese\b|\bpuncao\s+(?:de\s+)?(?:derrame\s+)?pleural\b"),
    "ACESSO VENOSO PERIFÉRICO": re.compile(_PERIPHERAL),
}
# Na frase de sucesso de um acesso CENTRAL, nada disto pode aparecer:
# dispositivo que não é cateter central, periférico, ou cateter de diálise
# (que tem pedido próprio).
_NOT_CENTRAL_DEVICE = re.compile(
    r"\bperiferic\w*|\bavp\b|\bnasal\b|\bvesical\b|\bsondas?\b|\bsvd\b|\bcvd\b|\bo2\b|\boxigen\w*|\bjelco\b"
    r"|\babocath\b|\bscalp\b|\bsne\b|\bsng\b|\bgtt\b|\bgastrostomia\b|" + _HD_CATHETER
)
_NOT_PERIPHERAL_DEVICE = re.compile(_CENTRAL_LINE + r"|\bpicc\b|\bnasal\b|\bvesical\b|\bsondas?\b|" + _HD_CATHETER)
_ARTERY = re.compile(r"\barteri\w*")
# Time de punção / acesso vascular (caso 3 do auditor).
PUNCTURE_TEAM = re.compile(r"\b(?:equipe|time|grupo|servico)\s+(?:d[aeo]\s+)?(?:puncao|puncoes|acesso\s+vascular|acessos)\b")


def puncture_target(text: str | None) -> str | None:
    folded = fold(text)
    if not _PUNCTURE_ITEM.search(folded) and not PUNCTURE_TEAM.search(folded):
        return None
    for label, pattern in _PUNCTURE_TYPE_COMPILED:
        if pattern.search(folded):
            return label
    return PUNCTURE_GENERIC


_STOPWORDS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "para", "por", "com", "na", "no",
    "nas", "nos", "um", "uma", "ao", "aos", "pela", "pelo", "aguarda", "aguardando", "aguardar",
}
# Palavras de "cabeça" que não dizem O QUE se espera ("Aguarda REALIZAÇÃO de
# colecistectomia", "avaliação da EQUIPE de cuidados paliativos"): fora do
# alvo genérico, senão a mesma pendência escrita de outro jeito não casa.
_GENERIC_HEAD_WORDS = {
    "realizacao", "realizar", "avaliacao", "avaliar", "definicao", "definir", "programacao", "programar",
    "agendamento", "solicitacao", "solicitado", "solicitada", "pendente", "paciente", "equipe",
    "servico", "time", "grupo", "parecer", "interconsulta", "ic", "resposta", "conduta", "nova", "novo",
}


def _generic_target(text: str | None) -> str | None:
    words = [w for w in re.findall(r"[a-z0-9]+", fold(text)) if w not in _STOPWORDS and w not in _GENERIC_HEAD_WORDS]
    return " ".join(words[:6]).upper() or None


def _generic_consult_target(text: str | None) -> str | None:
    """Alvo genérico de interconsulta a especialidade fora da tabela
    ("CUIDADOS PALIATIVOS", "NUTROLOGIA"): o que vem depois de "avaliação/
    parecer da", senão o texto todo. Sem ele, duas interconsultas a equipes
    fora da tabela viravam uma só e fechar uma apagava a outra (2ª revisão)."""
    folded = fold(text)
    head = _CONSULT_HEAD.search(folded)
    if head is not None:
        # Parênteses ficam: "ortopedia (grupo de mão)" não é a ortopedia.
        # Corta em 4 palavras DEPOIS de tirar as vazias -- "equipe de
        # controle de infecção" e "equipe de controle da dor" viravam os
        # dois "CONTROLE" (3ª revisão).
        after = re.split(r"[,;.]|\bpara\b|\bpor\b|\bdevido\b|\bpois\b", folded[head.end():], maxsplit=1)[0]
        target = _generic_target(after.replace("(", " ").replace(")", " "))
        if target:
            return " ".join(target.split()[:4])
    return _generic_target(text)


def _consult_or_puncture(text: str | None) -> str | None:
    """Pedido à equipe de punção/acesso vascular é punção (caso 3), mesmo
    escrito como "avaliação da equipe de acesso vascular"; senão, a
    especialidade; senão, punção citada."""
    if text and PUNCTURE_TEAM.search(fold(text)):
        return puncture_target(text)
    return consult_target(text) or puncture_target(text)


def normalize_target(
    category: str, raw_alvo: str | None, description: str | None = None, evidence: str | None = None,
) -> str | None:
    """`alvo` canônico de uma pendência -- chave de casamento entre execuções
    e entre fontes (regra x IA). Ordem: o alvo que veio (da IA ou da regra),
    a descrição e, por último, a evidência -- só com âncora ("parecer da X"),
    nunca "a única especialidade citada" (costuma ser quem pediu)."""
    if category == CATEGORY_INTERCONSULTA:
        for text in (raw_alvo, description):
            target = _consult_or_puncture(text)
            if target:
                return target
        target = consult_target(evidence, require_head=True)
        if target:
            return target
        # Especialidade fora da tabela: alvo genérico (nunca canônico -- o
        # detector não fecha -- mas separa uma interconsulta da outra).
        return _generic_consult_target(raw_alvo or description)
    if category == CATEGORY_DIAGNOSTICO:
        for text in (raw_alvo, description):
            target = exam_target(text)
            if target:
                exam, region = _split_exam_target(target)
                if region is None and exam not in _EXAMS_WITHOUT_REGION:
                    region = _region_for(exam, (description, evidence))
                return f"{exam} DE {region}" if region else exam
        return _generic_target(raw_alvo or description)
    if category in _PUNCTURE_CATEGORIES:
        for text in (raw_alvo, description):
            target = puncture_target(text)
            if target:
                return target
    # Sem alvo da IA: o da descrição -- alvo vazio casava com qualquer outra
    # pendência da categoria ("traqueostomia" engolindo "colecistectomia").
    return _generic_target(raw_alvo or description)


def is_canonical_target(category: str, alvo: str | None) -> bool:
    """Alvo vindo das tabelas (especialidade, exame, punção) -- estável
    entre execuções. Alvo genérico (texto livre da IA, ex.: "DESMAME
    OXIGENIO") pode vir escrito de outro jeito na próxima análise: aí o
    casamento é aproximado (ver orchestrator)."""
    if not alvo:
        return False
    if category == CATEGORY_INTERCONSULTA:
        return consult_target(alvo) == alvo or alvo in PUNCTURE_LABELS
    if category == CATEGORY_DIAGNOSTICO:
        return exam_target(alvo) == alvo
    if category in _PUNCTURE_CATEGORIES:
        return alvo in PUNCTURE_LABELS
    return False


# ============================================================ evidência


@dataclass(frozen=True)
class DetectorNote:
    timestamp: str | None  # ISO local (horário do GSUS), sem fuso
    role: str | None  # cargo do autor (`notes.source_type`)
    text: str


@dataclass(frozen=True)
class DetectedResolution:
    pending_id: str
    kind: str
    target: str
    action: str  # ACTION_RESOLVE | ACTION_CANCEL | ACTION_REVIEW
    evidence: str
    evidence_date: str  # ISO local da evolução que provou
    resolved_at: str  # ISO UTC, nunca antes de `created_at`
    note: str  # explicação curta pro histórico


# Frase: não corta no ponto de "S. aureus", "E. coli", "100.000", "15.08".
_SENTENCE_SPLIT = re.compile(r"(?<!\b[A-Za-z])\.(?!\d)|[;\n]+")
# Data dd/mm[/aaaa] ou dd.mm[.aaaa] (com ponto, só dois dígitos: "1.5" de
# creatinina não é data).
_DATE_IN_TEXT = re.compile(r"(?<![\d/.,])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])"
                           r"|(?<![\d/.,])(\d{2})\.(\d{2})(?:\.(\d{2,4}))?(?![\d.])")
# Fração clínica não é data: "força grau 4/5", "PA 18/10", "hemoculturas 2/2".
# Contexto forte (PA, força, Glasgow...): nunca é data. Contexto fraco
# (cultura, frascos, "positiva"): só quando o par não pode ser data -- "HMC
# 15/08: negativa" é data, "hemoculturas 2/2" é fração (2ª revisão).
_FRACTION_BEFORE = re.compile(
    r"(?:forca|grau|\bpa\b|\bp\.a\.?|glasgow|escore|\beva\b|\bdor\b|sopro|\+|\bx\b|\bft\b|\bfc\b|\bfr\b)"
    r"\s*(?:grau\s*)?[:=]?\s*$"
)
_FRACTION_AFTER = re.compile(r"^\s*(?:mmhg|\+|x\b)")
_WEAK_FRACTION_BEFORE = re.compile(r"(?:hmc|hemoculturas?|uroculturas?|culturas?|frascos?|amostras?)\s*[:=]?\s*$")
_WEAK_FRACTION_AFTER = re.compile(r"^\s*(?:amostras?|frascos?|positiv\w*|negativ\w*|com\s+cresc\w*)")
# Outras formas de data em evolução: "03/2025" (mês/ano), "(ECO 2025)",
# "(15-08)", "do dia 15", "15 de agosto".
_MONTH_YEAR = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{4})(?![\d/])")
_BARE_YEAR = re.compile(
    r"(?:\b(?:em|de|ano\s+de|desde)\s+|\(\s*|\b(?:eco|ett|ete|tc|rm|rnm|rx|usg|exame|exames|cultura|biopsia)\s+)"
    r"((?:19|20)\d{2})\b(?!\s*(?:ml|mg|ui|g\b|kg|mcg|cc|%|mm|cm|un\b|/))"
)
_DASH_DATE = re.compile(r"(?:\(|\bem\s+|\bde\s+|\bdia\s+)(\d{1,2})-(\d{1,2})(?:-(\d{2,4}))?(?![\d-])")
_MONTHS = ("janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
           "novembro", "dezembro")
_DAY_OF_MONTH_NAME = re.compile(r"\b(\d{1,2})\s+de\s+(" + "|".join(_MONTHS) + r")(?:\s+de\s+(\d{4}))?\b")
_DAY_ONLY = re.compile(
    r"\b(?:do|no|em|desde\s+o)\s+dia\s+(\d{1,2})\b(?!\s*[/.-]\s*\d)(?!\s+de\s+(?!" + "|".join(_MONTHS) + r")[a-z])"
)
# Datas relativas: "TC de crânio de ontem", "avaliado na véspera", "realizada
# na sexta-feira", "semana passada" (4ª revisão: resultado/avaliação de antes
# do pedido fechava por não ter data escrita).
_RELATIVE_DAY = re.compile(r"\b(anteontem|ontem)\b|\b(?:na|da)\s+(vespera)\b|\b(?:semana\s+passada|ha\s+uma\s+semana)\b")
_RELATIVE_OFFSET = {"ontem": 1, "anteontem": 2, "vespera": 1}
_WEEKDAYS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
_WEEKDAY_CITED = re.compile(
    r"\b(?:na|no|nesta|neste|desde(?:\s+[ao])?|de|da|do|ultim[oa])\s+"
    r"(?:(segunda|terca|quarta|quinta|sexta)\s*-?\s*feira|(sabado|domingo))\b"
)
# Frase de histórico: não prova nada sobre o pedido atual.
_HISTORY_STRONG = re.compile(
    r"\badmissao\b|\badmitid[oa]s?\b|\binternad[oa]s?\s+(?:por|com|devido|para)\b|\bdeu\s+entrada\b"
    r"|\binternac\w*\s+(?:anterior|previa|passada)|\bambulatori\w*|\bpreviamente\b|\banteriormente\b"
    r"|\bha\s+\d+\s+(?:dias?|semanas?|mes(?:es)?|anos?)\b|\bna\s+ocasiao\b|\bnaquele\s+momento\b|\bem\s+\d{4}\b"
    r"|\bhistoric\w*|\bantecedente\w*|\bhpp\b|\b(?:da|na|de)\s+entrada\b|\b(?:do|no)\s+(?:ps|pronto\s*-?\s*socorro)\b"
    r"|\boutr[oa]\s+(?:servico|hospital|instituicao|unidade)\b|\b(?:servico|hospital)\s+de\s+origem\b"
    r"|\bextern[oa]s?\b|\bchegada\b"
    # Linha de diagnóstico/problema que repete o exame que originou a
    # internação ("HD: AVCi (TC com hipodensidade...)", "# TCE grave: TC com
    # hematoma") -- 3ª revisão.
    r"|^\s*(?:hd|hda|hipotese\s+diagnostica|diagnosticos?|problemas?)\s*:|^\s*#"
)
# "anterior/prévio/inicial" só é histórico colado ao exame ou à internação
# ("TC de crânio anterior", "hemoculturas prévias", "TC inicial") -- não na
# anatomia ("artéria descendente anterior", "mediastino anterior") nem na
# comparação com o exame anterior ("redução em relação ao exame anterior").
_HISTORY_WORD_TARGET = (
    r"(?:tc|tomografia\w*|rx|raio\s*-?\s*x|radiografia\w*|eco\w*|ett|ete|usg|ultrass\w*|rm|rnm|ressonancia\w*"
    r"|doppler\w*|exames?|culturas?|hemoculturas?|uroculturas?|biopsias?|endoscopia|eda|internac\w*|avaliac\w*"
    r"|parecer\w*|laudos?|resultados?)"
)
_HISTORY_ADJACENT = re.compile(
    r"\b" + _HISTORY_WORD_TARGET + r"\s+(?:\w+\s+){0,3}?(?:anterior\w*|previ[oa]s?|inicia(?:l|is))\b"
    r"|\b(?:anterior\w*|previ[oa]s?)\s+" + _HISTORY_WORD_TARGET + r"\b"
)
_COMPARISON = re.compile(
    r"\b(?:em\s+relacao|comparad[oa]s?|comparativamente|quando\s+comparad[oa])\s+(?:a[o]?s?|com\s+(?:o|a)?s?)\s+"
    r"(?:\w+\s+){0,2}?(?:anterior\w*|previ[oa]s?)\b"
)


@lru_cache(maxsize=20_000)
def is_history(folded: str) -> bool:
    """Frase de histórico: não prova nada sobre o pedido atual (admissão,
    internação/serviço anterior, PS, linha de diagnóstico, exame "anterior"
    ou "prévio"). Em cache: o detector testa a mesma frase para cada
    pendência e cada prova (4ª revisão: era 2/3 do tempo numa internação
    longa com conflito tardio)."""
    return history_strong(folded) or history_adjacent(folded)


def history_strong(folded: str) -> bool:
    return _HISTORY_STRONG.search(folded) is not None


def history_adjacent(folded: str) -> bool:
    return _HISTORY_ADJACENT.search(_COMPARISON.sub(" ", folded)) is not None


# --- interconsulta
# Frase de outra equipe que RELATA a resposta -- forma estrita (lista
# positiva), e no máximo verificação: "Parecer da cirurgia torácica: sem
# indicação cirúrgica", "avaliado pela nefrologia", "nefrologia indicou HD".
_REPORTED_PARTICIPLE_BEFORE = re.compile(
    r"\b(?:avaliad|examinad|vist)[oa]s?\s+pel[ao]s?\s+(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$"
)
_REPORTED_MODAL = re.compile(
    r"\b(?:dever\w*|deve|devem|ficou|fica|ficara|precis\w*|necessit\w*|para|sem|ser|sera|nao|aguard\w*|a)\s+"
    r"(?:ser\s+)?$"
)
_REPORTED_COLON_BEFORE = re.compile(
    r"\b(?:parecer|avaliacao|resposta)\s+(?:d[aeo]s?\s+)?(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$"
)
_REPORTED_AFTER = re.compile(
    r"^\s*(?:avaliou|orientou|indicou|sugeriu|contraindicou|liberou|definiu|recomendou|optou|descartou)\b"
)
# Conteúdo depois de "Parecer da X:" que NÃO é resposta.
_NOT_AN_ANSWER_CONTENT = re.compile(
    r"^\s*(?:hoje|amanha|a\s+definir|nao\b|pendente|aguard|solicit|agend|marcad|previst|realizar|em\s+andamento"
    r"|\?|-|segunda|terca|quarta|quinta|sexta|sabado|domingo|\d)"
)
# Frase que diz que a resposta NÃO veio, ou que ainda é pedido/plano.
_NOT_ANSWERED = re.compile(
    r"aguard\w*|solicit\w*|\breiter\w*|\bpedid[oa]s?\b|\bpeco\b|\bpedimos\b|\bcobra\w*|\bpendente\w*|\bpendenc\w*"
    r"|\bainda\s+nao\b|\bnao\b|\bsem\s+(?:parecer|avaliacao|resposta|ser)\b|\bem\s+aberto\b|\bsera\b|\bamanha\b"
    r"|\bprogramad\w*|\bnecessit\w*|\bnecessari\w*|\bfalta\b|\bprecis\w*|\bindico\b|\bindicad[oa]\b|\bagend\w*"
    r"|\bmarcad[oa]\b|\bprevist[oa]\b|\bdever\w*|\bficou\s+de\b|\bfica\s+de\b|\ba\s+ser\b|\bfeito\b|\benviad\w*"
    r"|\bencaminh\w*|\bandamento\b|\ba\s+definir\b|\?|\bdiscutir\b|\bconduta\s*:|\bplano\s*:|\bcd\s*:"
    r"|\bsuspens\w*|\bcancelad\w*|\bdispensad\w*"
)
# Quem é FONTE de uma ordem não está sendo esperado: "USG solicitado pela
# nefrologia", "conforme nefrologia" (2ª revisão: a menção a pedido do
# próprio especialista punha a interconsulta respondida em verificação).
_ORDER_SOURCE_BEFORE = re.compile(
    r"(?:solicitad[oa]s?|pedid[oa]s?|indicad[oa]s?|sugerid[oa]s?|orientad[oa]s?|prescrit[oa]s?)\s+pel[ao]s?\s+"
    r"(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$"
    r"|\b(?:conforme|segundo|de\s+acordo\s+com|orientacao\s+d[ao]|discutido\s+com)\s+(?:(?:a|o)\s+)?"
    r"(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$"
)

# --- cancelamento ("Exame suspenso por mudança da estratégia terapêutica",
# exemplo do auditor): a palavra tem de valer pro PRÓPRIO exame/parecer --
# "suspensa dieta para TC" é preparo, não cancelamento da TC. Suspensão
# temporária ("reagendar", "assim que estável") não é cancelamento.
_CANCEL_WORD = r"(?:suspens|cancelad|dispensad)[oa]s?"
_CONSULT_NOUN_BEFORE = (
    r"(?:avaliacao|parecer|interconsulta|ic)\s+(?:d[aeo]s?\s+|com\s+(?:a\s+|o\s+)?)?(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$"
)
_CONSULT_CANCEL_BEFORE = re.compile(
    _CANCEL_WORD + r"\s+(?:a\s+|o\s+)?(?:solicitacao\s+d[eao]\s+)?" + _CONSULT_NOUN_BEFORE
    + r"|\bsem\s+(?:mais\s+)?necessidade\s+de\s+" + _CONSULT_NOUN_BEFORE
)
_CONSULT_NOUN_ONLY_BEFORE = re.compile(_CONSULT_NOUN_BEFORE)
_CANCEL_AFTER = re.compile(r"^(?:\s+d[eao]\s+\w+)?\s+(?:foi\s+)?" + _CANCEL_WORD + r"\b")
# "Conforme avaliação da nefrologia suspensa A VANCOMICINA": a palavra de
# cancelamento é de outro objeto (3ª revisão).
_CANCEL_OF_OTHER_OBJECT = re.compile(
    r"^(?:\s+d[eao]\s+\w+)?\s+(?:foi\s+)?" + _CANCEL_WORD + r"\s+(?:a|o|as|os)\s+"
    r"(?!avaliacao|parecer|interconsulta|ic\b|solicitacao)\w+"
)
_EXAM_CANCEL_BEFORE = re.compile(
    _CANCEL_WORD + r"\s+(?:a\s+|o\s+)?(?:(?:realizacao|solicitacao|pedido)\s+d[aeo]s?\s+)?$"
)
_EXAM_NOT_DONE_AFTER = re.compile(r"^[^,.;]{0,30}\bnao\s+sera\s+(?:mais\s+)?realizad")
# Suspensão que é ADIAMENTO, troca de quem faz, pedido em duplicidade,
# condição, pergunta ou negação do próprio cancelamento -- nunca
# cancelamento (2ª e 3ª revisões: "suspensa por ora", "em duplicidade",
# "caso mantenha boa diurese, sem necessidade...", "cancelada?", "não foi
# cancelado" viravam canceladas).
_POSTPONED = re.compile(
    r"\breagend\w*|\bremarc\w*|\breprogram\w*|\badiad\w*|\bassim\s+que\b|\bate\b|\bquando\b|\bhoje\b"
    r"|\btemporari\w*|\brealizar\b|\baguard\w*|\bnova\s+data\b|\bfalta\s+de\b|\bapos\b|\bamanha\b"
    r"|\bsem\s+condic\w*|\binstabilidade\b|\binstavel\b|\brepetir\b|\bremarcar\b|\bpor\s+ora\b|\bpor\s+enquanto\b"
    r"|\bno\s+momento\b|\bduplicidad\w*|\bduplicata\b|\bduplicad[oa]\b|\bsera\b|\bserao\b|\breavaliar\b"
    r"|\boutra\s+equipe\b|\bpel[ao]\s+(?:equipe|cirurgia|servico)\b|\bsem\s+contraste\b|\bcom\s+contraste\b"
    r"|\bagitac\w*|\bjejum\b|\bvaga\b|\btransporte\b|\bnovo\s+pedido\b|\bnova\s+solicitac\w*"
    r"|\bse\b|\bcaso\b|\?|\b(?:re)?discutir\b|\bconfirmar\b|\bavaliar\s+necessidade\b|\bquestion\w*"
    r"|\bnao\s+(?:foi\s+|sera\s+|esta\s+)?(?:cancelad|suspens|dispensad)\w*"
)
# Cancelamento DEFINITIVO só com o motivo escrito (exemplo do auditor:
# "suspenso por mudança da estratégia terapêutica"). Sem ele: verificação.
# O motivo escrito na forma negativa já é a negação ("sem indicação").
_DEFINITIVE_NEGATIVE_FORM = re.compile(
    r"\bnao\s+(?:ha|havera)\s+mais\s+(?:indicac\w*|necessidade)|\bsem\s+(?:mais\s+)?(?:indicac\w*|necessidade)\b"
    r"|\bnao\s+(?:esta\s+|e\s+)?mais\s+(?:indicad\w*|necessari\w*)|\bdesnecessari\w*"
    r"|\bnao\s+sera\s+mais\s+(?:realizad|necessari|feit)\w*"
)
# Motivo na forma positiva: não vale negado ("SEM melhora clínica", "não
# houve melhora" -- 3ª revisão).
_DEFINITIVE_POSITIVE_FORM = re.compile(
    r"\bmudanca\s+(?:d[aeo]\s+)?(?:estrategia|conduta|plano|programac\w*)"
    r"|\bmelhora\s+(?:clinica|do\s+quadro|importante|significativa)|\bresolucao\s+d[oa]\s+quadro|\bquadro\s+resolvid\w*"
    r"|\bobito\b|\balta\s+(?:hospitalar|medica)\b|\brecus\w*|\bdesist\w*|\bcontraindicad\w*"
    r"|\bcuidados\s+paliativos\s+exclusivos\b|\blimitac\w*\s+(?:de\s+)?(?:suporte|esforco|terapeutic\w*)"
)
_NEGATED_BEFORE = re.compile(r"\b(?:sem|nao|nem|ausencia\s+de|nao\s+houve|sem\s+qualquer)\s+(?:\w+\s+){0,2}$")


# "Sem necessidade/indicação": só vale fechando a oração ("sem necessidade.",
# "sem indicação, por melhora") ou com o PRÓPRIO pedido como objeto -- "sem
# necessidade de transferir", "sem indicação de sedação" (4ª revisão), "sem
# indicação formal de...", "sem indicação cirúrgica", "sem necessidade de TC"
# num ECO cancelado (5ª revisão) vão para verificação. `about(objeto)` diz se
# o objeto é o pedido.
_REASON_ENDS = re.compile(r"^\s*(?:[.,;:!?)]|$|\b(?:por|pois|devido|apos|no\s+momento|neste\s+momento)\b)")
_REASON_OBJECT = re.compile(r"^\s*(?:de|da|do|das|dos|para|p/|pra)\s+(?:(?:o|a|os|as|um|uma|nov[oa])\s+)?")


def _no_object(_text: str) -> bool:
    return False


def _negative_reason_about_item(folded: str, match: re.Match, about) -> bool:
    tail = folded[match.end():match.end() + 60]
    if _REASON_ENDS.match(tail):
        return True
    obj = _REASON_OBJECT.match(tail)
    if obj is None:
        return False  # adjetivo/advérbio depois ("sem indicação formal de sedação")
    return bool(about(re.split(r"[,;.:]", tail[obj.end():], maxsplit=1)[0].strip()))


def _has_definitive_reason(folded: str, about=_no_object) -> bool:
    if any(_negative_reason_about_item(folded, match, about) for match in _DEFINITIVE_NEGATIVE_FORM.finditer(folded)):
        return True
    return any(not _NEGATED_BEFORE.search(folded[max(0, match.start() - 30):match.start()])
               for match in _DEFINITIVE_POSITIVE_FORM.finditer(folded))


_BARE_EXAM_OBJECT = re.compile(r"^(?:exame|realiz\w*|fazer|repetir)(?:\s+(?:o|a|do|da)?\s*(?:exame|mesm[oa]))?$")
_CONSULT_OBJECT = re.compile(r"^(?:(?:re)?avaliac\w*|parecer\w*|interconsulta|ic\b|acompanhament\w*|seguimento"
                             r"|especialidade|equipe)")
_PUNCTURE_OBJECT = re.compile(r"^(?:nov[oa]\s+)?(?:acesso|puncao|procedimento|passagem|cateter)\b")


def _exam_object(exam: str):
    def about(obj: str) -> bool:
        found = exam_mentions(obj)
        if found:
            return any(label == exam for _start, _end, label, _region in found)
        return _BARE_EXAM_OBJECT.match(obj) is not None
    return about


def _consult_object(target: str):
    def about(obj: str) -> bool:
        labels = [label for _start, _end, label in specialties.specialty_spans(obj)]
        if labels:
            return target in labels
        return _CONSULT_OBJECT.match(obj) is not None
    return about


def _puncture_object(target: str):
    pattern = _PUNCTURE_SUCCESS_TYPE.get(target)

    def about(obj: str) -> bool:
        if pattern is not None and pattern.search(obj):
            return _typed_ok(target, obj)
        return pattern is None and _PUNCTURE_OBJECT.match(obj) is not None
    return about


# --- exame
# Vocabulário de RESULTADO de exame de imagem/endoscopia: achado normal,
# achado patológico, verbo de laudo. Só vale ligado ao PRÓPRIO exame (na
# oração dele ou depois dos dois-pontos dele) -- a 2ª revisão mostrou "sem
# sinais de desconforto respiratório", "RX de tórax sem alterações" (outro
# exame) e "função renal normal" fechando a TC de crânio.
_FINDING_CORE = (
    r"\bsem\s+(?:novas?\s+|outras?\s+)?(?:sinais\s+de\s+|evidencias?\s+de\s+)?(?:alteraco\w*|anormalidade\w*|achados"
    r"|particularidades|lesoes?|fraturas?|sangramentos?|hemorragia\w*|isquemia\w*|pneumotorax|derrames?|consolidac\w*"
    r"|colec\w*|hidronefrose|tromb\w*|tvp|estenose\w*|vegetac\w*|desvio\w*|dilatac\w*|obstruc\w*|calculos?|\w*litiase"
    r"|massas?|nodulos?|edema|efusao|liquido\s+livre|pneumoperitonio|perfurac\w*|hematoma\w*|avc|hidrocefalia"
    r"|infarto\w*)\b"
    r"|\bs/\s*alt\w*|\bnormal\b|\bnormais\b|\bdentro\s+da\s+normalidade\b|\bndn\b|\bnada\s+digno\s+de\s+nota\b"
    r"|\binalterad\w*|\bnegativ[oa]s?\s+para\b|\bfe\s*(?:de\s*)?\d|\bfracao\s+de\s+ejecao\b"
    r"|\bhematoma\w*|\bhemorragi\w*|\bsangramento\w*|\bisquemi\w*|\binfarto\w*|\bavc\w*|\bhipodens\w*|\bhiperdens\w*"
    r"|\bedema\b|\bderrame\w*|\bconsolidac\w*|\batelectasia\w*|\bpneumotorax\b|\bhidrotorax\b|\bnodul\w*|\bmassa\w*"
    r"|\btumor\w*|\bneoplas\w*|\blesao\b|\blesoes\b|\bfratura\w*|\bluxac\w*|\bcolec\w*|\babscess\w*|\bhidronefrose\b"
    r"|\b\w*litiase\b|\bcalculo\w*|\btrombo\w*|\btvp\b|\btep\b|\bembolia\b|\bestenose\w*|\boclus\w*|\bobstruc\w*"
    r"|\bdilatac\w*|\bespessamento\w*|\besteatose\b|\bcirrose\b|\bhepatomegalia\b|\besplenomegalia\b|\bascite\b"
    r"|\bapendicite\b|\bcolecistite\b|\bpancreatite\b|\bdiverticul\w*|\bperfurac\w*|\bpneumoperitonio\b|\bulcera\w*"
    r"|\b\w*gastrite\b|\besofagite\b|\bvarizes\b|\bpolipo\w*|\bhernia\w*|\bvegetac\w*|\bdisfunc\w*|\bhipocinesia\b"
    r"|\bacinesia\b|\binsuficiencia\b|\bregurgitac\w*|\baneurisma\w*|\bdisseccao\b|\bplacas?\b|\bateroma\w*"
    r"|\bsinusopatia\b|\bsinusite\b|\bpneumonia\b|\binfiltrado\w*|\bopacidade\w*|\bcardiomegalia\b|\bcongest\w*"
    r"|\bcalcific\w*|\bcisto\w*|\bmetasta\w*|\bcarcinoma\w*|\badenoma\w*|\bhipertrofia\b|\batrofia\b|\bgliose\b"
    r"|\bmicroangiopatia\b|\bleucoaraiose\b|\bhidrocefalia\b|\bvelamento\b|\bhipotransparencia\b"
    r"|\bdistensao\s+de\s+alcas\b|\bniveis\s+hidroaereos\b|\bnefropatia\b|\bliquido\s+livre\b|\bvidro\s+fosco\b"
    r"|\bpervi[oa]s?\b|\bcompressive(?:l|is)\b|\bhipertensao\s+pulmonar\b|\bpsap\s*\d"
)
# Verbos de laudo e qualificadores soltos: valem na oração do exame, nunca
# na oração/frase SEGUINTE a "exame realizado" (3ª revisão: "mostrou-se
# colaborativo", "saturando 94%", "na presença de familiar" fechavam).
_FINDING_WEAK = (
    r"|\bevidenci\w*|\bmostr(?:a|ou|ando)\b|\bdemonstr\w*|\brevel(?:a|ou)\b|\bidentific(?:a|ou|ado|ada)\b"
    r"|\bvisualiz\w*|\bconstat\w*|\bcompativel\s+com\b|\bsugestiv[oa]\s+de\b|\b\d{1,3}\s*%"
    r"|\bpreservad[oa]s?\b|\baumentad[oa]s?\b|\breduzid[oa]s?\b|\bausencia\s+de\b|\bpresenca\s+de\b"
)
_FINDING = re.compile(_FINDING_CORE + _FINDING_WEAK)
_FOLLOWING_FINDING = re.compile(_FINDING_CORE)
# Indicação/plano escrito junto do exame não é achado: "TC de crânio: para
# descartar hematoma", "(suspeita de AVC)", "investigar sangramento".
_INDICATION = re.compile(
    r"\b(?:para|p/|pra)\s+(?:descartar|afastar|avaliar|reavaliar|investigar|pesquisa\w*|excluir|controle|elucida\w*"
    r"|complementar)|\bdescartar\b|\bafastar\b|\breavaliar\b|\binvestiga\w*|\bpesquisa\s+de\b|\bsuspeita\s+de\b"
    r"|\bhipotese\s+de\b|\?|\bse\s+(?:piora|necessario|houver)\b"
)
# Logo depois do exame (e da região): o MOTIVO dele, não o resultado --
# "Doppler de MMII POR edema", "TC de controle DO hematoma", "Urocultura de
# controle (ITU por E. coli)" (3ª revisão).
_INDICATION_AFTER_EXAM = re.compile(
    r"^(?:\s+(?:de|do|da|dos|das)\s+[a-z]+(?:\s+(?:e|de|do|da)\s+[a-z]+)?)?(?:\s+(?:de\s+)?controle)?\s*"
    r"(?:\(|\b(?:por|devido|pois|para|p/|pra)\b|\bd[aeo]s?\s+(?:\w+\s+)?(?:hematoma|consolidac\w*|derrame|abscess\w*"
    r"|colec\w*|massa|lesao|nodulo|fratura|avc|tromb\w*|tvp|pneumonia|infecc\w*|bacteremia|itu))"
)
# Outro exame dentro da oração da cultura: o resultado é dele ("Urocultura:
# EAS com nitrito positivo").
_OTHER_TEST = re.compile(
    r"\beas\b|\burina\s+(?:tipo\s+)?(?:1|i)\b|\bsumario\s+de\s+urina\b|\bnitrito\b|\bleucocit\w*|\bpcr\b"
    r"|\bprocalcitonina\b|\bgasometria\b|\bhemograma\b"
)
_CULTURE_RESULT = re.compile(
    r"\bnegativ\w*|\bpositiv\w*|\bisolad[oa]s?\b|\bsem\s+crescimento\b|\bcrescimento\s+de\b|\bufc\b|\bsensivel\b"
    r"|\bresistente\b|\b[a-z]\.\s*[a-z]{3,}|\b(?:staphylococ|klebsiell|escherichi|pseudomon|enterococ|candida"
    r"|acinetobact|proteus|streptococ|enterobact|serratia|e\s+coli|s\s+aureus)\w*"
)
# Cultura PARCIAL ou em curso: no máximo verificação (2ª revisão: "positiva,
# em identificação" e "negativa em 48 horas" fechavam).
_CULTURE_PARTIAL = re.compile(
    r"\bem\s+identificac\w*|\bidentificac\w*\s+(?:em\s+andamento|pendente)|\baguard\w*\s+(?:a\s+|o\s+)?"
    r"(?:identificac\w*|antibiograma|tsa)|\bgram\b|\bcocos\b|\bbacilos\b|\bleveduras?\b|\bparcia\w*|\bpreliminar\w*"
    r"|\b(?:com|em|apos|ate)\s+(?:\d{1,2}\s*(?:h|hs|horas)\b|[1-3]\s*dias?\b)|\b\d{1,2}\s*(?:h|horas)\s+de\s+incubac"
    r"|\bincubac\w*|\bem\s+curso\b|\bem\s+andamento\b|\bate\s+o\s+momento\b|\bsem\s+tsa\b|\btsa\s+pendente\b"
    r"|\bantibiograma\s+pendente\b|\benviad\w*|\bcolhid\w*|\bcoletad\w*"
)
# Anatomopatológico: só o DIAGNÓSTICO do patologista, com marcador explícito
# ("Biópsia: adenocarcinoma", "AP evidenciou carcinoma"). A descrição da
# lesão na nota do procedimento ("lesão sugestiva de neoplasia, material
# enviado") não é resultado (2ª revisão).
_AP_DIAGNOSIS = re.compile(
    r"\b(?:adeno)?carcinoma\w*|\blinfoma\w*|\bsarcoma\w*|\bmelanoma\w*|\bdisplasia\w*|\bmetaplasia\w*|\bbenign\w*"
    r"|\bneoplasia\s+(?:maligna|benigna)\b|\bnegativ[oa]\s+para\s+(?:malignidade|neoplasia)"
    r"|\bausencia\s+de\s+(?:malignidade|neoplasia|atipias?)|\bsem\s+(?:sinais\s+de\s+)?(?:malignidade|atipias?|neoplasia)"
    r"|\bprocesso\s+inflamatorio\s+cronico\b|\binflamac\w*\s+cronic\w*|\bgranulom\w*|\btuberculose\b|\bhiperplasia\w*"
    r"|\badenoma\w*|\bleiomioma\w*|\blipoma\w*|\bmetasta\w*|\bgist\b"
)
_AP_RESULT_VERB = re.compile(r"^\s*(?:evidenciou|revelou|mostrou|demonstrou|confirmou|com\s+(?:laudo\s+de\s+)?)")
_AP_PENDING = re.compile(
    r"\benviad\w*|\bencaminhad\w*|\bem\s+analise\b|\baguard\w*|\bpendente\w*|\bsugestiv\w*|\bsuspeit\w*|\baspecto\b"
    r"|\brealizad[oa]\s+(?:a\s+)?(?:biopsia|eda|colonoscopia|broncoscopia)|\bbiopsiad\w*|\bcolhid\w*|\bcoletad\w*"
)
# Ausência de resultado, resultado parcial, cancelamento e linha de plano na
# FRASE inteira: nunca é resultado.
_EXAM_PENDING_SENTENCE = re.compile(
    r"\bem\s+andamento\b|\bparcia\w*|\bpreliminar\w*|\bsem\s+(?:laudo|resultado)\b|\bainda\s+(?:nao|sem)\b"
    r"|\bnao\s+(?:foi\s+|esta\s+|consta\s+)?(?:realizad\w*|disponivel|disponibilizad\w*|localizad\w*"
    r"|encontrad\w*|liberad\w*|anexad\w*|emitid\w*|saiu|consta)\b|\bindisponivel\b|\bnao\s+ha\s+(?:laudo|resultado)"
    r"|\bcancelad\w*|\bsuspens\w*|\bconduta\s*:|\bcd\s*:|\bplano\s*:|\bresultado\s+em\b|\bate\s+o\s+momento\b"
    r"|\binterromp\w*|\babortad\w*|\bnao\s+tolerou\b|\bnao\s+colaborou\b"
    r"|\ba\s+liberar\b|\ba\s+ser\s+(?:emitid|liberad|realizad|feit|laudad)\w*|\bem\s+(?:confecc|processa|digita|analise)\w*"
    r"|\baguard\w*\s+(?:o\s+|a\s+)?(?:laudo|resultado)"
)
# Pedido/plano/espera na ORAÇÃO do próprio exame (3ª revisão: "Repetir TC de
# crânio de controle do hematoma", "Providenciar TC..."; e um pedido NOVO de
# outra coisa na mesma frase não apaga o resultado: "TC de crânio com AVC,
# solicitado parecer da neurologia").
_EXAM_PENDING_CLAUSE = re.compile(
    r"aguard\w*|solicit\w*|\bpendente\w*|\bpendenc\w*|agend\w*|program\w*|\bmarcad[oa]\b|\bprevist[oa]\b"
    r"|\bpedid[oa]\b|\brealizar\b|\bfazer\b|\bsera\b|\bamanha\b|\bcobr\w*|\bverific\w*|\bchec\w*|\bver\b"
    r"|\bcolhid\w*|\bcoletad\w*|\breagend\w*|\bremarc\w*|\benviad\w*|\bencaminhad\w*|\brepetir\b|\bnecessit\w*"
    r"|\bnecessari\w*|\bprovidenci\w*|\blevar\b|\bcolher\b|\bcoletar\b|\bpedir\b|\bencaminhar\b|\bseguir\s+com\b"
    r"|\bpeco\b|\bpedimos\b|\bindicad[oa]\s+(?:a\s+|o\s+)?(?:tc|tomografia|rx|eco|usg|rm|exame)"
)
# A frase diz que o resultado NÃO está lá -- com "realizado": verificação.
_RESULT_ABSENT = re.compile(
    r"\bnao\s+(?:foi\s+|esta\s+|consta\s+)?(?:disponivel|disponibilizad\w*|localizad\w*|encontrad\w*|liberad\w*"
    r"|anexad\w*|emitid\w*|saiu|consta)\b|\bindisponivel\b|\bsem\s+(?:laudo|resultado)\b|\bnao\s+ha\s+(?:laudo|resultado)"
    r"|\bainda\s+sem\s+(?:laudo|resultado)\b|\bate\s+o\s+momento\b|\baguard\w*\s+(?:o\s+|a\s+)?(?:laudo|resultado)"
    r"|\ba\s+liberar\b|\ba\s+ser\s+(?:emitid|liberad|laudad)\w*|\bem\s+(?:confecc|processa|digita|analise)\w*"
    r"|\b(?:laudo|resultado)\s+pendente\b|\bpendente\s+(?:o\s+|a\s+)?(?:laudo|resultado)\b"
)
_EXAM_DONE =re.compile(r"\brealizad[oa]s?\b|\brealizou\b")
_DONE_BEFORE_EXAM = re.compile(r"\b(?:realizad[oa]s?|realizou|feit[oa]s?)\s+(?:\w+\s+){0,2}$")
# Frase seguinte que é RESULTADO ("TC de abdome realizada. Sem alterações
# agudas."): rótulo de laudo com achado, ou achado de exame logo no começo
# -- nunca exame físico, nem "sem sinais de desconforto respiratório".
_RESULT_LABEL = re.compile(r"^\s*(?:laudo|resultado|conclusao|impressao|achados?)\b\s*:?\s*")
_RESULT_START = re.compile(
    r"^\s*(?:sem\s+(?:novas?\s+)?(?:alteraco\w*|anormalidade\w*|achados)|s/\s*alt\w*|dentro\s+da\s+normalidade"
    r"|nada\s+digno|ndn\b|normal\b"
    r"|sem\s+(?:sinais\s+de\s+|evidencias?\s+de\s+)?(?:sangramento|hemorragia|isquemia|fratura|lesao|lesoes|hematoma"
    r"|avc|desvio|hidrocefalia|pneumotorax|derrame|consolidac|colec|tromb|tvp|estenose|vegetac|obstruc|dilatac"
    r"|calculo|litiase|perfurac|pneumoperitonio|liquido\s+livre))"
)
_PHYSICAL_EXAM_START = re.compile(r"^\s*(?:ao\s+exame|exame\s+fisico|ef\b|neuro\w*\s*:|acv\b|ar\s*:|abd\w*\s*:|ssvv|sv\s*:)")
# Outro assunto na oração: o achado não é do exame ("realizada TC, paciente
# com padrão respiratório normal", "sem alterações hemodinâmicas durante o
# transporte", "retornou ao setor sem alterações" -- 3ª revisão).
_OTHER_SUBJECT = re.compile(
    r"\bpaciente\b|\bpcte?\b|\bneuro\s*:|\becg\b|\bacv\b|\bdiurese\b|\bdieta\b|\bsinais\s+vitais\b|\bexame\s+fisico\b"
    r"|\bao\s+exame\b(?!\s+(?:anterior\w*|previ[oa]s?)\b)|\bfuncao\s+renal\b|\bhemodinamic\w*|\btransporte\b|\bsatur\w*|\bspo2\b|\bvazao\b"
    r"|\bnoradrenalina\b|\bdva\b|\b(?:retorn|volt|encaminhad|levad)\w*\s+(?:a[o]?|para\s+o|para\s+a)\s+"
    r"(?:setor|leito|quarto|unidade|uti|enfermaria)\b|\bfamiliar\w*|\bacompanhante\b|\bcolaborativ\w*"
    r"|\bsitio\s+de\s+puncao\b|\bostio\b|\bcurativo\b|\bsacral\b|\bintercorrenc\w*|\bconsciente\b"
)
# O que pode vir entre o exame e os dois-pontos: região, data, "com/sem
# contraste", "realizada (hoje/ontem/em dd/mm)", "de controle".
_MAX_WORDS_BEFORE_COLON = 7
# Seção de plano numa linha só ("CONDUTA:", "PLANO:", "PENDÊNCIAS:"): os
# itens de lista abaixo dela são plano, não resultado (3ª revisão).
_PLAN_SECTION = re.compile(
    r"^[\s\-*•]*(?:conduta|condutas|plano|cd|pendencias?|planejamento|programacao|solicitac\w*|exames\s+solicitados"
    r"|a\s+fazer|solicito)\s*:?\s*$"
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d{1,2}\s*[.)-])")

# --- punção
# "PAI" (pressão arterial invasiva) só como dispositivo: depois de verbo de
# inserção ou com lugar ao lado (3ª revisão: "Pai orientado", "visita do pai"
# fechavam o pedido de PAI).
_PUNCTURE_NOUN = (
    r"(?:puncao|puncoes|acesso|cvc|picc|cateter|paracentese|toracocentese|avp|" + _PAI + r"|pl|liquor|shilley"
    r"|permcath|jelco|abocath|scalp|intracath|mahurkar)"
)
_FILLER = r"(?:(?:novo|nova|o|a|um|uma|de|do|da|passagem|insercao|implante|instalacao|puncao)\s+){0,3}"
# O verbo de sucesso tem de vir COLADO ao procedimento: "Realizada troca de
# curativo do CVC" não é punção. Terceira pessoa entra ("a equipe de punção
# realizou AVP" -- 2ª revisão). Particípio DEPOIS do dispositivo só com o
# sítio no meio ("CVC em VJID realizado com sucesso"), nunca outro objeto
# ("AVP em MSE realizada medicação") -- e "locado" descreve dispositivo que
# já estava lá (3ª revisão).
_SITE = r"(?:\s+(?:em|na|no|pela|pelo)\s+[a-z0-9]+(?:\s+[a-z0-9]+){0,3}?)?"
_PUNCTURE_SUCCESS = re.compile(
    r"\bpuncionad[oa]s?\b"
    r"|\b(?:realizad|passad|obtid|inserid|instalad|implantad)[oa]s?\s+" + _FILLER + _PUNCTURE_NOUN + r"\b"
    r"|\b(?:realizo|realizei|realizamos|realizou|realizaram|passo|passei|passamos|passou|passaram|puncionei"
    r"|puncionamos|puncionou|puncionaram|obteve|obtivemos|instalou|inseriu)\s+" + _FILLER + _PUNCTURE_NOUN + r"\b"
    r"|\b" + _PUNCTURE_NOUN + r"\b" + _SITE + r"\s+(?:foi\s+)?(?:com\s+(?:sucesso|exito)|realizad[oa]|passad[oa]"
    r"|obtid[oa]|puncionad[oa]|inserid[oa]|instalad[oa])\b(?!\s+(?:a\s+|o\s+)?(?:medicac|troca|coleta|curativo|lavagem"
    r"|flush|infusao|administrac))"
    # Forma nominal no começo da frase: "Passagem de CVC em VJID guiada por
    # USG, sem intercorrências" (caso 3, 3ª revisão).
    r"|^\s*(?:realizad[oa]\s+)?(?:passagem|insercao|implante|puncao)\s+de\s+(?:novo\s+|nova\s+)?"
    r"(?:cvc|picc|avp|shilley|permcath|cateter\s+\w+|acesso\s+\w+)\b[^.]*?\b(?:sem\s+intercorrenc\w*|com\s+(?:sucesso|exito)"
    r"|guiad[oa]\s+por\s+usg)"
)
# Expressões neutras retiradas antes da negação: "não houve intercorrências",
# "paciente não apresentou intercorrências" (3ª revisão).
_PUNCTURE_NEUTRAL = re.compile(
    r"\b(?:paciente\s+)?nao\s+(?:houve|apresentou|apresentando|ocorreram|ocorreu|teve|foram\s+observadas)\s+"
    r"intercorrenc\w*|\bsem\s+intercorrenc\w*"
)
# Negação, futuro, falha, motivo que não é acesso -- em QUALQUER ponto da frase.
_PUNCTURE_NEGATED = re.compile(
    r"\bnao\b|\bainda\s+sem\b|\bsem\s+(?:sucesso|exito|cvc|picc|acesso|avp|cateter)\b|\binsucesso\b"
    r"|\bfalh\w*|\bperd(?:a|id[oa]|eu)\b|\bretirad\w*|\bsacad\w*|\btracionad\w*|\bremovid\w*"
    r"|\bobstru\w*|\bdificil\b|\bdificuldade\b|\binfiltr\w*|\bflebite\b|\bflogistic\w*|\bhiperemia\b"
    r"|\bpurulent\w*|\bser\b|\bnecessari\w*|\bnecessidade\b|\bdever\w*|\bprecis\w*|\bfavor\b|\bcontraindic\w*"
    r"|\bgasometri\w*|\bsera\b|\bamanha\b|\bvira\b|\bavaliar\b"
)
# "Tentativa(s)" só nega quando não há sucesso explícito ("puncionado com
# sucesso após 2 tentativas" é sucesso -- 3ª revisão).
_ATTEMPT = re.compile(r"\btentativas?\b")
_ATTEMPT_SUCCEEDED = re.compile(
    r"\bcom\s+(?:sucesso|exito)\b|\b(?:em|na)\s+(?:primeira|segunda|terceira|\d+\S*)\s+tentativa\b"
    r"|\bapos\s+\w+\s+tentativas\b"
)
# Contexto de plano/manutenção: só vale na mesma ORAÇÃO do sucesso ("Passado
# CVC em VJID, solicitado RX de controle" é sucesso; "aguarda nova punção" não).
_PUNCTURE_PLAN_CLAUSE = re.compile(
    r"aguard\w*|solicit\w*|program\w*|agend\w*|necessit\w*|\bindicad[oa]\b|\bmantem\b|\bmantid[oa]\b"
    r"|\ba\s+seguir\b|\bem\s+seguida\b|\bposteriormente\b"
    r"|\bem\s+uso\s+de\b|\bportador\w*|\bcurativo\w*|\bcoleta\w*|\blavage\w*|\bsalinizad\w*|\bheparinizad\w*"
    r"|\bequipo\w*|\btroca\s+de\s+(?:equipo|curativo|fixac\w*|filtro|torneir\w*|extensor\w*)"
)
# Punção para outra coisa (coleta de exame, medicação): não é o acesso pedido.
_PUNCTURE_FOR_OTHER = re.compile(
    r"\bcolhid\w*|\bcoletad\w*|\bexames?\s+laborator\w*|\bhemocultur\w*|\bgasometri\w*|\bmedicac\w*|\bequipo\w*"
)
# Pedido de TROCA do dispositivo: só a troca feita fecha (3ª revisão).
_REPLACEMENT_REQUEST = re.compile(r"\btroca\b|\btrocar\b")
_REPLACEMENT_DONE = re.compile(
    r"\btrocad[oa]s?\b|\btroca\s+d[eo]\s+(?:cvc|cateter|acesso|picc|avp)\s+(?:foi\s+)?realizad|"
    r"\bnov[oa]\s+(?:cvc|cateter|acesso|puncao|picc|avp)\b"
)
# Contexto de família no pedido de PAI ("pai do paciente").
_FATHER_CONTEXT = re.compile(
    r"\bpai\s+(?:do|da)\s+paciente\b|\bvisita\b|\bboletim\b|\borientad\w*|\bfamiliar\w*|\btelefon\w*"
    r"|\bcontato\s+com\s+o\s+pai\b"
)
# Espera por OUTRA punção/acesso em qualquer ponto da frase: "Paciente com
# CVC passado no PS, aguarda Shilley para HD" não é o Shilley passado.
_PUNCTURE_WAITING_ANY = re.compile(
    r"\b(?:aguard\w*|program\w*|agend\w*|necessit\w*|solicit\w*|indicad[oa]|precis\w*)\s+(?:\w+\s+){0,3}?"
    r"(?:nov[oa]\s+)?(?:puncao|acesso|cvc|picc|avp|cateter|shilley|permcath|passagem|equipe|mahurkar|pai)\b"
)
_PUNCTURE_CANCEL = re.compile(
    _CANCEL_WORD + r"\s+(?:a\s+|o\s+)?(?:(?:passagem|realizacao|solicitacao)\s+d[eoa]s?\s+)?" + _FILLER
    + _PUNCTURE_NOUN + r"\b"
)
# Caso 3 do auditor: "a equipe de punção registrar atendimento realizado com
# sucesso" -- na MESMA frase que cita a equipe (ou logo abaixo do cabeçalho
# "EQUIPE DE PUNÇÃO"); nunca qualquer "realizado com sucesso" (SNE, banho,
# sondagem) numa nota que cita a equipe.
_TEAM_SUCCESS = re.compile(
    r"\b(?:atendimento|procedimento|puncao|acesso|avp|cvc|picc)\s+(?:foi\s+)?realizad[oa]\s+com\s+(?:sucesso|exito)\b"
    r"|\batendid[oa]\s+pel[ao]\s+(?:equipe|time)\s+d[aeo]\s+(?:puncao|acesso)\w*[^.;]{0,40}\b(?:acesso|avp|cvc|picc"
    r"|periferic\w*|jelco|abocath)\b"
)
# Depois do sucesso, na mesma frase ou na seguinte: a punção não ficou.
_TEAM_WAITING_AFTER = re.compile(
    r"\baguard\w*\s+(?:\w+\s+){0,3}?(?:nov[ao]\s+)?(?:equipe|puncao|acesso|avp|cvc|picc)\b|\bnao\s+compareceu\b"
    r"|\bvira[o]?\b|\bperd\w*\s+(?:d[oa]\s+)?(?:acesso|avp|puncao)|\bnova\s+puncao\b|\bsem\s+sucesso\b|\binsucesso\b"
)
_TEAM_HEADER = re.compile(r"^[\s\-*•]*(?:equipe|time|grupo|servico)\s+(?:d[aeo]\s+)?(?:puncao|puncoes|acesso\s+vascular|acessos)[\s:.\-]*$")


def _parse_local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _utc_to_local(iso_utc: str | None, local_tz: tzinfo | None) -> datetime | None:
    if not iso_utc:
        return None
    try:
        parsed = datetime.fromisoformat(iso_utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(local_tz).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _local_to_utc_iso(local: datetime, local_tz: tzinfo | None) -> str | None:
    """None quando o Windows não converte (ano < 1970 ou absurdo) -- quem
    chama trata como "sem data" (revisão: data 1958 copiada pela IA
    derrubava a análise do paciente)."""
    try:
        aware = local.replace(tzinfo=local_tz) if local_tz is not None else local.astimezone()
        return aware.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _aware(iso: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reference_time(evidence_date: str | None, created_at: str | None, local_tz: tzinfo | None = None) -> datetime | None:
    """A partir de quando uma evidência conta como POSTERIOR ao pedido (horário
    local, sem fuso; a evidência precisa ser > isto). Data sem hora vira o
    fim do dia. Sem data nenhuma: quando o sistema registrou a pendência.
    Data impossível (ano 9999) = sem referência -> o item fica como está."""
    parsed = _parse_local(evidence_date)
    if parsed is not None:
        if parsed.time() == datetime.min.time():
            try:
                return parsed + timedelta(days=1) - timedelta(microseconds=1)
            except OverflowError:
                return None
        return parsed
    return _utc_to_local(created_at, local_tz)


def resolution_time_utc(evidence_date: str | None, created_at: str | None, local_tz: tzinfo | None = None) -> str | None:
    """`resolved_at` (ISO UTC) a partir da data local da evidência de
    conclusão, nunca antes do registro da pendência. None sem data
    reconhecível ou conversível (quem grava usa "agora")."""
    parsed = _parse_local(evidence_date)
    if parsed is None:
        return None
    resolved_at = _local_to_utc_iso(parsed, local_tz)
    if resolved_at is None:
        return None
    created = _aware(created_at) if created_at else None
    resolved = _aware(resolved_at)
    if created is not None and resolved is not None and resolved < created:
        return created_at
    return resolved_at


def request_already_handled(
    request_evidence_date: str | None,
    resolution_evidence_date: str | None,
    resolved_at: str | None,
    local_tz: tzinfo | None = None,
) -> bool:
    """O pedido (`request_evidence_date`, horário local) é anterior ou igual
    ao encerramento de uma pendência com o mesmo alvo? Então já foi atendido
    -- não vira pendência nova. Encerramento = data da evidência que provou
    (detector/IA) ou, sem ela, o momento da decisão (auditor, legado). Pedido
    sem data não dá pra comparar: conta como atendido (nunca recria em loop)."""
    closure = _parse_local(resolution_evidence_date) or _utc_to_local(resolved_at, local_tz)
    if closure is None:
        return False
    request = _parse_local(request_evidence_date)
    if request is None:
        return True
    if request.time() == datetime.min.time():
        return request.date() <= closure.date()
    return request <= closure


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def _comparable(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text)).split())


# Data sem ano até um mês à frente da evolução é agendamento; mais que isso
# é do ano anterior ("15/12" numa evolução de agosto -- 2ª revisão).
_SCHEDULING_HORIZON = timedelta(days=31)


def _day_month(day: int, month: int, year: int | None, note_day: date) -> date | None:
    """dd/mm[/aaaa] -> data; None para data impossível ou agendamento."""
    try:
        if year is not None:
            return date(year + (2000 if year < 100 else 0), month, day)
        cited = date(note_day.year, month, day)
    except ValueError:
        return None
    if cited > note_day:
        if cited - note_day <= _SCHEDULING_HORIZON:
            return None  # data futura próxima: agendamento, não histórico
        try:
            return date(note_day.year - 1, month, day)
        except ValueError:
            return None
    return cited


def _append_shifted(found: list[date], note_day: date, days_back: int) -> None:
    try:
        found.append(note_day - timedelta(days=days_back))
    except OverflowError:
        pass


def _could_be_fraction(day: int, month: int) -> bool:
    """"2/2", "1/2", "3/4": numerador <= denominador <= 10 -- contagem de
    frascos/amostras, não data."""
    return day <= month <= 10


def cited_dates(folded: str, note_day: date) -> list[date]:
    """Datas citadas na frase -- dd/mm[/aaaa], dd.mm, (dd-mm), mm/aaaa (vira
    o último dia do mês), ano solto com contexto ("ECO 2025" -> 31/12/2025),
    "do dia 15", "15 de agosto", "ontem", "na sexta-feira", "semana passada"
    -- sem frações clínicas ("força 4/5", "PA
    18/10", "hemoculturas 2/2"). Sem ano: o ano da evolução, ou o anterior
    quando a data cairia mais de um mês no futuro; até um mês à frente é
    agendamento e fica de fora."""
    found: list[date] = []
    for match in _DATE_IN_TEXT.finditer(folded):
        groups = match.groups()
        day, month, year = (groups[0], groups[1], groups[2]) if groups[0] else (groups[3], groups[4], groups[5])
        before = folded[max(0, match.start() - 20):match.start()]
        after = folded[match.end():match.end() + 20]
        if _FRACTION_BEFORE.search(before) or _FRACTION_AFTER.match(after):
            continue
        if (not year and _could_be_fraction(int(day), int(month))
                and (_WEAK_FRACTION_BEFORE.search(before) or _WEAK_FRACTION_AFTER.match(after))):
            continue
        cited = _day_month(int(day), int(month), int(year) if year else None, note_day)
        if cited is not None:
            found.append(cited)
    for match in _DASH_DATE.finditer(folded):
        cited = _day_month(int(match.group(1)), int(match.group(2)),
                           int(match.group(3)) if match.group(3) else None, note_day)
        if cited is not None:
            found.append(cited)
    for match in _MONTH_YEAR.finditer(folded):
        month, year = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            # "validade 12/9999", "lote 06/0000": não é data -- e não pode
            # derrubar o detector do paciente toda noite (4ª revisão).
            try:
                last_day = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
            except (ValueError, OverflowError):
                continue
            if last_day - note_day <= _SCHEDULING_HORIZON:
                found.append(last_day)
    for match in _RELATIVE_DAY.finditer(folded):
        word = match.group(1) or match.group(2)
        _append_shifted(found, note_day, _RELATIVE_OFFSET[word] if word else 7)
    for match in _WEEKDAY_CITED.finditer(folded):
        weekday = _WEEKDAYS.index(match.group(1) or match.group(2))
        _append_shifted(found, note_day, (note_day.weekday() - weekday) % 7 or 7)
    for match in _BARE_YEAR.finditer(folded):
        year = int(match.group(1))
        if year < note_day.year:
            found.append(date(year, 12, 31))
    for match in _DAY_OF_MONTH_NAME.finditer(folded):
        cited = _day_month(int(match.group(1)), _MONTHS.index(match.group(2)) + 1,
                           int(match.group(3)) if match.group(3) else None, note_day)
        if cited is not None:
            found.append(cited)
    for match in _DAY_ONLY.finditer(folded):
        day = int(match.group(1))
        if not 1 <= day <= 31:
            continue
        month, year = note_day.month, note_day.year
        if day > note_day.day:  # "do dia 28" numa evolução do dia 3: mês anterior
            month, year = (12, year - 1) if month == 1 else (month - 1, year)
        try:
            found.append(date(year, month, day))
        except ValueError:
            continue
    return found


@lru_cache(maxsize=30_000)
def _cited_dates_cached(folded: str, note_day: date) -> tuple[date, ...]:
    """`cited_dates` em cache: evolução copiada repete as mesmas frases, e o
    detector testa a mesma frase para cada pendência (3ª revisão: era o
    maior custo do detector numa internação longa)."""
    return tuple(cited_dates(folded, note_day))


def _cites_date_before(folded: str, note_time: datetime, reference: datetime) -> bool:
    """Frase que cita uma data anterior ao pedido é histórico."""
    return any(cited < reference.date() for cited in _cited_dates_cached(folded, note_time.date()))


# Contador de dias que a cópia diária atualiza ("DIH 5" -> "DIH 6", "D3 de
# meropenem" -> "D4"): fora da chave de cópia (3ª revisão -- a frase copiada
# com o contador novo contava como evidência nova).
_DAY_COUNTER = re.compile(
    r"\b(?:d|dih|dpo|po|pod|dia)\s?\d{1,3}\b|\b\d{1,3}\s?(?:o\s+)?(?:dia|dih|dpo)\b|\bd\d{1,3}\b"
)


def _mask_counters(comparable: str) -> str:
    return _DAY_COUNTER.sub("#", comparable)


def clear_caches() -> None:
    """Libera os caches por frase no fim de uma execução (memória do PC fraco)."""
    _sentence_features.cache_clear()
    _cited_dates_cached.cache_clear()
    is_history.cache_clear()


def _snippet(text: str) -> str:
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= _EVIDENCE_MAX_CHARS else one_line[: _EVIDENCE_MAX_CHARS - 1] + "…"


@dataclass
class _Sentence:
    original: str
    folded: str
    comparable: str
    masked: str = ""  # comparável sem contador de dias (chave de cópia)
    specialty_spans: list = field(default_factory=list)
    exam_spans: list = field(default_factory=list)  # (início, fim, exame)
    exam_regions: list = field(default_factory=list)  # regiões de CADA exame (mesma ordem), tupla
    regions: list = field(default_factory=list)
    in_plan: bool = False  # item de lista debaixo de "CONDUTA:"/"PLANO:"


@dataclass
class _NoteView:
    """Uma evolução pré-processada uma vez só (o detector roda pra todo
    paciente em toda execução, ~200 pacientes no hardware fraco; a revisão
    mediu 8 s por paciente de internação longa sem esse cache)."""
    time: datetime
    note: DetectorNote
    role_specialty: str | None
    pela: tuple  # blocos "PELA <especialidade>" (specialties.PelaBlock)
    sentences: list[_Sentence]
    folded_text: str
    header_blocks: list | None = None  # cabeçalhos de especialidade sem "pela", calculados na 1ª vez


# Região de um exame: a que vem LOGO depois dele, antes de vírgula/dois-pontos,
# do primeiro achado e do próximo exame (2ª revisão: "TC de crânio sem
# sangramento, mantém dreno de tórax" fechava a TC de tórax). "TC: crânio sem
# alterações" -- a região logo depois dos dois-pontos também vale.
_SPAN_REGION_WINDOW = 60
_CONTRAST = re.compile(r"\b(?:com|sem)\s+contraste(?:\s+(?:venoso|oral|iodado|endovenoso|ev))?\b")
_REGION_WINDOW_STOP = re.compile(
    r"[;:]|\b(?:sem|com|normal|evidenci\w*|mostr\w*|apresent\w*|realizad\w*|negativ\w*|positiv\w*)\b"
)
# Entre duas regiões de uma lista: "de crânio, tórax e abdome".
_REGION_LIST_GLUE = re.compile(r"^\s*(?:,|\be\b)\s*(?:(?:de|do|da|dos|das)\s+)?$")


def listed_regions(folded: str, end: int, stop: int, exam: str) -> list[str]:
    """Região(ões) logo depois de um exame, antes de dois-pontos, do primeiro
    achado e do próximo exame. Lista conta ("TC de crânio, tórax e abdome"
    -- 3ª revisão). "TC: crânio sem alterações" -- a região logo depois dos
    dois-pontos também vale."""
    if exam in _EXAMS_WITHOUT_REGION:
        return []
    window = folded[end:min(stop, end + _SPAN_REGION_WINDOW)]
    window = _CONTRAST.sub(lambda match: " " * len(match.group(0)), window)
    colon = re.match(r"\s*:\s*", window)
    if colon:
        window = window[colon.end():colon.end() + 25]
    cut = _REGION_WINDOW_STOP.search(window)
    if cut:
        window = window[:cut.start()]
    spans = label_spans(_region_patterns(exam), window)
    if not spans or window[:spans[0][0]].count(",") > 0:
        return []
    regions = [spans[0][2]]
    previous_end = spans[0][1]
    for start, span_end, label in spans[1:]:
        if not _REGION_LIST_GLUE.match(window[previous_end:start]):
            break
        if label not in regions:
            regions.append(label)
        previous_end = span_end
    return regions


def _span_regions(folded: str, exam_spans) -> list[tuple[str, ...]]:
    """Regiões de cada exame da frase (a primeira é a principal)."""
    result: list[tuple[str, ...]] = []
    for index, (start, end, exam) in enumerate(exam_spans):
        stop = exam_spans[index + 1][0] if index + 1 < len(exam_spans) else len(folded)
        result.append(tuple(listed_regions(folded, end, stop, exam)))
    return result


@lru_cache(maxsize=30_000)
def _sentence_features(folded: str) -> tuple[tuple, tuple, tuple, tuple]:
    """Especialidades, exames, região de cada exame e regiões da frase -- em
    cache: evolução copiada de um dia pro outro repete as mesmas frases."""
    exam_spans = tuple(label_spans(_EXAM_COMPILED, folded))
    return (
        tuple(specialties.specialty_spans(folded)),
        exam_spans,
        tuple(_span_regions(folded, exam_spans)),
        tuple(_labels(_REGION_COMPILED, folded)),
    )


_SECTION_LINE = re.compile(r"^\s*[-*•]?\s*[a-z][a-z /]{2,30}:\s*$")


def _view(note_time: datetime, note: DetectorNote) -> _NoteView:
    sentences = []
    in_plan = False
    for line in (note.text or "").split("\n"):
        folded_line = fold(line)
        if not folded_line.strip():
            in_plan = False
            continue
        if _PLAN_SECTION.match(folded_line):
            in_plan = True
            continue
        if _SECTION_LINE.match(folded_line):
            in_plan = False
        for original in _sentences(line):
            folded = fold(original)
            specialty_spans, exam_spans, exam_regions, regions = _sentence_features(folded)
            comparable = _comparable(original)
            sentences.append(_Sentence(
                original=original, folded=folded, comparable=comparable, masked=_mask_counters(comparable),
                specialty_spans=list(specialty_spans), exam_spans=list(exam_spans), exam_regions=list(exam_regions),
                regions=list(regions), in_plan=in_plan,
            ))
    return _NoteView(
        time=note_time, note=note, role_specialty=specialties.specialty_from_role(note.role),
        pela=specialties.pela_blocks(note.text), sentences=sentences, folded_text=fold(note.text),
    )


@dataclass(frozen=True)
class _Finding:
    action: str  # ACTION_RESOLVE | ACTION_CANCEL | ACTION_REVIEW
    evidence: str
    why: str
    # Frases/linhas (forma comparável) que serviram de prova: ficam fora da
    # checagem de conflito na MESMA evolução -- o resto dela não (2ª revisão:
    # "PELA NEFRO: sem indicação de HD. CONDUTA: reitero parecer da nefro"
    # fechava).
    keys: frozenset = frozenset()


_HISTORY_SECTION = re.compile(r"^[\s\-*•]*[a-z ]{3,30}:\s*$")


@dataclass
class _Context:
    """O que vale para um item: a partir de quando, que frases já existiam
    antes do pedido (cópia de evolução anterior não é evidência nova), desde
    quando cada especialidade evolui o paciente e o texto do pedido."""
    reference: datetime
    seen_before: dict[str, datetime]  # frase comparável (e sem contador) -> primeira vez que apareceu
    role_first: dict[str, datetime] = field(default_factory=dict)  # especialidade do cargo -> 1ª evolução
    pela_first: dict[str, datetime] = field(default_factory=dict)  # especialidade -> 1º bloco "PELA" dela
    item_text: str = ""  # descrição do pedido, dobrada ("troca de CVC")

    def _copied(self, *keys: str) -> bool:
        for key in keys:
            first = self.seen_before.get(key) if key else None
            if first is not None and first <= self.reference:
                return True
        return False

    def is_stale(self, sentence: _Sentence, view: _NoteView) -> bool:
        if self._copied(sentence.comparable, sentence.masked):
            return True  # cópia (mesmo com o contador de dias trocado) de frase anterior ao pedido
        if is_history(sentence.folded):
            return True
        return _cites_date_before(sentence.folded, view.time, self.reference)

    def block_is_stale(self, block, view: _NoteView) -> bool:
        """Bloco "PELA X": cópia do bloco INTEIRO (cabeçalho + conteúdo, com
        ou sem o contador de dias trocado), ou cabeçalho datado antes do
        pedido/numa seção de histórico. Só o cabeçalho repetido não é cópia
        (2ª revisão: "PELA NEFROLOGIA" sozinho na linha se repete em toda
        avaliação)."""
        key = _comparable(block.text)
        if self._copied(key, _mask_counters(key)):
            return True
        if is_history(block.head):
            return True
        if _HISTORY_SECTION.match(block.previous_folded) and is_history(block.previous_folded):
            return True
        return _cites_date_before(block.head, view.time, self.reference)

    def cites_request_day(self, folded: str, view: _NoteView) -> bool:
        """A frase data o resultado no DIA do pedido: pode ter sido antes dele."""
        return any(cited == self.reference.date() for cited in _cited_dates_cached(folded, view.time.date()))

    def specialty_followed_before(self, *labels: str | None) -> bool:
        """A especialidade já evoluía o paciente antes do pedido (equipe
        assistente, hemodiálise diária, interconsulta anterior)?"""
        for label in labels:
            for first in (self.role_first.get(label), self.pela_first.get(label)):
                if first is not None and first <= self.reference:
                    return True
        return False


def _cancel_or_review(folded: str, about=_no_object) -> str:
    """Cancelamento só com motivo definitivo escrito (nunca negado, e sobre o
    próprio pedido), sem condição, pergunta ou cara de adiamento."""
    if _POSTPONED.search(folded):
        return ACTION_REVIEW
    return ACTION_CANCEL if _has_definitive_reason(folded, about) else ACTION_REVIEW


# ------------------------------------------------------------ interconsulta


def role_answers(role_specialty: str | None, target: str) -> bool:
    """O cargo do autor é da especialidade esperada? Fisioterapeuta
    respiratório ou motor responde pedido de "fisioterapia" genérico; o
    contrário (cargo genérico, pedido de fisio motora) não."""
    if role_specialty is None:
        return False
    if role_specialty == target:
        return True
    return target == specialties.FISIOTERAPIA and role_specialty in (
        specialties.FISIOTERAPIA_MOTORA, specialties.FISIOTERAPIA_RESPIRATORIA)


def _mentions(sentence: _Sentence, target: str) -> bool:
    return any(label == target for _start, _end, label in sentence.specialty_spans)


def _reports_answer(sentence: _Sentence, target: str) -> bool:
    if not _mentions(sentence, target):
        return False
    folded = sentence.folded
    if _NOT_ANSWERED.search(folded):
        return False
    for start, end, label in sentence.specialty_spans:
        if label != target:
            continue
        before = folded[max(0, start - 60):start]
        after = folded[end:]
        participle = _REPORTED_PARTICIPLE_BEFORE.search(before)
        if participle and not _REPORTED_MODAL.search(before[:participle.start()]):
            return True
        if _REPORTED_AFTER.match(after):
            return True
        if _REPORTED_COLON_BEFORE.search(before):
            colon = re.match(r"\s*[:\-]\s*(.*)$", after)
            if colon and len(colon.group(1).split()) >= 2 and not _NOT_AN_ANSWER_CONTENT.match(colon.group(1)):
                return True
    return False


def _cancels_consult(sentence: _Sentence, target: str) -> bool:
    if not _mentions(sentence, target):
        return False
    folded = sentence.folded
    for start, end, label in sentence.specialty_spans:
        if label != target:
            continue
        before = folded[max(0, start - 80):start]
        if _CONSULT_CANCEL_BEFORE.search(before):
            return True
        noun = _CONSULT_NOUN_ONLY_BEFORE.search(before)
        after = folded[end:end + 50]
        # "Conforme avaliação da nefrologia suspensa a vancomicina": a
        # avaliação já feita, e o cancelamento é de outra coisa (3ª revisão).
        if (noun and not REPORTED_HEAD_BEFORE.search(before[:noun.start()]) and _CANCEL_AFTER.match(after)
                and not _CANCEL_OF_OTHER_OBJECT.match(after)):
            return True
    return False


_STATUS_REASON = {
    specialties.STATUS_NOT_SEEN: "diz que o paciente ainda não foi avaliado",
    specialties.STATUS_WAITING: "ainda trata a interconsulta como pendente",
    specialties.STATUS_CONFLICT: "traz avaliação, mas volta a tratar a interconsulta como pendente",
    specialties.STATUS_UNCLEAR: "não traz conteúdo de avaliação",
}
# O próprio especialista pedindo exames (caso 2 do auditor: o original fecha
# e o exame vira pendência nova).
_ORDER_VERB = re.compile(r"\b(?:solicito|solicitamos|solicitar|colher|coletar|peco|pedimos)\b")
# Avaliação datada ANTES do pedido no texto da especialidade ("avaliado em
# 15/08, sem indicação naquele momento") -- 3ª revisão; também "avaliado
# ontem", "no dia 18", "na sexta-feira" (4ª revisão).
_EVALUATION_DATE_BOUND = re.compile(
    r"\b(?:avaliad\w*|vist[oa]s?|parecer|discutid\w*|avaliacao|reavaliad\w*)\s+(?:(?:em|no\s+dia|dia|de)\s+"
    r"(\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?)|(?:no\s+|do\s+)?dia\s+(\d{1,2})\b"
    r"|(ontem|anteontem|na\s+vespera|na\s+semana\s+passada|(?:na|no)\s+(?:(?:segunda|terca|quarta|quinta|sexta)"
    r"\s*-?\s*feira|sabado|domingo)))"
)


def _evaluation_dated_before(folded: str, view: _NoteView, context: _Context) -> bool:
    for match in _EVALUATION_DATE_BOUND.finditer(folded):
        cited = match.group(1) or (f"no dia {match.group(2)}" if match.group(2) else match.group(3))
        if _cites_date_before(cited, view.time, context.reference):
            return True
    return False


def _consult_evidence(target: str, view: _NoteView, context: _Context) -> _Finding | None:
    """Sinais do plano aprovado e da regra do auditor, nesta ordem: evolução
    ASSINADA pela especialidade, bloco "PELA <ESPECIALIDADE>" -- os dois só
    concluem com avaliação feita e forte (2ª e 3ª revisões), e nunca quando
    a especialidade já evoluía o paciente antes do pedido -- e, no máximo
    verificação, outra equipe relatando a resposta."""
    note = view.note
    if role_answers(view.role_specialty, target):
        keys = frozenset(sentence.comparable for sentence in view.sentences)
        evidence = _snippet(f"{note.role}: {note.text}")
        orders_exam = any(sentence.exam_spans and _ORDER_VERB.search(sentence.folded) for sentence in view.sentences)
        status = specialties.evaluation_status(view.folded_text, target, pela_block=False, orders_exam=orders_exam)
        if status != specialties.STATUS_ANSWER:
            return _Finding(ACTION_REVIEW, evidence, f"evolução de {note.role} posterior ao pedido "
                                                      f"{_STATUS_REASON[status]}", keys)
        if context.specialty_followed_before(view.role_specialty, target):
            # A especialidade já evoluía o paciente antes do pedido (equipe
            # assistente, hemodiálise diária): a evolução de rotina dela não
            # prova que a interconsulta NOVA foi respondida.
            return _Finding(ACTION_REVIEW, evidence, f"{note.role} já evoluía o paciente antes do pedido -- "
                                                      "confira se esta evolução responde à interconsulta", keys)
        if _evaluation_dated_before(view.folded_text, view, context):
            return _Finding(ACTION_REVIEW, evidence, f"evolução de {note.role} cita avaliação de antes do pedido",
                            keys)
        return _Finding(ACTION_RESOLVE, evidence, f"evolução posterior ao pedido escrita por {note.role}", keys)
    review: _Finding | None = None
    for block in view.pela:
        if block.label != target or context.block_is_stale(block, view):
            continue
        block_key = _comparable(block.text)
        keys = frozenset({block_key} | {s.comparable for s in view.sentences if s.comparable and s.comparable in block_key})
        status = specialties.evaluation_status(block.content, target, pela_block=True)
        if status == specialties.STATUS_ANSWER:
            if context.specialty_followed_before(target):
                review = review or _Finding(ACTION_REVIEW, _snippet(block.text),
                                            "a especialidade já evoluía o paciente antes do pedido -- confira se "
                                            "esta evolução responde à interconsulta", keys)
                continue
            if _evaluation_dated_before(block.content, view, context):
                review = review or _Finding(ACTION_REVIEW, _snippet(block.text),
                                            "a linha da especialidade cita avaliação de antes do pedido", keys)
                continue
            return _Finding(ACTION_RESOLVE, _snippet(block.text),
                            "evolução posterior ao pedido identificada pela própria especialidade", keys)
        if status != specialties.STATUS_WAITING:
            review = review or _Finding(ACTION_REVIEW, _snippet(block.text),
                                        f"linha da especialidade posterior ao pedido {_STATUS_REASON[status]}", keys)
    for sentence in view.sentences:
        if not _mentions(sentence, target) or context.is_stale(sentence, view):
            continue
        keys = frozenset({sentence.comparable})
        if _cancels_consult(sentence, target):
            action = _cancel_or_review(sentence.folded, _consult_object(target))
            if action == ACTION_CANCEL and _written_by_other_specialty(sentence, view, target):
                # "PELA INFECTOLOGIA: sem necessidade de avaliação da cirurgia
                # vascular" -- opinião de outra especialidade, não a equipe que
                # pediu retirando o pedido (4ª revisão).
                action = ACTION_REVIEW
            why = ("a equipe retirou o pedido em evolução posterior" if action == ACTION_CANCEL
                   else "pedido suspenso sem motivo definitivo (ou por outra especialidade) -- confira")
            found = _Finding(action, _snippet(sentence.original), why, keys)
            if action == ACTION_CANCEL:
                return found
            review = review or found
            continue
        if _reports_answer(sentence, target):
            review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                        "outra equipe relata a resposta da especialidade, mas não há evolução dela", keys)
    return review


def _written_by_other_specialty(sentence: _Sentence, view: _NoteView, target: str) -> bool:
    if view.role_specialty is not None and not role_answers(view.role_specialty, target):
        return True
    return any(block.label != target and sentence.comparable and sentence.comparable in _comparable(block.text)
               for block in view.pela)


def _consult_conflict(sentence: _Sentence, target: str) -> bool:
    """Frase nova que diz que a interconsulta AINDA não foi respondida
    (reiteração, "ainda não avaliado", "sem resposta", "aguarda parecer da
    X", "Nefrologia: aguarda avaliação", "IC nefrologia pendente"). Pedido
    novo, ordem que veio da especialidade e seguimento não são conflito -- a
    regra do auditor manda abrir pendência nova."""
    if not _mentions(sentence, target):
        return False
    return specialties.waiting_about(sentence.folded, target, unbound_counts=False, include_requests=False)


# -------------------------------------------------------------------- exame


def _exam_spans_for(sentence: _Sentence, exam: str) -> list[tuple[int, int, tuple[str, ...]]]:
    return [(start, end, regions) for (start, end, label), regions in zip(sentence.exam_spans, sentence.exam_regions)
            if label == exam]


def _span_fit(exam: str, wanted: str | None, span_regions: tuple[str, ...]) -> str:
    """'ok', 'mismatch' ou 'unknown' para UMA menção do exame. Pedido com
    região: a menção tem de citar a MESMA região ("RX de bacia" nunca fecha
    "RX de tórax"; ombro nunca fecha joelho; "TC de tórax e abdome" vale
    para as duas); região mais ampla ou mais estreita (MMII x MIE, coluna x
    coluna lombar) ou nenhuma: no máximo verificação. Pedido sem região e
    menção COM região: também só verificação."""
    if exam in _EXAMS_WITHOUT_REGION:
        return "ok"
    if wanted is not None:
        if wanted in span_regions:
            return "ok"
        if not span_regions or any(region in _ancestors(wanted) or wanted in _ancestors(region)
                                   for region in span_regions):
            return "unknown"
        return "mismatch"
    return "unknown" if span_regions else "ok"


def _next_exam_start(sentence: _Sentence, end: int) -> int:
    return next((start for start, _end, _label in sentence.exam_spans if start >= end), len(sentence.folded))


def _first_clause(text: str) -> tuple[str, str]:
    cut = re.search(r"[,;]", text)
    return (text[:cut.start()], text[cut.end():]) if cut else (text, "")


def _mention_clause(folded: str, start: int, end: int) -> str:
    """A oração da menção do exame: da vírgula/ponto e vírgula anterior até a
    seguinte (os dois-pontos ficam dentro: "TC de crânio: aguarda")."""
    left = max(folded.rfind(",", 0, start), folded.rfind(";", 0, start)) + 1
    rights = [index for index in (folded.find(",", end), folded.find(";", end)) if index != -1]
    return folded[left:min(rights) if rights else len(folded)]


_ONLY_DONE_MARKER = re.compile(
    r"^\s*(?:(?:de|do|da)\s+[a-z]+(?:\s+[a-z]+)?\s+)?(?:\(?[\d/.\-]+\)?\s+)?(?:foi\s+)?(?:realizad[oa]s?|feit[oa]s?)"
    r"(?:\s+(?:hoje|ontem|em\s+[\d/.\-]+|no\s+dia\s+\d+|pela\s+manha|a\s+tarde|a\s+noite|as\s+\d+\s*h\w*))*\s*$"
)
_REGION_CONNECTORS = {"de", "do", "da", "dos", "das", "e", "com", "sem", "contraste", "total", "controle", "venoso",
                      "oral"}


def _only_region(text: str) -> bool:
    """A oração só tem região/data/contraste ("Realizada TC DE CRÂNIO, ...")."""
    for _label, pattern in _REGION_COMPILED:
        text = pattern.sub(" ", text)
    words = re.findall(r"[a-z]+", re.sub(r"\d", " ", text))
    return all(word in _REGION_CONNECTORS for word in words)


# Na oração/frase SEGUINTE a "exame realizado": contexto de enfermagem ou de
# exame clínico nunca é laudo ("Sem alterações no período", "sem alterações
# durante o procedimento", "mantém edema de MMII", "Normal à ausculta",
# "Sem lesões por pressão" -- 4ª revisão).
_NARROW_REJECT = re.compile(
    r"\bno\s+periodo\b|\bplantao\b|\bdurante\b|\bprocediment\w*|\bpor\s+pressao\b|\bao\s+toque\b|\bauscult\w*"
    r"|\bmant(?:em|eve|endo)\b|\bsegue\b|\bseguindo\b|\bevolui\w*|\bpermanece\w*|\bapresent(?:a|ou|ando)\b"
    r"|\bmembros?\b|\bmmii\b|\bmmss\b|\bperfus\w*|\bpele\b|\bdeambul\w*|\bmedicac\w*|\bas\s+\d+\s*h"
    r"|\bdiureti\w*|\bdor\b|\bqueixa\w*|\babdome\s+(?:flacido|globoso|plano|indolor|doloroso)\b"
)
# Achado genérico que, sozinho, pode ser do paciente: só vale como laudo com
# qualificador radiológico ("sem alterações AGUDAS") ou depois de "Laudo:".
_GENERIC_FINDING = re.compile(
    r"\bsem\s+(?:novas?\s+)?(?:alteraco\w*|anormalidade\w*|achados|particularidades|lesoes?)\b"
    r"(?!\s+(?:agudas?|significativ\w*|relevantes?|estrutura\w*|isquemic\w*|hemorragic\w*|osse\w*|pleuro\w*"
    r"|parenquimat\w*|segmentar\w*|focais|expansiv\w*|traumatic\w*|evolutiv\w*))"
    r"|\bs/\s*alt\w*|\bnormal\b|\bnormais\b|\bndn\b|\bnada\s+digno\s+de\s+nota\b|\binalterad\w*"
    r"|\bdentro\s+da\s+normalidade\b|\bedema\b(?!\s+(?:cerebral|pulmonar|perilesional|vasogenico|citotoxico"
    r"|de\s+alcas|de\s+parede))|\binsuficiencia\s+(?:renal|respiratoria)\b|\blesoes?\s+por\s+pressao\b"
)


# O genérico sozinho, a oração INTEIRA: "TC de tórax realizada, sem alterações."
# -- nunca "hemograma sem alterações", "glicemia normal", "sem alterações na
# conduta" (5ª revisão).
_BARE_GENERIC = re.compile(
    r"^\s*(?:sem\s+(?:novas?\s+)?(?:alteraco\w*|anormalidade\w*|achados)(?:\s+(?:agudas?|significativ\w*|relevantes?))?"
    r"|normal|normais|ndn|nada\s+digno\s+de\s+nota|dentro\s+da\s+normalidade|s/\s*alt\w*)\s*[.!]?\s*$"
)


def _narrow_finding(text: str, allow_generic: bool = False) -> bool:
    """Achado na oração/frase seguinte a "exame realizado". O genérico ("sem
    alterações", "normal") só vale escrito por MÉDICO -- na enfermagem é o
    estado do paciente (4ª revisão) -- e só quando é a oração inteira (5ª)."""
    if _FOLLOWING_FINDING.search(_GENERIC_FINDING.sub(" ", text)):
        return True
    return allow_generic and _BARE_GENERIC.match(text) is not None


def _clause_has_result(clause: str, *, narrow: bool = False, allow_generic: bool = False) -> bool:
    """Achado de exame numa oração ligada ao exame: depois dos dois-pontos
    dele (com no máximo região/data/"realizada" antes) ou na própria oração
    -- nunca indicação ("para descartar hematoma", "por edema", "de controle
    do hematoma") nem outro assunto. `narrow`: só achado radiológico (a
    oração/frase SEGUINTE a "exame realizado")."""
    if not clause.strip() or _INDICATION.search(clause) or _OTHER_SUBJECT.search(clause):
        return False
    if narrow and _NARROW_REJECT.search(clause):
        return False
    colon = re.search(r":|\s-\s", clause)
    if colon is not None:
        before, after = clause[:colon.start()], clause[colon.end():]
        if len(before.split()) > _MAX_WORDS_BEFORE_COLON:
            return False
        label = _RESULT_LABEL.match(after)
        if label:
            return bool((_FOLLOWING_FINDING if narrow else _FINDING).search(after[label.end():]))
        return _narrow_finding(after, allow_generic) if narrow else bool(_FINDING.search(after))
    if _INDICATION_AFTER_EXAM.match(clause):
        return False
    return _narrow_finding(clause, allow_generic) if narrow else bool(_FINDING.search(clause))


_REGION_PENDING = re.compile(r"aguard\w*|\bpendente\b|\bsem\s+laudo\b|\ba\s+liberar\b|\bnao\s+(?:liberad|laudad)\w*")
_REPEAT_LATER = re.compile(
    r"\brepetir\b|\brepeticao\b|\bcontrole\s+(?:tomografic\w*|radiologic\w*|de\s+imagem|em\s+\d+|amanha|apos)"
    r"|\bnov[oa]\s+(?:tc|tomografia|exame|rx|raio|eco|usg|ultrass\w*|rm|ressonancia|imagem|cultura|hemocultura"
    r"|controle)\b|\bseriad[oa]s?\b|\bem\s+\d+\s*(?:h|hs|horas)\b"
)


def _related_regions(first: str, second: str) -> bool:
    return first == second or first in _ancestors(second) or second in _ancestors(first)


def _regions_pending_after(exam: str, following) -> set[str]:
    """Regiões que as frases seguintes da mesma evolução dizem estar sem laudo."""
    pending: set[str] = set()
    for sentence in following:
        if _REGION_PENDING.search(sentence.folded):
            pending.update(label for _start, _end, label in label_spans(_region_patterns(exam), sentence.folded))
    return pending


def _result_names_region(exam: str, after_mention: str) -> bool:
    colon = after_mention.find(":")
    return colon != -1 and bool(label_spans(_region_patterns(exam), after_mention[colon + 1:]))


def _listed_region_fit(exam: str, wanted: str, after_mention: str, fit: str) -> str:
    """Exame de várias regiões com laudo por região ("TC de crânio e abdome:
    crânio sem alterações, abdome aguarda laudo"): o resultado só vale para a
    região que ele cita -- citando só outra, 'unknown'; a parte da região
    pedida pendente, 'pending' (4ª revisão)."""
    colon = after_mention.find(":")
    if colon == -1:
        return fit
    named_other = False
    for part in re.split(r"[,;]", after_mention[colon + 1:]):
        labels = [label for _start, _end, label in label_spans(_region_patterns(exam), part)]
        if not labels:
            continue
        if any(label == wanted or wanted in _ancestors(label) or label in _ancestors(wanted) for label in labels):
            pending = _RESULT_ABSENT.search(part) or re.search(r"aguard\w*|\bpendente\b", part)
            return "pending" if pending else fit
        named_other = True
    return "unknown" if named_other else fit


def _imaging_result_after(folded: str, start: int, end: int, stop: int, *, physician: bool = False) -> bool:
    clause, rest = _first_clause(folded[end:stop])
    if _clause_has_result(clause):
        return True
    # "TC de crânio realizada, sem sinais de sangramento" / "Realizada TC de
    # crânio, sem alterações agudas": a oração do exame só diz que foi feito
    # -- o achado vem na oração seguinte, e só achado radiológico conta.
    done_before = _DONE_BEFORE_EXAM.search(folded[max(0, start - 30):start]) is not None
    if _ONLY_DONE_MARKER.match(clause) or (done_before and _only_region(clause)):
        following, _ = _first_clause(rest)
        return _clause_has_result(following, narrow=True, allow_generic=physician)
    return False


def _culture_result_after(folded: str, end: int, stop: int) -> bool:
    clause, _rest = _first_clause(folded[end:stop])
    if _INDICATION.search(clause) or _INDICATION_AFTER_EXAM.match(clause) or _OTHER_TEST.search(clause):
        return False
    return bool(_CULTURE_RESULT.search(clause))


def _ap_result_after(folded: str, end: int, stop: int) -> bool:
    if _AP_PENDING.search(folded):
        return False
    clause = re.split(r"[;]", folded[end:stop], maxsplit=1)[0]
    colon = re.search(r":|\s-\s", clause)
    if colon is not None and len(clause[:colon.start()].split()) <= 5:
        return bool(_AP_DIAGNOSIS.search(clause[colon.end():]))
    after_name = re.sub(r"^\s*(?:d[eoa]s?\s+[a-z]+(?:\s+[a-z]+)?)?", "", clause)
    return bool(_AP_RESULT_VERB.match(after_name) and _AP_DIAGNOSIS.search(after_name))


def _following_is_result(sentence: _Sentence, *, physician: bool = False) -> bool:
    """Frase seguinte a "<exame> realizado." que é o laudo dele: rótulo de
    laudo com achado radiológico, ou achado radiológico logo no começo."""
    folded = sentence.folded
    if sentence.exam_spans or sentence.specialty_spans or sentence.in_plan:
        return False
    if (_PHYSICAL_EXAM_START.match(folded) or _OTHER_SUBJECT.search(folded) or _EXAM_PENDING_SENTENCE.search(folded)
            or _EXAM_PENDING_CLAUSE.search(folded) or _INDICATION.search(folded) or _NARROW_REJECT.search(folded)):
        return False
    label = _RESULT_LABEL.match(folded)
    if label:
        return bool(_FOLLOWING_FINDING.search(folded[label.end():]))
    return bool(_RESULT_START.match(folded)) and _narrow_finding(folded, physician)


def _cancels_exam(sentence: _Sentence, spans: list[tuple[int, int]]) -> bool:
    folded = sentence.folded
    for start, end in spans:
        if _EXAM_CANCEL_BEFORE.search(folded[max(0, start - 50):start]):
            return True
        after = folded[end:end + 50]
        if _CANCEL_AFTER.match(after) or _EXAM_NOT_DONE_AFTER.match(after):
            return True
    return False


# Frase com centenas de menções do mesmo exame (texto colado, sem vírgula)
# ficava quadrática; nenhuma evolução real chega perto disto (4ª revisão).
_MAX_MENTIONS_PER_SENTENCE = 20


def _exam_evidence(target: str, view: _NoteView, context: _Context) -> _Finding | None:
    exam, region = _split_exam_target(target)
    review: _Finding | None = None
    sentences = view.sentences
    physician = is_physician(view.note.role)
    repeat_item = _REPEAT_REQUEST_ITEM.search(context.item_text or "") is not None
    for index, sentence in enumerate(sentences):
        mentions = [(start, end, _span_fit(exam, region, span_regions), span_regions)
                    for start, end, span_regions in _exam_spans_for(sentence, exam)]
        mentions = [mention for mention in mentions if mention[2] != "mismatch"]
        if not mentions or context.is_stale(sentence, view):
            continue
        folded = sentence.folded
        keys = frozenset({sentence.comparable})
        all_ok = all(fit == "ok" for _start, _end, fit, _regions in mentions)
        if _cancels_exam(sentence, [(start, end) for start, end, _fit, _regions in mentions]):
            action = _cancel_or_review(folded, _exam_object(exam)) if all_ok else ACTION_REVIEW
            if action == ACTION_CANCEL:
                return _Finding(action, _snippet(sentence.original),
                                "a equipe suspendeu o exame em evolução posterior ao pedido", keys)
            review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                        "exame suspenso sem motivo definitivo -- talvez só adiado", keys)
            continue
        if sentence.in_plan:
            continue  # item de lista debaixo de "CONDUTA:": é plano, não resultado
        done = _EXAM_DONE.search(folded) is not None and exam != _AP_EXAM  # biópsia feita não é o AP pronto
        if _EXAM_PENDING_SENTENCE.search(folded):
            if done and _RESULT_ABSENT.search(folded):
                review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                            "a equipe registra que o exame foi realizado, mas o resultado não "
                                            "foi localizado", keys)
            continue
        fit_with_result = None
        clause_pending = False
        for start, end, fit, span_regions in mentions[:_MAX_MENTIONS_PER_SENTENCE]:
            if _EXAM_PENDING_CLAUSE.search(_mention_clause(folded, start, end)):
                clause_pending = True  # a oração do PRÓPRIO exame é pedido/plano/espera
                continue
            stop = _next_exam_start(sentence, end)
            if exam in _CULTURE_EXAMS:
                has_result = _culture_result_after(folded, end, stop)
            elif exam == _AP_EXAM:
                has_result = _ap_result_after(folded, end, stop)
            else:
                has_result = _imaging_result_after(folded, start, end, stop, physician=physician)
            if has_result and region and len(span_regions) > 1:
                fit = _listed_region_fit(exam, region, folded[end:stop], fit)
                # "...; abdome aguarda laudo" / "Abdome: aguarda laudo." na frase
                # seguinte da mesma evolução (5ª revisão).
                pending_regions = _regions_pending_after(exam, sentences[index + 1:index + 4]) & set(span_regions)
                if fit != "pending" and pending_regions:
                    if any(_related_regions(region, other) for other in pending_regions):
                        fit = "pending"
                    elif not _result_names_region(exam, folded[end:stop]):
                        fit = "unknown"
                if fit == "pending":
                    clause_pending = True
                    continue
            if has_result and fit_with_result != "ok":
                fit_with_result = fit
        if fit_with_result is None and clause_pending:
            continue
        if fit_with_result is not None and clause_pending:
            # "TC de crânio com hematoma, aguarda TC de controle": a mesma
            # frase traz um resultado e volta a esperar o exame (4ª revisão).
            review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                        "a mesma frase traz um resultado e volta a pedir ou esperar o exame", keys)
            continue
        if fit_with_result is not None and repeat_item:
            # Pedido de controle: "TC com hematoma, repetir em 24h" -- o
            # resultado citado pode ser o anterior (5ª revisão).
            first_end = min(end for _start, end, _fit, _regions in mentions)
            comma = folded.find(",", first_end)
            if comma != -1 and _REPEAT_LATER.search(folded[comma:]):
                review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                            "o pedido é de controle e a mesma frase ainda programa a repetição", keys)
                continue
        evidence = sentence.original
        if fit_with_result is None and done and exam not in _CULTURE_EXAMS and index + 1 < len(sentences):
            following = sentences[index + 1]
            if not context.is_stale(following, view) and _following_is_result(following, physician=physician):
                fit_with_result = "ok" if all_ok else "unknown"
                evidence = f"{sentence.original}. {following.original}"
                keys = keys | {following.comparable}
        if fit_with_result is not None:
            if exam in _CULTURE_EXAMS and _CULTURE_PARTIAL.search(folded):
                review = review or _Finding(ACTION_REVIEW, _snippet(evidence),
                                            "resultado parcial da cultura (em identificação/incubação)", keys)
            elif fit_with_result != "ok":
                review = review or _Finding(ACTION_REVIEW, _snippet(evidence),
                                            "há resultado do exame, mas a evolução não diz de que região/material", keys)
            elif context.cites_request_day(folded, view):
                review = review or _Finding(ACTION_REVIEW, _snippet(evidence),
                                            "resultado datado no dia do pedido -- pode ser de antes dele", keys)
            else:
                return _Finding(ACTION_RESOLVE, _snippet(evidence),
                                f"resultado de {exam.lower()} em evolução posterior ao pedido", keys)
            continue
        if done:
            review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                        "a equipe registra que o exame foi realizado, mas o resultado não foi "
                                        "localizado", keys)
    return review


_EXAM_WAITING = re.compile(
    r"aguard\w*|\bpendente\w*|\bainda\s+(?:nao|sem)\b|\bnao\s+(?:foi\s+)?realizad\w*|\bsem\s+(?:laudo|resultado)\b"
    r"|\bcobrar\b"
)
_EXAM_NEW_REQUEST = re.compile(r"\brepetir\b|\bcontrole\b|\bde\s+novo\b|\bnovamente\b")
# Pedido que JÁ é o de controle/repetição: "aguarda TC de controle" depois do
# resultado é a espera DELE, não pedido novo (4ª revisão).
_REPEAT_REQUEST_ITEM = re.compile(r"\bcontrole\b|\bnov[oa]s?\b|\brepetir\b|\brepeticao\b|\breavaliar\b|\bseriad[oa]s?\b")
_REPEAT_WAITING = re.compile(
    r"\bagend\w*|\bprogramad\w*|\bmarcad[oa]\b|\bprevist[oa]\b|\bsolicit\w*|\brepetir\b|\brealizar\b|\bamanha\b"
)


def _exam_conflict(sentence: _Sentence, target: str, item_text: str = "") -> bool:
    """Frase nova voltando a esperar o MESMO exame (mesma região). "Nova
    TC", "repetir", "controle" é pedido novo, não conflito com o resultado
    que já veio (2ª revisão: "nova área isquêmica" punha em verificação) --
    a não ser que o próprio pedido seja o de controle/repetição."""
    exam, region = _split_exam_target(target)
    folded = sentence.folded
    repeat_item = _REPEAT_REQUEST_ITEM.search(item_text or "") is not None
    for start, end, span_regions in _exam_spans_for(sentence, exam)[:_MAX_MENTIONS_PER_SENTENCE]:
        if _span_fit(exam, region, span_regions) == "mismatch":
            continue
        clause = _clause_of(folded, start, end)
        # Pedido de controle: "TC de controle agendada para amanhã", "Repetir TC"
        # depois do resultado também é a espera dele (5ª revisão).
        if not (_EXAM_WAITING.search(clause) or (repeat_item and _REPEAT_WAITING.search(clause))):
            continue
        if not repeat_item and (re.search(r"\bnov[oa]s?\s+$", folded[max(0, start - 8):start])
                                or _EXAM_NEW_REQUEST.search(clause)):
            continue
        if _clause_has_result(folded[end:_next_exam_start(sentence, end)]):
            continue
        return True
    return False


# ------------------------------------------------------------------- punção


def _clause_of(folded: str, start: int, end: int) -> str:
    left = folded.rfind(",", 0, start) + 1
    right = folded.find(",", end)
    return folded[left:right if right != -1 else len(folded)]


def _typed_ok(target: str, folded: str) -> bool:
    """A frase de sucesso fala do TIPO pedido (e de nenhum dispositivo que
    não seja ele)."""
    pattern = _PUNCTURE_SUCCESS_TYPE.get(target)
    if pattern is None:
        return True  # pedido genérico: quem decide é a regra do genérico
    if not pattern.search(folded):
        return False
    if target == "ACESSO VENOSO CENTRAL":
        # "Passada PAI em femoral direita": femoral é sítio de CVC, mas o
        # dispositivo é a PAI (4ª revisão).
        return not (_ARTERY.search(folded) or _NOT_CENTRAL_DEVICE.search(folded) or re.search(_PAI, folded)
                    or re.search(r"\blinha\s+arterial\b", folded))
    if target == "PICC":
        return not re.search(_HD_CATHETER, folded)
    if target == "ACESSO VENOSO PERIFÉRICO":
        return not (_NOT_PERIPHERAL_DEVICE.search(folded) or _PUNCTURE_FOR_OTHER.search(folded))
    if target == "ACESSO ARTERIAL":
        return not _FATHER_CONTEXT.search(folded)
    return True


def _cancels_own_device(target: str, folded: str, cancel: re.Match) -> bool:
    pattern = _PUNCTURE_SUCCESS_TYPE.get(target)
    if pattern is None:
        return True
    span = re.split(r"[,;]", folded[cancel.start():cancel.end() + 30])[0]
    if not pattern.search(span) or not _typed_ok(target, span):
        return False
    return not (target == "ACESSO VENOSO CENTRAL" and re.search(r"\bpicc\b|" + _HD_CATHETER, span))


def _names_other_type(target: str, folded: str) -> bool:
    return any(label != target and pattern.search(folded) for label, pattern in _PUNCTURE_SUCCESS_TYPE.items()) \
        and not _typed_ok(target, folded)


def _puncture_evidence(target: str, view: _NoteView, context: _Context) -> _Finding | None:
    generic = target == PUNCTURE_GENERIC
    replacement = _REPLACEMENT_REQUEST.search(context.item_text) is not None
    review: _Finding | None = None
    sentences = view.sentences
    for index, sentence in enumerate(sentences):
        folded = sentence.folded
        if context.is_stale(sentence, view):
            continue
        keys = frozenset({sentence.comparable})
        typed = _typed_ok(target, folded)
        type_pattern = _PUNCTURE_SUCCESS_TYPE.get(target)
        mentioned = type_pattern is None or type_pattern.search(folded) is not None
        cancel = _PUNCTURE_CANCEL.search(folded)
        if cancel and mentioned:
            # O dispositivo cancelado tem de ser o do pedido: "Suspensa
            # passagem de Shilley em VJID" não cancela o CVC (4ª revisão).
            action = (_cancel_or_review(folded, _puncture_object(target))
                      if _cancels_own_device(target, folded, cancel) else ACTION_REVIEW)
            found = _Finding(action, _snippet(sentence.original),
                             "a equipe retirou a indicação em evolução posterior ao pedido" if action == ACTION_CANCEL
                             else "punção suspensa sem motivo definitivo -- talvez só adiada", keys)
            if action == ACTION_CANCEL:
                return found
            review = review or found
            continue
        if sentence.in_plan:
            continue
        cleaned = _PUNCTURE_NEUTRAL.sub(" ", folded)
        if _PUNCTURE_NEGATED.search(cleaned) or _PUNCTURE_WAITING_ANY.search(folded):
            continue
        if _ATTEMPT.search(cleaned) and not _ATTEMPT_SUCCEEDED.search(cleaned):
            continue
        if generic and _PUNCTURE_FOR_OTHER.search(folded):
            continue
        if replacement and not _REPLACEMENT_DONE.search(folded):
            continue  # pedido de TROCA: só a troca feita fecha
        team_named = PUNCTURE_TEAM.search(folded) is not None or (
            index > 0 and _TEAM_HEADER.match(sentences[index - 1].folded) is not None)
        success = _PUNCTURE_SUCCESS.search(folded)
        if success and _PUNCTURE_PLAN_CLAUSE.search(_clause_of(folded, success.start(), success.end())):
            success = None
        team_success = _TEAM_SUCCESS.search(folded) if team_named else None
        marker = success or team_success
        if marker is None:
            continue
        if team_named:
            following = sentences[index + 1].folded if index + 1 < len(sentences) else ""
            if _TEAM_WAITING_AFTER.search(folded[marker.end():] + " " + following):
                continue
            if not generic and not typed and _names_other_type(target, folded):
                review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                            "a equipe de punção registrou outro tipo de acesso", keys)
                continue
            return _Finding(ACTION_RESOLVE, _snippet(sentence.original),
                            "equipe de punção registrou atendimento realizado com sucesso", keys)
        if not typed:
            continue
        marker_clause = _clause_of(folded, marker.start(), marker.end())
        if not generic and re.search(r"\b" + _PUNCTURE_NOUN + r"\b", marker_clause) and not _typed_ok(target, marker_clause):
            # O tipo vale no dispositivo que o verbo de sucesso governa: "Passada
            # PAI em radial, punção de CVC a seguir" não é o CVC (4ª revisão).
            continue
        if generic:
            # "Aguarda punção" sem dizer de quê: qualquer punção feita (até
            # liquórica) não prova que era ESSA -- o auditor confere.
            review = review or _Finding(ACTION_REVIEW, _snippet(sentence.original),
                                        "punção realizada, mas o pedido não diz de que tipo -- confira", keys)
            continue
        return _Finding(ACTION_RESOLVE, _snippet(sentence.original),
                        "punção/acesso realizado em evolução posterior ao pedido", keys)
    return review


def _puncture_conflict(sentence: _Sentence, target: str) -> bool:
    """Frase nova voltando a esperar a MESMA punção ("ainda aguarda passagem
    de CVC") -- 3ª revisão: punção não tinha checagem de conflito."""
    if not _PUNCTURE_WAITING_ANY.search(sentence.folded):
        return False
    pattern = _PUNCTURE_SUCCESS_TYPE.get(target)
    return pattern is None or pattern.search(sentence.folded) is not None


# ------------------------------------------------------------------- comum


def kind_and_target(item) -> tuple[str, str] | None:
    """(tipo, alvo) de uma pendência que o detector sabe avaliar, ou None."""
    category = item["category"]
    subcategory = item["subcategory"]
    alvo = item["alvo"]
    description = item["description"]
    evidence = item["evidence"]
    if category == CATEGORY_INTERCONSULTA:
        if subcategory not in _CONSULT_CLOSABLE_SUBTYPES:
            return None
        if alvo:
            target = alvo if alvo in PUNCTURE_LABELS else consult_target(alvo)
        else:
            target = normalize_target(category, None, description, evidence if item["source"] == "LLM" else None)
        if target is None or not is_canonical_target(category, target):
            return None  # especialidade fora da tabela: o detector não sabe quem responde
        return (KIND_PUNCTURE, target) if target in PUNCTURE_LABELS else (KIND_CONSULT, target)
    if category == CATEGORY_DIAGNOSTICO:
        if subcategory not in _EXAM_CLOSABLE_SUBTYPES:
            return None
        target = exam_target(alvo) if alvo else None
        if target is None:
            target = normalize_target(category, None, description, evidence)
        elif _split_exam_target(target)[1] is None and _split_exam_target(target)[0] not in _EXAMS_WITHOUT_REGION:
            # Alvo sem região ("TC", regra antiga "tomografia"): a região vem
            # da descrição/evidência se for inequívoca.
            region = _region_for(target, (description, evidence))
            target = f"{target} DE {region}" if region else target
        if not target or exam_target(target) != target:
            return None  # alvo genérico ("DEFINICAO DIAGNOSTICA"): não é um exame que o detector conheça
        return KIND_EXAM, target
    if category in _PUNCTURE_CATEGORIES:
        target = (alvo if alvo in PUNCTURE_LABELS else None) or puncture_target(alvo) or puncture_target(description)
        return (KIND_PUNCTURE, target) if target else None
    return None


def detector_handles(item) -> bool:
    """A categoria é das que o detector avalia (interconsulta, exame, punção)
    -- mesmo quando ELE recusa o subtipo ou o alvo. Aí uma decisão de
    encerrar da IA vai pra verificação, nunca pra checagem genérica (2ª
    revisão: "TC realizada" sem laudo fechava quando a IA escolhia o subtipo
    OUTRO)."""
    category = item["category"]
    if category in (CATEGORY_INTERCONSULTA, CATEGORY_DIAGNOSTICO):
        return True
    if category in _PUNCTURE_CATEGORIES:
        return bool(item["alvo"] in PUNCTURE_LABELS or puncture_target(item["alvo"])
                    or puncture_target(item["description"]))
    return False


def _evidence_for(kind: str, target: str, view: _NoteView, context: _Context) -> _Finding | None:
    if kind == KIND_CONSULT:
        return _consult_evidence(target, view, context)
    if kind == KIND_EXAM:
        return _exam_evidence(target, view, context)
    return _puncture_evidence(target, view, context)


def _conflict_after(kind: str, target: str, views: list[_NoteView], proof_view: _NoteView, finding: _Finding,
                    context: _Context) -> _Sentence | None:
    """Frase NOVA (que não existia antes), da evolução da prova em diante,
    voltando a esperar o mesmo alvo: informação conflitante (tabela do
    auditor). Inclui a própria evolução da prova (menos as frases da prova)
    e as de mesmo horário (2ª revisão), e a punção (3ª revisão)."""
    for view in views:
        if view.time < proof_view.time:
            continue
        same = view is proof_view
        if kind == KIND_CONSULT:
            # "PELA NEFROLOGIA: aguarda avaliação" -- o formato da casa para a
            # espera da equipe que pediu (4ª revisão: o cabeçalho "PELA" nunca
            # liga a espera à especialidade, e o conflito passava).
            for block in view.pela:
                if block.label != target:
                    continue
                key = _comparable(block.text)
                if same and key in finding.keys:
                    continue
                first = context.seen_before.get(key)
                if (first is not None and first < view.time) or is_history(block.head):
                    continue
                status = specialties.evaluation_status(block.content, target, pela_block=True)
                waits = status in (specialties.STATUS_WAITING, specialties.STATUS_CONFLICT) or (
                    # "PELA NEFROLOGIA: ainda não avaliado, paciente em exame" da
                    # equipe que pediu (5ª revisão).
                    status == specialties.STATUS_NOT_SEEN and not role_answers(view.role_specialty, target))
                if waits:
                    return _Sentence(original=block.text, folded=fold(block.text), comparable=key)
            # A evolução assinada que É a prova já foi lida inteira pelo status.
            if not (same and role_answers(view.role_specialty, target)):
                header_wait = _header_block_waits(view, target, context)
                if header_wait is not None:
                    return header_wait
        for sentence in view.sentences:
            if same and (sentence.comparable in finding.keys
                         or any(sentence.comparable and sentence.comparable in key for key in finding.keys)):
                continue
            first = context.seen_before.get(sentence.comparable)
            if first is not None and first < view.time:
                continue  # cópia de frase já existente
            if kind == KIND_CONSULT:
                conflict = _consult_conflict(sentence, target)
            elif kind == KIND_EXAM:
                conflict = _exam_conflict(sentence, target, context.item_text)
            else:
                conflict = _puncture_conflict(sentence, target)
            # Espera no PRESENTE com a data do pedido original ("segue aguardando
            # ..., solicitada há 2 dias") é conflito, não histórico (5ª revisão).
            if conflict and (_PRESENT_WAIT.search(sentence.folded) or not is_history(sentence.folded)):
                return sentence
    return None


_PRESENT_WAIT = re.compile(
    r"\baguard(?:a|am|amos|ando|o)\b|\bsegue\s+aguardando\b|\breitero\b|\breiteramos\b|\bainda\s+(?:nao|sem|aguarda)\b"
    r"|\bpendente\b|\bem\s+aberto\b"
)


def _header_block_waits(view: _NoteView, target: str, context: _Context) -> _Sentence | None:
    """Espera debaixo de um cabeçalho com o nome da especialidade, sem "pela"
    ("Nefrologia:\\n- aguarda avaliação") -- linha nova, não cópia (5ª revisão)."""
    if view.header_blocks is None:
        view.header_blocks = specialties.specialty_header_blocks(view.note.text, view.folded_text)
    for label, content, lines in view.header_blocks:
        if label != target or not content:
            continue
        keys = [key for line in lines for key in (_comparable(part) for part in _sentences(line)) if key]
        if all((first := context.seen_before.get(key)) is not None and first < view.time for key in keys):
            continue  # cópia do bloco
        if specialties.waiting_about(content, target, unbound_counts=True, include_requests=False) \
                or specialties.pela_waiting_anywhere(content, target):
            return _Sentence(original=content, folded=content, comparable=_comparable(content))
    return None


_NOTE_PREFIX = {
    ACTION_RESOLVE: "Concluída automaticamente",
    ACTION_CANCEL: "Cancelada automaticamente",
    ACTION_REVIEW: "Em verificação pelo auditor",
}


def _manifests(block) -> bool:
    """O bloco "PELA X" é a especialidade se manifestando (não a espera da
    equipe que pediu)."""
    return specialties.evaluation_status(block.content, block.label, pela_block=True) != specialties.STATUS_WAITING


def _views_for(notes: list[DetectorNote], earliest_reference: datetime | None = None):
    """Evoluções em ordem de horário. As anteriores ao pedido mais antigo
    nunca são evidência: delas só interessa saber que frases/blocos já
    existiam (cópia de evolução anterior) e que especialidade já evoluía o
    paciente -- não passam pelo processamento completo. Devolve (views,
    first_seen, role_first, pela_first)."""
    timed = sorted(
        ((parsed, note) for note in notes if (parsed := _parse_local(note.timestamp)) is not None),
        key=lambda pair: pair[0],
    )
    views: list[_NoteView] = []
    first_seen: dict[str, datetime] = {}
    role_first: dict[str, datetime] = {}
    pela_first: dict[str, datetime] = {}
    for time, note in timed:
        role = specialties.specialty_from_role(note.role)
        if role is not None:
            role_first.setdefault(role, time)
        if earliest_reference is not None and time <= earliest_reference:
            for label in specialties.specialty_header_labels(note.text):
                pela_first.setdefault(label, time)
            for original in _sentences(note.text):
                comparable = _comparable(original)
                first_seen.setdefault(comparable, time)
                first_seen.setdefault(_mask_counters(comparable), time)
            for block in specialties.pela_blocks(note.text):
                key = _comparable(block.text)
                first_seen.setdefault(key, time)
                first_seen.setdefault(_mask_counters(key), time)
                if _manifests(block):
                    pela_first.setdefault(block.label, time)
            continue
        view = _view(time, note)
        views.append(view)
        view.header_blocks = specialties.specialty_header_blocks(note.text, view.folded_text)
        for label, content, _lines in view.header_blocks:
            if label not in pela_first and specialties.evaluation_status(
                    content, label, pela_block=True) == specialties.STATUS_ANSWER:
                pela_first[label] = time
        for sentence in view.sentences:
            first_seen.setdefault(sentence.comparable, time)
            first_seen.setdefault(sentence.masked, time)
        for block in view.pela:
            key = _comparable(block.text)
            first_seen.setdefault(key, time)
            first_seen.setdefault(_mask_counters(key), time)
            if _manifests(block):
                pela_first.setdefault(block.label, time)
    return views, first_seen, role_first, pela_first


def _decide(item, kind: str, target: str, views: list[_NoteView], first_seen: dict, role_first: dict,
            pela_first: dict, local_tz) -> DetectedResolution | None:
    reference = reference_time(item["evidence_date"], item["created_at"], local_tz)
    if reference is None:
        return None
    context = _Context(reference=reference, seen_before=first_seen, role_first=role_first, pela_first=pela_first,
                       item_text=fold(item["description"]))
    decisive: list[tuple[_NoteView, _Finding]] = []
    review: tuple[_NoteView, _Finding] | None = None
    for view in views:
        if view.time <= reference:
            continue
        found = _evidence_for(kind, target, view, context)
        if found is None:
            continue
        if found.action == ACTION_REVIEW:
            review = review or (view, found)
            continue
        decisive.append((view, found))
    # A primeira prova SEM informação conflitante depois dela -- não só a
    # primeira prova (2ª revisão: uma reiteração no meio deixava o item em
    # verificação para sempre, mesmo com avaliação limpa depois).
    chosen: tuple[_NoteView, _Finding] | None = None
    last_conflict: tuple[_NoteView, _Sentence] | None = None
    for view, found in decisive:
        conflict = _conflict_after(kind, target, views, view, found, context)
        if conflict is None:
            chosen = (view, found)
            break
        last_conflict = (view, conflict)
    if chosen is None and last_conflict is not None:
        chosen = (last_conflict[0], _Finding(
            ACTION_REVIEW, _snippet(last_conflict[1].original),
            "informação conflitante: evolução posterior à prova volta a pedir ou esperar"))
    chosen = chosen or review
    if chosen is None:
        return None
    view, found = chosen
    resolved_at = resolution_time_utc(view.time.isoformat(timespec="seconds"), item["created_at"], local_tz)
    return DetectedResolution(
        pending_id=item["pending_id"], kind=kind, target=target, action=found.action, evidence=found.evidence,
        evidence_date=view.time.isoformat(timespec="seconds"),
        resolved_at=resolved_at or item["created_at"],
        note=f"{_NOTE_PREFIX[found.action]} ({target}): {found.why}.",
    )


def detect_resolutions(
    open_items, notes: list[DetectorNote], *, local_tz: tzinfo | None = None,
) -> list[DetectedResolution]:
    """Para cada pendência aberta (linhas de `pending_items`), a PRIMEIRA
    evolução posterior ao pedido que prova o atendimento (ou o cancelamento)
    sem informação conflitante depois; sem prova, mas com informação
    incompleta/conflitante: verificação. `notes` = todas as evoluções da
    internação atual (qualquer ordem). `local_tz` = fuso do horário do GSUS
    (None = o do computador, que é o do hospital)."""
    targets = {}
    for item in open_items:
        kind_target = kind_and_target(item)
        if kind_target is not None:
            targets[item["pending_id"]] = kind_target
    if not targets:
        return []
    references = [
        reference for item in open_items if item["pending_id"] in targets
        and (reference := reference_time(item["evidence_date"], item["created_at"], local_tz)) is not None
    ]
    views, first_seen, role_first, pela_first = _views_for(notes, min(references) if references else None)
    results = []
    for item in open_items:
        if item["pending_id"] not in targets:
            continue
        kind, target = targets[item["pending_id"]]
        decided = _decide(item, kind, target, views, first_seen, role_first, pela_first, local_tz)
        if decided is not None:
            results.append(decided)
    return results


class DetectorSession:
    """As evoluções da internação de UM paciente, pré-processadas uma vez
    para todas as decisões de uma fase (2ª e 3ª revisões: refazer tudo por
    decisão, e de novo no fim da análise, custava dezenas de segundos por
    paciente de internação longa no hardware fraco)."""

    def __init__(self, notes: list[DetectorNote], *, local_tz: tzinfo | None = None):
        self.notes = notes
        self.local_tz = local_tz
        self._prepared: tuple | None = None
        self._blobs: list[tuple[DetectorNote, str]] | None = None
        self._sentence_sets: list[tuple[DetectorNote, frozenset]] | None = None

    def _views(self):
        if self._prepared is None:
            self._prepared = _views_for(self.notes)
        return self._prepared

    def first_seen(self) -> dict[str, datetime]:
        return self._views()[1]

    def decide(self, item) -> DetectedResolution | None:
        kind_target = kind_and_target(item)
        if kind_target is None:
            return None
        views, first_seen, role_first, pela_first = self._views()
        return _decide(item, *kind_target, views, first_seen, role_first, pela_first, self.local_tz)

    def detect(self, open_items) -> list[DetectedResolution]:
        """`detect_resolutions` reaproveitando as evoluções já processadas."""
        if self._prepared is None:
            return detect_resolutions(open_items, self.notes, local_tz=self.local_tz)
        return [found for item in open_items if (found := self.decide(item)) is not None]

    def agrees(self, item, action: str) -> bool | None:
        """Para decisão CONCLUIDA/CANCELADA da IA (contrato v2): o detector,
        olhando a internação inteira (com a checagem de conflito), chega à
        MESMA ação? None só quando a categoria não é das que ele avalia (aí
        vale a checagem genérica do trecho). Subtipo ou alvo que ele recusa
        dá False -- vai pra verificação (2ª revisão)."""
        if not detector_handles(item):
            return None
        decided = self.decide(item)
        return decided is not None and decided.action == action

    def notes_containing(self, quote: str | None) -> list[DetectorNote]:
        """Evoluções (com data) que contêm o trecho citado INTEIRO, da mais antiga pra mais nova."""
        if self._blobs is None:
            self._blobs = [(note, notes_blob([note.text])) for note in self.notes if note.timestamp]
        found = [note for note, blob in self._blobs if quote_in_notes(quote, blob)]
        return sorted(found, key=lambda note: note.timestamp)

    def notes_with_sentence(self, sentence: str | None) -> list[DetectorNote]:
        """Evoluções (com data) com exatamente esta frase -- vale para frase
        curta ("Peço ECO."), que `notes_containing` não confere (3ª revisão)."""
        wanted = _comparable(sentence)
        if not wanted:
            return []
        if self._sentence_sets is None:
            self._sentence_sets = [
                (note, frozenset(_comparable(original) for original in _sentences(note.text)))
                for note in self.notes if note.timestamp
            ]
        found = [note for note, sentences in self._sentence_sets if wanted in sentences]
        return sorted(found, key=lambda note: note.timestamp)


def detector_agrees(item, notes: list[DetectorNote], quote: str | None = None, action: str = ACTION_RESOLVE, *,
                    local_tz: tzinfo | None = None) -> bool | None:
    """Atalho de `DetectorSession.agrees` para uma decisão só."""
    return DetectorSession(notes, local_tz=local_tz).agrees(item, action)


# --- checagem do trecho da IA para categorias que o detector não avalia
# (transferência, terapêutica, alta, procedimento que não é punção): lista
# POSITIVA de conclusão e nenhuma palavra de recusa/adiamento/falha (2ª
# revisão: "vaga negada", "transferência adiada", "colecistectomia
# remarcada" e "falha no desmame" fechavam ou cancelavam).
_DONE_WORDS = re.compile(
    r"\brealizad[oa]s?\b|\brealizou\b|\bfeit[oa]s?\b|\befetuad\w*|\bconcluid\w*|\bexecutad\w*|\btransferid[oa]s?\b"
    r"|\badmitid[oa]s?\b|\brecebid[oa]s?\b|\brecebeu\b|\bliberad[oa]s?\b|\binstalad[oa]s?\b|\biniciad[oa]s?\b"
    r"|\binicio\s+d[eoa]\b|\bimplantad[oa]s?\b|\bpassad[oa]s?\b|\bcolocad[oa]s?\b|\bretirad[oa]s?\b|\bremovid[oa]s?\b"
    r"|\bextubad[oa]\b|\bdesmamad[oa]\b|\balta\s+(?:hospitalar|dada|realizada|da\s+uti|para)\b|\bde\s+alta\b"
    r"|\bteve\s+alta\b|\bobteve\b|\bconseguid[oa]\b|\baprovad[oa]\b|\bautorizad[oa]\b|\bentregue\b|\bassinad[oa]\b"
    r"|\bdisponibilizad[oa]\b|\bchegou\b|\boperad[oa]\b|\bsubmetid[oa]\b"
)
_NOT_DONE_WORDS = re.compile(
    r"aguard\w*|solicit\w*|\bpendente\w*|\bpendenc\w*|\bainda\s+nao\b|\bsem\s+resposta\b|\breiter\w*|\bnao\b"
    r"|\bprevist[oa]\b|\bagend\w*|\bprogramad\w*|\bamanha\b|\bsera\b|\bnegad\w*|\brecusad\w*|\bindeferid\w*"
    r"|\badiad\w*|\bremarc\w*|\breagend\w*|\breprogram\w*|\bsuspens\w*|\bcancel\w*|\bfalh\w*|\binsucesso\b"
    r"|\bsem\s+sucesso\b|\bem\s+discussao\b|\bdiscutir\b|\bsem\s+vaga\b|\breavaliar\b|\btentativa\w*"
    r"|\bem\s+andamento\b|\bparcia\w*|\bpossibilidade\b|\bcaso\b|\bem\s+fila\b|\bfila\b|\bretorn\w*\s+(?:a|ao)\b"
)
_CANCEL_STATEMENT = re.compile(
    r"\bsuspens[oa]s?\b|\bcancelad[oa]s?\b|\bcancelamento\b|\bdispensad[oa]s?\b|\bdesist\w*"
    r"|\bretirad[oa]\s+(?:a\s+)?(?:indicacao|solicitacao|pedido)\b|\bnao\s+sera\s+mais\b"
    r"|\bsem\s+(?:mais\s+)?indicacao\b|\bnao\s+(?:ha|havera)\s+mais\s+indicacao\b"
)


def _clauses(folded: str) -> list[str]:
    return [clause for clause in re.split(r"[,;.]", folded) if clause.strip()]


# Palavra do assunto que é só MODIFICADOR de outra coisa: "cateter DE
# hemodiálise passado" não é a hemodiálise feita, "gasometria a pedido DA UTI"
# não é a vaga (4ª revisão).
_MODIFIER_BEFORE = re.compile(
    r"\b(?:cateter|acesso|preparo|jejum|pedido|equipe|visita|boletim|familia|familiar|medico|plantonista)\s+"
    r"(?:d[aeo]s?|para|p/|pra)\s+(?:a\s+|o\s+)?$"
)
# Vaga/transferência só com o verbo dela (4ª revisão: "Recebido DA enfermaria"
# é o paciente vindo de lá; "Recebeu visita da equipe da UTI" não é vaga).
_PLACE_WORDS = {"uti", "cti", "enfermaria", "semi", "semiintensiva", "leito", "quarto", "retaguarda", "hospital",
                "unidade", "apartamento", "isolamento", "adulto", "pediatrica", "neonatal"}
_VACANCY_DONE = re.compile(
    r"\btransferid[oa]s?\s+(?:para|p/|pra|a|ao|à)\b|\btransferencia\s+(?:realizada|efetivada|concluida)\b"
    r"|\b(?:admitid|recebid)[oa]s?\s+n[ao]s?\b|\bchegou\s+(?:a|ao|na|no)\b"
    r"|\bvaga\s+(?:\w+\s+){0,3}?(?:liberad|conseguid|disponibilizad|obtid|garantid|confirmad|autorizad)\w*"
    r"|\b(?:liberad|conseguid|disponibilizad|obtid|garantid|confirmad)[oa]s?\s+(?:a\s+)?vaga\b"
)
_VACANCY_NOT_PATIENT = re.compile(r"\bfamili\w*|\bvisita\w*|\bequipe\b|\bboletim\b|\bacompanhante\b")
_TOPIC_MAX_DISTANCE = 4


# Finalidade entre a conclusão e o assunto: "Iniciada furosemida PARA EVITAR
# hemodiálise" não é a hemodiálise iniciada (5ª revisão).
_PURPOSE_BETWEEN = re.compile(r"\bpara\s+(?:evitar|adiar|postergar|prevenir|retardar)\b|\bvisando\b|\ba\s+fim\s+de\b"
                              r"|\bpara\s+nao\b")
# Quem autoriza/pede não é o assunto ("liberado meropenem pela CCIH").
_REQUESTER_WORDS = {"ccih", "regulacao", "central", "nir", "scih", "farmacia", "diretoria"}


def _topic_near_done(clause: str, done_pattern, own: set[str], *, topic_inside_done: bool = False) -> bool:
    """A palavra de conclusão e uma do assunto (que não seja ela mesma, nem
    modificador de outra coisa, nem separada por uma finalidade) a no máximo
    4 palavras uma da outra. `topic_inside_done`: a expressão de conclusão já
    contém o assunto ("vaga de UTI liberada")."""
    tokens = [(match.start(), match.end(), match.group(0)) for match in re.finditer(r"[a-z0-9]+", clause)]
    done_spans = [(match.start(), match.end()) for match in done_pattern.finditer(clause)]
    done_positions = [index for index, (start, end, _word) in enumerate(tokens)
                      if any(start < d_end and end > d_start for d_start, d_end in done_spans)]
    topic_positions = [index for index, (start, _end, word) in enumerate(tokens)
                       if (topic_inside_done or index not in done_positions) and _word_in(word, own)
                       and not _MODIFIER_BEFORE.search(clause[max(0, start - 40):start])]
    for done in done_positions:
        for topic in topic_positions:
            if abs(done - topic) > _TOPIC_MAX_DISTANCE:
                continue
            low, high = sorted((done, topic))
            if not _PURPOSE_BETWEEN.search(clause[tokens[low][0]:tokens[high][1]]):
                return True
    return False


def _alvo_core_words(item) -> set[str]:
    """Palavras do alvo que distinguem a pendência: sem cabeça ("vaga"), sem
    ação ("início") e sem quem autoriza ("CCIH")."""
    return {word for word in re.findall(r"[a-z0-9]+", fold(item["alvo"]))
            if len(word) >= 3 and word not in _TOPIC_STOPWORDS and word not in _ALVO_HEAD_WORDS
            and word not in _ACTION_STEMS and word not in _REQUESTER_WORDS}


alvo_core_words = _alvo_core_words


def _only_place_target(item) -> bool:
    words = _alvo_core_words(item)
    return item["category"] == "TRANSFERENCIA" or (bool(words) and words <= _PLACE_WORDS)


# "Vaga DE ENFERMARIA liberada" para a vaga de UTI: o complemento da vaga é
# outro lugar (5ª revisão).
_VACANCY_COMPLEMENT = re.compile(r"\bvaga\s+(?:de|da|do|na|no|em|para)\s+(?:a\s+|o\s+)?(\w+)")


def _clause_has_core(clause: str, core: set[str]) -> bool:
    words = set(re.findall(r"[a-z0-9]+", clause))
    return all(_word_in(word, words) for word in core)


def quote_confirms_done(quote: str | None, item=None) -> bool:
    """O trecho diz que foi FEITO (lista positiva, nenhuma recusa/adiamento/
    falha) -- e, com `item`, a palavra de conclusão está na MESMA oração de
    uma palavra do assunto dele, perto dela ("Hemodiálise realizada na UTI"
    não é a vaga de UTI obtida -- 3ª revisão; vaga/transferência só com o
    verbo dela -- 4ª revisão), com TODAS as palavras próprias do alvo nessa
    oração ("Vaga de enfermaria liberada..., critérios de UTI" não é a vaga
    de UTI -- 5ª revisão)."""
    folded = fold(quote)
    if not quote or _DONE_WORDS.search(folded) is None or _NOT_DONE_WORDS.search(folded) is not None:
        return False
    if item is None:
        return True
    own = _topic_words(item["description"], item["alvo"]) - _REQUESTER_WORDS
    core = _alvo_core_words(item)
    if _only_place_target(item):
        for clause in _clauses(folded):
            complement = _VACANCY_COMPLEMENT.search(clause)
            if complement and core and complement.group(1) in _PLACE_WORDS and complement.group(1) not in core:
                continue
            if (_VACANCY_DONE.search(clause) and not _VACANCY_NOT_PATIENT.search(clause) and _clause_has_core(clause, core)
                    and _topic_near_done(clause, _VACANCY_DONE, own, topic_inside_done=True)):
                return True
        return False
    return any(_clause_has_core(clause, core) and _topic_near_done(clause, _DONE_WORDS, core or own)
               for clause in _clauses(folded))


def quote_confirms_cancel(quote: str | None, item=None) -> bool:
    """Cancelamento com motivo definitivo -- e, com `item`, a palavra de
    cancelamento perto de uma do assunto, na MESMA oração (4ª revisão:
    "Vancomicina suspensa..., mantido meropenem" cancelava o meropenem); o
    motivo negativo ("sem indicação de X") tem de ser sobre o próprio item."""
    folded = fold(quote)
    if item is None:
        own: set[str] = set()
        about = lambda _obj: True  # noqa: E731 -- sem item, qualquer objeto
    else:
        own = _topic_words(item["description"], item["alvo"]) - _REQUESTER_WORDS

        def about(obj: str) -> bool:
            return any(_word_in(word, own) for word in re.findall(r"[a-z0-9]+", obj))
    if not (quote and _CANCEL_STATEMENT.search(folded) is not None and _has_definitive_reason(folded, about)
            and _POSTPONED.search(folded) is None):
        return False
    if item is None:
        return True
    return any(_topic_near_done(clause, _CANCEL_STATEMENT, own) for clause in _clauses(folded))


_TOPIC_STOPWORDS = _STOPWORDS | _GENERIC_HEAD_WORDS | {
    "paciente", "definicao", "conduta", "para", "pela", "pelo", "apos", "ainda", "sobre", "mais", "segue",
    "mantem", "hoje", "aguarda", "aguardando",
}


# Cabeça do alvo genérico que o trecho não precisa repetir ("Transferido para
# a enfermaria" responde a "VAGA ENFERMARIA"); "início" fica -- cateter de
# diálise passado não é o início da diálise.
_ALVO_HEAD_WORDS = {
    "vaga", "cirurgia", "passagem", "exame", "exames", "procedimento", "transferencia", "leito", "liberacao",
    "autorizacao", "resultado", "terapia", "tratamento", "ajuste", "suspensao", "controle",
}


def _topic_words(description: str | None, alvo: str | None) -> set[str]:
    words = {word for word in re.findall(r"[a-z0-9]+", fold(description))
             if len(word) >= 4 and word not in _TOPIC_STOPWORDS}
    words |= {word for word in re.findall(r"[a-z0-9]+", fold(alvo)) if len(word) >= 3 and word not in _TOPIC_STOPWORDS}
    return words


# Famílias de palavras com o mesmo sentido ("iniciada" x "início", "transfundido"
# x "transfusão"). Radical genérico de 5 letras não: "hemodinâmica" virava
# "hemodiálise", "traqueal" virava "traqueostomia", "transferido" virava
# "transfusão" (4ª revisão).
_WORD_FAMILIES = (
    ("hemodialis", "dialis"), ("dialis", "dialis"), ("inici", "inici"), ("transfund", "transfus"),
    ("transfus", "transfus"), ("transfer", "transfer"), ("desmam", "desmam"), ("extuba", "extuba"),
    ("intuba", "intuba"), ("traqueostom", "traqueostom"), ("gastrostom", "gastrostom"),
    ("colecistectom", "colecistectom"), ("amputa", "amputa"), ("drenag", "drena"), ("drenad", "drena"),
    ("libera", "libera"), ("suspen", "suspen"), ("retirad", "retir"), ("retirar", "retir"), ("trocad", "troc"),
    ("trocar", "troc"), ("ajust", "ajust"), ("internac", "intern"), ("internad", "intern"), ("operad", "opera"),
    ("cirurgi", "cirurgi"), ("transplant", "transplant"), ("fisioterap", "fisioterap"), ("antibiot", "antibiot"),
    ("anticoagul", "anticoagul"), ("hemotransfus", "transfus"), ("vacin", "vacin"),
)


def _family(word: str) -> str | None:
    for prefix, family in _WORD_FAMILIES:
        if word.startswith(prefix):
            return family
    return None


def _word_in(word: str, words) -> bool:
    """Mesma palavra, ou da mesma família listada ("iniciada" x "início")."""
    if word in words:
        return True
    family = _family(word)
    return family is not None and any(_family(other) == family for other in words)


# Ação pedida no item (início x suspensão x liberação): o trecho tem de
# trazer a MESMA ação -- "Vancomicina liberada" não conclui "suspensão de
# vancomicina" (4ª revisão).
_ACTION_STEMS = {"inicio": "inici", "iniciar": "inici", "suspensao": "suspen", "suspender": "suspen",
                 "retirada": "retir", "retirar": "retir", "troca": "troc", "trocar": "troc", "liberacao": "libera",
                 "liberar": "libera", "ajuste": "ajust", "ajustar": "ajust"}


def _shares_topic(text: str, own: set[str]) -> bool:
    words = set(re.findall(r"[a-z0-9]+", text))
    return any(_word_in(word, words) for word in own)


def quote_is_about_item(quote: str | None, item, other_items=()) -> bool:
    """O trecho fala DESTA pendência e não de outra aberta do paciente?
    - precisa de uma palavra que só ela tem (2ª revisão: "vaga" é de todas
      as vagas);
    - todas as palavras do alvo têm de estar no trecho -- ou, sem alvo, pelo
      menos duas do assunto (3ª revisão: só "UTI" fechava a vaga de UTI com
      "hemodiálise realizada na UTI");
    - nenhuma outra pendência pode casar igual ou melhor com o trecho."""
    quote_words = set(re.findall(r"[a-z0-9]+", fold(quote)))
    own = _topic_words(item["description"], item["alvo"])
    others = [_topic_words(other["description"], other["alvo"]) for other in other_items
              if other["pending_id"] != item["pending_id"]]
    shared = set().union(*others) if others else set()
    if not any(_word_in(word, quote_words) for word in own - shared):
        return False
    item_words = re.findall(r"[a-z0-9]+", fold(f"{item['description']} {item['alvo'] or ''}"))
    actions = {_ACTION_STEMS[word] for word in item_words if word in _ACTION_STEMS}
    if any(not any(word.startswith(stem) for word in quote_words) for stem in actions):
        return False
    alvo_words = {word for word in re.findall(r"[a-z0-9]+", fold(item["alvo"]))
                  if len(word) >= 3 and word not in _TOPIC_STOPWORDS and word not in _ALVO_HEAD_WORDS}
    if alvo_words:
        if not all(_word_in(word, quote_words) for word in alvo_words):
            return False
    elif len(own) >= 2 and sum(_word_in(word, quote_words) for word in own) < 2:
        return False
    own_overlap = sum(_word_in(word, quote_words) for word in own)
    return all(sum(_word_in(word, quote_words) for word in words) < own_overlap for words in others)


def evidence_shows_done(item) -> bool:
    """A própria evidência de uma pendência NOVA prova que ela já foi
    atendida? Só o sinal forte: um bloco "PELA <alvo>" com avaliação feita no
    trecho (avaliação real: a IA reemitiu "aguardo avaliação da infectologia"
    citando "Pela INFECTOLOGIA: paciente avaliado."). Qualquer outra coisa
    cria o item e o detector decide."""
    kind_target = kind_and_target(item)
    if kind_target is None or kind_target[0] != KIND_CONSULT or not item["evidence"]:
        return False
    return any(
        block.label == kind_target[1]
        and specialties.evaluation_status(block.content, block.label, pela_block=True) == specialties.STATUS_ANSWER
        for block in specialties.pela_blocks(item["evidence"])
    )


def puncture_already_done(item, quote: str | None, note: DetectorNote, seen_before: dict | None = None) -> bool:
    """Anotação que registra a punção JÁ FEITA não abre pendência (plano do
    PEND-001, caso 3 do auditor): o trecho citado -- ou o que vem depois dele
    na mesma evolução -- tem o sucesso do tipo pedido. Sucesso ANTES do
    trecho não conta (o pedido pode ser uma punção nova), nem sucesso datado
    antes da evolução, de histórico ou copiado (3ª revisão: "AVP puncionado
    em 08/08" na lista de dispositivos derrubava o pedido novo)."""
    kind_target = kind_and_target(item)
    if kind_target is None or kind_target[0] != KIND_PUNCTURE or not quote:
        return False
    time = _parse_local(note.timestamp) or datetime(2000, 1, 1)
    view = _view(time, note)
    wanted = _comparable(quote)[:60]
    start = next((index for index, sentence in enumerate(view.sentences)
                  if wanted and (wanted in sentence.comparable or sentence.comparable in wanted and sentence.comparable)),
                 None)
    if start is None:
        return False
    view.sentences = view.sentences[start:]
    earlier = {key: first for key, first in (seen_before or {}).items() if first < time}
    context = _Context(reference=time - timedelta(microseconds=1), seen_before=earlier, item_text=fold(item["description"]))
    found = _puncture_evidence(kind_target[1], view, context)
    return found is not None and found.action == ACTION_RESOLVE


def notes_blob(texts) -> str:
    """Texto das evoluções enviadas à IA, em forma comparável (sem acento,
    sem pontuação) -- base de `quote_in_notes`."""
    return " " + " ".join(_comparable(text) for text in texts) + " "


def quote_in_notes(quote: str | None, blob: str) -> bool:
    """O trecho citado pela IA está mesmo nas evoluções -- INTEIRO? Achado da
    avaliação real: o modelo copiou como "evidência" a linha da lista de
    pendências do próprio prompt. 3ª revisão: conferir só o começo de trecho
    longo deixava passar um final inventado ("..., transferido para a UTI")."""
    comparable = _comparable(quote)
    if len(comparable) < 12:
        return False
    return f" {comparable} " in blob


def notes_containing(quote: str | None, notes: list[DetectorNote]) -> list[DetectorNote]:
    """Evoluções (com data) que contêm o trecho citado, da mais antiga pra mais nova."""
    found = [note for note in notes if note.timestamp and quote_in_notes(quote, notes_blob([note.text]))]
    return sorted(found, key=lambda note: note.timestamp)
