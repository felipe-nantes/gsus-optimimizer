"""Testes de `get_full_admission_history_text` (recurso "Localizar", RF-19) --
só a parte pura (parsing de data / reordenação), sem Playwright/GSUS real.
Dados 100% fictícios (paciente/hospital de teste)."""
from app.gsus.records import _digits_only, _format_episodes_newest_first, _parse_header_date


def test_digits_only_ignores_mask_and_user_punctuation():
    # o campo do GSUS reformata o valor; o usuário também digita com ponto
    assert _digits_only("262.531") == _digits_only("262531")
    assert _digits_only("533.335-6") == "5333356"
    assert _digits_only("") == ""
    assert _digits_only(None) == ""


def test_digits_only_still_distinguishes_different_records():
    assert _digits_only("5333356") != _digits_only("5333366")


def test_parse_header_date_current_admission():
    header = "Internação (19 de Agosto de 2026 - Permanece Internado / Origem do P.A.)"
    assert _parse_header_date(header) == "2026-08-19"


def test_parse_header_date_discharged_episode():
    header = "Internação (05 de Maio de 2020 - Alta em 06 de Maio de 2020 / Origem do P.A.)"
    assert _parse_header_date(header) == "2020-05-05"


def test_parse_header_date_accented_month():
    header = "Internação (01 de Março de 2014 - Alta em 10 de Março de 2014)"
    assert _parse_header_date(header) == "2014-03-01"


def test_parse_header_date_unrecognized_format_returns_none():
    assert _parse_header_date("Internação (formato inesperado)") is None


def test_format_episodes_reorders_newest_first():
    headers = [
        "Internação (05 de Maio de 2014 - Alta em 06 de Maio de 2014)",
        "Internação (10 de Junho de 2020 - Alta em 12 de Junho de 2020)",
        "Internação (19 de Agosto de 2026 - Permanece Internado)",
    ]
    full_text = (
        "Internação (05 de Maio de 2014 - Alta em 06 de Maio de 2014)\nEvolução A\n"
        "Internação (10 de Junho de 2020 - Alta em 12 de Junho de 2020)\nEvolução B\n"
        "Internação (19 de Agosto de 2026 - Permanece Internado)\nEvolução C\n"
    )
    result = _format_episodes_newest_first(full_text, headers)

    idx_2026 = result.index("2026")
    idx_2020 = result.index("2020")
    idx_2014 = result.index("2014")
    assert idx_2026 < idx_2020 < idx_2014
    assert "Evolução A" in result and "Evolução B" in result and "Evolução C" in result
    assert "Internação 1 de 3" in result
    assert "Internação 3 de 3" in result


def test_format_episodes_single_episode_labeled():
    headers = ["Internação (19 de Agosto de 2026 - Permanece Internado)"]
    full_text = "Internação (19 de Agosto de 2026 - Permanece Internado)\nEvolução única\n"
    result = _format_episodes_newest_first(full_text, headers)
    assert "Internação 1 de 1" in result
    assert "Evolução única" in result


def test_format_episodes_no_headers_returns_text_unchanged():
    full_text = "texto sem cabeçalho de internação"
    assert _format_episodes_newest_first(full_text, []) == full_text


def test_format_episodes_finds_headers_in_text_covering_all_episode_types():
    """Sem header_texts, os cabeçalhos vêm do próprio texto -- e precisam
    cobrir tipos diferentes de episódio (DEC-040). Formato real confirmado
    pelo usuário; datas/rótulos aqui são fictícios."""
    full_text = (
        "Internação (16 de Agosto de 2026 - Permanece Internado / Origem do P.A.)\n"
        "HOSPITAL TESTE - Cidade/PR\n"
        "conteudo A\n"
        "Pronto-Atendimento (15 de Agosto de 2026 - 16 de Agosto de 2026)\n"
        "HOSPITAL TESTE - Cidade/PR\n"
        "conteudo B\n"
    )
    result = _format_episodes_newest_first(full_text)
    assert "Internação 1 de 2" in result
    assert "Internação 2 de 2" in result
    assert result.index("conteudo A") < result.index("conteudo B")  # 16/08 antes de 15/08
    assert "conteudo A" in result and "conteudo B" in result


def test_format_episodes_from_text_ignores_day_headers():
    """Cabeçalho de DIA tem parêntese ("(Hoje)") mas não data logo depois --
    não pode ser confundido com episódio."""
    full_text = (
        "Internação (16 de Agosto de 2026 - Permanece Internado)\n"
        "▶ 21 de agosto de 2026 - Sexta-Feira (Hoje)\n"
        "▶ 20 de agosto de 2026 - Quinta-Feira\n"
    )
    result = _format_episodes_newest_first(full_text)
    assert "Internação 1 de 1" in result
    assert "Internação 2 de" not in result


def test_format_episodes_header_not_found_falls_back_unchanged():
    headers = ["Internação (texto que não existe no corpo)"]
    full_text = "corpo qualquer sem esse cabeçalho"
    assert _format_episodes_newest_first(full_text, headers) == full_text


def test_format_episodes_duplicate_header_text_splits_by_position():
    header = "Internação (01 de Janeiro de 2020 - Alta em 02 de Janeiro de 2020)"
    full_text = f"{header}\nEvolução primeira\n{header}\nEvolução segunda\n"
    result = _format_episodes_newest_first(full_text, [header, header])
    assert "Evolução primeira" in result
    assert "Evolução segunda" in result
    assert "Internação 1 de 2" in result and "Internação 2 de 2" in result


# ------------------------------------------------- fechar só página nova

class FakeClosablePage:
    """Simula uma Page real do Playwright: tem `.close()`, é distinguível
    por identidade de objeto (cada instância é única, como cada Page
    capturada por `expect_page()` -- DEC-053)."""

    def __init__(self, label):
        self.label = label
        self.closed = False

    def close(self):
        self.closed = True


def test_close_if_new_page_closes_page_not_in_snapshot():
    from app.gsus.records import _close_if_new_page

    existing = frozenset({FakeClosablePage("login")})
    new_page = FakeClosablePage("popup-novo")

    _close_if_new_page(existing, new_page)

    assert new_page.closed is True


def test_close_if_new_page_never_closes_page_already_in_snapshot():
    """Caso real que quebrou a rodada em lote (DEC-053): a página do
    pop-up de prontuário, mesmo sendo um objeto Python distinto do
    original, pode ser a MESMA janela de trabalho por trás -- fechar por
    identidade de objeto (`is`) fechava a sessão inteira. A defesa correta
    é objetiva: só fecha o que não existia antes."""
    from app.gsus.records import _close_if_new_page

    working_page = FakeClosablePage("trabalho")
    existing = frozenset({working_page})

    _close_if_new_page(existing, working_page)  # mesmo objeto -- óbvio

    assert working_page.closed is False


def test_close_if_new_page_ignores_frame_without_close():
    from app.gsus.records import _close_if_new_page

    class FakeFrame:  # Frame não tem `.close()`
        pass

    _close_if_new_page(frozenset(), FakeFrame())  # não deve levantar exceção


def test_close_if_new_page_ignores_none():
    from app.gsus.records import _close_if_new_page

    _close_if_new_page(frozenset(), None)  # não deve levantar exceção
