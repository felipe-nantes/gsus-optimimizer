"""Script de verificação manual do login GSUS (GSUS-001).

IMPORTANTE -- rode este script você mesmo, no seu terminal, e olhe a tela
com seus próprios olhos. Não cole dados reais de paciente de volta no chat
com a IA -- se algo não bater com o esperado, descreva a estrutura (nomes de
campos, botões) ou me envie um HTML/screenshot SANITIZADO (sem nome/prontuário
reais), como já vínhamos fazendo.

Pré-requisito (uma vez só):
    .venv\\Scripts\\python.exe -m playwright install firefox

Uso:
    .venv\\Scripts\\python.exe scripts\\check_login.py

O script usa a credencial já salva no Windows Credential Manager pelo
próprio app (tela de configuração inicial). Roda com o navegador visível
(headless=False) para você acompanhar. Ele só confirma se o login foi bem
sucedido (URL pós-login) -- não extrai nem imprime nenhum dado de paciente.

Já validado automaticamente (headless, sem ver dado de paciente) em
2026-08-20 -- ver DECISIONS.md DEC-009/DEC-010. Este script serve para você
confirmar visualmente e navegar até as telas de censo/prontuário, que ainda
estão bloqueadas (BLOCKED_GSUS) até você descrever a estrutura delas.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

from _common import setup_file_logging
from app import config
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
    base_url = app_config.gsus_base_url

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)
        page = browser.new_page()
        print(f"Abrindo {base_url} ...")
        page.goto(base_url, wait_until="load")

        try:
            gsus_page = login(page, username, password)
        except GSUSLoginError as exc:
            print(f"FALHOU: {exc}")
            print(f"URL atual: {page.url}")
            input("Pressione Enter para fechar o navegador...")
            browser.close()
            return

        print(f"OK: login confirmado e estabelecimento/unidade padrão aceito. URL: {gsus_page.url}")
        print("O GSUS abriu numa JANELA NOVA (pop-up) -- é ela que importa, pode ignorar a primeira aba.")
        print("Dê uma olhada na tela (censo de internados, pesquisa de prontuário, etc.).")
        print("Quando terminar, volte aqui e descreva a ESTRUTURA (não os dados reais) do que apareceu.")
        input("Pressione Enter para fechar o navegador quando terminar...")
        browser.close()


if __name__ == "__main__":
    main()
