"""Script de verificação manual de abertura de prontuário + extração de
evoluções (GSUS-004/005).

IMPORTANTE -- rode você mesmo. Este script NUNCA imprime conteúdo
clínico, nome de paciente ou nome de profissional -- só contagem de
evoluções encontradas e se a extração funcionou. A JANELA do navegador vai
mostrar dado real (inevitável, é o prontuário abrindo de verdade) -- não
me cole nada do que aparecer lá; só me diga se funcionou ou onde travou
(e pode colar a mensagem de erro do TERMINAL -- isso não tem dado de
paciente, é só saída de programa).

Usa automaticamente o primeiro paciente do censo (não pede número de
prontuário -- evita até você precisar copiar/colar um). Pára de paginar o
censo assim que acha 1 paciente (pedido do usuário 2026-08-20, pra encurtar
o ciclo de teste) -- a função `get_census` continua buscando o censo
completo por padrão; só este script de teste usa o atalho.

Uso:
    .venv\\Scripts\\python.exe scripts\\check_notes.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright

from _common import setup_file_logging
from app import config
from app.extraction.parser import parse_note_blocks
from app.gsus.census import GSUSCensusError, get_census
from app.gsus.client import get_content_frame
from app.gsus.login import GSUSLoginError, login
from app.gsus.records import GSUSRecordError, extract_notes, open_current_admission
from app.security import credentials


def _fail(stage: str, exc: BaseException) -> None:
    print(f"\n{stage} FALHOU: {type(exc).__name__}: {exc}")
    print("--- detalhe técnico (sem dado de paciente, pode me colar) ---")
    traceback.print_exc()
    print("--- fim do detalhe ---\n")


def main() -> None:
    log_path = setup_file_logging()
    config.configure_playwright_browsers_path()
    print(f"(logs deste script vão para {log_path}, não pro terminal)")

    cred = credentials.get_credential("gsus")
    if cred is None:
        print("Nenhuma credencial GSUS salva.")
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
            _fail("LOGIN", exc)
            input("Enter para fechar...")
            browser.close()
            return

        content_frame = get_content_frame(gsus_page)

        print("Pegando 1 paciente de teste do censo (número não será impresso)...")
        try:
            patients = get_census(content_frame, app_config.unit, max_patients=1)
        except (GSUSCensusError, NotImplementedError, Exception) as exc:
            _fail("CENSO", exc)
            print("A janela do navegador ficou aberta -- olhe onde parou.")
            input("Enter para fechar...")
            browser.close()
            return

        if not patients:
            print("Censo vazio -- nada pra testar.")
            input("Enter para fechar...")
            browser.close()
            return
        test_patient = patients[0]
        print("OK: peguei 1 paciente de teste. Testando abertura do prontuário.")

        try:
            record_page = open_current_admission(content_frame, test_patient.record_number)
        except (NotImplementedError, GSUSRecordError, Exception) as exc:
            _fail("ABRIR PRONTUÁRIO", exc)
            print("Olhe a janela e me diga (em texto) o que travou -- não cole conteúdo do prontuário.")
            input("Enter para fechar...")
            browser.close()
            return
        print("OK: prontuário aberto, episódio atual selecionado.")

        try:
            raw_text = extract_notes(record_page, test_patient.record_number)
        except (NotImplementedError, GSUSRecordError, Exception) as exc:
            _fail("EXTRAIR EVOLUÇÕES", exc)
            input("Enter para fechar...")
            browser.close()
            return

        blocks = parse_note_blocks(raw_text)
        recognized = sum(1 for b in blocks if b["timestamp_raw"] is not None)
        print(f"OK: {len(blocks)} bloco(s) de evolução encontrados, {recognized} com cabeçalho reconhecido.")
        if recognized < len(blocks):
            print(
                f"Aviso: {len(blocks) - recognized} bloco(s) não bateram com o cabeçalho "
                "esperado (mantidos mesmo assim, sem metadados -- fail-soft)."
            )

        input("Enter para fechar o navegador quando terminar de olhar...")
        browser.close()


if __name__ == "__main__":
    main()
