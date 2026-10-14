"""LLM-006 (pedido do auditor, DEC-129 seções 2 e 6): fonte da evolução pelo
cargo do profissional e faixas de normalidade de sinais vitais. Os cargos e
nomes de setor abaixo são os rótulos genéricos que o GSUS usa (medidos só
por contagem no banco de dev) -- não identificam paciente nem profissional."""
import pytest

from app.analysis import sources, vital_ranges


@pytest.mark.parametrize("role", [
    "Médico residente", "Médico clínico", "Médico generalista", "Médico ortopedista e traumatologista",
    "Médico cirurgião geral", "Médico em medicina intensiva", "Médico pediatra", "Médico infectologista",
    "Médico ginecologista e obstetra", "MÉDICO NEFROLOGISTA",
])
def test_physician_roles(role):
    assert sources.classify_source(role) == sources.SOURCE_MEDICA
    assert sources.is_physician(role)
    assert sources.source_label(role) == "EVOLUÇÃO MÉDICA"


@pytest.mark.parametrize("role", [
    "Técnico de enfermagem", "Técnico de Enfermagem", "Enfermeiro", "Enfermeiro de terapia intensiva",
    "Auxiliar de enfermagem",
])
def test_nursing_roles(role):
    assert sources.classify_source(role) == sources.SOURCE_ENFERMAGEM


@pytest.mark.parametrize("role", [
    "Fisioterapeuta geral", "Fisioterapeuta respiratória", "Nutricionista", "Assistente social",
    "Psicólogo hospitalar", "Psicólogo clínico", "Fonoaudiólogo geral", "Farmacêutico",
    "Farmacêutico hospitalar e clínico",
])
def test_multiprofessional_roles(role):
    assert sources.classify_source(role) == sources.SOURCE_MULTIPROFISSIONAL


@pytest.mark.parametrize("role", [None, "", "Direita", "Esquerda", "Evolução"])
def test_parser_leftovers_are_never_treated_as_physician_notes(role):
    """~1,5% das evoluções do banco de dev têm "Direita"/"Esquerda" no lugar
    do cargo (sobra do parser) -- nunca podem ganhar peso de evolução médica."""
    assert sources.classify_source(role) == sources.SOURCE_NAO_IDENTIFICADA
    assert not sources.is_physician(role)


@pytest.mark.parametrize("unit,population", [
    ("ENF. MEDICO-CIRURGICA 1 (30 leitos)", vital_ranges.POPULATION_ADULT),
    ("UTI Geral (12 leitos)", vital_ranges.POPULATION_ADULT),
    ("UTI Adulto (8 leitos)", vital_ranges.POPULATION_ADULT),
    ("3 - Internados P.A.", vital_ranges.POPULATION_ADULT),
    ("Enfer. GO (28 leitos)", vital_ranges.POPULATION_OBSTETRIC),  # 2ª revisão: valores fetais e metas da gestação
    ("Enfer. GO-AC (28 leitos)", vital_ranges.POPULATION_PEDIATRIC),
    ("Enfer.-GO-AC (28 leitos)", vital_ranges.POPULATION_PEDIATRIC),
    ("Enf. Pediátrica (24 leitos)", vital_ranges.POPULATION_PEDIATRIC),
    ("Enfer.-Pediatria (23 leitos)", vital_ranges.POPULATION_PEDIATRIC),
    ("UTI PED 1 (6 leitos)", vital_ranges.POPULATION_PEDIATRIC),
    ("Neonatal - UTI (10 leitos)", vital_ranges.POPULATION_PEDIATRIC),
    ("Neonatal - Semi Intensivo - UCI (4 leitos)", vital_ranges.POPULATION_PEDIATRIC),
    (None, vital_ranges.POPULATION_ADULT),
])
def test_unit_decides_whether_adult_ranges_apply(unit, population):
    assert vital_ranges.population_for_unit(unit) == population


def test_adult_ranges_match_the_proposal_sent_to_the_auditor():
    assert vital_ranges.is_within_range("FR", 18) is True  # caso 5 do auditor
    assert vital_ranges.is_within_range("FR", 11) is False
    assert vital_ranges.is_within_range("FR", 21) is False
    assert vital_ranges.is_within_range("SPO2", 94) is True
    assert vital_ranges.is_within_range("SPO2", 93) is False
    assert vital_ranges.is_within_range("TAX", 37.7) is True
    assert vital_ranges.is_within_range("TAX", 37.8) is False
    assert vital_ranges.is_within_range("XYZ", 1) is None


def test_prompt_block_lists_every_range_in_brazilian_notation():
    block = vital_ranges.prompt_block()
    for fragment in ("FR 12-20 irpm", "FC 60-100 bpm", "TAX 36-37,7 °C", "SPO2 >= 94%", "HGT 70-180 mg/dL"):
        assert fragment in block
