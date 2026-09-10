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
#define MyAppVersion "1.5.1"
#define MyAppExeName "gsus-auditoria.exe"
#define SourceDir "dist\gsus-auditoria"

; BUNDLE-001: variante que leva o modelo de IA (~4,92GB) dentro do proprio
; instalador, para quem nao pode contar com acesso a internet do hospital
; para o download automatico da 1a execucao (model_downloader.py, DEC-072).
; Gera um instalador SEPARADO, maior -- a variante padrao (sem /DBundleModel)
; continua identica a antes, baixando o modelo sozinha na 1a execucao.
;
; Uso:
;   ISCC.exe gsus-auditoria.iss                       (variante online, ~120MB)
;   ISCC.exe /DBundleModel gsus-auditoria.iss          (variante com IA embutida, ~5GB)
;   ISCC.exe /DBundleModel /DModelSourcePath="C:\caminho\model.gguf" gsus-auditoria.iss
;
; ModelSourcePath (so usado com /DBundleModel) aponta pro .gguf ja baixado
; nesta maquina -- nunca versionado no repositorio (mesmo tratamento de
; runtime/ e playwright-browsers/, ver .gitignore). Default abaixo e onde o
; modelo ja validado em producao vive nesta maquina de desenvolvimento.
#ifndef ModelSourcePath
  #define ModelSourcePath "C:\Users\profurg\AppData\Local\Programs\GSUS Auditoria\models\model.gguf"
#endif

#ifdef BundleModel
  #define MyOutputBaseFilename "GSUSAuditoria-Setup-ComIA"
  #define MyAppNameSuffix " (com IA embutida)"
#else
  #define MyOutputBaseFilename "GSUSAuditoria-Setup"
  #define MyAppNameSuffix ""
#endif

[Setup]
AppId={{6C6F3B6E-2F9D-4C7A-9C2E-7B6E8F1A2D3C}
AppName={#MyAppName}{#MyAppNameSuffix}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename={#MyOutputBaseFilename}
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
#ifdef BundleModel
; BUNDLE-001: um Setup.exe classico (nao "disk spanning") nao pode passar de
; ~4,2GB -- limite do formato PE32 do Windows, nao do Inno Setup (achado
; real: 1a tentativa embutindo o .gguf direto estourou esse teto e o ISCC
; recusou compilar). `external` evita isso sem precisar de disk spanning:
; o .gguf NAO fica dentro do Setup.exe, fica como um arquivo separado do
; LADO do instalador (`{src}` = pasta de onde o Setup.exe esta rodando); o
; script de build copia o .gguf pra `installer\output\model.gguf` logo
; depois de compilar, entao os dois arquivos SAEM juntos da mesma pasta.
; Entregar ao usuario final: os DOIS arquivos (Setup-ComIA.exe + model.gguf)
; na MESMA pasta -- nunca so o .exe sozinho, senao a instalacao nao acha o
; modelo e cai de volta pro download online (mesmo caminho de codigo da
; variante padrao). `ensure_model_downloaded` (idempotente, so checa
; `model_path.exists()`) nunca sabe a diferenca entre os dois casos.
Source: "{src}\model.gguf"; DestDir: "{app}\models"; DestName: "model.gguf"; Flags: external ignoreversion
#endif

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
