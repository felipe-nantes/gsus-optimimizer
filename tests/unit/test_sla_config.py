"""Testes de app/analysis/sla_config.py (RF-23)."""
from app.analysis.sla_config import get_sla_hours, is_over_sla
from app.analysis.taxonomy import CATEGORY_DIAGNOSTICO, CATEGORY_TERAPEUTICA


def test_get_sla_hours_known_category():
    assert get_sla_hours(CATEGORY_DIAGNOSTICO) == 48


def test_get_sla_hours_terapeutica_has_no_limit():
    """TERAPEUTICA é meta clínica, não espera de terceiro -- não faz
    sentido SLA genérico (orientação técnica, seção 4)."""
    assert get_sla_hours(CATEGORY_TERAPEUTICA) is None


def test_get_sla_hours_unknown_category_returns_none():
    assert get_sla_hours("CATEGORIA_INVENTADA") is None


def test_is_over_sla_true_when_elapsed_exceeds_limit():
    assert is_over_sla(CATEGORY_DIAGNOSTICO, 49) is True


def test_is_over_sla_false_when_within_limit():
    assert is_over_sla(CATEGORY_DIAGNOSTICO, 47) is False


def test_is_over_sla_false_when_no_limit_defined():
    assert is_over_sla(CATEGORY_TERAPEUTICA, 1000) is False


def test_is_over_sla_false_when_hours_elapsed_unknown():
    """Nunca declara atraso sem saber o tempo decorrido (RF-23)."""
    assert is_over_sla(CATEGORY_DIAGNOSTICO, None) is False
