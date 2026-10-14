"""LLM-006 (pedido do auditor, DEC-129 seções 1 e 6): filtros de exibição do
resumo clínico. Os testes `test_caso*` são os casos mínimos de aceitação da
seção 7 do documento do auditor que esta fase cobre (1, 4 e 5). Todo texto
aqui é fictício."""
import pytest

from app.analysis import clinical_filters as cf
from app.analysis.vital_ranges import POPULATION_PEDIATRIC

UTI = "UTI Geral (12 leitos)"
ENFERMARIA = "ENF. MEDICO-CIRURGICA 1 (30 leitos)"


# --------------------------------------------- casos de aceitação do auditor

def test_caso1_paciente_na_uti_nao_aparece_em_leito_de_observacao():
    text, fixes = cf.fix_location("Paciente em leito de observação, com pneumonia grave em VM.", UTI)
    assert "observação" not in text
    assert "pneumonia grave em VM" in text
    assert fixes == 1


@pytest.mark.parametrize("text", [
    "Saiu do leito de observação para a UTI.",
    "Admitido na UTI vindo de leito de observação do PS por choque séptico.",
    "Proveniente da sala de observação do pronto-socorro.",
    "Estava no leito de observação 2 há 48h.",
])
def test_caso1_historico_de_onde_veio_nao_e_apagado(text):
    """Achado da revisão: "saiu do leito de observação", "vindo de" contam a
    trajetória, não dizem que o paciente ainda está lá."""
    assert cf.fix_location(text, UTI) == (text, 0)


@pytest.mark.parametrize("text,expected", [
    ("Em leito de observação. Choque séptico em noradrenalina.", "Choque séptico em noradrenalina."),
    ("Paciente estável. Em leito de observação, aguarda vaga.", "Paciente estável. Aguarda vaga."),
])
def test_caso1_frase_que_so_dizia_a_localizacao_some_sem_quebrar_o_texto(text, expected):
    assert cf.fix_location(text, UTI) == (expected, 1)


def test_caso1_fora_da_uti_a_frase_fica():
    """Internados no P.A. podem estar mesmo em leito de observação -- a
    correção só vale quando o censo diz UTI."""
    original = "Paciente em leito de observação, com pneumonia."
    assert cf.fix_location(original, "3 - Internados P.A.") == (original, 0)


def test_caso4_rotina_de_enfermagem_sai_do_resumo_e_o_resto_fica():
    text, removed = cf.strip_routine(
        "Pneumonia em uso de ceftriaxona, mantém AVP em MSE, recebe cuidados de enfermagem. "
        "Monitorização de rotina, aguarda TC de tórax."
    )
    assert text == "Pneumonia em uso de ceftriaxona. Aguarda TC de tórax."
    assert removed == 3


def test_caso4_acesso_com_complicacao_continua_no_resumo():
    original = "AVP com flebite em MSD, trocado. Em monitorização invasiva de PAM, noradrenalina."
    assert cf.strip_routine(original) == (original, 0)


def test_caso5_fr_de_18_nao_e_descrita_como_baixa_em_adulto():
    text, fixes = cf.fix_vital_qualifiers("Eupneico, FR baixa (18 irpm), afebril.")
    assert "baixa" not in text
    assert "FR (18 irpm)" in text
    assert fixes == 1
    assert cf.fix_vital_qualifiers("Frequência respiratória de 18 irpm, baixa.")[0] == (
        "Frequência respiratória de 18 irpm."
    )


# -------------------------------------------------- sinal vital, sem excesso

@pytest.mark.parametrize("text", [
    "FR 30 irpm, taquipneico.",                       # alterado de verdade
    "FC 120 bpm, taquicárdico.",
    "Tax 38,2 °C, febril.",
    "SpO2 90% em ar ambiente, dessaturado.",
    "SpO2 95% com O2 2 L/min, dessaturado.",          # com suporte de O2: não mexe
    "FR 18 irpm, alta prevista para amanhã.",          # "alta" do paciente, não da FR
    "FR 18, PA baixa 80x50.",                          # "baixa" é da pressão
])
def test_vital_qualifier_kept_when_abnormal_or_not_about_that_sign(text):
    assert cf.fix_vital_qualifiers(text) == (text, 0)


@pytest.mark.parametrize("text,expected", [
    ("Taquicárdico (FC 88 bpm).", "(FC 88 bpm)."),
    ("Tax 37,5°C, febril.", "Tax 37,5°C."),
    ("SpO2 95% em ar ambiente, dessaturado.", "SpO2 95% em ar ambiente."),
    ("HGT 150 mg/dL, hiperglicêmico.", "HGT 150 mg/dL."),
    ("FC 88 bpm, taquicárdico.", "FC 88 bpm."),
])
def test_vital_qualifier_removed_when_value_is_normal(text, expected):
    assert cf.fix_vital_qualifiers(text)[0] == expected


# Achados da revisão adversarial da Fase 2: o filtro de sinal vital só pode
# tirar ADJETIVO colado a um valor normal -- nunca evento, negação, faixa,
# tendência, outro sinal, nem nada com suporte de O2.
@pytest.mark.parametrize("text", [
    "FC 88 bpm, sem bradicardia ou taquicardia.",
    "Tax 37 °C, sem febre.",
    "SpO2 96%, nega dessaturação.",
    "Febre no D1 de internação, hoje Tax 36,8 C.",
    "Episódio de hipoglicemia às 3h, glicemia atual 110 mg/dL.",
    "Paciente febril há 3 dias, temperatura hoje 37,5.",
    "HGT 150 mg/dL, hiperglicemia.",
    "FC 80 bpm, elevada troponina.",
    "FC 95 bpm, alta resposta ventricular.",
    "HGT 150, alta dose de insulina.",
    "FR 18 irpm, alto fluxo de O2.",
    "FR 18-24 irpm, taquipneico.",
    "FR entre 18 e 28 irpm, taquipneico.",
    "FC de 90 para 135 bpm, taquicárdico.",
    "FC 88 às 14h, taquicárdico às 20h (FC 125), investigar.",
    "FC materna 88 bpm, BCF 175 taquicárdico.",
    "Em VNI, FR 20 irpm, taquipneico com uso de musculatura acessória.",
    "Em O2 por cateter nasal 3 L/min, FC 98 bpm, taquicárdico.",
    "Não taquicárdico (FC 88 bpm).",
])
def test_vital_filter_never_touches_events_negations_ranges_or_other_signs(text):
    assert cf.fix_vital_qualifiers(text) == (text, 0)


def test_pediatric_and_neonatal_units_are_never_qualified_by_adult_ranges():
    text = "RN com FR 40 irpm, taquipneico."
    assert cf.fix_vital_qualifiers(text, POPULATION_PEDIATRIC) == (text, 0)
    assert cf.clean_text(text, "Neonatal - UTI (10 leitos)") == text


# Achados da revisão adversarial da Fase 2: só sai trecho feito SÓ de
# rotina -- motivo de monitorização, droga, complicação e cuidado específico ficam.
@pytest.mark.parametrize("text", [
    "ICC descompensada, em monitorização por FA, furosemida EV.",
    "Pneumonia grave. Monitorização cardíaca contínua por arritmia.",
    "IAM sem supra. Monitorização cardíaca: episódios de TV não sustentada.",
    "Em monitorização por suspeita de TEP, aguarda angio-TC.",
    "TCE grave em VM, monitorização de PIC com valores elevados, sedado.",
    "AVC isquêmico em monitorização neurológica, aguarda RM.",
    "Sepse pulmonar, mantém AVP para meropenem.",
    "Em uso de vancomicina por AVP, nível sérico pendente.",
    "Pós-operatório de colectomia, cuidados de enfermagem com a ferida operatória e dreno.",
    "Pneumonia em ceftriaxona. AVP em MSD com sinais flogísticos, trocado.",
    "AVP com hiperemia e edema no sítio de punção.",
    "Perda de AVP, sem nova punção.",
    "AVP não funcionante, aguarda PICC.",
    "AVP tracionado, repuncionado.",
])
def test_routine_filter_keeps_any_clause_with_real_clinical_content(text):
    assert cf.strip_routine(text) == (text, 0)


def test_routine_mixed_with_treatment_by_and_loses_only_the_routine_part():
    """Forma real que o modelo de 8B escreveu na avaliação com dados
    fictícios (caso B): a rotina vem grudada no tratamento por "e"."""
    text, removed = cf.strip_routine(
        "Erisipela em melhora. Mantém oxacilina EV e recebe cuidados de enfermagem, "
        "incluindo monitorização de sinais vitais e dieta."
    )
    assert text == "Erisipela em melhora. Mantém oxacilina EV."
    assert removed == 2


def test_routine_sentence_with_a_leftover_filler_clause_goes_away_entirely():
    text, _removed = cf.strip_routine(
        "Paciente internado com erisipela. Atualmente, o paciente está recebendo cuidados de "
        "enfermagem e mantém sinais vitais estáveis."
    )
    assert text == "Paciente internado com erisipela."


def test_routine_removal_keeps_the_summary_capitalized():
    assert cf.strip_routine("Mantém AVP; aguarda punção de CVC.")[0] == "Aguarda punção de CVC."


def test_decimal_comma_is_not_treated_as_a_clause_separator():
    assert cf.strip_routine("Tax 37,5, mantém AVP.") == ("Tax 37,5.", 1)


def test_routine_only_text_is_never_emptied():
    assert cf.strip_routine("Monitorização contínua.") == ("Monitorização contínua.", 0)


# ----------------------------------------------------------------- repetição

def test_display_summary_hides_fields_that_only_repeat_earlier_ones():
    state = {
        "clinical_context": "Pneumonia comunitária grave em antibioticoterapia venosa com ceftriaxona.",
        "necessidade_hospitalar_justificativa": "Pneumonia comunitária grave em antibioticoterapia venosa.",
        "objetivo_terapeutico": "Completar antibiótico e desmame de oxigênio.",
        "proximo_passo": "Completar antibiótico e desmame de oxigênio.",
        "dia_causa": "Aguarda resultado da tomografia de tórax.",
    }
    summary = cf.build_display_summary(state, ENFERMARIA, "Aguarda resultado da tomografia de tórax")
    assert summary.context.startswith("Pneumonia")
    assert summary.justificativa is None          # repetia o contexto
    assert summary.objetivo == "Completar antibiótico e desmame de oxigênio."
    assert summary.proximo_passo is None          # repetia o objetivo
    assert summary.dia_causa is None              # repetia a pendência principal
    assert summary.fixes[cf.FIX_REPEAT] == 3


def test_display_summary_keeps_distinct_fields_and_counts_every_fix():
    state = {
        "clinical_context": "Em leito de observação, pneumonia em ceftriaxona, mantém AVP. FR baixa (18 irpm).",
        "necessidade_hospitalar_justificativa": "Antibiótico venoso.",
        "objetivo_terapeutico": "Extubação.",
        "proximo_passo": "Teste de respiração espontânea amanhã.",
        "dia_causa": None,
    }
    summary = cf.build_display_summary(state, UTI)
    assert "observação" not in summary.context and "AVP" not in summary.context and "baixa" not in summary.context
    assert summary.justificativa == "Antibiótico venoso."
    assert summary.proximo_passo == "Teste de respiração espontânea amanhã."
    assert summary.fixes[cf.FIX_ROUTINE] == 1
    assert summary.fixes[cf.FIX_VITAL] == 1
    assert summary.fixes[cf.FIX_LOCATION] == 1
    assert summary.fixes[cf.FIX_REPEAT] == 0


def test_short_fields_are_never_hidden_as_repetition():
    assert cf.repeats("Extubação.", ["Extubação prevista."]) is False


def test_a_longer_field_that_adds_information_is_never_hidden():
    """Achado da revisão: a medida é sobre o PRÓPRIO campo -- um próximo
    passo que contém o objetivo e acrescenta conduta tem que aparecer."""
    assert cf.repeats(
        "Concluir 7 dias de ceftriaxona para sepse de foco urinário e controle de urocultura antes da alta.",
        ["Controle da sepse de foco urinário."],
    ) is False
    assert cf.repeats(
        "Aguarda resultado de urocultura colhida há 5 dias, laboratório terceirizado sem previsão de liberação.",
        ["Aguarda resultado de urocultura"],
    ) is False


def test_opposite_meaning_is_never_treated_as_repetition():
    assert cf.repeats("Não iniciar antibiótico; aguardar cultura.", ["Iniciar antibiótico após cultura."]) is False


# ------------------------------------------- achados da 2ª revisão adversarial

GO = "Enfer. GO (28 leitos)"


@pytest.mark.parametrize("text", [
    "Feto bradicárdico (FC fetal 90 bpm).",
    "Frequência cardíaca fetal de 90 bpm, bradicárdica.",
    "Cardiotocografia com FC basal 95 bpm, bradicárdica.",
    "Glicemia de jejum 150 mg/dL, elevada.",
    "DMG em dieta, glicemia pós-prandial 150 mg/dL, elevada.",
])
def test_obstetric_ward_and_lab_glucose_are_never_qualified_by_adult_capillary_ranges(text):
    assert cf.clean_text(text, GO) == text
    assert cf.fix_vital_qualifiers(text) == (text, 0)  # mesmo fora da obstetrícia: contexto fetal/glicemia de laboratório


@pytest.mark.parametrize("text", [
    "Não está taquicárdico (FC 88 bpm).",
    "Hemodinamicamente estável, não mais taquicárdico (FC 88 bpm).",
    "Já não está febril (Tax 36,5 °C).",
    "Nem febril (Tax 36,8 °C) nem taquicárdico (FC 80 bpm).",
    "Nunca esteve febril (Tax máx 37,2 °C).",
    "Deixou de ser taquicárdico (FC 88 bpm).",
    "Em VNI; FR 20 irpm, taquipneico.",
    "Sob VM. SpO2 95%, dessaturado.",
    "Intubado, em PSV, FR 20 irpm, taquipneico.",
    "Em TQT, SpO2 94%, dessaturado.",
    "Se FC < 100 bpm e Tax 36,8 °C, alta.",
    "Mantendo FC 80 bpm, alta.",
])
def test_vital_filter_respects_negation_ventilation_and_discharge_words(text):
    assert cf.fix_vital_qualifiers(text) == (text, 0)


def test_vital_filter_keeps_the_semicolon_that_closed_the_sentence():
    assert cf.fix_vital_qualifiers("FC 88 bpm, taquicárdico; PA 130x80.")[0] == "FC 88 bpm; PA 130x80."


@pytest.mark.parametrize("text", [
    "Erisipela em MID em regressão, sem sinais flogísticos.",
    "Ferida operatória limpa e seca, sem sinais flogísticos.",
    "PO de apendicectomia D1, aceitando dieta.",
])
def test_clinical_status_words_are_not_routine(text):
    assert cf.strip_routine(text) == (text, 0)


def test_clinical_status_next_to_routine_survives():
    assert cf.strip_routine("PO de apendicectomia D1, aceitando dieta, mantém AVP em MSE.")[0] == (
        "PO de apendicectomia D1, aceitando dieta."
    )
    assert cf.strip_routine("Estável, em monitorização. Choque séptico em melhora.")[0] == (
        "Estável. Choque séptico em melhora."
    )


def test_commas_inside_parentheses_never_split_a_clause():
    text = "Pneumonia (em ceftriaxona, mantém AVP), estável."
    assert cf.strip_routine(text) == (text, 0)


@pytest.mark.parametrize("text", [
    "Admitida no PA em 08/10, em leito de observação por 2 dias, transferida para UTI em 10/10.",
    "Ficou 3 dias em leito de observação do PA.",
    "Anteriormente em leito de observação do PS por 3 dias.",
    "Passou 2 dias em leito de observação, evoluiu com choque.",
])
def test_caso1_trajetoria_com_tempo_ou_verbo_no_passado_fica(text):
    assert cf.fix_location(text, UTI) == (text, 0)


def test_caso1_colon_after_the_removed_location_does_not_dangle():
    assert cf.fix_location("Em leito de observação do PS: sepse, iniciado meropenem.", UTI)[0] == (
        "Sepse, iniciado meropenem."
    )


@pytest.mark.parametrize("candidate,shown", [
    ("EDA para controle da hemorragia digestiva alta.", "Controle da hemorragia digestiva alta."),
    ("Aguarda resultado de hemocultura de controle e ETE.", "Aguarda resultado de hemocultura de controle"),
    ("Aumentar dose de noradrenalina e reavaliar lactato.", "Reduzir dose de noradrenalina e reavaliar lactato."),
    ("Controle da sepse de foco abdominal e relaparotomia.", "Controle da sepse de foco abdominal."),
])
def test_any_new_word_or_acronym_keeps_the_field(candidate, shown):
    assert cf.repeats(candidate, [shown]) is False
