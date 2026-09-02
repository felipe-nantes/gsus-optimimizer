"""Script de verificação manual do censo de internados (GSUS-002/003).

IMPORTANTE -- rode você mesmo, no seu terminal. Este script NUNCA imprime
nome de paciente, data de nascimento ou nome da mãe -- só contagem e
leito/unidade (não identificam ninguém sozinhos). Ainda assim, olhe a tela
com seus próprios olhos antes de confiar; se algo parecer errado, me
descreva em texto (ex.: "o menu não é 'Internação', é 'Internações'") --
não precisa me mandar screenshot de novo.

Pré-requisito (uma vez só):
    .venv\\Scripts\\python.exe -m playwright install firefox

Uso:
    .venv\\Scripts\\python.exe scripts\\check_census.py

O caminho de navegação até a tela de censo (menu "Internação" ->
"Pesquisar Internação") ainda é uma SUPOSIÇÃO baseada no menu visível na
tela pós-login -- se este script falhar exatamente nesse passo, me diga
qual o nome exato do item de menu que você clica.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

from _common import setup_file_logging
from app import config
from app.gsus.census import GSUSCensusError, get_census
from app.gsus.client import get_content_frame
from app.gsus.login import GSUSLoginError, login
from app.security import credentials


def main() -> None:
    log_path = setup_file_logging()
    config.configure_playwright_browsers_path()
    print(f"(logs deste script vão para {log_path}, não pro terminal)")

    cred = credentials.get_credential("gsus")
    if cred is None:
        print("Nenhuma credencial GSUS salva. Rode o app e conclua a configuração inicial primeiro.")
        return
    username, password = cred

    app_config = config.load_config()

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)
        page = browser.new_page()
        page.goto(app_config.gsus_base_url, wait_until="load")

        try:
            gsus_page = login(page, username, password)
        except GSUSLoginError as exc:
            print(f"LOGIN FALHOU: {exc}")
            input("Pressione Enter para fechar...")
            browser.close()
            return

        content_frame = get_content_frame(gsus_page)
        try:
            patients = get_census(content_frame, app_config.unit)
        except NotImplementedError as exc:
            print(f"NAVEGAÇÃO NÃO CONFIRMADA: {exc}")
            print("Olhe a janela do GSUS aberta e me diga qual menu/link leva até 'Pesquisar Internação'.")
            input("Pressione Enter para fechar quando terminar de olhar...")
            browser.close()
            return
        except (GSUSCensusError, Exception) as exc:
            print(f"FALHOU AO EXTRAIR: {type(exc).__name__}: {exc}")
            input("Pressione Enter para fechar...")
            browser.close()
            return

        print(f"OK: {len(patients)} pacientes extraídos.")
        print("Distribuição por unidade/leito (sem nome/prontuário):")
        for p_ in patients[:20]:
            print(f"  - unidade={p_.unit!r} leito={p_.bed!r} internação={p_.admission_date!r}")
        if len(patients) > 20:
            print(f"  ... e mais {len(patients) - 20}")

        input("Pressione Enter para fechar o navegador...")
        browser.close()


if __name__ == "__main__":
    main()
