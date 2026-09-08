"""EDD-001: previsão de alta relativa ancorada na data da evolução. Só texto
sintético -- nenhum trecho real de prontuário."""
from datetime import datetime

import pytest

from app.analysis.edd import infer_edd_from_notes, infer_edd_from_text
from app.analysis.rules import StructuredNote

ANCHOR = datetime(2026, 9, 8, 10, 30)


@pytest.mark.parametrize("text, expected", [
    ("Paciente estável. Previsão de alta em 48h.", "2026-09-10"),
    ("previsao de alta em 24 horas se afebril", "2026-09-09"),
    ("Alta em 2 dias.", "2026-09-10"),
    ("Alta hospitalar prevista para 3 dias", "2026-09-11"),
    ("Previsão de alta: 24-48h", "2026-09-10"),          # faixa: limite superior
    ("Provável alta amanhã.", "2026-09-09"),
    ("alta hoje após resultado", "2026-09-08"),
    ("Previsão de alta 12/09", "2026-09-12"),
    ("previsão de alta em 12/09/2026", "2026-09-12"),
    ("Puérpera, GO: alta em 48h.", "2026-09-10"),
])
def test_relative_and_explicit_forecasts_anchor_on_the_note_date(text, expected):
    assert infer_edd_from_text(text, ANCHOR) == expected


@pytest.mark.parametrize("text", [
    "Sem previsão de alta no momento.",
    "sem previsão de alta em 48h",                        # negação vence
    "Alta da UTI em 24h, segue internado.",               # alta de SETOR, não hospitalar
    "Alta do CTI prevista para amanhã",
    "Aguarda parecer da cirurgia.",
    "Previsão de alta a definir.",
    "",
    None,
])
def test_no_forecast_or_negated_or_sector_discharge_yields_none(text):
    assert infer_edd_from_text(text, ANCHOR) is None


def test_most_recent_note_with_a_forecast_wins_and_undated_notes_are_ignored():
    notes = [
        StructuredNote(timestamp="2026-09-06T09:00:00", specialty="MED", source_type="Evolução", text="Alta em 5 dias."),
        StructuredNote(timestamp="2026-09-08T09:00:00", specialty="MED", source_type="Evolução", text="Alta em 48h."),
        StructuredNote(timestamp=None, specialty="MED", source_type="Evolução", text="Alta hoje."),
        StructuredNote(timestamp="2026-09-07T09:00:00", specialty="MED", source_type="Evolução", text="Sem intercorrências."),
    ]
    assert infer_edd_from_notes(notes) == "2026-09-10"


def test_notes_without_any_forecast_yield_none():
    notes = [StructuredNote(timestamp="2026-09-08T09:00:00", specialty="MED", source_type="Evolução", text="Estável.")]
    assert infer_edd_from_notes(notes) is None


# ---------------------------------------------- formas reais (cobertura 2026-09-08)
@pytest.mark.parametrize("text, anchor, expected", [
    ("Previsão de alta: dia 30/08/26.", datetime(2026, 8, 20, 9), "2026-08-30"),
    ("- PREVISÃO DE ALTA: DIA 07/09/26", datetime(2026, 8, 31, 9), "2026-09-07"),
    ("previsão de alta: dia 07/09/26", datetime(2026, 9, 8, 9), "2026-09-07"),   # previsão que venceu: mantida
    ("previsão de alta: em 13/09/2026", datetime(2026, 9, 8, 9), "2026-09-13"),
    ("Alta em 14/08 se exames normais.", datetime(2026, 8, 10, 9), "2026-08-14"),
    ("alta para o dia 15/08", datetime(2026, 8, 10, 9), "2026-08-15"),
    ("Alta prevista dia 02/01", datetime(2026, 12, 28, 9), "2027-01-02"),          # virada do ano
])
def test_real_world_explicit_forms(text, anchor, expected):
    assert infer_edd_from_text(text, anchor) == expected


@pytest.mark.parametrize("text, anchor", [
    ("Internação anterior com alta em 11/07, retornou em 13/07.", datetime(2026, 8, 20, 9)),  # história, não previsão
    ("previsão de alta: sem previsão", datetime(2026, 9, 8, 9)),
    ("previsão de alta: - -", datetime(2026, 9, 8, 9)),
    ("alta para casa com acompanhante", datetime(2026, 9, 8, 9)),
])
def test_real_world_forms_that_are_not_forecasts(text, anchor):
    assert infer_edd_from_text(text, anchor) is None


def test_past_date_without_year_is_not_pushed_a_year_ahead_outside_the_turn_of_the_year():
    # "previsão de alta 10/03" escrita em setembro: previsão antiga que venceu, não março do ano seguinte.
    assert infer_edd_from_text("previsão de alta 10/03", datetime(2026, 9, 8, 9)) == "2026-03-10"
