; INSTALL-001 -- instalador Windows via Inno Setup (escolhido em DEC-004,
; formalizado em DEC-074). Empacota a saída ONEDIR do PyInstaller
; (installer/dist/gsus-auditoria/ -- gerada por gsus-auditoria.spec, que
; já inclui runtime/llama-server.exe e playwright-browsers/firefox, ver
; DEC-072/073). O modelo GGUF NUNCA é empacotado (baixa sozinho na 1ª
; execução, DEC-072) -- nada aqui precisa saber disso.
;
; Instalação POR USUÁRIO (PrivilegesRequired=lowest), NÃO em Program Files:
; (1) o app baixa o modelo (~4,92GB) pra dentro da própria pasta de
;     instalação em tempo de execução (resolve_app_path/get_app_root,
;     DEC-069) -- Program Files normalmente não é gravável sem admin pra
;     um usuário comum, quebraria o download; (2) o público real (equipe
;     de auditoria hospitalar) frequentemente NÃO tem direito de admin na
;     máquina de trabalho -- exigir elevação impediria a própria instalação.

#define MyAppName "GSUS Auditoria"
#define MyAppVersion "1.0.0"
#define MyAppExeName "gsus-auditoria.exe"
#define SourceDir "dist\gsus-auditoria"

[Setup]
AppId={{6C6F3B6E-2F9D-4C7A-9C2E-7B6E8F1A2D3C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=GSUSAuditoria-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\gsus-auditoria.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos adicionais:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir {#MyAppName} agora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; SCHEDULE-001/DEC-075: a tarefa diária (nome fixo em app/scheduling.py::
; TASK_NAME -- manter sincronizado se um dia mudar) roda `gsus-auditoria.exe
; --auto-update`. Sem remover isso no desinstalar, o Task Scheduler ficaria
; tentando abrir um .exe que não existe mais, todo dia, pra sempre. Se a
; tarefa nunca chegou a ser criada (usuário desinstalou antes de configurar),
; schtasks só retorna erro -- "& exit 0" garante que isso nunca trava o
; desinstalador.
Filename: "{cmd}"; Parameters: "/C schtasks /Delete /TN GSUSAuditoria_AtualizacaoDiaria /F & exit 0"; Flags: runhidden; RunOnceId: "RemoveScheduledTask"
