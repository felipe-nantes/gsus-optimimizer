# TASKS.md — GSUS Auditoria

Regra: enquanto houver P0 pendente, não iniciar P1. Cada task segue o ciclo READ → PLAN → IMPLEMENT → TEST → FIX → TEST → DOCUMENT → STOP.

## P0

```
[x] APP-001      Scaffold (estrutura de pastas + docs SDD)
[x] APP-002      Application shell mínimo (janela abre, config base, logging, SQLite inicial)
[x] DB-001       Schema SQLite completo (runs, patients, notes, processing_queue, patient_state,
                 pending_items, pending_item_evidence). Expandido 2026-08-24 para o modelo de
                 auditoria concorrente (DEC-057) com migração para bancos já existentes
[x] SEC-001      credentials.py (Windows Credential Manager via ctypes)
[x] GSUS-001     Login GSUS — validado ponta a ponta de verdade (Firefox, pop-up + frameset + confirmação de estabelecimento) — DEC-008/009/010.
                 ACHADO 2026-08-26 (E2E-001, DEC-077): numa máquina limpa de verdade, login
                 automatizado em modo headless travava 100% das vezes (GSUS/WAF bloqueia
                 navegador controlado invisível) -- o mesmo Firefox aberto manualmente
                 (sem automação) logava normal. `GSUSClient` passou a usar `headless=False`
                 permanentemente -- janela do Firefox aparece visível durante "Atualizar
                 agora" (e na atualização automática de madrugada), é esperado.
[x] GSUS-002     Navegação até censo de internados        — validada ponta a ponta contra o GSUS real pelo usuário (FROZEN — DEC-012)
                 BUG REAL GRAVE encontrado e corrigido 2026-08-27 (DEC-080): fallback do
                 DEC-024 (paginação desiste após 2 tentativas de "Próxima") devolvia a lista
                 parcial via `return patients` -- `mark_patients_inactive_not_in`
                 (orchestrator.py) tratava isso como censo completo e marcou 134 pacientes
                 reais internados como inativos/alta numa execução real. Corrigido: paginação
                 truncada agora levanta `GSUSCensusIncompleteError` (carrega o parcial em
                 `.patients`); `run_once` processa o parcial normalmente mas PULA
                 `mark_patients_inactive_not_in` nesse caso. Reabriu módulo FROZEN com motivo
                 real e regressão coberta (mesmo racional do DEC-015).
[x] GSUS-003     Extração da lista de internados          — validada ponta a ponta contra o GSUS real pelo usuário (FROZEN — DEC-012).
                 OBSERVAÇÃO 2026-08-24 (DEC-061): `get_census(gsus_frame, unit, ...)` recebe
                 `unit` mas nunca usa pra filtrar a busca de verdade (confirmado lendo
                 census.py) — o censo sempre traz tudo que a conta enxerga. Confirmado com o
                 usuário que é o comportamento desejado (conta = escopo de auditoria), então
                 NÃO é bug — só documentando que o parâmetro é vestigial, caso algum dia
                 alguém precise filtrar de verdade no lado do GSUS
[x] GSUS-004     Navegação até prontuário/internação atual — validado ponta a ponta pelo usuário: abre o prontuário certo, internação certa (DEC-025/026)
                 BUG REAL GRAVE encontrado e corrigido 2026-08-27 (DEC-081): retry de
                 `open_current_admission` (`SEARCH_RETRY_ATTEMPTS`) só protegia a espera pelo
                 resultado da busca -- o clique nos itens de menu "Atendimento"/"Pesquisar
                 Prontuário" (widget `<div>` instável, mesmo padrão do DEC-012) ficava fora do
                 `try/except`, escapando sem nunca tentar de novo. Numa execução real de 183
                 pacientes, 178 (97%) falharam exatamente aí. Corrigido com
                 `_click_menu_to_search_screen()`, agora dentro do loop de retry existente.
                 BUG DE SEGURANÇA encontrado e corrigido 2026-08-27 (DEC-082): validando o fix
                 acima, achado um `PlaywrightError` não-timeout ("Frame was detached", durante
                 expansão do accordion de episódio) que escapava do `except PlaywrightTimeoutError`
                 e vazava um identificador vinculado a paciente/episódio (atributo `onmousedown`)
                 pro log via `str(exceção)`. Corrigido ampliando 4 pontos pra `except
                 PlaywrightError` (classe-base). Causa raiz de por que a sessão "trava" pro resto
                 do lote depois disso NÃO foi 100% confirmada -- reiniciar o app continua sendo a
                 mitigação prática; investigação a fundo precisa de observação ao vivo do GSUS
                 real (P1).
                 CAUSA RAIZ ENCONTRADA 2026-08-27 (DEC-083): usuário mandou print real da tela
                 do GSUS no momento da falha -- "Sua sessão expirou". Numa execução longa
                 (~180 pacientes, 1h30+), a sessão do GSUS expira no meio do lote -- daí a
                 cascata de falhas que nada recuperava sozinho. Corrigido com detecção
                 automática (`GSUSAdapter._session_expired`) + relogin automático
                 (`_relogin`, abre aba nova, refaz login, fecha a antiga) antes de cada
                 paciente. Ainda não validado contra execução longa real (só o cenário foi
                 confirmado, a recuperação em si precisa de outra rodada de 1h+ pra confirmar).
                 ACHADO ADICIONAL 2026-08-27 (DEC-084): validando o fix acima, achado que nem
                 toda cascata de falhas é sessão expirada -- dessa vez foi a ABA DO FIREFOX
                 crashando de verdade ("Gah. Your tab just crashed.", tela nativa do
                 navegador). Mesma solução (`_relogin`) resolve os dois casos -- generalizado
                 pra `_needs_relogin()` (sessão expirada OU aba crashada). Rebuild aplicado logo
                 depois (confirmado por vários ciclos de rebuild+reinstalação subsequentes,
                 DEC-090 em diante) -- nota "rebuild pendente" estava desatualizada.
[x] GSUS-005     Extração de evoluções — REABERTO em 2026-08-21 (DEC-040), cadeia de bugs
                 reais corrigida (DEC-040 a DEC-055): três níveis de accordion, accordion
                 EXCLUSIVO, detecção de "aberto" por tamanho do corpo, captura dia-a-dia,
                 seletor estrutural, pop-up órfão (entre pacientes/dentro do retry/identidade
                 de objeto), modal de justificativa bloqueando o lote.
                 VALIDADO no "Localizar" (11/11 dias, 2 episódios, ~257k caracteres, ~71s) e
                 na rotina automática com "Atualizar agora" real (2026-08-24, 174 pacientes,
                 168 sucesso/96,5%, 0 padrão de colapso, 0 processo órfão -- DEC-056).
                 FECHADO -- ver DEC-056 para nota sobre 6 falhas residuais não bloqueadoras
[x] EXTRACT-001  parser.py (DOM → estrutura intermediária, com fixtures sintéticas)
[x] EXTRACT-002  normalizer.py (normalização de texto)
[x] QUEUE-001    Fila de processamento (processing_queue) + resume
[x] ROBUST-001   Retry/timeout controlado em operações críticas (fila + LLM); GSUS ainda sem alvo real p/ testar timeout de rede
[x] INCR-001     Hash SHA-256 por evolução + SKIP/NEW_NOTE
[x] RULES-001    Regras determinísticas (exame/interconsulta) — candidatas, pendente validação clínica antes de produção real (RF-07). Migradas para a taxonomia fechada (DEC-057).
                 2 bugs reais corrigidos 2026-08-25 (DEC-064, achados por verificação
                 adversarial): "aguardando laudo"/"aguarda parecer" eram tratados como
                 CONCLUSÃO por engano (citam a palavra, significam o oposto). Também
                 ganhou reconciliação entre execuções (dedup por descrição determinística,
                 resolução só quando solicitação+conclusão aparecem na mesma janela —
                 nunca por ausência, ver DEC-062/064)
[x] LLM-001      Binário+modelo reais validados ponta a ponta: carga 108,7s, 1 análise 80,6s, saída válida e semanticamente correta (CPU fraca — ver CURRENT_STATE.md para números completos)
[x] LLM-002      Contrato + validação + retry + LLM_ANALYSIS_ERROR. Contrato REESCRITO 2026-08-24
                 para o modelo de auditoria concorrente (RF-20 a RF-28, DEC-057) — necessidade
                 hospitalar, taxonomia fechada, EDD, dia verde/vermelho, inferência com
                 confiança. `LocalLLM.start()` agora envolve o `Popen` em try/except (OSError
                 real virava exceção crua, quebrando o contrato de friendly_message — DEC-059)
[x] LLM-003      Causa raiz do timeout investigada com evidência real (DEC-070: hardware
                 confirmado lento, ~1,1 tok/s no i5-1235U, não config) e corrigida arquiteturalmente
                 (DEC-071): coleta+regras e análise por IA viram duas fases — relatório com regras
                 fica disponível ANTES do LLM começar, timeout por chamada sobe pra 1800s (só é
                 seguro porque deixou de bloquear quem clicou "Atualizar agora"), `max_tokens`
                 finalmente limitado (achado secundário do DEC-070). Funciona igual em hardware
                 fraco ou forte — throughput escala com a máquina, sem branch por tipo de hardware
                 PRIORIZAÇÃO 2026-08-27 (DEC-085, pedido do usuário): Fase 2 processa do menor
                 pro maior volume de texto (proxy pra demora, sem medir de verdade) -- pacientes
                 de longa permanência (mais texto acumulado, mais risco de timeout) ficam por
                 último, não atrasam o resto do relatório. Rebuild aplicado (confirmado por
                 ciclos subsequentes) -- nota estava desatualizada. NOTA 2026-09-02 (DEC-109):
                 essa ordenação agora só vale pro backlog (pacientes sem nota nova); quem chega
                 fresco na Fase 1 é consumido na ordem de chegada, já que a Fase 2 passou a
                 rodar em paralelo com a Fase 1 em vez de depois dela.
[x] MODEL-001    Taxonomia fechada de pendências (7 categorias + subtipos, RF-22) —
                 app/analysis/taxonomy.py, fonte única para prompt e validação (DEC-057)
[x] MODEL-002    Prioridade por regra determinística (ALTA/MÉDIA/MONITORAMENTO, RF-24) + SLA
                 parametrizável por categoria (RF-23) — app/analysis/priority.py,
                 sla_config.py. Limites default a validar clinicamente, mesmo princípio do
                 RULES-001/RF-07 (DEC-057)
[x] MODEL-003    Validação do novo contrato do LLM contra o modelo real (LocalLLM, Llama 3.1
                 8B). Primeira rodada real 2026-08-24 (fixture fictícia, long_admission.txt)
                 achou e corrigiu 2 bugs reais (timeout 60s→240s; "null" como string literal
                 em vez de JSON null) — DEC-058. Observação de qualidade (necessidade_
                 hospitalar não priorizava a evolução mais recente) corrigida via marcador
                 explícito em build_prompt + regra no SYSTEM_PROMPT — DEC-058.
                 FECHADO 2026-08-24: pipeline completo ponta a ponta rodado contra GSUS real
                 + LLM real (não só fixture) via scripts/check_full_pipeline.py — mesmo
                 `run_once` real que a UI usa, amostra de 3 pacientes reais, banco/relatório
                 isolados. 3/3 completos, 0 falha, carga do LLM 82,2s, ~38min total (3
                 pacientes) — ver DECISIONS.md DEC-060 para números completos. IMPORTANTE:
                 isso valida a INTEGRAÇÃO técnica (roda sem quebrar, persiste, gera
                 relatório) -- nunca validei o CONTEÚDO clínico das 3 análises (PHI não
                 pode chegar até mim por design). Revisão clínica humana do relatório
                 isolado (RF-07) e uma rodada real via botão "ATUALIZAR AGORA" contra o
                 censo completo continuam pendentes, não bloqueiam mais este item.
                 AUDITORIA DE CONFORMIDADE contra o PDF original feita 2026-08-25
                 (DEC-062/063/064): reconciliação de pendências entre execuções
                 implementada (nunca existia — cada rodada duplicava), coluna Unidade,
                 selo interna/externa+confiança, SLA aplicável mostrado, evidências
                 múltiplas (EVIDÊNCIA 1/2), especialidade/origem_internacao/model_version
                 preenchidos de verdade. Verificação adversarial encontrou e corrigiu 21
                 problemas reais, incluindo 3 regressões na própria reconciliação nova —
                 ver DEC-064 para a lista completa (inclui itens deliberadamente NÃO
                 corrigidos: SLA por subtipo, pseudonimização no relatório, correção/
                 override do auditor, trilha histórica de patient_state)
[x] REPORT-001   Relatório HTML por setor/leito, com falhas visíveis. REFORMULADO 2026-08-24:
                 censo de pendências por unidade (RF-29) + relatório individual completo no
                 formato da orientação técnica (contexto, necessidade hospitalar, objetivo,
                 pendência principal com evidência, próximo passo, EDD, dia verde/vermelho) —
                 BUG REAL encontrado e corrigido 2026-08-27 (E2E-001, DEC-078): `generate_report`
                 só listava paciente com pendência ativa OU `patient_state` já gravado — um
                 paciente processado com sucesso pela Fase 1 (regras, DEC-071) sem nenhum
                 achado, e que a Fase 2 (IA) ainda não tivesse alcançado, ficava INVISÍVEL no
                 relatório inteiro (censo e individual), mesmo ativo e corretamente processado.
                 Corrigido com `Repository.get_all_active_patients()`, nova fonte da verdade de
                 QUEM entra no relatório. Confirmado contra o banco real de produção: só 8 de
                 177 pacientes ativos tinham `patient_state`, 39 tinham pendência — o relatório
                 mostrava só a união dos dois (~40-45), nunca os 177 reais
                 DEC-057. BUG REAL encontrado e corrigido 2026-08-24 (DEC-061): relatório
                 filtrava por `patients.unit = setor_configurado`, mas `unit` é texto livre
                 escavado do GSUS por paciente (a conta enxerga várias unidades ao mesmo
                 tempo, confirmado pelo usuário) — vinha sempre vazio contra dado real.
                 Filtro removido (unit agora só rotula o título); `mark_patients_inactive_
                 not_in` tinha o mesmo bug (nunca marcava ninguém inativo)
[x] UI-001       Tela principal (status, progresso, atualizar agora → pipeline real, abrir relatório).
                 ACHADO 2026-08-26 (E2E-001, máquina real): usuário observou o texto de status
                 preso num paciente antigo (ex.: "11 de 145") enquanto o relatório já mostrava
                 dado bem mais recente (~45 pacientes processados) -- os DADOS estavam corretos
                 e adiantados, só a ETIQUETA de status na tela que ficou defasada. Suspeita:
                 a janela do Firefox visível (DEC-077) competindo por responsividade da UI do
                 Tkinter. NÃO CORRIGIDO ainda -- não bloqueia corretude (relatório é a fonte
                 confiável), só a experiência de acompanhar o progresso ao vivo. Investigar
                 depois.
                 ATUALIZAÇÃO 2026-08-27 (DEC-078): o número "~45 pacientes processados" citado
                 acima NÃO era o censo real (177) -- era a união (pendência ∪ patient_state), o
                 mesmo bug de REPORT-001 corrigido em DEC-078. Ou seja, uma parte real do
                 "desencontro" percebido aqui já tinha essa causa, não só atraso de rótulo de
                 status -- reavaliar se o desencontro ainda aparece depois da correção, antes de
                 investigar a hipótese original (janela do Firefox/Tkinter) a fundo.
                 GAP ENCONTRADO E CORRIGIDO 2026-08-24: botão real nunca chamava o LLM
                 (`llm=None` fixo, apesar de `config.py` já ter os campos prontos) — nenhuma
                 rodada real anterior teve análise por IA, só regras. Corrigido: LLM inicia
                 antes do GSUS, falha ao iniciar degrada pra só-regras em vez de abortar
                 (DEC-059)
[x] UI-002       Tela de configuração inicial (usuário, senha, setor, horário) + edição sem obrigar reentrar senha
[x] UI-003       Mensagens de erro amigáveis (mapa erro técnico → mensagem usuário)
[x] UI-004       "Localizar Paciente" — busca manual por prontuário, histórico completo de
                 internações (atual + antigas), só exibe (não persiste, não roda regras/LLM —
                 DEC-031). Múltiplos episódios (2014-2026) confirmados no GSUS real
                 (2026-08-21). Reordenação por data (mais-recente-primeiro, RF-19) + rótulo
                 por episódio implementados e testados com fixtures (DEC-033). Bug real de
                 navegação corrigido (DEC-034). GSUS exige justificativa de acesso auditada
                 para prontuário não internado na unidade agora — "Localizar" falha limpo
                 nesse caso, nunca preenche sozinho (DEC-035, decisão do usuário).
                 VALIDADO ponta a ponta em 2026-08-24 com prontuário internado: 11/11 dias,
                 2 episódios, ordenado do mais recente ao mais antigo. Ver PERF-001 sobre o
                 custo por prontuário.
                 REFORMULADO 2026-08-25 (DEC-067, pedido do usuário): não navega mais no GSUS
                 ao vivo -- gera o relatório individual (RF-20 a RF-30) a partir do que já
                 está no banco local, só para paciente ainda internado (alta/desconhecido
                 viram mensagem direta, sem tentar buscar nada). Simplificação real: virou
                 busca síncrona, sem thread/fila/credencial GSUS. Todo o trabalho de
                 navegação acima (episódios múltiplos, DEC-033/034/035) fica só no código do
                 GSUS (`get_full_admission_history_text`/`lookup_full_history`, FROZEN, não
                 removido) -- não é mais chamado por esta tela, mas continua disponível pra
                 uso futuro (script ou outra tela). Marcado `[x]` -- redesenho completo e
                 testado
[x] SCHEDULE-001 Registro real no Windows Task Scheduler validado (create→exists→delete), autorizado pelo
                 usuário. ACHADO 2026-08-26 (DEC-075): o mecanismo existia mas nada no app o chamava --
                 nenhuma tarefa jamais seria criada de verdade, mesmo com instalador perfeito. Corrigido:
                 `SetupWindow._on_submit` chama `register_daily_task` a cada configuração salva (modo
                 frozen), com `--auto-update` no `/TR` -- sem essa flag, a tarefa só abriria a janela do
                 app, sem disparar a atualização sozinha (não tem humano de madrugada). Lógica de
                 atualização extraída pra `app/update_flow.py::run_update` (única fonte de verdade,
                 usada pelo clique manual E pelo modo automático). Não testado com uma tarefa real de
                 novo (exigiria autorização explícita, mesmo racional da 1ª vez) -- lógica de despacho
                 é só Python, totalmente coberta por teste com mock.
                 BUG REAL ENCONTRADO 2026-08-28 (DEC-086, madrugada): a tarefa agendada disparou
                 (00:01, ainda presa nesse horário por causa do DEC-079) EM CIMA de uma execução
                 manual já em andamento -- sem nenhum mecanismo de exclusão mútua. A 2ª instância
                 ficou travada (quase 7h sem log nenhum além do "Iniciando"), consumindo pouco
                 recurso mas evidenciando um risco real de colisão em produção.
                 CORRIGIDO 2026-08-28 (DEC-088): lock de instância única em `run_update`
                 (`app/update_flow.py::_InstanceLock`, via `msvcrt.locking` -- API nativa do
                 Windows, sem dependência nova, libera sozinho se o processo cair/crashar).
                 Segunda tentativa concorrente (clique manual ou tarefa agendada) levanta
                 `UpdateAlreadyRunningError` de cara -- tratado como "pulo" gracioso na tarefa
                 agendada, mensagem amigável no clique manual. 6 testes novos.
                 ACHADO 2026-08-27 (E2E-001, passo 7, DEC-079): nesta máquina, a tarefa real
                 (`GSUSAuditoria_AtualizacaoDiaria`) criada com sucesso na config inicial (26/08)
                 passou a rejeitar `/Create /F` E `/Delete /F` com "Acesso negado" -- mesmo pelo
                 próprio dono, sem elevação. Tarefa NOVA (nome nunca visto) cria/sobrescreve sem
                 problema -- não é bug em `register_daily_task` (já usa `/F` corretamente) nem
                 política de grupo genérica, é específico dessa tarefa já existente (suspeita:
                 antivírus/EDR da máquina passou a proteger a tarefa depois de criada, por
                 apontar pra `.exe` não assinado com início automático). Resultado prático: nesta
                 máquina, mudar o horário em "Configurações" depois da 1ª vez não atualiza a
                 tarefa real (fica presa no horário original), e o `[UninstallRun]` do
                 desinstalador (DEC-076) provavelmente também vai falhar em removê-la (órfã, sem
                 risco real -- aponta pro `.exe` que deixaria de existir). Não bloqueia nada (app
                 já degrada graciosamente, DEC-075) -- registrado pra considerar em PILOT-001 se
                 outras máquinas reais tiverem o mesmo antivírus/política.
[x] BUILD-001    PyInstaller onedir com build real testado (não só lido) -- `.exe` gerado,
                 iniciado, ficou de pé sem erro, encerrado manualmente. Modelo GGUF: decisão
                 do usuário 2026-08-25 -- NÃO empacota, baixa sozinho na 1ª execução
                 (app/analysis/model_downloader.py, DEC-072). `runtime/` (llama-server.exe +
                 DLLs) e Firefox (engine real, DEC-010) empacotados via `Tree()` no spec
                 (DEC-073) -- Firefox reinstalado numa pasta dedicada do projeto
                 (`playwright-browsers/`, nunca teve Chromium, resolve de brinde a remoção
                 dele do pacote). 2 achados reais só descobertos rodando o build de verdade:
                 PyInstaller 6.x usa `_internal/` por padrão (`contents_directory='.'` no
                 EXE() restaura o layout plano que `get_app_root()` sempre assumiu) e o
                 caminho do script em `Analysis()` resolvia contra a pasta do `.spec`, não
                 do invocador (corrigido com caminho absoluto).
                 ACHADO 2026-08-24 (DEC-059): `model_path`/`llm_server_path` default são
                 caminhos RELATIVOS; no Windows, `Popen` não resolve executável relativo
                 contra o diretório de trabalho do jeito que `Path.exists()` resolve —
                 `LLMStartupError` (degrada pra só-regras, não trava, mas análise por IA
                 para de rodar silenciosamente). Risco real em produção: `.exe` empacotado
                 ou disparado pelo Task Scheduler (SCHEDULE-001) não garante diretório de
                 trabalho = pasta de instalação.
                 RESOLVIDO 2026-08-25 (DEC-069): `config.get_app_root()`/`resolve_app_path()`
                 resolvem caminho relativo contra a pasta do executável (`sys.frozen`) em
                 produção, ou a raiz do projeto em dev -- nunca contra o `cwd` do processo.
                 Testado via mock de `sys.frozen`, ainda não contra um `.exe` real (falta o
                 empacotamento em si, que é o resto desta task)
[x] PERF-001     Custo de extração por paciente. Rodada final (2026-08-24, pós DEC-055):
                 174/174 processados, 168 sucesso (96,5%), ~44min total, sem padrão de
                 colapso, 0 processo órfão. FECHADO -- ver DEC-056. Regime incremental "de
                 regime" (banco já povoado de rodadas anteriores) ainda não medido
                 isoladamente, mas não é bloqueador -- resultado real já é viável
[x] INSTALL-001  Instalador Windows via Inno Setup (DEC-004/074) -- `installer/gsus-auditoria.iss`.
                 Instalação POR USUÁRIO (`{localappdata}\Programs\...`, sem exigir admin -- necessário
                 pro download do modelo em runtime e porque a equipe de auditoria hospitalar
                 frequentemente não tem direito de admin na máquina). Build real testado: instalado,
                 aberto sem erro, desinstalado limpo (`unins000.exe`). `installer/output/GSUSAuditoria-
                 Setup.exe` (~107MB, fora do controle de versão).
                 REBUILD 1.1.0 (2026-09-04, DEC-114): visual novo (DEC-112/113) levado ao
                 produto final. Build a partir da worktree com Firefox e runtime/ reaproveitados
                 do app instalado; Inno Setup 6.7.3 instalado nesta máquina via winget (escopo de
                 usuário). `installer/output/GSUSAuditoria-Setup.exe` 120,0 MB, SHA-256
                 E2D0E140...08C5, instalado silenciosamente e verificado por hash contra o dist
                 testado; modelo GGUF movido para dentro da instalação. Tarefa agendada ainda
                 precisa ser registrada pelo usuário (Configurações → Concluir no build empacotado).
                 REGENERADO (DEC-115, ícone na janela): mesmo 1.1.0, novo
                 `GSUSAuditoria-Setup.exe` 120,0 MB, SHA-256 00A95DAE...1F71 -- substitui o de DEC-114.
                 REGENERADO 1.2.0 (DEC-116, Encerrar + navegador visível/segundo plano): novo
                 `GSUSAuditoria-Setup.exe` 120,0 MB, SHA-256 2CB17C12...9718 -- substitui o de DEC-115.
[x] E2E-001      Teste em máquina limpa (sem Python/Playwright/llama.cpp pré-instalados) --
                 marcado como não feito por muito tempo por engano: na prática, extensivamente
                 executado nesta máquina real desde 2026-08-26 (login headless, paginação de
                 censo, extração de evoluções, tarefa agendada, dashboard), com vários bugs
                 reais encontrados e corrigidos como resultado direto (ver DEC-077 a DEC-109).
                 Corrigido em 2026-09-03 durante auditoria de prontidão para entrega.
[ ] PILOT-001    Piloto em produção controlada (1 usuário, 1 máquina, 1 setor) -- DEC-076:
                 usuário confirmou que não é possível agora; E2E-001 já é suficiente por ora.
```

## P1 (não iniciar enquanto houver P0 pendente)

```
[x] LLM-004      Retomada de Fase 2 interrompida sem script ad-hoc (achado real, DEC-087,
                 2026-08-28) -- FEITO em DEC-091 (2026-08-28): `Repository.
                 get_active_patients_pending_ai_analysis()` + `_reconstruct_all_notes` (app/
                 orchestrator.py) recolocam automaticamente, em TODA execução, qualquer
                 paciente ativo com nota registrada mas sem NENHUMA análise de IA bem-sucedida
                 -- usando o histórico completo já extraído (não uma nota isolada). Não depende
                 mais de nota genuinamente nova, nem de script manual. Testado (e2e +
                 unitário).

[x] LLM-005      Reparo de retry (DEC-089) -- backfill dos 86 pacientes reais pendentes
                 (identificados no DEC-090) rodado via backfill_pending_ia.py (scratchpad,
                 descartável, não faz parte do código-fonte -- reusa LLM-004 + DEC-089 já
                 dentro do app real via app.orchestrator, respeita o lock do DEC-088).
                 CONCLUÍDO 2026-08-29: 69 de 86 sucesso (80%), 17 falhas fail-closed (13 por
                 estouro de contexto -- DEC-093, 2 RF-28 irrecuperável mesmo com reparo, 2
                 JSON inválido). Ver DEC-093 pro detalhe e follow-up (RESIL-006).

[ ] RESIL-001     Cluster de resiliência do GSUS NÃO corrigido nesta sessão (auditoria DEC-091,
                 achados confirmados mas deferidos por escopo/tempo antes da entrega) --
                 precisa de atenção dedicada, e alguns itens merecem validação contra o GSUS
                 real antes de mexer (não só leitura de código):
                 - Censo (app/gsus/census.py) sem retry protegendo o clique de menu/Pesquisar
                   (mesma classe de bug que DEC-081 corrigiu só em records.py) -- uma falha
                   aqui derruba a execução inteira, sem relatório nenhum no dia.
                 - "Frame was detached" no meio da paginação do censo descarta TODAS as
                   páginas já coletadas (não só a atual).
                 - Uma linha de tabela fora do formato aborta o censo inteiro.
                 - Censo parcial ainda pode ser tratado como COMPLETO por um segundo caminho
                   de saída da paginação (reabriria o dano do DEC-080 -- paciente internado
                   marcado como alta).
                 - Login sem retry algum, só captura Timeout, `_relogin` vaza uma aba do
                   Firefox a cada falha (sem limite de tentativas).
                 - `open_current_admission`: `_submit_search` e o clique final ficam FORA do
                   try/except do laço de retry (DEC-081 corrigido pela metade).
                 - Conferência fail-closed do prontuário levanta em vez de tentar de novo --
                   atraso de AJAX vira falha permanente do paciente.
                 - Sessão expirada (DEC-083): checagem dentro do frame `content` já foi
                   adicionada como blindagem defensiva (DEC-091), mas NÃO foi validada contra
                   o GSUS real -- confirmar na próxima execução real se o cenário do frame
                   chegou a ocorrer de verdade.

[ ] RESIL-002     Fase 2 sem teto de tempo GLOBAL (só por paciente, DEFAULT_TIMEOUT_SECONDS).
                 Interação real com o lock de instância única (DEC-088): uma Fase 2 muito
                 longa numa madrugada pode fazer a tarefa agendada da NOITE SEGUINTE ser
                 pulada inteira (lock ainda ocupado). Considerar um deadline de rodada (ex.:
                 parar de iniciar novos pacientes N horas depois do início) antes do piloto.

[ ] RESIL-003     Tarefa agendada frágil (app/scheduling.py): não roda na bateria, não acorda a
                 máquina, sem `/RU`, sem recuperação de horário perdido -- confirmado na tarefa
                 REAL desta máquina (não pôde ser atualizada/removida durante os testes desta
                 sessão). Configuração do Windows Task Scheduler, não é mudança de código Python
                 só.

[ ] RESIL-004     Navegador (Firefox) fica visível e aberto durante TODA a Fase 2 (só análise
                 local por IA, não usa GSUS) -- horas de uso de memória à toa, alimentando a
                 mesma exaustão de recursos que já causou o crash de aba do DEC-084. Fechar o
                 client GSUS antes da Fase 2 começar (Fase 2 não depende dele).

[ ] RESIL-005     Itens menores da auditoria (DEC-091), baixa prioridade pro piloto: download de
                 modelo aceita resposta HTTP truncada como sucesso (model_downloader.py); toda
                 falha de paciente é logada duas vezes (orchestrator + repository); timeout
                 default do Playwright (30s) nunca configurado explicitamente;
                 GSUSClient.__exit__ sem proteção por passo.

[x] RESIL-006     Estouro de contexto (`--ctx-size 8192`, DEC-092) confirmado como causa real de
                 13 das 17 falhas do backfill do LLM-005 (DEC-093) -- FEITO em DEC-094
                 (2026-08-29): corte adicional por TAMANHO (`_limit_to_char_budget` +
                 `_prepare_notes_for_llm`, app/orchestrator.py, `MAX_NOTES_CHARS_FOR_LLM=9000`)
                 antes da janela de dias virar prompt -- descarta evolução mais antiga primeiro,
                 nunca aumenta RAM (opção (b) da lista original, preferida a subir --ctx-size).
                 RESULTADO FINAL (verificado no banco, 2026-08-30): **143 de 173 pacientes
                 ativos (82,7%) com análise de IA real, ZERO falhas remanescentes** entre quem
                 tinha nota pra analisar -- os 30 restantes nunca tiveram nenhuma evolução
                 registrada (não são falha, são pacientes genuinamente sem o que analisar
                 ainda). Os 2 casos RF-28 antes tidos como "irrecuperáveis mesmo com reparo"
                 (DEC-093) também passaram nesta rodada -- o corte por tamanho mudou o
                 conjunto exato de notas enviadas, dando ao modelo uma entrada genuinamente
                 diferente (não uma repetição idêntica).

[x] RESIL-007     Auditoria adversarial da dashboard (Fases 1-3, REPORT-003/004) -- FEITA
                 2026-09-01 (DEC-101), pedido explícito do usuário ("verifique e faça os testes
                 antes de partir pras próximas fases"). 6 achados reais confirmados e corrigidos
                 (nenhum travava/crashava o app hoje, mas o #1 era um risco de PHI real):
                 1. [ALTA] `dia_causa` (texto livre do LLM, sem taxonomia fechada) estava sendo
                    gravado verbatim em `daily_snapshot_category.label` -- tabela sem expurgo.
                    Corrigido: `save_snapshot` nunca mais persiste essa dimensão, só a contagem
                    agregada (`patients_dia_vermelho`). Ver PRIVACY-001 abaixo pro item que falta.
                 2. [ALTA] `_poll_update_queue`/`_finish_update` sem a guarda `_is_alive` que
                    `_periodic_refresh` já tinha -- trocar de tela durante "Atualizar agora"
                    perdia o resultado em silêncio (TclError). Corrigido.
                 3. [ALTA/cosmético] `SetupWindow`/`LookupWindow` não resetavam `root.minsize()`
                    (só `resizable`) -- abriam presas no mínimo 1024x700 herdado da dashboard.
                    Corrigido com `root.minsize(1, 1)`.
                 4. [MÉDIA] `after` do `_periodic_refresh` nunca cancelado na troca de tela --
                    `MainWindow` antiga (com Figure do matplotlib) ficava presa até seu próprio
                    timer de 60s vencer. Corrigido com `<Destroy>` + `after_cancel`.
                 5. [MÉDIA] Dois caminhos de erro (`_resolution_hours`/`_refresh_dashboard`) sem
                    nenhum teste forçando o cenário de falha. Testes adicionados.
                 6. [BAIXA] Um teste de ordenação cronológica era cego a mutação (passaria igual
                    sem `ORDER BY`). Reescrito forçando ordem física ≠ ordem cronológica.
                 Achado operacional (não-bug): banco real com ~46% das últimas 13 runs FAILED --
                 consistente com a instabilidade do GSUS já enfrentada nesta sessão (DEC-096/097),
                 não uma regressão nova. CAUSA RAIZ DIAGNOSTICADA em RESIL-008 (abaixo).
                 Testes: +5. Suíte completa: 360 passed, mesmos 10 erros de Chromium.

[ ] PRIVACY-001  `dia_causa` (RF-28, "dia vermelho por causa") precisa de taxonomia FECHADA
                 (lista pré-definida, como `category` já tem em `app/analysis/taxonomy.py`)
                 antes de poder ser historizado com segurança -- achado da auditoria RESIL-007/
                 DEC-101: hoje é texto livre do LLM (frase específica por paciente), por isso
                 `daily_snapshot_category` nunca grava essa dimensão (só a contagem agregada de
                 dias vermelhos). Sem isso, a quebra "dias vermelhos POR CAUSA" que o RF-28 pede
                 fica sem histórico -- só o total.

[x] RESIL-008     Falha real ao vivo em `open_current_admission` (RF-12, prontuário) -- FEITA
                 2026-09-01 (DEC-102), achada durante um "Atualizar Agora" real disparado a
                 pedido do usuário como teste de aceitação antes de avançar de fase.
                 - 1ª tentativa: `TargetClosedError` do Playwright durante paginação do censo
                   (navegador fechou sozinho) -- recuperação limpa confirmada (run_id nunca
                   chegou a existir, nenhuma run presa).
                 - 2ª tentativa: censo parcial (40/180, GSUS instável) e 21 de 23 pacientes
                   tentados falharam em `open_current_admission` ("marcador não apareceu após 3
                   tentativas"), quase cronometrado a cada ~90s. Usuário autorizou encerrar.
                 - Log real mostrou o MESMO erro desde 2026-08-26 (inclusive pico de 58
                   ocorrências num minuto em 2026-08-27) -- NÃO é regressão desta sessão, é
                   causa raiz da taxa de falha de ~46% já observada em RESIL-007/DEC-101.
                 - Causa provável: `llm.start()` (DEC-058) fica residente em RAM durante toda a
                   Fase 1, competindo por recurso com Playwright/Firefox em hardware fraco.
                 - Corrigido: `SEARCH_RETRY_ATTEMPTS` 3→5 em `app/gsus/records.py` (mesmo padrão
                   de DEC-024/DEC-096) -- `open_current_admission` estava FROZEN, motivo real
                   confirmado pra destravar. NÃO movida a arquitetura (`llm.start()` continua
                   antes do GSUS, DEC-058) -- mudança maior, fora de escopo sob prazo apertado.
                 - Limpeza pós-encerramento: 1 run presa em RUNNING + 1 `llama-server` órfão
                   (mesma classe de achado de kill forçado já documentada) -- ambos limpos.
                 - Testes: +1 (caminho de esgotamento de tentativas, sem cobertura antes).
                   Suíte completa: 361 passed, mesmos 10 erros de Chromium.
                 - Considerar como mitigação mais estrutural (fora de escopo agora): mover
                   `llm.start()` pra depois da Fase 1 terminar -- ver RESIL-004 (que já apontava
                   a mesma tensão de recurso, na direção inversa: fechar o GSUS antes da Fase 2).

[x] RESIL-009     Investigação completa das falhas de `open_current_admission` (RF-12) pós
                 DEC-102 -- FEITA 2026-09-01 (DEC-103), pedido explícito do usuário ("todas
                 [as falhas] precisam ser justificadas quando o projeto entrar em produção
                 real"). 3 investigações em paralelo + 1 verificação cética independente (que
                 pegou um relatório com "prova no log" FABRICADA -- tese estrutural continuava
                 válida, só o exemplo citado não existia de verdade no log).
                 - Categoria "Menu não respondeu"/"marcador não apareceu" (maioria): SEM bug de
                   código -- timing medido bate exatamente com os timeouts configurados, código
                   já protegido corretamente, falhas em rajadas (não crescentes) descartam
                   degradação progressiva local. Limitação de ambiente já mitigada por DEC-102.
                 - MESMA categoria: hipótese REAL e estruturalmente fundamentada de
                   má-classificação -- `open_current_admission` só reconhece "sem internação
                   atual" via 1 sinal (modal), diferente da função irmã (`_collect_days`, que
                   usa checagem estrutural mais ampla). Precedente real: DEC-054/055 (2 caminhos
                   já descobertos) + DEC-056 (suspeita de 3º caminho, achada nesta verificação).
                   NÃO corrigido -- precisa de verificação manual do usuário em 1-2 prontuários
                   reais no GSUS antes de mudar código (risco de esconder falha técnica real
                   atrás de um rótulo errado de "provável alta").
                 - Categoria "Locator.click: Timeout 30000ms exceeded": BUG CONFIRMADO e
                   CORRIGIDO -- `current_card.click()` estava fora de qualquer try/except.
                   Corrigido com o mesmo padrão de retry do resto do laço.
                 - Achado colateral (achado pela verificação cética, corrigindo um erro do
                   relatório original): existe um 4º modo de falha real, ainda não investigado
                   -- ver RESIL-010 abaixo.
                 Testes: +1. Suíte completa: 362 passed, mesmos 10 erros de Chromium.

[x] PRIVACY-002  Verificar manualmente no GSUS real 1-2 prontuários que falharam hoje com
                 "Nenhuma internação em andamento encontrada" (RESIL-009/DEC-103) -- FEITO
                 2026-09-01 pelo usuário. RESULTADO: os 3 prontuários verificados estavam TODOS
                 genuinamente internados, com evolução real na tela -- nenhum caiu no caso
                 "sem internação atual mostrada de um terceiro jeito" (a hipótese de
                 má-classificação do DEC-103). Hipótese DESCARTADA por evidência direta (amostra
                 de 3) -- reforça que a explicação original do DEC-102 (marcador existe mas não
                 é confirmado a tempo, por lentidão/instabilidade real do GSUS) é a causa
                 dominante, não um bug de classificação. NÃO implementar a checagem estrutural
                 de `EPISODE_CARD_SELECTOR` em `open_current_admission` -- não há evidência de
                 que resolveria algo real, e mudar sem necessidade correria o risco oposto (uma
                 falha técnica genuína virar silenciosamente "provável alta" errada).

[ ] RESIL-010    4º modo de falha real achado em RESIL-009/DEC-103, não investigado a fundo:
                 sequência de avisos "Não foi possível expandir um item de episódio" (mesmo
                 padrão do DEC-082) até estourar `EXPAND_TIME_BUDGET_S=210s` em `_expand_all`
                 (app/gsus/records.py). Confirmado real no log (paciente pac-96497b7b, 21
                 avisos consecutivos antes do timeout), mas causa raiz ainda não investigada.

[x] RESIL-011    Auditoria de certificação pré-entrega (2026-09-01, DEC-105) -- 4 achados reais
                 de isolamento de falha corrigidos: (1) GRAVE, confirmado no banco de PRODUÇÃO
                 real -- bookkeeping do loop principal (`next_pending`/`report`/`mark_processing`
                 Fase 1, `is_healthy`/`report` Fase 2) rodava fora do try/except por paciente,
                 abortando runs inteiras sem nenhum `ERROR` registrado (RF-12 furado); (2)
                 `census.py::get_census` sem retry no clique inicial (RESIL-001 reconfirmado);
                 (3) reabertura do DEC-080 por um segundo caminho (paginação parada sem
                 exceção); (4) `LocalLLM.start()` vazando conexão SQLite sob disco cheio. Não
                 corrigido (fora de escopo): `_InstanceLock` só aplicado em `update_flow`, não
                 em `orchestrator`/`Repository` -- ver acompanhamento futuro.
                 Testes: +6. Suíte completa: 380 passed, mesmos 10 erros de Chromium.

[x] PRIVACY-003  CPF do usuário (`gsus_username`) removido de `config.json` (DEC-104, auditoria
                 de segurança pré-entrega, 2026-09-01) -- já vinha causando exposição real
                 documentada. Agora só vive no Windows Credential Manager. `config.json` de
                 produção limpo manualmente.

[x] REPORT-005   RF-29 (indicadores agregados + colunas de censo) fechado no relatório HTML e
                 na dashboard (DEC-106, auditoria de certificação, 2026-09-01) -- nova seção
                 "Indicadores do serviço" em html_report.py; DIH/Contexto/Pendência principal
                 (descrição real) adicionados à tabela da dashboard, reordenada pra bater com a
                 ordem do RF-29. Gaps conscientemente deferidos: RF-26 (dado extraído × variável
                 derivada, mudança de schema), RF-30 override (já rotulado "futuro" na spec).

[x] DIH-001      DIH quebrado pra quase todo paciente real desde sempre (DEC-107, achado durante
                 REPORT-005, 2026-09-01) -- `admission_date` vem em DD/MM/AAAA (formato real do
                 GSUS), a função de cálculo só aceitava ISO. 190 de 192 pacientes ativos
                 mostravam "DIH não determinado" no relatório já em produção. Corrigido em
                 `days_since_admission` (app/analysis/priority.py), único lugar compartilhado
                 entre relatório e dashboard.

[x] RESIL-012    GRAVE, achado na primeira execução autônoma sem supervisão (madrugada
                 2026-09-01/02, DEC-108) -- paciente internado de verdade sendo marcado como
                 alta. 3 execuções reais seguidas terminaram a paginação do censo sem nenhum
                 erro (`census_complete=True`) mas capturando bem menos pacientes que o real
                 (190→159→176→175); `next_link.count()==0` sozinho não provava censo completo.
                 Corrigido comparando contra "Total de N registros" que o próprio GSUS anuncia
                 no rodapé da tabela -- gap grande agora levanta `GSUSCensusIncompleteError`
                 (mesmo tratamento do DEC-080/105) em vez de disparar
                 `mark_patients_inactive_not_in` sobre um censo parcial. Não corrige
                 retroativamente quem já foi marcado inativo por engano -- próxima execução
                 real com o binário corrigido reativa sozinha via `upsert_patient`.
                 Testes: +3. Rebuild + reinstalação silenciosa aplicada.

[x] PERF-001     Fase 2 (IA) rodava só depois que a Fase 1 (GSUS) terminava por completo pra
                 TODO o lote (DEC-071/109, 2026-09-02) -- pedido explícito do usuário depois
                 de ver só 16 de 182 pacientes entrarem na Fase 2 depois de mais de 1h de Fase
                 1. Agora a Fase 2 roda numa thread dedicada, consumindo fila conforme a Fase 1
                 libera cada paciente -- as duas se sobrepõem de verdade (rede vs. CPU/GPU
                 local, recursos diferentes). Revisão adversarial ANTES de aplicar (23 agentes)
                 confirmou 18 achados reais em 4 causas -- todas corrigidas: (1) GRAVE,
                 reproduzido -- thread + conexão SQLite vazavam pra sempre se qualquer exceção
                 escapasse entre o início da thread e o sentinela (`finish_run`, laço de
                 backlog); corrigido com `finally` cobrindo tudo; (2) GRAVE -- abertura da
                 própria conexão da thread ficava fora do try/except dela, falha ali pulava a
                 Fase 2 inteira em silêncio (só stderr, nunca o log da aplicação); (3) ALTO --
                 sem `busy_timeout`, duas conexões concorrentes podiam estourar o timeout padrão
                 do sqlite3 (5s) sob contenção no hardware fraco confirmado; (4) MÉDIO -- join()
                 sem sinal de vida periódico. Preço consciente: pacientes frescos não seguem
                 mais a ordenação menor-primeiro do DEC-085 (só o backlog continua ordenado
                 assim). Deferido (BAIXO, cosmético/autocorretivo): `generate_report` pode
                 mostrar retrato momentaneamente inconsistente com dois escritores agora.
                 Testes: +4. Suíte completa: 362 passed, repetida 5x sem flakiness. Rebuild +
                 reinstalação silenciosa aplicada, execução real disparada em produção.
```

```
[ ] RULES-002    Regras determinísticas adicionais validadas por usuário clínico
[x] UI-005       Design/UX da tela principal, tela de configuração e tela de localizar
                 paciente (2026-09-03) -- adiado explicitamente desde 2026-09-01 ("resto
                 funcional primeiro"), incluído nesta entrega por decisão do usuário após a
                 auditoria de prontidão confirmar que o resto do sistema já estava sólido.
                 Paleta única (cor de marca + as MESMAS cores de prioridade já usadas no
                 relatório HTML, PRIORITY_COLORS -- nunca reinterpretadas), tipografia
                 consistente (Segoe UI), cartões de KPI com faixa de destaque colorida no
                 topo, botões ttk customizados (tema `clam`, único que respeita cor
                 customizada de botão no Windows), gráficos matplotlib com espinha/eixo
                 consistentes com o resto da tela, listras alternadas na tabela de censo.
                 `configure_app_style()` extraída como função de módulo (não método) porque
                 as telas de Configuração/Localizar Paciente podem abrir ANTES da tela
                 principal alguma vez existir (primeira execução). Nenhum widget mudou de
                 identidade -- mesmos atributos que os testes já esperavam
                 (`_kpi_labels`, `_census_tree`, `status_label`, `update_button`).
                 Achado incidental: 2 testes localizavam botões só por `isinstance(w,
                 tk.Button)`, que não reconhece `ttk.Button` -- corrigidos pra checar as
                 duas classes.
                 Testes: 362 passed (mesma suíte, +correção de 2 testes de detecção de
                 botão). Verificado visualmente com dados 100% sintéticos (nunca banco real)
                 nas 3 telas.
                 REFINADO 2026-09-04 (DEC-112): o primeiro layout colocava os 7 KPIs em uma
                 linha e cortava os cartões à direita. Reorganizado em 4 cartões críticos +
                 faixa de 3 indicadores complementares; cabeçalho/status, gráficos, censo,
                 configuração e consulta local ganharam hierarquia e espaçamento consistentes.
                 Novo `scripts/preview_ui.py` gera screenshots das 3 telas com banco temporário
                 e prontuários DEMO-* (nunca toca dado real). Verificação: 420 testes aprovados.
                 REFERÊNCIA VISUAL 2026-09-04 (DEC-113): fundo cinza suave, navegação lateral
                 branca, cartões claros, tipografia escura, destaque laranja, ícones lineares
                 próprios e novo símbolo do aplicativo aplicados ao painel, conexão inicial,
                 configuração e consulta. O censo permanece compacto e o restante continua
                 acessível por rolagem; em 1024×700, os gráficos viram automaticamente um
                 resumo textual legível e reaparecem ao ampliar a janela.
                 ÍCONE 2026-09-04 (DEC-115): a janela Tk passou a usar o próprio
                 `assets/gsus-auditoria.ico` (empacotado como dado ao lado do .exe), unificando
                 título/barra de tarefas com .exe, atalho e instalador; PhotoImage vira fallback.
                 +3 testes (`tests/unit/test_icons.py`). Instalador 1.1.0 regenerado e reinstalado.
[x] UI-006       Botão "ENCERRAR" a auditoria em andamento (2026-09-04, pedido do usuário, DEC-116):
                 cancelamento cooperativo via `cancel_event` (antes do censo, entre pacientes na
                 Fase 1, entre itens na Fase 2; IA em andamento derrubada na hora). Run `CANCELLED`,
                 diagnóstico `CANCELADA` neutro, sem relatório parcial nem snapshot. +8 testes.
                 Melhoria possível (não bloqueadora): cancelar também entre dias dentro de
                 `records.extract_notes` para responder em segundos.
[x] UI-007       Interruptor "Navegador visível" / "Segundo plano" (2026-09-04, DEC-116):
                 `AppConfig.browser_visible` persistido em config.json (vale na tarefa agendada),
                 `GSUSClient(headless=...)`, `ToggleSwitch` em Canvas na barra de status. Padrão
                 visível (DEC-077). PENDENTE: validação REAL do modo segundo plano pelo usuário --
                 o GSUS já bloqueou navegador oculto antes; a tela sugere voltar se falhar.
[x] REPORT-002   Relatório HTML alinhado à mesma referência visual (DEC-113): cartões arredondados,
                 hierarquia tipográfica, tabelas claras, tags e destaques laranja, responsivo
                 para telas menores. Prévia segura reproduzível em `scripts/preview_report.py`,
                 sempre com banco temporário e dados DEMO-*.
[x] DIAG-001     Diagnóstico de execução em linguagem simples (2026-09-03, pedido explícito
                 do usuário) -- o auditor (sem conhecimento técnico) precisa saber se uma
                 falha foi o GSUS/rede/máquina (não é defeito do programa) ou algo que precisa
                 de suporte de verdade, sem abrir log técnico. Auditoria exaustiva de catálogo
                 de falhas (4 agentes, DEC-111) ANTES de implementar -- achado central: várias
                 falhas (login, censo total) não deixavam nenhum rastro no banco, e o nome da
                 classe de exceção sozinho não bastava pra classificar "foi o GSUS" (algumas
                 exceções GSUS* cobrem casos que merecem investigação de verdade). Nova tabela
                 aditiva `run_diagnostics`, módulo puro `app/analysis/run_diagnosis.py`
                 (classificação por padrão de mensagem curado DEC a DEC, nunca "parece que é"),
                 aviso de "atualização agendada pode não ter rodado" (RESIL-003, máquina
                 desligada de noite) calculado ao vivo, faixa nova na tela principal.
                 Testes: +19 unitários (classificação pura) +4 e2e (grava certo nos 3
                 desfechos, incluindo o cenário antes invisível). Suíte completa: 386 passed.
[ ] DIAG-002     Erro de ambiente rotulado como "falha do GSUS" (achado 2026-09-04, DEC-114):
                 `classify_top_level_exception` devolve FALHA_GSUS para QUALQUER `playwright.Error`,
                 inclusive "Executable doesn't exist" (navegador interno ausente) -- hoje isso
                 registrou 7x "o GSUS não respondeu a tempo" quando o Firefox simplesmente não
                 existia na instância dev. Proposta: casar "Executable doesn't exist"/"playwright
                 install" antes do ramo Playwright e devolver FALHA_INESPERADA com orientação de
                 reinstalar/acionar suporte. +testes unitários em `test_run_diagnosis.py`.
[x] REPORT-003   Indicadores agregados do serviço (RF-28/RF-29, seção 12 da orientação técnica
                 de auditoria concorrente) -- PLANEJADO em detalhe 2026-09-01 (DEC-099), pedido
                 explícito do usuário pra avançar às "fases finais do projeto". Passou de "não
                 prioritário" pra ativo. Fases 1 e 2 IMPLEMENTADAS 2026-09-01 -- dado agregado
                 (instantâneo + histórico) pronto pra REPORT-004 consumir.
                 [x] Fase 1 (sem mudar schema) -- IMPLEMENTADA 2026-09-01:
                 - `app/reports/dashboard_metrics.py` (novo): `compute_service_indicators`
                   (% pacientes com pelo menos uma pendência ativa, distribuição por categoria/
                   prioridade/origem -- recalculadas na leitura, nunca lidas do snapshot de
                   criação, mesmo princípio de `html_report.py`; % sem EDD documentada; % EDD
                   vencida; tempo mediano de resolução geral e por categoria) e
                   `compute_unit_census` (censo agregado por unidade de verdade -- subtotais,
                   diferente da tabela `.census` de `html_report.py`, que é uma linha por
                   paciente).
                 - `Repository.get_resolved_pending_items` (novo, só SQL).
                 - `app/analysis/priority.py::is_edd_overdue` (novo) extraído de
                   `html_report.py::_format_edd` -- achado real durante a implementação: "sem
                   EDD documentada" e "EDD vencida" são conceitos DISTINTOS e não sobrepostos
                   (VENCIDA/REGISTRADA-mas-já-passada TIVERAM data real documentada; só
                   NAO_REGISTRADA ou nunca analisado conta como "sem previsão") -- um teste
                   pego escrevendo os testes, não em produção, mas seria um indicador errado se
                   tivesse ido pro ar sem essa distinção.
                 - Testes: +8 (`test_priority.py::is_edd_overdue`) +9
                   (`test_dashboard_metrics.py`). Suíte completa: 337 passed, mesmos 10 erros de
                   Chromium desta máquina (não relacionados).
                 [x] Fase 2 (schema novo, aditivo) -- IMPLEMENTADA 2026-09-01:
                 - `app\storage\database.py`: tabelas novas `daily_snapshot` (rollup do serviço:
                   total de ativos, com pendência ativa, sem EDD, EDD vencida, dia_vermelho/
                   verde, tempo mediano de resolução) e `daily_snapshot_category` (filha 1-N,
                   `dimension`/`label`/`total` -- uma tabela só para as 4 dimensões
                   category/origin/priority/dia_causa em vez de 4 tabelas quase idênticas).
                   `CREATE TABLE IF NOT EXISTS` basta (tabela nova, sem `_migrate`).
                 - `Repository.save_daily_snapshot` (grava pai + filhos numa chamada),
                   `get_daily_snapshots` (ordem cronológica, com `limit` opcional pros N mais
                   recentes) e `get_daily_snapshot_categories`.
                 - `app\reports\dashboard_metrics.py`: `ServiceIndicators` ganhou
                   `patients_dia_vermelho`/`patients_dia_verde`/`dia_causa_counts` (calculados
                   no MESMO loop que já lia `patient_state` pra EDD -- sem consulta duplicada).
                   `save_snapshot(repo, run_id)` reusa `compute_service_indicators` (nunca
                   duas fontes de verdade sobre o que conta como "dia vermelho" entre o
                   instantâneo e o histórico) e grava via `Repository`.
                 - `app\orchestrator.py::run_once` chama `dashboard_metrics.save_snapshot` uma
                   vez ao fim de cada execução (depois da Fase 2/IA terminar -- é o retrato
                   final do dia), isolado em try/except (mesmo padrão de `generate_report`: uma
                   falha ao gravar o snapshot não pode jogar fora uma execução que já persistiu
                   tudo o que importa).
                 - Fecha a lacuna real do RF-28 ("manter histórico de dias vermelhos por
                   causa") -- antes desta tabela, `patient_state` era UPSERT puro (sobrescrito a
                   cada análise), sem NENHUM jeito de saber como o serviço estava há 1 semana.
                 - Habilita indicadores de TENDÊNCIA (% dias vermelhos ao longo do tempo,
                   barreiras por 100 pacientes-dia) pra REPORT-004 -- ainda vazios até a
                   próxima execução real gravar o 1º snapshot em produção.
                 - Testes: +9 (`test_repository.py::daily_snapshot`, 4;
                   `test_dashboard_metrics.py::dia_classificacao`/`save_snapshot`, 5). Suíte
                   completa: 346 passed, mesmos 10 erros de Chromium desta máquina.

[x] REPORT-004   Tela única (dashboard + painel de gerenciamento), PLANEJADO 2026-09-01
                 (DEC-099), IMPLEMENTADO 2026-09-01. `app/ui/main_window.py` reescrito por
                 completo (mesma classe `MainWindow`, atributos/métodos usados pelos testes de
                 "Atualizar agora" -- `status_label`/`_updating`/`_report_path`/`_on_update` --
                 preservados intactos).
                 - Barra de controle no topo (Atualizar Agora/Abrir Relatório/Localizar
                   Paciente/Configurações + status), agora dentro de um `Frame` (não mais filhos
                   diretos de `root`) pra caber ao lado de Setor/Próxima atualização.
                 - 7 cartões de KPI (pacientes ativos, com pendência ativa, sem EDD, EDD vencida,
                   dia vermelho/verde hoje, tempo mediano de resolução).
                 - 4 gráficos matplotlib (`FigureCanvasTkAgg`, `Figure` direto -- NUNCA
                   `matplotlib.pyplot`, pra não vazar memória no registro global de figuras a
                   cada vez que a janela é reconstruída ao navegar Configurações/Localizar
                   Paciente e voltar): pendências por categoria (barra), por prioridade (pizza,
                   cores iguais ao relatório HTML -- vermelho/âmbar/verde), interno×externo
                   (pizza), tendência de dias vermelhos (linha, lê `Repository.get_daily_snapshots`
                   -- mostra aviso "sem histórico suficiente" com menos de 2 execuções).
                 - Faixa compacta de censo por unidade (`compute_unit_census`, Fase 1) entre os
                   gráficos e a tabela.
                 - Tabela de censo POR PACIENTE (`ttk.Treeview`, `compute_patient_census_rows`
                   novo) -- decisão de esclarecimento sobre a ambiguidade do texto original do
                   DEC-099 ("censo agregado por unidade... com clique abrindo o relatório
                   individual"): uma tabela agregada por unidade não tem paciente pra abrir por
                   clique, então a tabela é por PACIENTE (ordenável clicando no cabeçalho,
                   filtro embutido por leito/prontuário/unidade/categoria), sortável por unidade
                   entre outras colunas -- e o agregado por unidade genuíno vira a faixa acima.
                   Duplo-clique numa linha abre o relatório individual (`generate_patient_report`,
                   mesmo código de "Localizar Paciente") -- "Localizar Paciente" continua
                   INTOCADO, é busca de HISTÓRICO COMPLETO (RF-19), ação diferente por natureza.
                 - Atualização da VISUALIZAÇÃO (reconsulta o banco local via conexão própria de
                   curta duração, nunca GSUS/IA) ao fim de toda execução (`_finish_update`) +
                   periódica a cada 60s enquanto a janela está aberta (`_periodic_refresh`,
                   com checagem `_is_alive` -- se a janela foi trocada por Configurações/
                   Localizar Paciente, para de reagendar em vez de tentar atualizar um
                   `status_label` já destruído).
                 - Janela raiz virou redimensionável (`root.resizable(True, True)`,
                   `geometry("1200x820")`, `minsize(1024, 700)`) -- `SetupWindow`/`LookupWindow`
                   voltam a fixar `resizable(False, False)` explicitamente no próprio
                   `__init__`, já que `app/main.py::render` só troca widgets, nunca reseta esse
                   estado sozinho.
                 - Nova dependência: matplotlib 3.11.1 (~40-90MB no instalador) -- primeira
                   dependência de terceiros "pesada" do projeto, aceita explicitamente pelo
                   usuário (DEC-099). Adicionada a `requirements.txt` e instalada no `.venv`.
                 - Rebuild + reinstalação completa CONCLUÍDA (DEC-100): achado real de
                   empacotamento (`numpy._core._exceptions` não coletado automaticamente pelo
                   hook do PyInstaller nesta combinação de versões -- o `.exe` quebrava
                   silenciosamente ao abrir a janela principal, antes de qualquer log). Corrigido
                   com `hiddenimports=collect_submodules('numpy._core')` em
                   `installer/gsus-auditoria.spec`. Binário final instalado e confirmado de pé
                   (sem traceback, matplotlib inicializando normalmente).
                 - Verificação visual: script fora da suíte (scratchpad) populou um banco
                   fictício (prontuários "F0001".."F0006", nenhum dado real) e tirou um
                   screenshot da janela renderizada de verdade -- confirmado visualmente antes
                   de considerar a fase concluída.
                 - Testes: +9 (`test_dashboard_metrics.py::compute_patient_census_rows`, 3;
                   `test_main_window_dashboard.py`, novo, 6) e 1 teste existente ajustado
                   (`test_app_shell.py` -- botões agora dentro de `Frame`s, busca passou a ser
                   recursiva). Suíte completa: 355 passed, mesmos 10 erros de Chromium desta
                   máquina (não relacionados).
                 Fora do V1 (futuro): drill-down mais rico, captura de concordância/discordância
                 do auditor por achado (alimenta a validação científica da seção 17/RF-30 da
                 orientação técnica).
[x] RETENTION-001 Política de retenção de texto bruto configurável. IMPLEMENTADO 2026-08-25
                 (DEC-068, pedido do usuário): texto bruto de evolução (`notes`) de paciente
                 com alta há mais de `AppConfig.raw_notes_retention_days` (default 90,
                 configurável) é apagado a cada "Atualizar agora". Resumo estruturado
                 (patient_state/pending_items/pending_item_evidence, com citações curtas de
                 evidência) NUNCA é apagado -- é o que a seção 17 da orientação técnica
                 precisa pra validação auditor×IA. Sem UI de configuração ainda (só via
                 config.json) -- suficiente por agora, campo de tela fica pra depois se
                 precisar
[ ] CENSUS-002   Filtro de data de internação no censo (pedido pelo usuário 2026-08-20 --
                 permitir extrair só "pacientes de hoje" em vez do censo completo. NOTA:
                 diferente do `max_patients` já implementado no DEC-029 -- aquele é um limite
                 de QUANTIDADE só pra teste rápido; isto aqui seria um filtro por DATA na
                 própria busca do GSUS, ainda não implementado)
```

## Notas de execução

- `GSUS-001` a `GSUS-005` estão todos validados ponta a ponta contra o GSUS real (2026-08-20) -- `FROZEN`. Não mexer sem motivo real (seção 48 do prompt mestre).
- Toda task, ao terminar, deve: rodar testes, atualizar `CURRENT_STATE.md`, marcar `[x]` aqui, e parar.
