# Empacota o app com PyInstaller (BUILD-001). Rodar da raiz do repositório.
#
# Estado atual: empacota só o shell Python/Tkinter/SQLite/Playwright(py).
# Ainda faltam (ver DECISIONS.md e CURRENT_STATE.md):
#   - `playwright install chromium` (binário do Chromium, não corre aqui --
#     precisa rodar uma vez antes, e o instalador final precisa embutir
#     esse chromium/ na pasta de distribuição);
#   - runtime/llama-server.exe + models/model.gguf (LLM local).
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& ".\.venv\Scripts\python.exe" -m PyInstaller "installer\gsus-auditoria.spec" `
    --distpath "installer\dist" --workpath "installer\build" --noconfirm

Write-Host "Build concluído em installer\dist\gsus-auditoria\"
