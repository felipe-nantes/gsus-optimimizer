"""Testa app/gsus/census.py contra fixtures HTML sintéticas locais (prompt
mestre seção 35) -- sem acessar o GSUS real, sem dado de paciente real.
Usa Playwright/Chromium só para carregar HTML estático (`file://`); não
tem relação com a escolha de engine para o GSUS real (Firefox, DEC-010)."""
from pathlib import Path
from urllib.request import pathname2url

import pytest
from playwright.sync_api import sync_playwright

from app.gsus.census import GSUSCensusError, collect_all_pages

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "gsus_html"


def _file_url(name: str) -> str:
    return "file:" + pathname2url(str(FIXTURES_DIR / name))


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


def test_single_page_extracts_all_patients(page):
    page.goto(_file_url("census_page.html"))
    patients = collect_all_pages(page)

    assert len(patients) == 3
    assert patients[0].record_number == "100001"
    assert patients[0].bed == "2A"
    assert patients[0].unit == "Clínica Médica"
    assert patients[0].admission_date == "15/08/2026"


def test_empty_results_returns_empty_list(page):
    page.goto(_file_url("census_page_empty.html"))
    patients = collect_all_pages(page)
    assert patients == []


def test_max_patients_stops_early_within_same_page(page):
    """Pedido do usuário 2026-08-20: encurtar o teste pegando só o
    primeiro paciente, sem mexer no comportamento padrão (sem limite)."""
    page.goto(_file_url("census_page.html"))  # tem 3 pacientes numa página só
    patients = collect_all_pages(page, max_patients=1)
    assert len(patients) == 1
    assert patients[0].record_number == "100001"


def test_max_patients_avoids_paginating_to_next_page(page):
    page.goto(_file_url("census_page_p1.html"))  # página 1 de 2, 1 paciente cada
    patients = collect_all_pages(page, max_patients=1)
    assert len(patients) == 1
    assert patients[0].record_number == "200001"  # o da página 1 -- nunca foi pra página 2


def test_max_patients_none_keeps_full_behavior():
    """Confirma que omitir max_patients continua buscando tudo -- sem
    mudança de comportamento pra quem já usa a função sem o parâmetro."""
    import inspect

    from app.gsus.census import collect_all_pages as fn

    assert inspect.signature(fn).parameters["max_patients"].default is None


def test_duplicate_record_number_kept_once(page):
    page.goto(_file_url("census_page_duplicate.html"))
    patients = collect_all_pages(page)
    assert len(patients) == 1
    assert patients[0].record_number == "300001"


def test_pagination_follows_proxima_across_pages(page):
    page.goto(_file_url("census_page_p1.html"))
    patients = collect_all_pages(page)

    assert len(patients) == 2
    assert {p.record_number for p in patients} == {"200001", "200002"}
    # confirma que realmente navegou (não só leu a página 1 duas vezes)
    assert any(p.bed == "4B" for p in patients)


def test_header_row_as_th_inside_tbody_is_skipped_not_erred(page):
    """Reproduz DEC-015: cabeçalho como <th> dentro do <tbody> (sem
    <thead> separado) não deve ser tratado como paciente nem levantar erro."""
    page.goto(_file_url("census_page_th_in_tbody.html"))
    patients = collect_all_pages(page)
    assert len(patients) == 1
    assert patients[0].record_number == "400001"


def test_wrong_table_with_few_columns_raises_clear_error(page):
    """Reproduz DEC-014: se a tabela localizada tem <td> reais mas de
    menos, é sinal de que pegamos a tabela errada -- deve falhar rápido e
    claro, não travar num timeout genérico do Playwright."""
    page.goto(_file_url("census_page_wrong_table_columns.html"))
    with pytest.raises(GSUSCensusError):
        collect_all_pages(page)


def test_pagination_waits_for_async_content_swap(page):
    """Reproduz DEC-016: paginação real é assíncrona (AJAX, sem navegação
    de página) -- a extração da 2ª página não pode rodar antes do conteúdo
    trocar de verdade."""
    page.goto(_file_url("census_page_ajax.html"))
    patients = collect_all_pages(page)

    assert len(patients) == 2
    assert {p.record_number for p in patients} == {"500001", "500002"}


def test_no_patient_name_field_ever_extracted(page):
    """Garantia estrutural: Patient não tem campo de nome -- a automação
    nunca guarda o nome do paciente, só identificadores administrativos."""
    page.goto(_file_url("census_page.html"))
    patients = collect_all_pages(page)
    for p in patients:
        assert not hasattr(p, "name")
        assert not hasattr(p, "patient_name")
