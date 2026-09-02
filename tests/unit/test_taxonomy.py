"""Testes de app/analysis/taxonomy.py (RF-22)."""
from app.analysis.taxonomy import (
    CATEGORY_DIAGNOSTICO,
    CATEGORY_INTERCONSULTA,
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    SUBTYPES_BY_CATEGORY,
    VALID_CATEGORIES,
    default_origin,
    is_valid_category,
    is_valid_subtype,
)


def test_exactly_seven_categories():
    """A orientação técnica é explícita: taxonomia FECHADA de 7 categorias."""
    assert len(VALID_CATEGORIES) == 7


def test_every_category_has_an_outro_escape_subtype():
    """Taxonomia fechada não pode travar num caso real não previsto --
    cada categoria precisa de uma válvula de escape."""
    for category, subtypes in SUBTYPES_BY_CATEGORY.items():
        assert "OUTRO" in subtypes, f"{category} sem subtipo OUTRO"


def test_is_valid_category():
    assert is_valid_category(CATEGORY_DIAGNOSTICO) is True
    assert is_valid_category("CATEGORIA_INVENTADA") is False


def test_is_valid_subtype_none_is_always_valid():
    """Subtipo é opcional -- None nunca é erro."""
    assert is_valid_subtype(CATEGORY_DIAGNOSTICO, None) is True


def test_is_valid_subtype_checks_the_right_category():
    valid_for_diagnostico = SUBTYPES_BY_CATEGORY[CATEGORY_DIAGNOSTICO][0]
    valid_for_interconsulta = SUBTYPES_BY_CATEGORY[CATEGORY_INTERCONSULTA][0]

    assert is_valid_subtype(CATEGORY_DIAGNOSTICO, valid_for_diagnostico) is True
    # subtipo de outra categoria não vale aqui, mesmo sendo uma string real do sistema
    assert is_valid_subtype(CATEGORY_DIAGNOSTICO, valid_for_interconsulta) is False


def test_is_valid_subtype_unknown_category_rejects_everything_but_none():
    assert is_valid_subtype("CATEGORIA_INVENTADA", "QUALQUER_SUBTIPO") is False
    assert is_valid_subtype("CATEGORIA_INVENTADA", None) is True


def test_default_origin_returns_valid_origin_for_every_category():
    for category in VALID_CATEGORIES:
        assert default_origin(category) in (ORIGIN_INTERNAL, ORIGIN_EXTERNAL)


def test_default_origin_unknown_category_falls_back_to_internal():
    assert default_origin("CATEGORIA_INVENTADA") == ORIGIN_INTERNAL
