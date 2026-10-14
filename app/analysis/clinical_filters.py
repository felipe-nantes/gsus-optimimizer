"""Filtros determinísticos de EXIBIÇÃO do resumo clínico (LLM-006, pedido do
auditor, DEC-129 seções 1 e 6; casos de aceitação 1, 4 e 5).

Nunca alteram o texto gravado pela IA (`patient_state` continua com a saída
original, auditável -- RF-26/RF-30): só a versão mostrada no relatório e na
dashboard passa por aqui, e o relatório oferece o texto original sempre que
algo foi ajustado. São a rede de segurança do prompt v2 -- na avaliação com o
modelo real, o 8B ainda pôs rotina de enfermagem no contexto.

Princípio (achado das duas revisões adversariais da Fase 2): na dúvida, o
texto FICA. Cada filtro só remove o que comprovadamente é rotina,
qualificador colado a um valor normal de adulto ou localização presente
contradita pelo censo -- qualquer palavra a mais mantém o trecho.

- Rotina: parte (entre vírgulas, ou ligada por "e") feita SÓ de rotina --
  "mantém AVP em MSE", "recebe cuidados de enfermagem", "monitorização de
  sinais vitais". "Em monitorização por FA", "AVP com sinais flogísticos",
  "AVP para meropenem", "aceitando dieta" ficam.
- Sinal vital: só o ADJETIVO colado ao nome ou ao número ("FR baixa (18
  irpm)", "FC 88 bpm, taquicárdico", "Taquicárdico (FC 88)"), em setor
  adulto, quando TODOS os números do trecho estão na faixa e não há faixa/
  tendência. Nunca substantivo ("febre"), nunca com negação no trecho,
  nunca com suporte de O2/ventilação no texto, nunca com contexto fetal,
  nunca "alta" depois do número (pode ser a alta do paciente).
- Localização: "em/no/na leito de observação" no presente, para paciente em
  UTI pelo censo. Frase com histórico ("ficou", "saiu", "vindo de") ou com
  duração/data fica.
- Repetição: campo cujo conteúdo INTEIRO já foi exibido num campo anterior
  some; qualquer palavra ou sigla nova mantém o campo.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from app.analysis.vital_ranges import POPULATION_ADULT, is_within_range, population_for_unit

FIX_ROUTINE = "rotina"
FIX_VITAL = "sinal_vital"
FIX_LOCATION = "localizacao"
FIX_REPEAT = "repeticao"


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower()


def _fold_keep_length(text: str) -> str:
    """Minúsculas sem acento com o MESMO comprimento do texto original --
    as posições achadas no texto dobrado valem pro original."""
    out = []
    for char in text:
        base = unicodedata.normalize("NFKD", char)
        stripped = "".join(c for c in base if not unicodedata.combining(c))
        out.append((stripped[:1] or char).lower())
    return "".join(out)


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    text = re.sub(r"([(])\s+", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"([.;])\s*,\s*", r"\1 ", text)
    text = re.sub(r",\s*([.;]|$)", r"\1", text)
    text = re.sub(r"\.(\s*\.)+", ".", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,;:")


def _capitalize_like(original: str, text: str) -> str:
    """Se o original abria com maiúscula e o resultado não (o começo foi
    removido), devolve a maiúscula."""
    if original[:1].isupper() and text[:1].islower():
        return text[:1].upper() + text[1:]
    return text


def _keep_tail(original: str, text: str) -> str:
    """Devolve o "." ou ";" que fechava a frase original, se a limpeza tirou."""
    tail = original.rstrip()[-1:]
    if text and tail in ".;" and not text.rstrip().endswith((".", ";")):
        return text.rstrip() + tail
    return text


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.;])\s+", text.strip())


def _clauses(sentence: str) -> list[str]:
    """Separa em vírgula seguida de espaço ("37,5" decimal não separa) e
    nunca dentro de parênteses."""
    parts, depth, start = [], 0, 0
    for match in re.finditer(r"[()]|,\s+", sentence):
        token = match.group()
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            parts.append(sentence[start:match.start()])
            start = match.end()
    parts.append(sentence[start:])
    return parts


def _rejoin_clauses(sentence: str, kept: list[str]) -> str:
    return _capitalize_like(sentence, _keep_tail(sentence, ", ".join(kept)))


# ------------------------------------------------------------------ rotina

_ROUTINE_PHRASE = re.compile(
    r"\b(acesso venoso periferico|avp|monitorizacao|monitorizad[oa]s?|cuidados (?:gerais )?de enfermagem|"
    r"cuidados gerais|sinais vitais)\b"
)
# Só contam como rotina JUNTO de uma expressão de rotina ("AVP sem sinais
# flogísticos"); sozinhos descrevem o quadro ("erisipela, sem sinais
# flogísticos") e ficam.
_ROUTINE_COMPANION = re.compile(r"\bsem (?:sinais flogisticos|intercorrencias?|complicac(?:ao|oes))\b")
# Enchimento que pode acompanhar uma expressão de rotina NA MESMA parte.
_ROUTINE_FILLER = {
    "mantem", "mantendo", "mantido", "mantida", "segue", "seguindo", "permanece", "em", "com", "de", "da",
    "do", "das", "dos", "no", "na", "nos", "nas", "sob", "e", "o", "a", "os", "as", "ao", "recebe",
    "recebendo", "rotina", "continua", "continuo", "continuas", "continuos", "salinizado", "salinizada",
    "pervio", "pervia", "permeavel", "msd", "mse", "mid", "mie", "mmss", "mmii", "membro", "membros",
    "superior", "superiores", "inferior", "inferiores", "direito", "direita", "esquerdo", "esquerda",
    "antebraco", "dorso", "mao", "paciente", "multiparametrica", "habituais", "gerais", "geral",
    "incluindo", "estaveis", "estavel", "normais", "dieta", "aceitacao", "aceitando", "aceita", "boa",
    "atualmente", "esta", "estao",
}
# Sobra de uma parte vizinha à rotina removida que também pode sair: só
# palavra vazia. "aceitando dieta" e "estável" NÃO estão aqui -- são
# critério clínico (achado da 2ª revisão).
_LEFTOVER_FILLER = {"atualmente", "paciente", "o", "a", "e", "esta", "segue", "em", "com", "de"}


def _tokens_outside(pattern: re.Pattern, folded: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", pattern.sub(" ", folded))


def _only(words: set[str], text: str) -> bool:
    return all(token in words for token in re.findall(r"[a-z0-9]+", _fold(text)))


def _routine_only(part: str) -> bool:
    folded = _fold(part)
    if not _ROUTINE_PHRASE.search(folded):
        return False
    rest = _tokens_outside(_ROUTINE_PHRASE, _ROUTINE_COMPANION.sub(" ", folded))
    return all(token in _ROUTINE_FILLER for token in rest)


def _strip_routine_clause(clause: str) -> str | None:
    """Trecho com rotina misturada por "e" ("Mantém oxacilina EV e recebe
    cuidados de enfermagem") perde só a parte de rotina; o que sobra ligado
    à rotina pelo mesmo "e" ("... sinais vitais e dieta") sai junto se for só
    enchimento. None = o trecho inteiro era rotina."""
    if not _ROUTINE_PHRASE.search(_fold(clause)):
        return clause
    parts = re.split(r"\s+[eE]\s+", clause)
    kept = [p for p in parts if not _routine_only(p)]
    if len(kept) < len(parts):
        kept = [p for p in kept if not _only(_ROUTINE_FILLER, p)]
    if not kept:
        return None
    if len(kept) == len(parts):
        return clause
    return _capitalize_like(clause, _keep_tail(clause, " e ".join(kept)))


def strip_routine(text: str) -> tuple[str, int]:
    """Remove trechos feitos só de rotina sem complicação (ver docstring do
    módulo). Nunca devolve texto vazio."""
    kept_sentences = []
    removed = 0
    for sentence in _sentences(text):
        clauses = _clauses(sentence)
        stripped = [_strip_routine_clause(c) for c in clauses]
        removed += sum(1 for before, after in zip(clauses, stripped) if after != before)
        kept = [c for c in stripped if c is not None]
        if len(kept) < len(clauses):
            kept = [c for c in kept if not _only(_LEFTOVER_FILLER, c)]
        if kept:
            kept_sentences.append(_rejoin_clauses(sentence, kept))
    if not removed:
        return text, 0
    result = _tidy(" ".join(kept_sentences))
    if not result:
        return text, 0
    return _capitalize_like(text, result), removed


# ------------------------------------------------------------ sinal vital

_NUMBER = re.compile(r"\d{1,3}(?:[.,]\d)?")
_VITAL_NAMES = {
    "FR": r"\b(?:fr|frequencia respiratoria)\b",
    "FC": r"\b(?:fc|frequencia cardiaca)\b",
    "SPO2": r"\b(?:spo2|sato2|sat o2|so2|saturacao)\b",
    "TAX": r"\b(?:tax|t ax|temperatura|temp)\b",
    # Só glicemia CAPILAR: glicemia de jejum/sérica tem outras metas.
    "HGT": r"\b(?:hgt|glicemia capilar|dextro)\b",
}
# Só ADJETIVOS -- substantivos ("febre", "taquicardia", "hipoglicemia")
# descrevem eventos, inclusive passados, e nunca são removidos.
_ADJECTIVES = {
    "FR": r"taquipneic[oa]|bradipneic[oa]",
    "FC": r"taquicardic[oa]|bradicardic[oa]",
    "SPO2": r"dessaturad[oa]|hipoxemic[oa]",
    "TAX": r"febril|hipertermic[oa]|hipotermic[oa]",
    "HGT": r"hipoglicemic[oa]|hiperglicemic[oa]",
}
_GENERIC_BETWEEN = r"baix[oa]|reduzid[oa]|diminuid[oa]|alt[oa]|elevad[oa]|aumentad[oa]"
# Depois do número, "alta" pode ser a alta do paciente ("se FC 80, alta").
_GENERIC_AFTER = r"baix[oa]|reduzid[oa]|diminuid[oa]|elevad[oa]|aumentad[oa]"
_PHRASE_END = r"(?=\s*(?:[,.;)]|$))"  # o qualificador fecha a expressão: "elevada troponina" não conta
_UNIT = r"(?:irpm|rpm|ipm|bpm|%|°\s*c|º\s*c|c|mg/dl|mmhg)"
# Sinais que não são corrigidos, mas delimitam até onde vai o trecho de outro.
_BOUNDARY_NAMES = re.compile(
    "|".join(_VITAL_NAMES.values())
    + r"|\bpa\b|\bpressao arterial\b|\bpam\b|\bpic\b|\bpvc\b|\bbcf\b|\bfcf\b|\bglicemia\b"
)
_OXYGEN_SUPPORT = re.compile(
    r"\bo2\b|cateter|mascara|\bvni\b|\bvm\b|ventila|\biot\b|intubad|\btot\b|\btqt\b|traqueost|cpap|bipap|"
    r"oxigenio|l/min|venturi|alto fluxo|\bcnaf\b|\bpsv\b|\bpcv\b|\bvcv\b|\bsimv\b|fio2|\bpeep\b|nebuliza"
)
_FETAL_CONTEXT = re.compile(r"\bfet[oa]l?\b|fetais|\bbasal\b|cardiotoco|\bctg\b|\bbcf\b|\bfcf\b")
_RANGE_CONNECTOR = re.compile(r"^\s*(?:-|–|a\b|e\b|ate\b|para\b|→|->|/)")
_NEGATOR = re.compile(r"\b(?:nao|nem|nunca|sem|nega|ausencia|deixou de)\b")


def _numbers_all_normal(key: str, segment: str) -> bool:
    numbers = list(_NUMBER.finditer(segment))
    if not numbers:
        return False
    if _RANGE_CONNECTOR.match(segment[numbers[0].end():]) or re.search(r"\bentre\b", segment[: numbers[0].start()]):
        return False
    return all(is_within_range(key, float(n.group().replace(",", "."))) is True for n in numbers)


def _negated(folded: str, position: int) -> bool:
    """Negação em qualquer lugar do trecho antes do qualificador ("não
    está taquicárdico", "nem febril")."""
    clause_start = max(folded.rfind(sep, 0, position) for sep in ",;:.")
    return bool(_NEGATOR.search(folded[clause_start + 1: position]))


def fix_vital_qualifiers(text: str, population: str = POPULATION_ADULT) -> tuple[str, int]:
    if population != POPULATION_ADULT or _OXYGEN_SUPPORT.search(_fold(text)):
        return text, 0
    sentences = _sentences(text)
    fixes = 0
    for index, sentence in enumerate(sentences):
        folded = _fold_keep_length(sentence)
        if _FETAL_CONTEXT.search(folded):
            continue
        boundaries = [m.span() for m in _BOUNDARY_NAMES.finditer(folded)]
        removals: set[tuple[int, int]] = set()
        for key, name_pattern in _VITAL_NAMES.items():
            adjective = _ADJECTIVES[key]
            for name in re.finditer(name_pattern, folded):
                next_name = min((s for s, _e in boundaries if s >= name.end()), default=len(folded))
                segment = folded[name.end():next_name]
                if not _numbers_all_normal(key, segment):
                    continue
                first_number = _NUMBER.search(segment)
                number_start = name.end() + first_number.start()
                number_end = name.end() + first_number.end()
                candidates = []
                between = re.match(
                    rf"\s*:?\s*\b({_GENERIC_BETWEEN}|{adjective})\b\s*\(?\s*$", folded[name.end():number_start]
                )
                if between:
                    candidates.append((name.end() + between.start(1), name.end() + between.end(1)))
                after = re.match(
                    rf"\s*{_UNIT}?\s*(?:em\s+ar\s+ambiente|\(?aa\)?)?\s*,?\s*\b({_GENERIC_AFTER}|{adjective})\b{_PHRASE_END}",
                    folded[number_end:next_name],
                )
                if after:
                    candidates.append((number_end + after.start(1), number_end + after.end(1)))
                before = re.search(rf"\b({adjective})\b\s*\(?\s*$", folded[: name.start()])
                if before:
                    candidates.append((before.start(1), before.end(1)))
                for start, end in candidates:
                    if not _negated(folded, start):
                        removals.add((start, end))
        if not removals:
            continue
        for start, end in sorted(removals, reverse=True):
            sentence = sentence[:start] + sentence[end:]
            fixes += 1
        sentences[index] = _capitalize_like(sentences[index], _keep_tail(sentences[index], _tidy(sentence)))
    if not fixes:
        return text, 0
    return " ".join(s for s in sentences if s), fixes


# -------------------------------------------------------------- localização

_ICU_UNIT = re.compile(r"\bUTI\b|\bUCI\b|INTENSIV")
_OBSERVATION_BED_NOW = re.compile(
    r"\b(?:em|no|na)\s+(?:leito|sala|box)\s+de\s+observacao(?:\s+\d+)?"
    r"(?:\s+d[oa]\s+(?:pa|ps|pronto[- ]?(?:atendimento|socorro)))?\b"
)
# Frase que conta o PASSADO (de onde o paciente veio, quanto tempo ficou) fica.
_HISTORY_MARKER = re.compile(
    r"\b(?:sai[ud]|saida|vind[oa]|veio|provenien|procedent|oriund|transferid|admitid|estava|esteve|"
    r"permaneceu|apos|antes|inicialmente|previamente|anteriormente|intubad|ficou|passou|deixou|subiu|"
    r"deu entrada|foi para|internad)"
)
_EMPTY_CLAUSE_WORDS = {"paciente", "segue", "esta", "permanece", "encontra", "se", "encontrase", "o", "a", "em"}


def fix_location(text: str, unit: str | None) -> tuple[str, int]:
    """Pode devolver texto vazio quando a frase só dizia a localização
    errada -- melhor "(sem contextualização)" que repetir o erro do caso 1."""
    folded_unit = unicodedata.normalize("NFKD", unit or "").upper()
    if not _ICU_UNIT.search(folded_unit):
        return text, 0
    fixes = 0
    kept_sentences = []
    for sentence in _sentences(text):
        if _HISTORY_MARKER.search(_fold(sentence)):
            kept_sentences.append(sentence)
            continue
        kept = []
        for clause in _clauses(sentence):
            folded = _fold_keep_length(clause)
            match = _OBSERVATION_BED_NOW.search(folded)
            if not match:
                kept.append(clause)
                continue
            remaining = _tidy(clause[: match.start()] + clause[match.end():])
            if re.search(r"\d", remaining):  # duração/data: é histórico, não localização atual
                kept.append(clause)
                continue
            fixes += 1
            words = re.findall(r"[a-z0-9]+", _fold(remaining))
            if any(w not in _EMPTY_CLAUSE_WORDS for w in words):
                kept.append(remaining)
        if kept:
            kept_sentences.append(_rejoin_clauses(sentence, kept))
    if not fixes:
        return text, 0
    result = _tidy(" ".join(kept_sentences))
    return _capitalize_like(text, result), fixes


# ----------------------------------------------------------------- repetição

_STOPWORDS = {
    "para", "com", "pela", "pelo", "como", "mais", "menos", "ainda", "apos", "antes", "desde",
    "paciente", "segue", "mantem", "manter", "esta", "sendo", "dias", "hoje", "atual",
}


def _content_tokens(text: str | None) -> set[str]:
    """Palavras de 4+ letras, siglas de 2-3 letras em MAIÚSCULAS (TC, RM,
    EDA, ETE) e negações."""
    text = text or ""
    words = re.findall(r"[a-z0-9]+", _fold(text))
    tokens = {w for w in words if len(w) >= 4 and w not in _STOPWORDS}
    tokens |= {_fold(a) for a in re.findall(r"\b[A-Z][A-Z0-9]{1,2}\b", text)}
    tokens |= {w for w in words if w in {"nao", "sem", "nem"}}
    return tokens


def repeats(candidate: str | None, shown: list[str]) -> bool:
    """True só se TODO o conteúdo de `candidate` já foi exibido num campo
    anterior -- qualquer palavra ou sigla nova (um exame, um verbo oposto)
    mantém o campo."""
    tokens = _content_tokens(candidate)
    if len(tokens) < 3:
        return False
    return any(tokens <= _content_tokens(other) for other in shown)


# -------------------------------------------------------------- composição

@dataclass
class DisplaySummary:
    context: str | None
    justificativa: str | None
    objetivo: str | None
    proximo_passo: str | None
    dia_causa: str | None
    fixes: Counter = field(default_factory=Counter)


def clean_text(text: str | None, unit: str | None, fixes: Counter | None = None) -> str | None:
    """Rotina + sinal vital + localização, nessa ordem, num texto só."""
    if not text:
        return text
    fixes = fixes if fixes is not None else Counter()
    text, n = strip_routine(text)
    fixes[FIX_ROUTINE] += n
    text, n = fix_vital_qualifiers(text, population_for_unit(unit))
    fixes[FIX_VITAL] += n
    text, n = fix_location(text, unit)
    fixes[FIX_LOCATION] += n
    return text


def build_display_summary(state, unit: str | None, main_pending_description: str | None = None) -> DisplaySummary:
    """Versão de exibição dos campos de texto de `patient_state`. `state`
    aceita `sqlite3.Row` ou dict (mesmas chaves)."""
    fixes: Counter = Counter()

    def value(key: str) -> str | None:
        try:
            return state[key]
        except (KeyError, IndexError):
            return None

    context = clean_text(value("clinical_context"), unit, fixes) or None
    shown = [t for t in (context,) if t]
    cleaned = {}
    for key in ("necessidade_hospitalar_justificativa", "objetivo_terapeutico", "proximo_passo", "dia_causa"):
        text = clean_text(value(key), unit, fixes) or None
        compare_with = shown + ([main_pending_description] if key == "dia_causa" and main_pending_description else [])
        if text and repeats(text, compare_with):
            fixes[FIX_REPEAT] += 1
            text = None
        if text:
            shown.append(text)
        cleaned[key] = text
    return DisplaySummary(
        context=context,
        justificativa=cleaned["necessidade_hospitalar_justificativa"],
        objetivo=cleaned["objetivo_terapeutico"],
        proximo_passo=cleaned["proximo_passo"],
        dia_causa=cleaned["dia_causa"],
        fixes=fixes,
    )
