"""Especialidade canônica a partir do cargo do profissional, de texto livre
e de linhas "PELA <ESPECIALIDADE>" (PEND-001, pedido do auditor, DEC-129
seção 4).

Uma interconsulta só pode ser fechada automaticamente se o sistema souber
QUAL especialidade ela espera e reconhecer, numa evolução posterior, que essa
especialidade avaliou. Dois sinais, medidos só em agregado no banco de dev
(cópia parada em 03/09):
- o cargo do autor (`notes.source_type`): "Médico nefrologista", "Médico
  ortopedista e traumatologista", "Médico cirurgião do aparelho digestivo"...;
- uma linha que COMEÇA com "PELA <especialidade>" (o residente da
  especialidade escrevendo): NEFROLOGIA 40 + "NEFRO" 5, CIRURGIA GERAL 26,
  FARMÁCIA 25, ORTOPEDIA 9, abreviações ("REUMATO", "INFECTO", "CIR",
  "OTORRINO") e até erros de digitação ("NEUROILOGIA", "CIRURGA").

Padrões com fronteira de palavra de propósito: "pneumonia", "hematoma",
"nefrostomia", "urocultura", "ortopneia" e "angiotomografia" começam como
uma especialidade e NÃO são uma. Na dúvida, nenhuma especialidade -- o item
fica aberto, nunca é fechado por palpite. Achado da revisão adversarial:
"cirurgia de cabeça e pescoço", "cirurgia oncológica" e "da mão" viravam
CIRURGIA GERAL, e "acesso vascular" virava CIRURGIA VASCULAR -- a evolução
diária de OUTRA equipe fechava a interconsulta.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

CARDIOLOGIA = "CARDIOLOGIA"
NEFROLOGIA = "NEFROLOGIA"
INFECTOLOGIA = "INFECTOLOGIA"
REUMATOLOGIA = "REUMATOLOGIA"
NEUROLOGIA = "NEUROLOGIA"
NEUROCIRURGIA = "NEUROCIRURGIA"
ORTOPEDIA = "ORTOPEDIA"
CIRURGIA_GERAL = "CIRURGIA GERAL"
CIRURGIA_APARELHO_DIGESTIVO = "CIRURGIA DO APARELHO DIGESTIVO"
CIRURGIA_VASCULAR = "CIRURGIA VASCULAR"
CIRURGIA_TORACICA = "CIRURGIA TORÁCICA"
CIRURGIA_PLASTICA = "CIRURGIA PLÁSTICA"
CIRURGIA_PEDIATRICA = "CIRURGIA PEDIÁTRICA"
CIRURGIA_CARDIOVASCULAR = "CIRURGIA CARDIOVASCULAR"
CIRURGIA_CABECA_PESCOCO = "CIRURGIA DE CABEÇA E PESCOÇO"
CIRURGIA_ONCOLOGICA = "CIRURGIA ONCOLÓGICA"
CIRURGIA_MAO = "CIRURGIA DA MÃO"
CIRURGIA_BARIATRICA = "CIRURGIA BARIÁTRICA"
BUCOMAXILOFACIAL = "CIRURGIA BUCOMAXILOFACIAL"
UROLOGIA = "UROLOGIA"
GASTROENTEROLOGIA = "GASTROENTEROLOGIA"
ENDOSCOPIA = "ENDOSCOPIA"
ENDOCRINOLOGIA = "ENDOCRINOLOGIA"
HEMATOLOGIA = "HEMATOLOGIA"
PNEUMOLOGIA = "PNEUMOLOGIA"
OFTALMOLOGIA = "OFTALMOLOGIA"
OTORRINOLARINGOLOGIA = "OTORRINOLARINGOLOGIA"
PSIQUIATRIA = "PSIQUIATRIA"
DERMATOLOGIA = "DERMATOLOGIA"
ONCOLOGIA = "ONCOLOGIA"
MASTOLOGIA = "MASTOLOGIA"
COLOPROCTOLOGIA = "COLOPROCTOLOGIA"
GERIATRIA = "GERIATRIA"
GINECOLOGIA_OBSTETRICIA = "GINECOLOGIA E OBSTETRÍCIA"
PEDIATRIA = "PEDIATRIA"
CLINICA_MEDICA = "CLÍNICA MÉDICA"
MEDICINA_INTENSIVA = "MEDICINA INTENSIVA"
ANESTESIOLOGIA = "ANESTESIOLOGIA"
RADIOLOGIA = "RADIOLOGIA"
NUTRICAO = "NUTRIÇÃO"
FISIOTERAPIA = "FISIOTERAPIA"
FISIOTERAPIA_MOTORA = "FISIOTERAPIA MOTORA"
FISIOTERAPIA_RESPIRATORIA = "FISIOTERAPIA RESPIRATÓRIA"
FONOAUDIOLOGIA = "FONOAUDIOLOGIA"
PSICOLOGIA = "PSICOLOGIA"
SERVICO_SOCIAL = "SERVIÇO SOCIAL"
FARMACIA = "FARMÁCIA"
ESTOMATERAPIA = "ESTOMATERAPIA"
TERAPIA_OCUPACIONAL = "TERAPIA OCUPACIONAL"
ODONTOLOGIA = "ODONTOLOGIA"

# "cirurgia", "cirurgião", "cirurgiã", "cir", "cir.", "cx" e o erro real "cirurga".
_CIR = r"(?:cirurgi(?:a|ao|ae|oes)|cirurga|cir\.?|cx\.?)"
_SURGICAL_QUALIFIER = (
    r"(?:vascular|toracic|plastic|pediatric|infantil|cardiac|cardiovasc|oncolog|bucomaxilo|maxilo|cabeca|mao\b"
    r"|bariatric|aparelho|digestiv|trauma|endovascular|urolog|gineco|neuro|colo|proct|ortop|hepat|endocrin"
    r"|mastolog|torac)"
)

# Ordem não importa: entre dois casamentos que se sobrepõem vence o que
# começa antes e, empatado, o mais longo ("cirurgia vascular" vence
# "cirurgia"; "cirurgião pediátrico" vence "pediátrico").
_PATTERNS: tuple[tuple[str, str], ...] = (
    (CARDIOLOGIA, r"\bcardio(?:log\w*)?\b"),
    (NEFROLOGIA, r"\bnefro(?:log\w*)?\b"),
    (INFECTOLOGIA, r"\binfecto(?:log\w*)?\b"),
    (REUMATOLOGIA, r"\breumato(?:log\w*)?\b"),
    (NEUROCIRURGIA, r"\bneurocirurgi\w*|\bneuro\s*-?\s*" + _CIR + r"(?!\w)|\bncr\b"),
    (NEUROLOGIA, r"\bneuro(?:log\w*|ilogia)?\b"),
    # "Traumatologia bucomaxilofacial" é a bucomaxilo, não a ortopedia
    # (2ª revisão adversarial: o ortopedista fechava o pedido).
    (ORTOPEDIA, r"\bortoped\w*|\borto\b|\btraumato(?:log\w*)?\b(?!\s*-?\s*(?:e\s+)?buco)"),
    (CIRURGIA_APARELHO_DIGESTIVO, r"(?:\b" + _CIR + r"\s+(?:d[oa]\s+)?)?\baparelho\s+digestivo\b"),
    # "vascular" sozinho só quando não é "acesso/doença/lesão vascular" -- o
    # time de acesso vascular (punção) não é a cirurgia vascular.
    (CIRURGIA_VASCULAR, r"\b" + _CIR + r"\s+vascular\b|(?<!acesso )(?<!acessos )(?<!doenca )(?<!lesao )"
                        r"(?<!insuficiencia )(?<!malformacao )\bvascular\b|\bangiolog\w*"),
    (CIRURGIA_TORACICA, r"\b" + _CIR + r"\s+toracic[ao]\b"),
    (CIRURGIA_PLASTICA, r"\b" + _CIR + r"\s+plastic[ao]\b|\bplastica\b"),
    (CIRURGIA_PEDIATRICA, r"\b" + _CIR + r"\s+(?:pediatric[ao]|infantil)\b"),
    (CIRURGIA_CARDIOVASCULAR, r"\b" + _CIR + r"\s+(?:cardiovascular|cardiac[ao])\b"),
    (CIRURGIA_CABECA_PESCOCO, r"(?:\b" + _CIR + r"\s+(?:de\s+|da\s+)?)?\bcabeca\s+e\s+pescoco\b|\bccp\b"),
    (CIRURGIA_ONCOLOGICA, r"\b" + _CIR + r"\s+oncologic[ao]\b"),
    (CIRURGIA_MAO, r"\b" + _CIR + r"\s+d[ae]\s+mao\b"),
    (CIRURGIA_BARIATRICA, r"\b" + _CIR + r"\s+bariatric[ao]\b"),
    (BUCOMAXILOFACIAL, r"(?:\b" + _CIR + r"\s+(?:e\s+)?)?(?:\btraumatolog\w*\s+)?"
                       r"(?:\bbuco\s*-?\s*maxilo\w*(?:\s*-?\s*facial)?|\bmaxilo\s*-?\s*facia\w*|\bbmf\b|\bctbmf\b|\bbuco\b)"),
    # "CIRURGIA" sozinha (e "CIR", "CX", "CIRURGA") = cirurgia geral -- é o
    # serviço que as evoluções chamam assim ("PELA CIRURGIA", 30 ocorrências).
    # Regra POSITIVA (2ª revisão adversarial: a lista de qualificadores
    # proibidos nunca fecha -- "maxilofacial", "ginecológica", "endovascular"
    # viravam cirurgia geral): só "geral", fim de frase/pontuação ou uma
    # palavra que claramente não qualifica a cirurgia. Qualquer outra coisa
    # ("cirurgia de ...", "cirurgia endócrina") é outra equipe: nenhuma.
    # Pontuação logo depois só vale quando não abre um qualificador: "PELA
    # CIRURGIA (VASCULAR)", "CIRURGIA - VASCULAR", "CX/TORÁCICA" são outra
    # equipe (3ª revisão).
    (CIRURGIA_GERAL, r"\b" + _CIR + r"(?:\s+geral\b|(?=[ \t]*(?:$|\n|(?![(\-/–][ \t]*" + _SURGICAL_QUALIFIER
                     + r")[^\w\s]))|(?=\s+(?:e|para|pra|p/|por|pois|devido|sobre"
                     r"|hoje|amanha|ja|nao|sem|com|que|apos|se|ou|aguard\w*|solicit\w*|avali\w*|indic\w*|orient\w*"
                     r"|sugere|conduta|ciente|mantem|segue|paciente|pcte|pct)\b))"),
    (UROLOGIA, r"\burolog\w*|\buro\b"),
    (GASTROENTEROLOGIA, r"\bgastro(?:enterolog\w*)?\b|\bgastroclinica\b"),
    (ENDOSCOPIA, r"\bendoscopi(?:a|sta)\b"),
    (ENDOCRINOLOGIA, r"\bendocrino(?:log\w*)?\b"),
    (HEMATOLOGIA, r"\bhemato(?:log\w*)?\b"),
    (PNEUMOLOGIA, r"\bpneumo(?:log\w*)?\b"),
    (OFTALMOLOGIA, r"\boftalmo(?:log\w*)?\b"),
    (OTORRINOLARINGOLOGIA, r"\botorrino\w*|\borl\b"),
    (PSIQUIATRIA, r"\bpsiquiatr\w*|\bpsiq\b"),
    (DERMATOLOGIA, r"\bdermato(?:log\w*)?\b"),
    (ONCOLOGIA, r"\bonco(?:log\w*)?\b|\boncohemato\w*|\bcancerolog\w*"),
    (MASTOLOGIA, r"\bmastolog\w*"),
    (COLOPROCTOLOGIA, r"\b(?:colo)?proctolog\w*"),
    (GERIATRIA, r"\bgeriatr\w*"),
    (GINECOLOGIA_OBSTETRICIA, r"\bgineco\w*|\bobstet\w*"),
    (PEDIATRIA, r"\bpediatr\w*|\bneonatolog\w*"),
    (CLINICA_MEDICA, r"\bclinica\s+medica\b|\bclinico\s+geral\b"),
    (MEDICINA_INTENSIVA, r"\bmedicina\s+intensiva\b|\bintensivista\b"),
    (ANESTESIOLOGIA, r"\banestesiolog\w*|\banestesia\b"),
    (RADIOLOGIA, r"\bradiolog\w*"),
    (NUTRICAO, r"\bnutri(?:cao|cionista|cional)?\b"),
    # Fisioterapia motora e respiratória são equipes diferentes nas UTIs: o
    # fisioterapeuta respiratório não fecha um pedido de fisio MOTORA.
    (FISIOTERAPIA_MOTORA, r"\bfisio(?:terap\w*)?\s+motora\b"),
    (FISIOTERAPIA_RESPIRATORIA, r"\bfisio(?:terap\w*)?\s+respiratori[ao]\b"),
    (FISIOTERAPIA, r"\bfisio(?:terap\w*)?\b"),
    (FONOAUDIOLOGIA, r"\bfono(?:audiolog\w*)?\b|\bfonoaudiolog\w*"),
    (PSICOLOGIA, r"\bpsicolog\w*|\bpsico\b"),
    (SERVICO_SOCIAL, r"\bservico\s+social\b|\bassistente\s+social\b"),
    (FARMACIA, r"\bfarmacia\b|\bfarmaceutic\w*"),
    (ESTOMATERAPIA, r"\bestomaterap\w*"),
    (TERAPIA_OCUPACIONAL, r"\bterapia\s+ocupacional\b|\bterapeuta\s+ocupacional\b"),
    (ODONTOLOGIA, r"\bodontolog\w*|\bdentista\b"),
)
_COMPILED = tuple((label, re.compile(pattern)) for label, pattern in _PATTERNS)

# Cargos que NÃO dizem a especialidade: o residente pode ser de qualquer
# serviço; o "generalista" é o plantonista/hospitalista de qualquer ala --
# usar o cargo dele fecharia interconsulta de clínica médica com a evolução
# diária da própria equipe. Esses só contam pela linha "PELA <esp>". O
# "Médico clínico" (1.888 evoluções no banco de dev) é o cargo dos
# plantonistas de enfermaria e PS: a 2ª revisão adversarial mostrou a
# evolução do plantão noturno fechando a interconsulta de clínica médica.
_ROLES_WITHOUT_SPECIALTY = ("residente", "generalista", "plantonista", "interno", "estagiario", "clinico")

# Cargo -> especialidade, testados nesta ordem (o mais específico antes):
# "Cirurgião-dentista - traumatologista bucomaxilofacial" é a bucomaxilo,
# não a odontologia; "Enfermeiro estomaterapeuta" é a estomaterapia.
_SPECIFIC_ROLE_WORDS: tuple[tuple[str, str], ...] = (
    ("bucomaxilo", BUCOMAXILOFACIAL),
    ("estomaterap", ESTOMATERAPIA),
)
_MULTIPROFESSIONAL_ROLES: tuple[tuple[str, str], ...] = (
    # Fisioterapia respiratória e motora são equipes diferentes nas UTIs; o
    # cargo genérico "Fisioterapeuta" fica genérico (só fecha pedido genérico).
    ("fisioterapeuta respirator", FISIOTERAPIA_RESPIRATORIA),
    ("fisioterapeuta motor", FISIOTERAPIA_MOTORA),
    ("fisioterapeuta neurofuncional", FISIOTERAPIA_MOTORA),
    ("fisioterapeuta traumato", FISIOTERAPIA_MOTORA),
    ("fisioterap", FISIOTERAPIA),
    ("nutricion", NUTRICAO),
    ("psicolog", PSICOLOGIA),
    ("fonoaudi", FONOAUDIOLOGIA),
    ("assistente social", SERVICO_SOCIAL),
    ("farmac", FARMACIA),
    ("terapeuta ocupacional", TERAPIA_OCUPACIONAL),
    ("odontolog", ODONTOLOGIA),
    ("cirurgiao-dentista", ODONTOLOGIA),
    ("cirurgiao dentista", ODONTOLOGIA),
)

# Linha que começa com "PELA/PELO <especialidade>" -- o especialista (ou o
# residente dele) se identificando. "PELA MANHÃ/TARDE/NOITE" e o resto não
# casam com nenhuma especialidade e são ignorados sozinhos.
_PELA_LINE = re.compile(r"(?:^|\n)[ \t\-*•]*pel[ao]s?\s+([^\n]{1,80})")
# "PELA EQUIPE DA NEFROLOGIA", "PELO SERVIÇO DE PSICOLOGIA".
_TEAM_PREFIX = re.compile(r"^(?:equipe|servico|time)\s+(?:d[aeo]s?\s+)?")
# Especialidade QUALIFICADA por um subgrupo: "cardiologia intervencionista",
# "ortopedia (grupo de mão)", "neurologia infantil" -- não é a especialidade
# inteira; a evolução diária da equipe-mãe não responde por ela (2ª revisão).
SUBGROUP_AFTER = re.compile(
    r"^[ \t]*\(?[ \t]*(?:(?:grupo|equipe|setor|ambulatorio)\s+d[aeo]s?\s+[a-z]|intervencion\w*|hemodinam\w*"
    r"|pediatric\w*|infantil|neonat\w*|eletrofisiolog\w*|arritm\w*|transplant\w*|oncolog\w*|da\s+dor\b"
    r"|do\s+sono\b)"
)

# --- O especialista escreveu, mas é avaliação de verdade? (2ª revisão
# adversarial: a lista do que NÃO é avaliação nunca fecha. 3ª revisão:
# palavra clínica solta -- "ciente", "estável", "piora", "mantém" -- também
# não; "será avaliado amanhã" muito menos. O sinal é POSITIVO e FORTE.)
#
# Paciente indisponível / não visto: a especialidade passou e não avaliou.
# Só com o PACIENTE como sujeito -- "diurese ausente", "pulso ausente",
# "sedestação fora do leito", "laparotomia no centro cirúrgico hoje" são
# avaliação (3ª revisão).
NOT_SEEN = re.compile(
    r"\b(?:paciente|pcte|pct)\s+(?:\w+\s+){0,2}?ausente\b"
    r"|\bausente\s+(?:d[oa]|n[oa])\s+(?:leito|quarto|setor|enfermaria|unidade)\b"
    r"|\b(?:paciente|pcte|pct)\s+(?:se\s+)?(?:(?:encontrava|encontra)\s*-?\s*se\s+|estava\s+|esta\s+)?"
    r"fora\s+do\s+(?:leito|quarto|setor)\b"
    r"|\bnao\s+se\s+encontrava\b|\bnao\s+estava\s+(?:no\s+(?:leito|quarto|setor)|presente)\b"
    r"|\btentativa\s+de\s+avaliac\w*|\bnao\s+(?:foi\s+|e\s+)?possivel\s+(?:avaliar|examinar|realizar\s+a\s+avaliacao)\b"
    r"|\bnao\s+(?:pode|pude|pudemos|podendo)\s+ser\s+avaliad\w*"
    r"|\bsem\s+condic\w*\s+de\s+(?:ser\s+)?avaliad\w*|\bsem\s+condic\w*\s+de\s+avaliac\w*"
    r"|\b(?:esta|estava|encontra\s*-?\s*se|encontrava\s*-?\s*se|paciente|pcte|pct)\s+(?:\w+\s+){0,2}?(?:em|no|na)\s+"
    r"(?:hemodialise|procedimento|exame|centro\s+cirurgico|cirurgia|bloco|transporte|tomografia)\b"
    r"|\b(?:em|no|na)\s+(?:hemodialise|procedimento|exame|centro\s+cirurgico|cirurgia|bloco|transporte)\s+no\s+momento\b"
)
# Só conta como "não visto" quando não há avaliação nenhuma no texto.
NOT_SEEN_WEAK = re.compile(r"\bdormindo\b|\bsonolent\w*")
# "Voltaremos mais tarde" sem nada avaliado = não visto; depois de uma
# avaliação ("Sugerimos manter ceftriaxona. Retornaremos em 48h") é seguimento.
# Futuro na terceira pessoa também (5ª revisão: "nefro avaliará amanhã",
# "nefrologia vai avaliar", "deve avaliar hoje", "para nefro avaliar").
_FUTURE_THIRD_PERSON = (
    r"\b(?:re)?avaliara[o]?\b|\b(?:vai|vao|ira[o]?|deve|devem|devera[o]?|precisa|ficou\s+de|fica\s+de)\s+(?:re)?avaliar\b"
    r"|\bpassara[o]?\b|\bpara\s+(?:a\s+|o\s+)?\w+\s+(?:re)?avaliar\b"
)
RETURN_LATER = re.compile(
    r"\b(?:passaremos|passarei|voltaremos|retornaremos|retornarei|tentaremos|veremos|reavaliaremos"
    r"|avaliaremos)\b|\bretorno\s+(?:mais\s+tarde|posteriormente|a\s+tarde)\b|\bvira[o]?\s+(?:avaliar|ver)\b|"
    + _FUTURE_THIRD_PERSON
)
# Avaliação no FUTURO ou na voz passiva sem ter acontecido: "será avaliado
# amanhã", "a ser avaliado", "avaliado após a sessão" -- retirada do texto
# antes de procurar a avaliação (3ª revisão); "será realizada avaliação",
# "avaliação será feita" (4ª revisão).
_FUTURE_EVALUATION = re.compile(
    r"\b(?:sera|serao|a\s+ser|vai\s+ser|vao\s+ser|ficou\s+de\s+ser|para\s+ser|sendo|deve\s+ser|devera\s+ser"
    r"|deveria\s+ser|precisa\s+ser|aguarda\s+ser|aguardando\s+ser)\s+(?:\w+\s+)?(?:re)?avaliad[oa]s?\b"
    r"|\b(?:re)?avaliad[oa]s?\s+(?:amanha|a\s+tarde|mais\s+tarde|posteriormente|apos|depois|em\s+breve)\b"
    r"|\b(?:sera|serao|a\s+ser|ser|vai\s+ser|ficou\s+de\s+ser|deve\s+ser|devera\s+ser)\s+(?:realizad[oa]|feit[oa])\s+"
    r"(?:a\s+|uma\s+|nova\s+)?(?:re)?avaliacao\b"
    r"|\b(?:re)?avaliacao\s+(?:sera|serao|a\s+ser|vai\s+ser|ficou\s+de\s+ser)\s+(?:realizad[oa]|feit[oa])\b"
)
# Conteúdo FORTE de avaliação: examinou, concluiu, conduziu. Verbo de
# conduta com objeto ("manter vancomicina", "iniciar meropenem"); nada de
# "ciente", "estável", "piora", "mantém oligúria" -- que a equipe que pediu
# também escreve no bloco "PELA X" do próprio plano. 4ª revisão: "mantido EM
# ventilação" é estado do paciente, "iniciada noradrenalina PELA EQUIPE DA
# UTI" é ato de outra equipe, "alta da UTI" é lugar, "risco cirúrgico" sem
# classe/valor é o pedido, "parecer: pendente" é a espera, "HD:" é
# hemodiálise na nota da nefrologia.
_STRONG_CORE = (
    r"\b(?:re)?avaliad[oa]s?\b|\bavaliamos\b|\bavaliei\b|\bexaminad[oa]s?\b|\bao\s+exame\b|\bexame\s+fisico\b"
    r"|\bimpressao\b|\bhipotese\s+diagnostica\b|\bsugir\w*|\bsugiro\b|\bsugerimos\b|\bsugere\s*-?\s*se\b"
    r"|\bindic(?:o|amos|ad[oa]s?)\s+(?!o\s+parecer|a\s+avaliacao|parecer|avaliacao|interconsulta|ic\b)\w+"
    r"|\bcontraindic\w*|\bconduta\s*:"
    r"|\bsem\s+indicac\w*|\bnao\s+(?:ha|vejo|vemos)\s+indicac\w*|\bsem\s+criterios?\b|\bpode\s+prosseguir\b"
    r"|\brisco\s+(?:cirurgico|cardiologico|cardiovascular|anestesico)\s*(?:[:=\-]\s*)?(?:\(\s*)?"
    r"(?:(?:asa|lee|goldman|classe)\s*[:=\-]?\s*(?:i{1,4}v?\b|\d)|baixo|intermediario|moderado|alto|elevado"
    r"|aceitavel|proibitivo|\d|i{1,3}v?\b)"
    r"|\brealizad[oa]\s+(?:a\s+)?avaliacao\b"
    r"|\bavaliacao\s+(?:realizada|feita)\b|\batendimento\s+(?:social|psicologico|realizado)\b|\bacolhimento\b"
    r"|\bentrevista\s+social\b|\bsem\s+(?:necessidade\s+de\s+)?(?:abordagem|intervencao)\b"
    r"|\bseguimos\s+(?:acompanhando|em\s+acompanhamento)\b|\boptamos\b"
    r"|\b(?:parecer|resposta)\s*:(?!\s*(?:pendente|solicitad|aguard|em\s+aberto|a\s+definir|enviad|pedid|reiterad"
    r"|sem\s+resposta|\?|$|hoje|amanha|a\s+ser|realizar|previst|agend|marcad|programad|nao\b|em\s+andamento|-|\d"
    r"|segunda|terca|quarta|quinta|sexta|sabado|domingo))"
)
_NOT_OTHER_TEAM = r"(?!(?:\w+\s+){0,3}?pel[ao]s?\s+(?:equipe|uti|plantao|plantonista|assistente|clinica|enfermagem))"
_STRONG_CONDUCT = (
    r"|\borient(?:o|amos|ad[oa]s?)\b"
    r"|\bmant(?:er|emos|enho|ido|ida|idos|idas)\s+(?!aguard|em\b|n[oa]s?\b|sob\b|com\s+(?:quadro|piora))"
    + _NOT_OTHER_TEAM + r"\w+"
    r"|\binici(?:ar|ado|ada|amos|o|ei)\s+" + _NOT_OTHER_TEAM + r"\w+"
    r"|\bsuspend(?:er|emos|ido|ida)\b|\bsuspens[oa]s?\b|\bajust(?:ar|ado|ada|amos)\b|\bprescri(?:to|ta|vemos)\b"
    r"|\bescalon\w*|\bdescalon\w*|\btroc(?:ar|ado|ada|amos)\b|\bassociar\b|\bacrescent(?:ar|ado|ada)\b"
    r"|\balta\s+d[ao]\s+(?!uti\b|cti\b|enfermaria|unidade|hospital|setor|sala|leito|semi\w*|observacao|ps\b)\w+"
)
STRONG_EVALUATION = re.compile(_STRONG_CORE + _STRONG_CONDUCT)
# Avaliação FEITA pelo próprio especialista -- a única que vale contra
# "retornaremos/avaliaremos/virá avaliar" (4ª revisão: "Paciente mantido em
# VM, sedado. Retornaremos amanhã", "Orientada enfermagem quanto ao jejum.
# Avaliaremos após o exame" fechavam). 5ª revisão: lista curta, sem "exame
# físico"/"ao exame" ("retornaremos para exame físico"), "indicado X",
# "parecer:" e "risco", e "avaliado" nunca por OUTRA equipe ("avaliado pela UTI").
_BY_OTHER_TEAM = r"(?!\s+(?:\w+\s+){0,2}?pel[ao]s?\s+(?:equipe|uti|cti|plantao|plantonista|assistente|clinica|enfermagem|ps\b))"
_EVALUATION_DONE = re.compile(
    r"\b(?:re)?avaliad[oa]s?\b" + _BY_OTHER_TEAM + r"|\bexaminad[oa]s?\b" + _BY_OTHER_TEAM
    + r"|\bavaliamos\b|\bavaliei\b|\bexaminamos\b|\bexaminei\b|\bsugir\w*|\bsugiro\b|\bsugerimos\b|\bsugere\s*-?\s*se\b"
    r"|\bconduta\s*:|\bsem\s+indicac\w*|\bnao\s+(?:ha|vejo|vemos)\s+indicac\w*|\bsem\s+criterios?\b|\bindic(?:o|amos)\b"
    r"|\bimpressao\b|\bhipotese\s+diagnostica\b|\boptamos\b|\bpode\s+prosseguir\b"
    r"|\batendimento\s+(?:social|psicologico|realizado)\b|\bacolhimento\b|\bentrevista\s+social\b"
    r"|\brealizad[oa]\s+(?:a\s+)?avaliacao\b(?!\s+(?:\w+\s+)?pel[ao]s?\b)"
)
# Exame/cultura logo depois de "solicitado"/"aguardar": é o ESPECIALISTA
# pedindo exame ("PELA INFECTO: solicitados ECO TE e hemoculturas"), não a
# espera da interconsulta.
_EXAMISH = re.compile(
    r"\b(?:tc|tomografia\w*|rm|rnm|ressonancia\w*|rx|raio|usg|ultrass\w*|eco|ecocardiogra\w*|ete|ett|doppler\w*"
    r"|hemoculturas?|uroculturas?|culturas?|exames?|laboratori\w*|biopsias?|endoscopia|eda|colonoscopia"
    r"|cintilografia|holter|eeg|gasometria|hemograma|funcao\s+renal|resultado\w*|laudo\w*)\b"
)
# A linha "PELA X" que é o plano/a espera da equipe que PEDIU: "PELA NEFRO:
# aguardando", "PELA NEFROLOGIA: solicitado parecer hoje", "IC enviada",
# "a avaliar", "feito pedido de parecer", "acionada, virá avaliar".
PELA_WAITING_START = re.compile(
    r"^[\s:.,;\-*•]*(?:aguard\w*|solicitad\w*|pedid\w*|enviad\w*|acionad\w*|encaminhad\w*|reiter\w*|pendente\w*"
    r"|pendencia\w*|cobrar|cobrad\w*"
    r"|feit[oa]\s+(?:o\s+|a\s+)?(?:pedido|ic\b|interconsulta|solicitac\w*|parecer)"
    r"|(?:ic|interconsulta|parecer|avaliacao|solicitacao)\s+(?:ja\s+)?(?:foi\s+)?(?:solicitad|pedid|enviad"
    r"|encaminhad|feit|reiterad)\w*|a\s+avaliar\b|a\s+ser\s+avaliad\w*|sera\s+avaliad\w*|vira[o]?\s+avaliar|em\s+aberto"
    r"|sem\s+resposta|\?)"
)
# Espera/pedido da PRÓPRIA interconsulta, em qualquer ponto do texto -- o
# que vem depois (ou logo antes) diz de QUAL especialidade é: "aguarda
# parecer da cardiologia" na evolução da nefrologia não é sobre a nefrologia.
WAITING_FOR_CONSULT = re.compile(
    r"\baguard\w*\s+(?:a\s+|o\s+)?(?:avaliacao|reavaliacao|parecer|resposta|interconsulta|visita|ser\s+avaliad[oa]"
    r"|retorno(?!\s+d[oa]s?\s+(?:exame|resultado|cultura|laudo|hemocultura|urocultura|usg|tc|rx|biopsia))"
    r"|contato|especialista)"
    r"|\bsem\s+(?:resposta|retorno|parecer|avaliacao)\b"
    r"|\b(?:ainda\s+)?nao\s+(?:foi\s+)?(?:avaliad[oa]|avaliou|vist[oa]|viu|passou|veio|compareceu|respond\w*)\b"
    r"|\breiter\w*\s+(?:a\s+)?(?:solicitac\w*|pedido|parecer|avaliacao|interconsulta|ic)\b"
    r"|\bsolicit\w*\s+(?:nova\s+|novamente\s+)?(?:a\s+)?(?:avaliacao|parecer|interconsulta|ic|reavaliacao)\b"
    # Pedido escrito com o substantivo antes ("IC enviada", "parecer
    # solicitado hoje") e indicação do pedido ("indicado parecer da X").
    r"|\b(?:ic|interconsulta|parecer|avaliacao|reavaliacao|solicitacao)\s+(?:ja\s+)?(?:foi\s+)?"
    r"(?:solicitad|pedid|enviad|encaminhad|feit|reiterad)\w*"
    r"|\b(?:indicad[oa]|indico|necessari[oa]|necessit\w*|precis\w*)\s+(?:o\s+|a\s+|de\s+)?"
    r"(?:parecer|avaliacao|interconsulta|ic|reavaliacao)\b"
    r"|\baguardando\s*(?=[.;,!]|$)"
)
# Só contam quando a especialidade está colada: "aguardando nefrologia", "IC
# nefrologia pendente" (3ª revisão). Soltos ("aguardar culturas") não.
_BOUND_ONLY_WAITING = re.compile(r"\baguard\w*\b|\bpendente\b")
# Espera em QUALQUER ponto do bloco "PELA X" (o assunto do bloco é X): "PELA
# NEFROLOGIA: iniciada furosemida, aguarda.", "mantida hidratação, IC em
# aberto", "..., parecer pendente", "equipe ainda não compareceu" (4ª
# revisão: só o começo do bloco era olhado). Exame/resultado pendente do
# próprio especialista ("cultura pendente") não conta.
_PELA_WAITING_ANY = re.compile(
    r"\baguard\w*\s*(?=[.;,!?]|$)|\bpendente\b|\bpendencia\b|\bem\s+aberto\b|\bsem\s+resposta\b"
    r"|\bvira[o]?\s+avaliar\b|\bacionad[oa]s?\b|\b(?:ainda\s+)?nao\s+(?:passou|veio|compareceu|avaliou|viu)\b|"
    + _FUTURE_THIRD_PERSON
)
_EXAM_BEFORE_PENDING = re.compile(
    r"\b(?:tc|tomografia\w*|rm|rnm|ressonancia\w*|rx|raio\s*-?\s*x|usg|ultrass\w*|eco|ecocardiogra\w*|ete|ett"
    r"|doppler\w*|hemoculturas?|uroculturas?|culturas?|exames?|biopsias?|resultados?|laudos?|antibiograma|tsa"
    r"|sorologias?|bioquimica|hemograma|gasometria)\b[^.;,]{0,25}$"
)
_PARTICIPLE_REQUEST = re.compile(
    r"^(?:solicitad|pedid|enviad|encaminhad|feit|reiterad|ic\b|interconsulta|parecer|avaliacao|reavaliacao|solicitacao)"
)
# Entre o sinal e a especialidade: preposição, parêntese e o nome da
# interconsulta ("reitero solicitação DE PARECER DA nefrologia", "aguarda
# parecer (nefrologia)").
_AFTER_CUE_PREFIX = re.compile(
    r"^\s*\(?\s*(?:(?:de|da|do|das|dos|com|para|p/|pel[ao]s?|a|o)\s+)?"
    r"(?:(?:parecer|avaliacao|interconsulta|ic|resposta|visita|reavaliacao|retorno)\s+)?\(?\s*"
    r"(?:(?:de|da|do|das|dos|com|para|p/|pel[ao]s?|a|o)\s+)?(?:(?:equipe|servico|time)\s+(?:d[aeo]s?\s+)?)?"
)
# Antes do sinal: "Nefrologia: aguarda avaliação", "Nefrologia - aguardando",
# "IC nefrologia pendente".
# O artigo é palavra inteira: sem o \b, "nefro ainda não avaliou" perdia o
# "o" final e virava "nefr" (5ª revisão).
_BEFORE_CUE_SPECIALTY = re.compile(r"(?:\b(?:a|o)\s+)?\s*[:\-–]?\s*(?:ainda\s+)?$")


def fold(text: str | None) -> str:
    """Minúsculas sem acento -- todo casamento deste módulo é sobre isto."""
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower()


def label_spans(compiled, folded: str) -> list[tuple[int, int, str]]:
    """(início, fim, rótulo) sem sobreposição: entre dois casamentos que se
    sobrepõem fica o que começa antes e, empatado, o mais longo. Usado
    também pelos exames de `specialty_evaluation`."""
    found = []
    for label, pattern in compiled:
        for match in pattern.finditer(folded):
            if match.end() > match.start():
                found.append((match.start(), match.end(), label))
    found.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    accepted: list[tuple[int, int, str]] = []
    for start, end, label in found:
        if accepted and start < accepted[-1][1]:
            continue
        accepted.append((start, end, label))
    return accepted


def specialty_spans(folded: str) -> list[tuple[int, int, str]]:
    return label_spans(_COMPILED, folded)


def specialties_in_text(text: str | None) -> list[str]:
    """Especialidades citadas, na ordem em que aparecem, sem repetição."""
    seen: list[str] = []
    for _start, _end, label in specialty_spans(fold(text)):
        if label not in seen:
            seen.append(label)
    return seen


def specialty_from_text(text: str | None) -> str | None:
    """Primeira especialidade citada no texto, ou None."""
    found = specialties_in_text(text)
    return found[0] if found else None


@lru_cache(maxsize=512)
def specialty_from_role(role: str | None) -> str | None:
    """Especialidade do autor pelo cargo do GSUS, ou None quando o cargo não
    diz (residente, generalista, plantonista "Médico clínico", enfermagem,
    sobra do parser) ou diz uma equipe sem rótulo próprio ("cirurgião do
    trauma")."""
    folded = fold(role).strip()
    if not folded:
        return None
    for word, label in _SPECIFIC_ROLE_WORDS:
        if word in folded:
            return label
    for prefix, label in _MULTIPROFESSIONAL_ROLES:
        if folded.startswith(prefix):
            return label
    if not folded.startswith("medic"):
        return None
    rest = re.sub(r"^medic[oa]\s+(?:em\s+)?", "", folded)
    if not rest or rest.startswith(_ROLES_WITHOUT_SPECIALTY):
        return None
    spans = specialty_spans(rest)
    return spans[0][2] if spans and spans[0][0] == 0 else None


def specialty_span_at_start(rest: str) -> tuple[str, int] | None:
    """(especialidade, fim) quando `rest` começa por uma especialidade não
    qualificada por subgrupo ("cardiologia intervencionista" não é a
    cardiologia inteira -- 2ª revisão adversarial)."""
    spans = specialty_spans(rest[:80])
    if not spans or spans[0][0] != 0:
        return None
    if SUBGROUP_AFTER.match(rest[spans[0][1]:spans[0][1] + 80]):
        return None
    return spans[0][2], spans[0][1]


def _specialty_at_start(rest: str) -> str | None:
    found = specialty_span_at_start(rest)
    return found[0] if found else None


# "PELA NEFROLOGIA: aguardamos USG" -- o cabeçalho é quem escreve, não o
# sujeito da espera.
_PELA_HEADER_BEFORE = re.compile(r"\bpel[ao]s?\s+(?:(?:equipe|servico)\s+(?:d[aeo]s?\s+)?)?$")


def _bound_specialties(folded: str, start: int, end: int, *, after: bool = True, before: bool = True) -> list[str]:
    """Especialidade(s) a que um sinal de espera se refere: logo depois
    ("aguarda parecer DA CARDIOLOGIA", "aguarda parecer (nefrologia)") ou
    logo antes ("NEFROLOGIA ainda não avaliou", "Nefrologia: aguarda
    avaliação"). Vazio = sinal solto."""
    found = []
    if after:
        text_after = folded[end:end + 80]
        prefix = _AFTER_CUE_PREFIX.match(text_after)
        label = _specialty_at_start(text_after[prefix.end():] if prefix else text_after)
        if label:
            found.append(label)
    if before:
        text_before = folded[max(0, start - 60):start]
        tail = _BEFORE_CUE_SPECIALTY.search(text_before)
        head = text_before[:tail.start()] if tail else text_before
        for span_start, span_end, span_label in specialty_spans(head):
            if not head[span_end:].strip() and not _PELA_HEADER_BEFORE.search(head[:span_start]):
                found.append(span_label)
    return found


# Verbos na voz ativa sem sujeito ("não respondeu ao esquema") só contam com
# a especialidade ao lado; "não foi avaliado"/"aguarda parecer" contam soltos.
_ACTIVE_VOICE_CUE = re.compile(r"avaliou|viu|passou|veio|compareceu|respond")


def waiting_about(
    folded: str, target: str, *, unbound_counts: bool = True, include_requests: bool = True,
    specialist_note: bool = False,
) -> bool:
    """O texto diz que a interconsulta de `target` ainda não foi respondida
    (espera, reiteração, "não avaliado", pedido da própria interconsulta)?
    - Sinal ligado a OUTRA especialidade não conta ("aguarda parecer da
      cardiologia" na evolução da nefrologia; "solicito avaliação da cirurgia
      vascular" no bloco da infectologia é o especialista encaminhando).
    - Sinal solto conta só quando `unbound_counts` (no bloco "PELA X" o
      assunto é X); "aguardando"/"pendente" soltos nunca ("aguardar culturas").
    - Sem `include_requests`, pedido novo ("solicito nova avaliação da X") não
      conta -- depois de uma resposta ele é pendência NOVA, não conflito.
    - Na evolução ASSINADA pela especialidade (`specialist_note`), o motivo
      transcrito ("Solicitado parecer da nefrologia por IRA. Paciente
      avaliado...") e o pedido a uma equipe fora da tabela não contam; o
      próprio especialista pedindo a MESMA especialidade conta (3ª revisão)."""
    for match in WAITING_FOR_CONSULT.finditer(folded):
        text = match.group(0)
        is_request = text.startswith("solicit") or _PARTICIPLE_REQUEST.match(text) is not None
        if not include_requests and is_request:
            continue
        bound = _bound_specialties(folded, match.start(), match.end())
        if bound:
            if target in bound:
                if specialist_note and _PARTICIPLE_REQUEST.match(text):
                    continue  # motivo transcrito do pedido, não a espera
                return True
            continue
        if specialist_note and is_request:
            continue
        if unbound_counts and not _ACTIVE_VOICE_CUE.search(text):
            return True
    for match in _BOUND_ONLY_WAITING.finditer(folded):
        # "aguardando NEFROLOGIA" (especialidade depois); "IC NEFROLOGIA
        # pendente" (antes).
        waiting = match.group(0).startswith("aguard")
        if target in _bound_specialties(folded, match.start(), match.end(), after=waiting, before=not waiting):
            return True
    return False


STATUS_ANSWER = "answer"
STATUS_WAITING = "waiting"
STATUS_NOT_SEEN = "not_seen"
STATUS_CONFLICT = "conflict"
STATUS_UNCLEAR = "unclear"


def _first_clause(text: str) -> str:
    return re.split(r"[.;]", text, maxsplit=1)[0]


def has_strong_evaluation(folded: str) -> bool:
    """Avaliação feita, com conteúdo forte -- a avaliação no futuro ("será
    avaliado amanhã") não conta."""
    return STRONG_EVALUATION.search(_FUTURE_EVALUATION.sub(" ", folded)) is not None


def evaluation_status(folded: str, target: str, *, pela_block: bool, orders_exam: bool = False) -> str:
    """O que um texto da especialidade (bloco "PELA X" ou evolução assinada
    por ela) diz sobre a interconsulta de `target`:
    - not_seen: o paciente não foi visto (indisponível; "voltaremos" ou
      "dormindo" sem nada avaliado) -- a especialidade tentou, o auditor confere;
    - waiting: é a espera/o pedido da interconsulta, não a resposta;
    - conflict: traz avaliação E volta a tratar a interconsulta como
      pendente ("sem indicação de HD. CONDUTA: reitero parecer") -- o
      auditor confere;
    - answer: há avaliação feita, com conteúdo forte (examinou, concluiu,
      conduziu). Na evolução assinada pela especialidade, o especialista
      pedindo exames (`orders_exam`) também é avaliação (caso 2 do auditor);
    - unclear: nada disso ("Ciente.", cabeçalho solto, "sessão de HD sem
      intercorrências") -- no máximo verificação, nunca concluída."""
    if NOT_SEEN.search(folded):
        return STATUS_NOT_SEEN
    strong = has_strong_evaluation(folded) or (orders_exam and not pela_block)
    if pela_block:
        start = PELA_WAITING_START.match(folded)
        if start and not _EXAMISH.search(_first_clause(folded[start.end():])):
            return STATUS_CONFLICT if strong else STATUS_WAITING
        if pela_waiting_anywhere(folded, target):
            return STATUS_CONFLICT if strong else STATUS_WAITING
    if waiting_about(folded, target, unbound_counts=True, specialist_note=not pela_block):
        return STATUS_CONFLICT if strong else STATUS_WAITING
    if RETURN_LATER.search(folded) or NOT_SEEN_WEAK.search(folded):
        # Contra "voltaremos/virá avaliar" só vale avaliação FEITA ("avaliado",
        # "sugiro", "sem indicação"); conduta solta pode ser estado do
        # paciente ou ato de outra equipe (4ª revisão).
        done = _EVALUATION_DONE.search(_FUTURE_EVALUATION.sub(" ", folded)) is not None
        if not done:
            return STATUS_NOT_SEEN
    return STATUS_ANSWER if strong else STATUS_UNCLEAR


def pela_waiting_anywhere(folded: str, target: str) -> bool:
    """Bloco "PELA X" que diz, em qualquer ponto, que a interconsulta de X
    ainda espera -- a espera ligada a OUTRA especialidade, ou a exame do
    próprio especialista ("cultura pendente"), não conta."""
    for match in _PELA_WAITING_ANY.finditer(folded):
        bound = _bound_specialties(folded, match.start(), match.end())
        if bound and target not in bound:
            continue
        text = match.group(0)
        if (text.startswith(("aguard", "pendent", "pendenc")) and not bound
                and _EXAM_BEFORE_PENDING.search(folded[max(0, match.start() - 60):match.start()])):
            continue
        return True
    return False


@dataclass(frozen=True)
class PelaBlock:
    """Um bloco "PELA <especialidade>": a linha do cabeçalho e o que vem com
    ela (resto da linha + linhas seguintes até linha em branco, outro
    cabeçalho de seção, outro "PELA" ou linha de outra especialidade)."""
    label: str
    header: str  # linha original do cabeçalho
    text: str  # bloco original (cabeçalho + conteúdo), numa linha só
    content: str  # dobrado: o que vem depois da especialidade
    header_folded: str
    previous_folded: str  # linha anterior não vazia, dobrada ("HISTÓRICO:")
    bare: bool  # cabeçalho sem conteúdo nenhum
    # Dobrado: "pela <especialidade>" e a data colada nele ("(18/08):") --
    # data/histórico só contam aqui; dentro do conteúdo é conteúdo.
    head: str = ""


# Cabeçalho de seção numa linha só: "CONDUTA:", "PLANO", "EXAMES:".
_SECTION_HEADER = re.compile(r"^[ \t\-*•]*[A-ZÀ-Ú][A-ZÀ-Ú0-9 /.]{2,40}:?[ \t]*$")
_TERMINAL_PUNCTUATION = re.compile(r"[.:;!?)\]]\s*$")
_DATE_ATTACHED_TO_HEADER = re.compile(r"^\s*[(\-:]*\s*(?:em\s+|de\s+|dia\s+)?\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?\s*\)?")
# Formato de cabeçalho: depois da especialidade vem ":", "-" ou o fim da linha.
_HEADER_SHAPE = re.compile(r"^\s*(?:$|[:\-–])")
# Linha de cima que continua em "pela X" ("aguarda avaliação\npela nefrologia",
# "solicitado parecer, a ser realizado\nPELA NEFROLOGIA").
_CONTINUES_INTO_PELA = re.compile(
    r"(?:avaliacao|avaliad[oa]s?|realizad[oa]s?|solicitad[oa]s?|parecer|feit[oa]s?|vist[oa]s?|examinad[oa]s?"
    r"|acompanhad[oa]s?|aguard\w*|\bde|\bpor|\bser|\be|\bcom)\s*$"
)
# Linha de OUTRA especialidade dentro do bloco: "Cardio: aguarda avaliação".
_OTHER_SPECIALTY_LINE = re.compile(r"^[ \t\-*•]*")
_MAX_BLOCK_LINES = 6


def pela_blocks(text: str | None) -> tuple[PelaBlock, ...]:
    """Blocos "PELA <especialidade>" da evolução. A especialidade tem de vir
    LOGO depois do "PELA" (ou de "equipe/serviço de") -- "PELA MANHÃ, avaliado
    pela cardiologia" não conta. Frase quebrada no meio não conta (2ª e 3ª
    revisões): linha de cima sem pontuação final que continua em "pela X"
    ("aguarda avaliação\npela nefrologia."), ou "pela X" sem formato de
    cabeçalho (":" / "-" / fim da linha) logo abaixo de linha sem pontuação
    -- inclusive em evolução toda em MAIÚSCULAS."""
    if not text or "pel" not in text.lower():
        return ()
    original_lines = text.split("\n")
    folded_lines = fold(text).split("\n")
    blocks: list[PelaBlock] = []
    for index, line in enumerate(folded_lines):
        match = _PELA_LINE.match("\n" + line)
        if not match:
            continue
        rest = match.group(1).strip()
        # "PELO SERVIÇO SOCIAL": o "serviço" é parte do nome -- tenta antes
        # de tirar o prefixo de equipe.
        found = specialty_span_at_start(rest)
        if found is None:
            stripped = _TEAM_PREFIX.match(rest)
            if stripped:
                inner = specialty_span_at_start(rest[stripped.end():])
                found = (inner[0], stripped.end() + inner[1]) if inner else None
        if found is None:
            continue
        label, label_end = found
        original = original_lines[index] if index < len(original_lines) else line
        same_line = rest[label_end:]
        previous_index = index - 1
        while previous_index >= 0 and not folded_lines[previous_index].strip():
            previous_index -= 1
        previous_folded = folded_lines[previous_index] if previous_index >= 0 else ""
        if previous_index == index - 1 and previous_folded.strip() and not _TERMINAL_PUNCTUATION.search(previous_folded):
            if _CONTINUES_INTO_PELA.search(previous_folded) or not _HEADER_SHAPE.match(same_line):
                continue  # prosa quebrada em duas linhas
        content_lines = [same_line] if same_line.strip(" \t:.-") else []
        original_block = [original.strip()]
        skipped_blank = False
        for next_index in range(index + 1, len(folded_lines)):
            folded_next = folded_lines[next_index]
            original_next = original_lines[next_index] if next_index < len(original_lines) else folded_next
            if not folded_next.strip():
                if not content_lines and not skipped_blank:
                    skipped_blank = True
                    continue
                break
            if _PELA_LINE.match("\n" + folded_next) or _SECTION_HEADER.match(original_next):
                break
            other = specialty_span_at_start(folded_next[_OTHER_SPECIALTY_LINE.match(folded_next).end():])
            if other is not None and other[0] != label:
                after_label = folded_next[_OTHER_SPECIALTY_LINE.match(folded_next).end() + other[1]:]
                if re.match(r"^\s*[:\-–]", after_label):
                    break  # linha de outra especialidade ("Cardio: aguarda avaliação")
            content_lines.append(folded_next)
            original_block.append(original_next.strip())
            if len(content_lines) >= _MAX_BLOCK_LINES:
                break
        content = " ".join(part.strip() for part in content_lines).strip()
        attached_date = _DATE_ATTACHED_TO_HEADER.match(same_line)
        head = "pela " + rest[:label_end] + (attached_date.group(0) if attached_date else "")
        blocks.append(PelaBlock(
            label=label, header=original.strip(), text=" ".join(original_block),
            content=content, header_folded=line, previous_folded=previous_folded,
            bare=not content.strip(" \t:.-"), head=head,
        ))
    return tuple(blocks)


# Cabeçalho de especialidade SEM "pela": "NEFROLOGIA:", "Nefro:", "Nefrologia -
# evolução:", "EVOLUÇÃO NEFROLOGIA" -- só usado para saber que a especialidade
# já evoluía o paciente antes do pedido (4ª revisão), nunca como prova.
# Depois do nome: "(HD)", "18/08", "/HD", "- evolução", travessão; e então
# ":", " - " ou o fim da linha (5ª revisão: só ":" e "-" eram aceitos).
_HEADER_TAIL = re.compile(
    r"[ \t]*(?:\([^)\n]{0,30}\)[ \t]*)?(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?[ \t]*)?(?:/[ \t]*\w+[ \t]*)?"
    r"(?:[-–—][ \t]*(?:evolucao|parecer|avaliacao|seguimento)[ \t]*)?(?::|[ \t][-–—](?:[ \t]|$)|\n|$)",
    re.MULTILINE,
)
# Uma varredura só por evolução: linha que COMEÇA por uma especialidade
# (o custo de procurar especialidade no texto todo pesava no PC fraco).
_HEADER_CANDIDATE = re.compile(
    r"(?m)^[ \t\-*•#]*(?:evolucao\s+(?:d[aeo]\s+)?)?(?:(?:equipe|servico)\s+d[aeo]s?\s+)?(?="
    + "|".join(f"(?:{pattern})" for _label, pattern in _PATTERNS) + ")"
)


def specialty_header_blocks(text: str | None, folded: str | None = None) -> list[tuple[str, str, list[str]]]:
    """(especialidade, conteúdo dobrado, linhas do conteúdo) de cada linha que
    abre com o nome de uma especialidade em forma de cabeçalho ("NEFROLOGIA:",
    "Nefro (HD) -", "Equipe de Nefrologia:")."""
    if not text:
        return []
    folded = fold(text) if folded is None else folded
    blocks: list[tuple[str, str, list[str]]] = []
    for candidate in _HEADER_CANDIDATE.finditer(folded):
        found = specialty_span_at_start(folded[candidate.end():candidate.end() + 80])
        if found is None:
            continue
        label, end = found[0], candidate.end() + found[1]
        tail = _HEADER_TAIL.match(folded, end)
        if tail is None:
            continue
        lines = []
        for line in folded[tail.end():tail.end() + 1500].split("\n")[:_MAX_BLOCK_LINES]:
            if not line.strip():
                if lines:
                    break
                continue
            lines.append(line)
        blocks.append((label, " ".join(lines), lines))
    return blocks


def specialty_header_labels(text: str | None, folded: str | None = None) -> list[str]:
    """Especialidades que escreveram sob um cabeçalho com o nome delas, com
    conteúdo de avaliação logo abaixo/ao lado."""
    labels: list[str] = []
    for label, content, _lines in specialty_header_blocks(text, folded):
        # Linha-problema da equipe que pede ("Nefro: IRA, mantida hidratação,
        # solicitado parecer") é espera, não a especialidade evoluindo.
        if label not in labels and evaluation_status(content, label, pela_block=True) == STATUS_ANSWER:
            labels.append(label)
    return labels


def pela_specialties(text: str | None) -> list[tuple[str, str]]:
    """(especialidade, linha original do cabeçalho) de cada bloco "PELA
    <especialidade>" em que a especialidade de fato se manifesta: nem a
    espera/o plano da equipe que pediu ("PELA NEFRO: aguardando", "IC
    enviada"), nem "paciente não visto". Cabeçalho solto conta aqui (quem
    decide entre concluir e verificar é o detector)."""
    return [
        (block.label, block.header) for block in pela_blocks(text)
        if evaluation_status(block.content, block.label, pela_block=True) in (STATUS_ANSWER, STATUS_UNCLEAR)
    ]
