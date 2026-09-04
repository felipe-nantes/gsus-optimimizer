# CURRENT_STATE.md — GSUS Auditoria

## 2026-08-20 (2) — EXTRACT-001/002, QUEUE-001, ROBUST-001, INCR-001, RULES-001, LLM-001(parcial)/002, REPORT-001, UI-001/002/003, SCHEDULE-001(parcial), BUILD-001(parcial), GSUS-001(login real)

**Task concluída:** Implementação de todo o pipeline que não depende de dado real do GSUS (extração/normalização/hash/regras/LLM/relatório/orquestração), integração real de "Atualizar agora" na UI, login GSUS real (seletores confirmados), scaffold de agendamento e empacotamento.

**Arquivos alterados/criados (principais):**
- `app/extraction/normalizer.py`, `parser.py`, `hashing.py`
- `app/storage/repository.py` (fila, resume, pendências, estado do paciente)
- `app/analysis/rules.py`, `schemas.py`, `llm.py`
- `app/reports/html_report.py`
- `app/orchestrator.py` (pipeline completo)
- `app/gsus/login.py` (real, não mais BLOCKED_GSUS), `adapter.py` (novo — ponte GSUS↔orchestrator), `records.py` (contrato ajustado p/ texto bruto)
- `app/ui/main_window.py` (atualizar agora real, com thread + fila de progresso), `setup_window.py` (edição sem obrigar reentrar senha), `errors.py` (novo)
- `app/scheduling.py` (novo)
- `fixtures/notes/*.txt` (11 fixtures sintéticas — seção 34)
- `tests/unit/*` (normalizer, hashing, parser, rules, repository, schemas, html_report, scheduling, app_shell, main_window_update), `tests/integration/test_llm.py`, `tests/e2e/test_pipeline.py`
- `installer/gsus-auditoria.spec`, `scripts/build.ps1`, `scripts/check_login.py`
- `DECISIONS.md` (DEC-006), `TASKS.md`, `PROJECT_SPEC.md` (campo `gsus_base_url`)

**Testes executados:**
```
./.venv/Scripts/python.exe -m pytest tests -q
67-68 passed (ver "Bloqueios" sobre 1 teste intermitente)
```
Cobertura: parsing fail-soft, normalização/hash determinísticos, fila com resume real (interrupção simulada), regras exame/interconsulta contra as 11 fixtures, contrato JSON do LLM (7 casos), cliente LLM contra servidor HTTP-stub (sucesso, retry, JSON cercado por ```json, evidência vazia rejeitada, esgotamento de tentativas), relatório HTML (contagens, falhas visíveis, escape de HTML), pipeline E2E completo (2 pacientes, 1 falha isolada, incrementalidade entre 2 execuções, funcionamento sem LLM), fluxo real de "Atualizar agora" (sucesso, credencial ausente, `NotImplementedError` mapeado para mensagem amigável).

**Build:** `PyInstaller --onedir` do shell (sem Chromium/llama ainda) gerado e testado — `installer/dist/gsus-auditoria/gsus-auditoria.exe` abre sem erro.

**Resultado:** PASS (com uma ressalva de ambiente — ver Bloqueios).

**Pendências:**
- Rodar `scripts/check_login.py` (o usuário, no próprio terminal) para validar o login real ponta a ponta e depois compartilhar estrutura *sanitizada* das telas de censo/prontuário/evoluções.
- Decidir e instalar Chromium do Playwright (`playwright install chromium`) e obter binário `llama-server` + modelo `.gguf` — ambos são downloads que exigem autorização explícita (ver pedido no chat).
- `RULES-001`: palavras-chave são candidatas: precisam de validação por alguém com critério clínico antes de uso em produção real (RF-07).

**Bloqueios:**
- `GSUS-002..005` continuam `BLOCKED_GSUS` — dependem de estrutura sanitizada das telas pós-login (não posso navegar até lá eu mesmo: exporia dado real de paciente à minha própria API, o que a spec proíbe explicitamente — ver DEC-006).
- `SCHEDULE-001`/registro real no Task Scheduler e teste real de `scripts/check_login.py` não foram executados por mim: são mudanças de configuração persistente do sistema / uso de credencial real, fora do escopo do que decidi autonomamente sem pedir — aguardando o usuário.
- **Flake de ambiente (não é bug do app):** ~1 em cada 4–8 execuções da suíte, um teste que usa `tkinter.Tk()` falha com erro do Tcl ("tk wasn't installed properly" / "invalid command name") e passa normalmente ao rodar de novo. Diagnóstico: `Get-MpComputerStatus` confirma Proteção em Tempo Real do Windows Defender ativa nesta máquina — padrão clássico de leitura intermitente de arquivo bloqueada por antivírus durante o `source` dos scripts `.tcl`. Não mexi em exclusões do Defender (mudança de configuração de segurança) — se persistir/atrapalhar no dia a dia, considerar adicionar uma exclusão do Defender para a pasta de instalação do Python/do app.

**Próxima task:** Depende do usuário: (a) rodar `scripts/check_login.py` e mandar estrutura sanitizada da tela de censo, ou (b) autorizar os downloads (Chromium do Playwright; binário+modelo do llama.cpp) pra eu seguir com `LLM-001` completo e testes reais de rede/timeout.

---

## 2026-08-20 (3) — Downloads autorizados, teste real de login, achado de RAM

**Task concluída:** Chromium do Playwright instalado, runtime llama.cpp + modelo Llama baixados, Windows Task Scheduler validado de verdade, 1 bug real corrigido no login GSUS.

**O que rodei (autorizado pelo usuário):**
1. `playwright install chromium` — Chromium 130 baixado com sucesso (~140MB).
2. Round-trip real em `app/scheduling.py` contra o Windows Task Scheduler de verdade (`GSUSAuditoria_TesteReal`: criei → confirmei que existe → apaguei → confirmei que sumiu). Sem resíduo.
3. Runtime `llama.cpp` (release `b10516`, build CPU-only Windows x64, ~18,5MB) baixado e extraído em `runtime/` — `llama-server.exe --help` roda.
4. Modelo `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf` (~4,6GB, bartowski/HuggingFace) baixado em `models/model.gguf` — tamanho conferido byte a byte contra o `Content-Length` do servidor. Ver DEC-007 (por que Llama 8B em vez do Qwen 3B menor que eu tinha sugerido).

**Bug real encontrado e corrigido:** testei `login()` contra o GSUS real (só confirmando a URL pós-login, sem abrir nenhuma tela com dado de paciente — mesmo cuidado do DEC-006). `get_by_label("CPF")` quebrou com "strict mode violation": a tela tem 4 campos ocultos (métodos SMS/Token/E-mail) que resolvem pro mesmo nome acessível. Troquei para os IDs estáveis que já tinha capturado por inspeção real (`#attribute_central`, `#password`, `#btn-central-acessar`). Ver DEC-008.

**Resultado do teste de login real, já com o fix:** o formulário foi submetido de verdade, mas a URL não virou `/gsus-integrado` dentro de 30s. **Parei de tentar de novo sozinho** — essa é a conta "Identidade Digital PR" (login único estadual, não exclusivo do GSUS); tentativa errada repetida pode bloquear a conta em outros serviços do estado, não só no GSUS. Isso não é uma decisão que devo tomar sozinho (ver DEC-008).

**Achado sobre RAM:** a máquina tem 15,7GB no total mas só **2,5GB livres** no momento (`Get-CimInstance Win32_OperatingSystem`). Carregar o modelo de 4,6GB com tão pouca RAM livre pode deixar a máquina lenta enquanto carrega (llama.cpp usa mmap por padrão, então não trava tudo, mas pagina bastante do disco). Por isso **não rodei ainda** o teste de inferência real (`LocalLLM.start()` com o modelo de verdade) — evitando travar a máquina enquanto o usuário pode estar usando ela. Fica pendente pra quando: (a) tiver mais RAM livre (fechar outros programas), ou (b) o usuário topar mesmo assim.

**Testes automatizados:** sem mudança de contagem (67-68 passed) -- as validações acima foram manuais/reais, fora da suíte pytest (não fazem sentido como teste automatizado repetível, já que dependem de credencial real e de uma conta de produção).

**Resultado:** PASS parcial — infraestrutura toda pronta e validada; faltam 2 decisões/ações do usuário (login real, teste de inferência com RAM apertada).

**Bloqueios:**
- Login real: aguardando o usuário rodar `scripts/check_login.py` (modo headed, interativo) pra ver com os próprios olhos o que acontece depois de "Entrar" (senha errada? 2FA? CAPTCHA? só lento?).
- Teste de inferência real do LLM: aguardando RAM livre ou confirmação do usuário pra tentar mesmo com pouca RAM livre.
- `GSUS-002..005` (censo/prontuário/evoluções) seguem bloqueados exatamente como antes — dependem do login funcionar primeiro, e depois de estrutura sanitizada das telas.

**Próxima task:** Aguardando o usuário nos dois pontos acima. Enquanto isso, nada mais fica bloqueado — todo o resto (regras, banco, relatório, orquestração, empacotamento do shell) já está implementado e testado.

---

## 2026-08-20 (4) — GSUS-001 e LLM-001 validados de verdade, ponta a ponta

**Task concluída:** Login GSUS real funcionando 100% (Firefox, pop-up, frameset, confirmação de estabelecimento) e LLM local rodando de verdade (binário+modelo reais, não mock) com saída correta.

**O que aconteceu:**
1. Usuário mandou 2 screenshots sanitizados (sem dado de paciente) mostrando: aba original diz "sistema aberto em outra janela"; o app real abre num **pop-up**, com modal "Selecionar Estabelecimento" (Confirmar/Desconectar).
2. Investigação headless (sem ver dado de paciente) confirmou: GSUS é frameset clássico, conteúdo real vive num frame chamado `content`; botão Confirmar é `#botaoConfirmar` (input submit) dentro desse frame.
3. **Achado principal:** com **Chromium**, o pop-up nunca chegava a abrir (timeout). Com **Firefox**, tudo funcionou -- confirma a observação original do usuário ("só abre no Mozilla"). Troquei o engine padrão do Playwright pra Firefox (DEC-010) -- desvio documentado da stack congelada original ("Chromium empacotado").
4. Login real validado ponta a ponta: depois de confirmar o estabelecimento, o frame `content` navega para `carregarEASPadrao` -- dentro do sistema de verdade. `GSUS-001` está **completo**.
5. RAM liberada pelo usuário (2,5GB → 6,5GB livre) permitiu testar o LLM de verdade. Achei e corrigi 2 bugs reais nesse processo:
   - `LocalLLM._wait_ready()` tinha timeout fixo de 60s (não configurável) -- baixo demais pra CPU fraca (modelo 8B levou 108,7s pra carregar). Virou `startup_timeout_seconds` configurável, default 300s.
   - Se `start()` falhasse no timeout, o processo `llama-server.exe` ficava **órfão**, consumindo ~8GB RAM sem eu saber (encontrei via `tasklist`, matei manualmente). Corrigido: `start()` agora chama `self.stop()` automaticamente se `_wait_ready()` falhar.
6. Com os fixes, rodei 1 análise real (síntética, prontuário fictício) via `scripts/_bench_llm.py`: **carga do modelo 108,7s, 1 análise 80,6s**, saída válida (`insufficient_information: False`, 1 pendência de exame identificada corretamente, `clinical_context` = "Investigação de dor abdominal" -- semanticamente correto pro fixture usado).

**Implicação de performance (RAM/CPU fracas confirmadas como perfil do hardware-alvo -- ver memória do projeto):** ~80s por paciente com notas novas, mais ~110s de carga única por execução. Para um setor com N pacientes com atualização, uma execução noturna agendada (ex.: 23:00) é totalmente viável mesmo levando dezenas de minutos; uso interativo via "Atualizar agora" vai ser perceptivelmente lento -- a UI já mostra progresso ("Processando paciente X de Y..."), o que ajuda a gerenciar expectativa, mas vale considerar no futuro (P1) mostrar uma estimativa de tempo.

**Arquivos alterados:** `app/gsus/login.py` (retorna Page do pop-up, usa frame `content`, corrige seletor CPF ambíguo), `app/gsus/client.py` (Firefox + `get_content_frame()`), `app/gsus/adapter.py` (usa página retornada por login), `app/analysis/llm.py` (timeout configurável + cleanup de processo órfão), `scripts/check_login.py` (Firefox, usa retorno de login), `scripts/_bench_llm.py` (novo, benchmark manual), `DECISIONS.md` (DEC-009, DEC-010), `ARCHITECTURE.md`/`PROJECT_SPEC.md` (Firefox em vez de Chromium).

**Testes automatizados:** 68 passed (suíte inteira, incluindo os módulos alterados -- nenhuma regressão).

**Resultado:** PASS. `GSUS-001` e `LLM-001`/`LLM-002` agora completos de verdade, não só "esqueleto testado com stub".

**Bloqueios:** `GSUS-002..005` (censo/prontuário/evoluções) seguem os únicos pendentes -- agora que o login funciona de ponta a ponta, o próximo passo real é o usuário rodar `scripts/check_login.py`, navegar até a tela de censo de internados dentro do pop-up, e descrever a estrutura (ou mandar HTML/screenshot sanitizado) pra eu implementar `census.py` de verdade.

**Próxima task:** `GSUS-002`/`GSUS-003` (censo), assim que a estrutura sanitizada da tela chegar. Sem isso, próximas tasks possíveis sem bloqueio: `BUILD-001` completo (empacotar Firefox+llama-server+modelo no PyInstaller), `INSTALL-001`.

---

## 2026-08-20 (5) — GSUS-003 (censo) implementado — com correção de manuseio de PHI no processo

**Task concluída:** `census.py` extrai a lista de internados (paginação incluída), testado com fixtures fictícias.

**Incidente registrado:** usuário enviou screenshot da tela de censo com dado real de 12 pacientes (nome, nascimento, nome da mãe, prontuário). Sinalizei o problema, não reproduzi nenhum dado real em lugar nenhum (código/fixture/log), e extraí só a estrutura (colunas, campos de formulário) -- ver `DECISIONS.md` DEC-011 para o relato completo e o que foi feito para conter isso.

**Arquivos alterados/criados:** `app/gsus/census.py` (implementado, antes só `NotImplementedError`), `app/gsus/adapter.py` (resolve o frame `content` antes de chamar census/records), `app/gsus/records.py` (type hints `Frame | Page`), `fixtures/gsus_html/census_page*.html` (5 fixtures fictícias: normal, vazia, duplicata, paginação p1/p2), `tests/integration/test_census_parser.py` (5 testes, Playwright real carregando HTML local via `file://`), `scripts/check_census.py` (novo -- validação real pelo usuário, nunca imprime nome/prontuário, só contagem/leito/unidade), `DECISIONS.md` (DEC-011), `TASKS.md`.

**Testes:** 73 passed (suíte inteira, +5 desde a última entrada).

**Resultado:** PASS.

**Pendências:**
- `_navigate_to_search_screen` (menu "Internação" → "Pesquisar Internação") é uma inferência a partir do menu visível na tela pós-login, **não confirmada em execução real**. `scripts/check_census.py` está pronto para o usuário validar -- se o caminho de menu estiver errado, ele avisa e pede pra descrever em texto (não screenshot) qual o nome certo do item de menu.
- Campo "Nº Prontuário" da busca tem 2 sub-campos (provável número + dígito verificador) -- relevante para `GSUS-004` (busca de prontuário individual), não para o censo.

**Bloqueios:** `GSUS-004`/`GSUS-005` (abrir prontuário individual, extrair evoluções) seguem bloqueados -- ainda não vi nem descrição sanitizada dessas telas.

**Próxima task:** Usuário roda `scripts/check_census.py` pra validar `GSUS-002`/`003` contra o GSUS real (sem me mandar dado nenhum de paciente -- só confirmar se rodou ou onde travou). Em paralelo, aguardando estrutura sanitizada da tela de prontuário individual (`GSUS-004`/`005`) -- **por favor, sem screenshot com paciente real**; descrição em texto, ou paciente fictício, ou print com os dados borrados/tarjados.

---

## 2026-08-20 (6) — GSUS-002 confirmado por texto

Usuário confirmou (só texto, sem screenshot) o caminho de navegação até o censo: **Internação → Pesquisar Internação → Pesquisar**. Bate exatamente com o que eu tinha inferido do menu visível no print anterior. `GSUS-002` e `GSUS-003` estão completos e confirmados -- nenhuma mudança de código necessária, só documentação (`DECISIONS.md` DEC-011, `TASKS.md`).

**Bloqueio restante:** só `GSUS-004`/`005` (abrir prontuário individual pelo número, extrair evoluções). Preciso da estrutura dessa tela -- texto, ou print com dado tarjado/paciente fictício (não real, ver incidente registrado acima).

---

## 2026-08-20 (7) — bug real de navegação corrigido (menu não é `<a>`)

Usuário reportou que `scripts/check_census.py` "abre perfeitamente mas para na tela inicial" -- sem erro visível. Investiguei headless, só na tela de menu/formulário vazio (sem paciente): "Internação" e "Pesquisar Internação" são `<div>` de um widget de menu customizado, não `<a>` -- por isso `get_by_role("link", ...)` nunca achava nada e a chamada ficava esperando até estourar timeout (exceção não capturada, script morre, navegador fica aberto parado -- exatamente o sintoma relatado). Corrigido pra `get_by_text(...)`. Botão "Pesquisar" também não é `<button>`, é `<input type="button" id="btConsultar">` -- troquei pro id real. Validei a função de navegação de verdade (headless) até o formulário aparecer, parei antes de clicar em Pesquisar (não vi tabela de paciente nenhuma). Ver `DECISIONS.md` DEC-012.

**Testes:** 73 passed (sem mudança de contagem -- os testes de census já bypassavam a navegação de propósito).

**Próxima task:** usuário roda `scripts/check_census.py` de novo -- agora deve passar da navegação e chegar a clicar "Pesquisar" de verdade, o que eu não posso validar sozinho (mostraria paciente real).

**Confirmado pelo usuário:** rodou de novo, "mostrou todo o censo de internações". `GSUS-002`/`GSUS-003` **validados ponta a ponta contra o GSUS real** -- login, navegação, pesquisa, extração e paginação todos funcionando. Marcado `FROZEN` (não mexer sem motivo, seção 48 do prompt mestre).

**Único bloqueio restante:** `GSUS-004`/`005` -- abrir prontuário individual pelo número (`#codPaciente`, já identificado) e extrair evoluções. Preciso da estrutura dessas duas telas (mesma regra de sempre: texto, ou print com dado tarjado/paciente fictício).

---

## 2026-08-20 (8) — GSUS-004/005 implementados (2º incidente de PHI, mais grave -- ver DEC-013)

**Task concluída:** `records.py` (abrir prontuário + extrair evoluções) implementado; `parser.py`/`rules.py` corrigidos para o formato de cabeçalho real (descoberto neste incidente).

**Incidente:** usuário mandou 7 screenshots; 3 delas (imagens 5-7) continham nota clínica completa e real -- diagnóstico, sinais vitais, medicação, nome de 3 profissionais, tudo do mesmo prontuário real do incidente anterior. Mais grave que o de `DEC-011`. Sinalizei de forma mais direta, não reproduzi nada. Ver `DECISIONS.md` DEC-013 para o relato completo.

**O que mudou de verdade (info nova, não suposição):**
- Cabeçalho real de cada evolução é `DD/MM/AAAA HH:MM - PROFISSIONAL (CARGO)` -- só 1 traço, sem campo de especialidade separado. Minha suposição original (`DEC-005`) tinha 2 traços; estava errada.
- `parser.py` reescrito: divide por ocorrência do padrão de cabeçalho (`re.finditer`), não mais por linha em branco -- mais robusto pra texto de vários blocos DOM concatenados.
- `rules.py`: regra de interconsulta não depende mais de campo de especialidade estruturado (não existe de verdade) -- resolve só por texto, igual à regra de exame.
- Nome do profissional nunca é guardado -- só o cargo.
- Formulário de "Pesquisar Prontuário" inspecionado por mim (headless, tela vazia): `#codPaciente`, `#btnConsultar` (diferente do `#btConsultar` do censo).

**Arquivos alterados:** `app/gsus/records.py` (implementado), `app/extraction/parser.py` (reescrito), `app/analysis/rules.py` (`apply_consult_rule` reescrita), `fixtures/notes/*.txt` (11 arquivos, novo formato de cabeçalho), `tests/unit/test_parser.py` (+2 testes), `scripts/check_notes.py` (novo -- validação real sem devolver conteúdo clínico), `DECISIONS.md` (DEC-013).

**Testes:** 75 passed.

**Resultado:** PASS parcial -- ver gap abaixo.

**Gap reconhecido (importante):** `extract_notes`/`open_current_admission` (interação com o DOM real de dias/evoluções) **não tem teste automatizado**. Construir uma fixture HTML de accordion "chutando" a estrutura real repetiria o erro que a seção 14 do prompt mestre proíbe -- não inspecionei esse DOM de verdade (só descrição visual das screenshots, que eu não posso reabrir pra inspecionar o HTML). `scripts/check_notes.py` é a validação real -- roda sozinho, escolhe o 1º paciente do censo, e só imprime contagem de blocos encontrados (nunca conteúdo).

**Bloqueios:** nenhum -- GSUS-001 a 005 todos implementados. Falta validação real de GSUS-004/005 pelo usuário.

**Próxima task:** usuário roda `scripts/check_notes.py`. Se funcionar (contagem de blocos > 0, sem erro), os 5 módulos de automação GSUS estão prontos -- próximo foco vira `BUILD-001` completo (empacotar Firefox+llama-server+modelo) e `INSTALL-001`.

---

## 2026-08-20 (9) — bug real em GSUS-003 encontrado só com log de terminal (sem PHI)

Usuário rodou `scripts/check_notes.py` e colou o log do terminal (texto puro, sem dado de paciente -- exatamente como pedi). Erro real: `table.filter(has_text="Prontuário").first` pegava a tabela do FORMULÁRIO de busca (que também tem a palavra "Prontuário" no rótulo), não a tabela de resultado -- por isso uma célula que não existia nunca aparecia e o Playwright travava em timeout genérico de 30s, sem pista da causa. Isso não apareceu no teste anterior (DEC-012) porque aquele parava antes do clique em "Pesquisar", de propósito. Corrigido: marcador trocado para "Inconsistente" (só existe no cabeçalho de resultado) + checagem de nº de colunas que falha rápido e claro se acontecer de novo. `scripts/check_notes.py` também ganhou tratamento de erro melhor (nunca mais trava mudo). Ver `DECISIONS.md` DEC-014.

**Testes:** 75 passed (fixtures já tinham a coluna "Inconsistente", nenhuma mudou).

**Próxima task:** usuário roda `scripts/check_notes.py` de novo.

---

## 2026-08-20 (10) — 2º bug real em GSUS-003, mesmo padrão (log de terminal sem PHI)

Novo log colado (sem dado de paciente): "Linha 0 da tabela de resultado tem 0 coluna(s)". Causa: a tabela real não usa `<thead>` -- o cabeçalho é um `<tr>` comum dentro do `<tbody>`, com `<th>` em vez de `<td>` (por isso 0 colunas `<td>`, o que minha checagem do fix anterior confundia com "tabela errada"). Corrigido: 0 colunas agora é tratado como linha de cabeçalho (pula, não é erro); só 1-6 colunas continua sendo tratado como sinal de tabela errada. 2 fixtures + 2 testes novos cobrindo os dois casos separadamente. Ver `DECISIONS.md` DEC-015.

**Testes:** 77 passed (75 + 2).

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- terceira tentativa. Se passar da extração do censo, deve conseguir abrir o prontuário e testar a extração de evoluções de verdade pela primeira vez.

---

## 2026-08-20 (11) — 2 bugs reais mais: paginação AJAX + vazamento de log + confusão id/name

**3ª rodada de `scripts/check_notes.py`:** passou do censo (155/180 -- faltavam ~25 por causa de paginação AJAX mal esperada), mas o log revelou **16-21 prontuários reais vazados pro meu contexto via `logger.warning`** (bug meu: nenhum script configurava handler de log, então o padrão do Python imprime WARNING+ no terminal). Corrigido: (1) paginação agora espera o conteúdo mudar de verdade em vez de confiar em `wait_for_load_state()` (que não serve pra AJAX); (2) removido o prontuário da mensagem de log; (3) `scripts/_common.py` novo -- redireciona log de aplicação pra arquivo em todos os 3 scripts, corrigindo a classe do bug pra sempre, não só essa ocorrência.

**4ª rodada:** passou do censo, travou em `open_current_admission` (timeout no botão "Pesquisar" do prontuário). Investigado com número de prontuário **inventado** (sem paciente real possível) -- achei que era erro meu de leitura: o botão não tem `id`, só `name="btnConsultar"` -- eu tinha confundido as colunas numa inspeção antiga. Corrigido pra seletor por `name`.

Ver `DECISIONS.md` DEC-016 e DEC-017 pro relato completo.

**Testes:** 78 passed (75 + 1 fixture/teste de paginação AJAX + a suíte já tinha os 2 anteriores).

**Próxima task:** usuário roda `scripts/check_notes.py` mais uma vez. Se o botão de busca funcionar agora, deve chegar a abrir o prontuário de verdade -- o próximo ponto de risco não testado é a expansão do card de internação + dias + extração das evoluções (`extract_notes`), que eu genuinamente não posso validar sozinho sem ver dado real.

---

## 2026-08-20 (12) — 5ª rodada: censo melhorou (175/180), novo bug no preenchimento do campo

Censo com o fix da paginação AJAX: 175 de 180 (melhor que 155 antes, mas ainda não 100% -- vale reavaliar o timeout/tentativas do "esperar mudar" se persistir). Travou em seguida: usuário mandou screenshot da tela onde parou -- sem paciente, só a mensagem de validação do GSUS "O campo Nº Prontuário é obrigatório". Ou seja, o formulário foi enviado vazio: `.fill()` rodou antes do campo da nova tela estar pronto (mesma classe de corrida do DEC-016, agora no formulário em vez da paginação). Corrigido: preenche, lê de volta, confirma que bateu, tenta de novo se não bateu (até 3x), erro claro se persistir. Ver `DECISIONS.md` DEC-018.

**Testes:** 78 passed.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo. Se passar do preenchimento, deve conseguir abrir o prontuário de verdade pela primeira vez -- ponto ainda não validado nenhuma vez até agora.

---

## 2026-08-20 (13) — mesmo erro persistiu; causa real era outra (autocomplete legado) + vazamento de processos encontrado

**6ª rodada:** mesmo erro exato ("campo obrigatório"), apesar do fix do DEC-018 (fill + reconferir) já estar em produção. Investiguei mais a fundo (headless, número inventado) e achei a causa provável: a tela tem um campo companheiro `#nomePaciente` desabilitado, típico de autocomplete legado que só reage a evento de teclado (`keyup`) -- `.fill()` do Playwright não dispara isso, só seta o valor. Troquei para digitação simulada caractere por caractere (`press_sequentially`).

**Não validei esse fix eu mesmo** -- durante a investigação, um diagnóstico meu travou (censo lento em headless) e tive que matar o processo à força. Ao checar, achei **30+ processos Firefox órfãos** acumulados de rodadas anteriores nesta sessão, comendo vários GB de RAM (relevante pra máquina fraca do usuário). Limpei tudo. Decidi parar de rodar tantos diagnósticos ao vivo seguidos contra o GSUS real -- já foram muitos numa hora, e o vazamento de processo é um sinal de que eu deveria desacelerar essa parte. Deixando a validação do fix pro usuário.

Ver `DECISIONS.md` DEC-019 pro relato completo.

**Testes:** 78 passed.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 7ª rodada. Essa é uma hipótese bem embasada mas não confirmada; se ainda falhar no mesmo ponto, preciso repensar (pode envolver ver a tela com mais detalhe, o que aí sim pode exigir a ajuda do usuário olhando o código-fonte da página, não eu).

---

## 2026-08-20 (14) — progresso real: formulário submeteu, mas número truncado por corrida

**7ª rodada:** a digitação simulada funcionou (formulário não ficou mais vazio) -- mas "262531" virou "26253" na busca (perdeu o último dígito), resultando em "Prontuário '26253' não encontrado!". Causa provável: resposta AJAX do autocomplete referente a um estado intermediário da digitação sobrescrevendo o campo entre o último caractere digitado e o clique em Pesquisar. Corrigido: `_search_by_record_number` agora trata "não encontrado" e "campo obrigatório" como possíveis falsos negativos de corrida, não como resposta definitiva -- tenta a sequência inteira (digitar + clicar + conferir resultado) de novo, até 3 vezes. Ver `DECISIONS.md` DEC-020.

**Testes:** 78 passed (sem novo teste -- gap de cobertura desta função já reconhecido, DEC-013).

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 8ª rodada.

---

## 2026-08-20 (15) — 8ª rodada: campo preencheu certo, mas clique automático não disparava a busca

Usuário: campo preencheu corretamente, mas a busca só aconteceu depois de um clique manual dele na tela -- o clique automático no botão não fez efeito (3 tentativas esgotadas, erro claro, sem dado de paciente). Provável causa: handler de `blur` no campo que precisa disparar antes do botão funcionar (mesma família dos problemas de autocomplete anteriores). Adicionado `Tab` pra sair do campo antes de clicar em Pesquisar. Ver `DECISIONS.md` DEC-021.

**Testes:** 78 passed.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 9ª rodada.

---

## 2026-08-20 (16) — 9ª rodada: achado o bug real (falso positivo no marcador de erro)

Screenshot mostrou o campo preenchido e validado CORRETAMENTE (nome do paciente confirmado no campo companheiro) -- mas as 3 tentativas esgotaram mesmo assim. Causa: `REQUIRED_FIELD_MARKER = "obrigatório"` batia na legenda fixa do rodapé do formulário ("(*) Campo de preenchimento obrigatório."), sempre presente, não só no erro real ("O campo Nº Prontuário **é** obrigatório."). Toda tentativa era tratada como falha e retry limpava um campo que já estava certo. Corrigido pra `"é obrigatório"` (com o verbo, só bate no erro de verdade). Ver `DECISIONS.md` DEC-022.

É possível que isso também explique o sintoma "clique não disparava busca" relatado -- pode ter sido o retry falso-positivo atropelando um clique que na verdade tinha funcionado.

**Testes:** 78 passed.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 10ª rodada.

---

## 2026-08-20 (17) — causa real encontrada: Pesquisar abre pop-up novo, eu olhava pra página errada

10ª rodada: mesmo sintoma exato de novo (preencheu certo, botão não disparou nada visível) mesmo com o fix do marcador. Investiguei direto (headless): testei 4 formas de clicar com número inventado (nenhuma gerou requisição -- inconclusivo), depois com o número real já visto antes (sem ler/mostrar o nome -- só confirmei que bateu) chamei a função JS do botão diretamente: zero requisição na página original. Conclusão: "Pesquisar" provavelmente abre uma **janela pop-up nova** pro prontuário, mesmo padrão do login (DEC-009) -- eu só estava olhando a página errada o tempo todo. Corrigido usando o mesmo mecanismo (`expect_page()`) já usado no login: se abrir pop-up, usa ele; se não abrir, segue na mesma página como antes. `open_current_admission`/`_search_by_record_number` agora retornam a página certa, e `adapter.py` foi atualizado pra usar isso.

Ver `DECISIONS.md` DEC-023 pro relato completo.

**Testes:** 78 passed. Nenhum processo Firefox órfão dessa vez (limpo).

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 11ª rodada. Se essa hipótese estiver certa, deve ser a primeira vez que passa desse ponto.

---

## 2026-08-20 (18) — censo (já FROZEN) teve falha pontual de estabilidade + pedido de filtro por data

**11ª rodada:** desta vez o CENSO (não o prontuário) travou -- timeout de 30s esperando o link "Próxima" ficar "estável" pro Playwright clicar, apesar do seletor ter achado o elemento certo. Provável lentidão momentânea do servidor (muito uso automatizado hoje). Adicionado retry limitado (2x, 15s cada) + fallback: se continuar falhando, para a paginação e devolve os pacientes já coletados até ali, em vez de derrubar a execução inteira -- mesmo princípio de isolamento de falha já usado por paciente (RF-12), agora também na paginação.

**Pedido do usuário:** quando isso destravar, adicionar opção de filtrar o censo por data de internação (ex.: só "hoje"), pra tornar os próximos testes mais rápidos e reduzir carga no GSUS real. Registrado em `TASKS.md` como `CENSUS-002` (P1) -- não implementado ainda, foco continua em destravar GSUS-004/005 primeiro.

Ver `DECISIONS.md` DEC-024.

**Testes:** 80 passed (78 + 2 novos, `_click_next_with_retry` testado com stub, sem precisar de browser).

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 12ª rodada.

---

## 2026-08-20 (19) — causa real definitiva: botão inerte, Enter é o caminho de verdade

12ª rodada: mesmo resultado (3 tentativas, nada). Usuário deu a informação decisiva: digitando manualmente e apertando **Enter** (sem clicar em nada), a busca funciona na hora. Isso explica todos os testes anteriores de clique (Playwright, mouse nas coordenadas exatas, JS, onclick direto) terem falhado -- o botão provavelmente está mesmo quebrado nessa tela do GSUS (bug do sistema, não da automação), e o caminho real sempre foi tecla Enter. Trocado `_search_by_record_number` pra usar `field.press("Enter")` em vez do clique no botão. Ver `DECISIONS.md` DEC-025.

**Testes:** 80 passed.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 13ª rodada. Essa não é mais uma hipótese -- é o comportamento manual confirmado, então a expectativa de funcionar agora é bem mais alta que nas tentativas anteriores.

---

## 2026-08-20 (20) — retry inútil de novo; troquei a estratégia de detecção de erro por detecção de sucesso

13ª rodada: usuário -- "faltou apertar o enter, fora que está tentando as 3 vezes atoa". Suspeita: o código ainda checava texto de erro ("não encontrado"/"é obrigatório") logo depois da busca, cedo demais pra uma busca AJAX -- mesma corrida de sempre, possivelmente mascarando buscas que na real funcionaram (mesmo padrão de falso positivo do DEC-022). Reestruturei pra não interpretar mais texto de erro nenhum: agora só espera (bastante, 20s) pelo sinal de sucesso real ("Permanece Internado"). Se não aparecer, recarrega a tela de busca inteira do zero (não só reescreve o campo) e tenta de novo, até 3 vezes. Ver `DECISIONS.md` DEC-026.

**Testes:** 80 passed.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 14ª rodada.

---

## 2026-08-20 (21) — GSUS-004 validado de verdade! + bug real em extract_notes corrigido

**14ª rodada -- marco importante:** usuário confirmou "Abriu o prontuário e a data certa" -- `open_current_admission` (GSUS-004) funciona ponta a ponta contra o GSUS real, pela primeira vez. `GSUS-001` a `GSUS-004` estão todos validados.

`extract_notes` (GSUS-005) rodou sem travar mas extraiu errado: "1 bloco(s), 0 com cabeçalho reconhecido" -- a lógica de "subir um nível de ancestral a partir do cabeçalho do dia" capturava só o cabeçalho em si, não o conteúdo expandido. Corrigido: em vez de tentar achar o container certo (mais chute), agora captura o texto da página inteira depois de expandir os dias, e deixa o parser (que já localiza evolução por padrão de cabeçalho via regex, não por posição no DOM) separar tudo sozinho. Ver `DECISIONS.md` DEC-027.

**Testes:** 80 passed.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 15ª rodada. Se a contagem de blocos reconhecidos for maior que 0 agora, `GSUS-005` fecha e os 5 módulos de automação GSUS estão completos.

---

## 2026-08-20 (22) — achado o elemento clicável real por inspeção estrutural (não texto)

15ª rodada: mesmo resultado exato de antes ("1 bloco, 0 reconhecido") -- confirmou que o problema nunca foi COMO extrair o texto, e sim que o clique de expansão nunca funcionava de verdade. Investiguei a estrutura real (headless, prontuário já conhecido de telas anteriores, só tag/id/onclick -- nunca conteúdo clínico lido ou impresso). Dois diagnósticos deram problema no caminho (censo lento travando, depois uma tempestade de 60+ retries de clique) -- limpei os processos nos dois casos. Um terceiro diagnóstico mais enxuto (pulando o censo) achou: o elemento clicável de verdade é `<a class="item" id="historicoEvolucao0Item">` -- um link de verdade, um por evolução individual (não por dia, como a estrutura visual sugeria), com índice numérico no id. Reescrevi `extract_notes` pra usar esse seletor direto em vez de tentar achar o cabeçalho de dia pelo texto. Ver `DECISIONS.md` DEC-028.

**Testes:** 80 passed. Processos limpos (nenhum Firefox/Python órfão).

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 16ª rodada. Essa é a melhor evidência que já tive pra essa parte -- seletor de `id` real, não suposição de texto/posição.

---

## 2026-08-20 (23) — censo de teste encurtado (pedido do usuário) + bug de página pós-busca corrigido

Pedido do usuário: `scripts/check_notes.py` não precisa esperar o censo inteiro (80-175 pacientes) só pra pegar o primeiro -- estava deixando o ciclo de teste lento (e contribuiu pros diagnósticos travarem nesta sessão). `get_census`/`collect_all_pages` ganharam parâmetro opcional `max_patients` -- para de paginar assim que atinge o número pedido; omitido, comportamento idêntico a antes (busca tudo, usado pela produção sem mudança). `check_notes.py` agora chama com `max_patients=1`.

De brinde, achei e corrigi uma inconsistência real: `check_notes.py` chamava `open_current_admission` mas descartava a página retornada, continuando a usar `content_frame` pra `extract_notes` -- devia usar a página retornada (pode ser um pop-up novo, DEC-023). Não tinha causado erro ainda porque a busca por Enter não abre pop-up na prática, mas estava inconsistente com `adapter.py`, que já fazia certo desde o DEC-023.

Ver `DECISIONS.md` DEC-029.

**Testes:** 83 passed (80 + 3 novos, cobrindo o limite dentro da mesma página, evitando paginar, e confirmando que o padrão sem limite não mudou).

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 16ª rodada, agora bem mais rápida pra chegar no ponto que importa (abrir prontuário + extrair evoluções).

---

## 2026-08-20 (24) — accordion de 2 níveis identificado (atendimento -> evolução)

16ª rodada: censo bem mais rápido (pedido anterior funcionou), abriu o prontuário certo. "1 bloco, 0 reconhecido" de novo -- mas dessa vez pedi pro usuário colar o LOG DE ARQUIVO (não aparece mais no terminal desde o DEC-016), que revelou: os 2 cliques em `historicoEvolucaoNItem` deram timeout tentando expandir. Investiguei (headless, estrutura só): esse elemento fica dentro de `card_body#historicoAtendimentoN`, que por sua vez está colapsado -- e tem um `card_header#historicoAtendimentoNItem` (visível, irmão anterior) que precisa ser clicado PRIMEIRO. É um accordion de 2 níveis (atendimento -> evolução), mesmo padrão de nome nos dois. `extract_notes` agora expande os dois níveis em sequência. Ver `DECISIONS.md` DEC-030.

**Testes:** 83 passed. Processos limpos.

**Próxima task:** usuário roda `scripts/check_notes.py` de novo -- 17ª rodada. Se o log de arquivo mostrar os cliques de expansão indo bem (sem "não foi possível expandir"), e a contagem de blocos reconhecidos finalmente for maior que 0, `GSUS-005` fecha.

---

## 2026-08-20 (25) — GSUS-005 confirmado! + novo recurso "Localizar Paciente" implementado

**MARCO:** usuário confirmou -- "foi perfeito. Todo o histórico do paciente foi aberto e consultado, perfeito." Os 5 módulos de automação GSUS (`GSUS-001` a `GSUS-005`) estão validados ponta a ponta contra o sistema real. Marcados `FROZEN`.

**Novo pedido do usuário:** manter a extração completa (todas as internações, atual + antigas) como uma opção manual ("Localizar Paciente" por número de prontuário), separada da rotina automática (que continua olhando só a internação atual, confiando no hash incremental já existente). Antes de implementar, confirmei 3 pontos que afetam quanto dado sensível fica acumulado:
1. Histórico antigo: só exibe na tela, NUNCA salva no banco.
2. Internação antiga: só texto bruto, NUNCA passa por regras/LLM.
3. Rotina automática: sem nenhuma mudança.

**Implementado:**
- `app/gsus/records.py`: `get_full_admission_history_text()` -- abre TODOS os episódios de internação (não só o atual), reaproveitando o accordion de 2 níveis já validado. Generalização do cabeçalho ("Internação (...)" em vez de só "Permanece Internado") -- **ainda não confirmada com um paciente que tenha mais de uma internação** (o único testado só tinha uma).
- `app/gsus/adapter.py`: `GSUSAdapter.lookup_full_history()`.
- `app/ui/lookup_window.py` (novo): tela "Localizar Paciente" -- busca por prontuário, mostra resultado numa área de texto rolável, roda em thread separada, nunca toca o banco.
- `app/ui/main_window.py`: botão novo "LOCALIZAR PACIENTE".
- `app/ui/errors.py`: mensagem amigável específica pra `GSUSRecordError`.
- `PROJECT_SPEC.md` (RF-19), `ARCHITECTURE.md` atualizados.

Ver `DECISIONS.md` DEC-031 para o relato completo.

**Testes:** 91 passed (83 + 8 novos, incluindo um que confirma explicitamente que o fluxo "Localizar" nunca cria `auditoria.db`).

**Bloqueios:** nenhum bloqueio real -- só uma confirmação pendente (múltiplos episódios de internação no "Localizar", não testável com o paciente disponível até agora).

**Próxima task:** usuário testa "Localizar Paciente" na tela (não precisa de script -- é um botão na tela principal agora). Se o paciente de teste só tem uma internação, o resultado deve ficar igual ao que já foi validado em `extract_notes`. Resto do projeto: `BUILD-001` completo (empacotar Firefox+llama-server+modelo), `INSTALL-001`, `E2E-001` (máquina limpa), `PILOT-001` (piloto real) seguem como próximos marcos.

---

## 2026-08-24 (1) — Modelo de auditoria concorrente (DEC-057) + primeira validação real do LLM (DEC-058)

**Task concluída:** usuário forneceu orientação técnica formal (documento de mestrado) especificando o modelo analítico esperado do relatório. Núcleo de análise (taxonomia, SLA, prioridade, contrato do LLM, schema de banco, relatório HTML) reescrito para atender -- detalhe completo em `DECISIONS.md` DEC-057 (não repetido aqui). Resumo: `app/analysis/taxonomy.py`/`sla_config.py`/`priority.py` novos; `schemas.py`/`llm.py`/`rules.py` reescritos; `database.py`/`repository.py` expandidos com migração; `html_report.py` reformulado (censo por unidade + relatório individual). `PROJECT_SPEC.md` ganhou RF-20 a RF-30. Bug de timezone (~3h de erro em todo tempo decorrido) e bug de evidência ausente em pendência única encontrados e corrigidos durante inspeção de relatório de exemplo fictício, antes de considerar a fase pronta.

Em seguida, pedido do usuário ("Roda uma análise real com o LLM pra ver a saída") -- primeira execução real (não-stub) do novo contrato contra `LocalLLM` (Llama 3.1 8B), fixture 100% fictícia. Achou e corrigiu 2 bugs reais (detalhe em `DECISIONS.md` DEC-058):
1. `DEFAULT_TIMEOUT_SECONDS=60` insuficiente pro prompt maior do DEC-057 -- 1ª tentativa estourou o timeout mesmo com o modelo já carregado. Corrigido para 240s.
2. Saída válida trouxe `"edd_data"`/`"dia_causa"`/`"flow_status"` como a STRING literal `"null"` em vez do valor JSON `null` -- passava despercebido pela validação (`isinstance(..., str)` aceita) e renderizaria errado no relatório (`html_report.py` trataria como valor real/truthy). Corrigido com `normalize_null_sentinels()` novo em `schemas.py`, chamado em `LocalLLM.analyze_patient` antes de validar. Prompt também reforçado (defesa em profundidade).

Também observada (não corrigida -- não é bug de código, ver DEC-058) uma inconsistência de qualidade do modelo: `necessidade_hospitalar` justificada com uma nota antiga, ignorando que a evolução mais recente da mesma fixture já indicava alta com investigação ambulatorial. Fica registrada para curadoria clínica futura (RF-07).

**Timing real observado (hardware confirmado fraco, sem GPU -- ver `project_gsus_target_hardware` na memória):** carga do modelo ~258s (variação vs. os 108,7s do LLM-001 original -- mesmo hardware, presumivelmente contenção de outros processos); análise completa (incluindo 1 timeout + 1 rejeição de validação por subtipo alucinado pelo modelo, ambos consumindo uma tentativa inteira cada) ~524s até obter saída válida. Ordem de grandeza real a considerar para UX/expectativa de tempo de execução em produção, não só o caso feliz de 1 tentativa.

**Testes:** 168 passed (variação 165-172 no total por flake de display Tk já conhecido -- não regressão; 9 testes novos desta entrada, em `test_schemas.py` e `test_llm.py`).

**Resultado:** PASS, com ressalvas já registradas. `TASKS.md::MODEL-003` segue `[~]` -- validação real corrigiu bugs reais, mas ainda falta rodar o pipeline completo ("Atualizar agora" ponta a ponta, não só o script ad-hoc) e a observação de qualidade do Achado 3 reforça que saída do LLM continua sendo hipótese a revisar, não fato.

**Próxima task:** a combinar com o usuário -- candidatos: (a) rodar "Atualizar agora" ponta a ponta com LLM habilitado (fecha MODEL-003 de vez); (b) avaliar se vale ajustar o prompt para priorizar a evolução mais recente ao decidir `necessidade_hospitalar` (Achado 3 do DEC-058); (c) retomar `BUILD-001`/`INSTALL-001`/`E2E-001`/`PILOT-001`.

---

## 2026-08-24 (2) — Achado 3 do DEC-058 corrigido: prompt agora marca a evolução mais recente

**Pedido do usuário:** "Ajuste o prompt" -- resposta direta ao Achado 3 registrado na entrada anterior (o modelo tinha ignorado a evolução mais recente ao decidir `necessidade_hospitalar`).

**Implementado (`app/analysis/llm.py`):**
- `build_prompt` calcula, entre as `new_notes`, qual tem o maior `timestamp` (string ISO 8601, mesma comparação já usada em `hours_elapsed_since`) e marca essa linha com `*** EVOLUÇÃO MAIS RECENTE ***` no texto do prompt -- por valor de timestamp, nunca por posição na lista (testei explicitamente com a lista fora de ordem, já que a ordem real de extração do GSUS não é uma garantia deste módulo).
- `SYSTEM_PROMPT` ganhou regra explícita: `necessidade_hospitalar`, `current_status` e `dia_classificacao` devem refletir a evolução marcada como mais recente, que vence qualquer contradição com evoluções anteriores.

**Testes:** `tests/unit/test_llm_prompt.py` (novo, 5 testes). Suíte inteira: 173 passed, 4 skipped (mesmo flake de display Tk de sempre).

Detalhe completo da atualização em `DECISIONS.md` DEC-058 (seção "Atualização -- Achado 3 corrigido").

**Resultado:** PASS. Continua sem validação clínica formal (RF-07) -- ajuste reduz um erro estrutural específico observado, não substitui revisão humana da saída.

**Próxima task:** mesma lista da entrada anterior, menos o item (b) que acabou de ser feito -- (a) rodar "Atualizar agora" ponta a ponta pra fechar `MODEL-003`, ou (c) retomar `BUILD-001`/`INSTALL-001`/`E2E-001`/`PILOT-001`.

---

## 2026-08-24 (3) — MODEL-003 fechado: pipeline completo validado contra GSUS real + LLM real

**Pedido do usuário:** "Roda o pipeline completo ponta a ponta". Como isso envolve login real no GSUS + dado real de paciente fluindo pelo LLM, perguntei antes como ele queria (eu rodar sozinho e reportar só agregados, ou ele mesmo clicar no app) -- escolheu "eu disparo, só leio agregados".

**Achado ao preparar a execução (antes de rodar contra dado real):** o botão real "ATUALIZAR AGORA" nunca chamava o LLM -- `llm=None` fixo em `main_window.py`, apesar de `config.py` já ter os campos prontos. Ou seja, nenhuma rodada real anterior deste projeto teve análise por IA, só regras determinísticas. Corrigido antes de validar (senão a validação não provaria nada de novo): LLM agora inicia antes do GSUS (evita sessão ociosa), e falha ao iniciar o LLM degrada pra só-regras em vez de abortar a atualização inteira (era a semântica que `friendly_message` já previa mas nunca era exercitada). No processo, achei e corrigi outro bug real: `LocalLLM.start()` deixava `OSError` do `Popen` escapar cru em vez de virar `LLMStartupError` (reproduzido de verdade com os caminhos relativos default). Detalhe completo em `DECISIONS.md` DEC-059. 5 testes novos, suíte: 177 passed (variação de skip = flake de Tk conhecido).

**Execução real:** `scripts/check_full_pipeline.py` (novo) -- chama o mesmo `run_once` real da UI, mas com banco/relatório ISOLADOS (nunca os reais -- a amostra limitada a 3 pacientes marcaria pacientes reais de fora dela como inativos se usasse o banco de produção) e log inteiro redirecionado pra arquivo (nunca terminal). Rodei em background; só li a linha final de resumo agregado.

**Resultado:** sucesso completo. LLM carregou em 82,2s. GSUS real acessado, 3 pacientes reais processados -- 3 completos, 0 falha. Tempo total: 2292,7s (~38,2min) para LLM+GSUS+3 pacientes -- primeiro número real de custo ponta a ponta COM o LLM plugado. Relatório real em `validacao_pipeline_completo.html`. Detalhe completo em `DECISIONS.md` DEC-060.

**O que ficou provado e o que não ficou:** a integração técnica funciona ponta a ponta contra o ambiente real, sem quebrar. O CONTEÚDO clínico das 3 análises não foi avaliado por mim (dado real nunca chega ao meu contexto, por design) -- só o usuário, abrindo o relatório isolado, pode avaliar isso. RF-07 (validação clínica humana da taxonomia/SLA/prioridade) continua pendente, agora estendida a estas 3 análises específicas.

**Testes:** 177 passed (mesma suíte da entrada anterior + as adições do DEC-059).

**Resultado:** PASS. `TASKS.md::MODEL-003` fechado (`[x]`).

**Próxima task:** a combinar com o usuário -- ele revisar `validacao_pipeline_completo.html` (dado real, só ele deve abrir); rodar via botão de verdade contra o censo completo antes de `PILOT-001`; ou retomar `BUILD-001`/`INSTALL-001`/`E2E-001`.

---

## 2026-08-24 (4) — Bug real no relatório: filtro por `unit` nunca deveria existir (DEC-061)

**Contexto:** usuário tentou abrir o relatório real do DEC-060 e não achou o arquivo (problema à parte, resolvido servindo por HTTP local -- Explorer/Brave não enxergavam um arquivo que o PowerShell confirmava existir no disco; contornado, não investigado a fundo). Depois de conseguir abrir, o relatório mostrava **"Nenhum paciente processado com sucesso nesta execução"** nas seções de censo/individual, contradizendo o cabeçalho ("3 Processados, 0 Falhas").

**Causa raiz encontrada** (só contagens agregadas do banco isolado, nunca conteúdo): `generate_report`/`get_pending_items_for_unit`/`mark_patients_inactive_not_in` filtravam por `patients.unit = app_config.unit` ("Auditoria"). Mas confirmei lendo `app/gsus/census.py` que `get_census` **nunca usa o parâmetro `unit` pra filtrar a busca real** -- `patients.unit` é texto escavado do GSUS por paciente. Os 3 pacientes reais do DEC-060 vieram com `unit` = "4-Internados P.A." (2) e "ENF. MEDICO-CIRURGICA 2 (21 leitos)" (1) -- nenhum batia com "Auditoria". Perguntei ao usuário o que a conta GSUS representa: "Minha conta GSUS tem acesso a todas as unidades e eu devo auditar todas elas" -- confirmando que `unit` nunca deveria ter sido filtro, só rótulo de exibição.

**Corrigido:** `mark_patients_inactive_not_in` e `get_pending_items_for_unit` (renomeado `get_all_active_pending_items`) não filtram mais por `unit`. Efeito colateral real descoberto no processo: como o filtro de `mark_patients_inactive_not_in` nunca batia, **nenhum paciente jamais foi marcado inativo em toda a história do projeto**, mesmo tendo alta. Detalhe completo em `DECISIONS.md` DEC-061.

**Validação:** teste novo reproduz o bug (dois pacientes com `unit` diferente do configurado, nenhum deveria sumir do relatório) + 3 testes novos de `mark_patients_inactive_not_in` (não tinha NENHUM teste antes). Suíte: 177 passed (skip = flake de Tk conhecido; isolando os 3 arquivos tocados: 23/23). Regenerei o relatório isolado do DEC-060 a partir dos MESMOS dados reais (sem rodar GSUS/LLM de novo) -- confirmei estruturalmente que os 3 pacientes aparecem agora e a mensagem de vazio sumiu.

**Por que isso importa:** provavelmente todo relatório gerado por este projeto até agora só "parecia" funcionar pelos números do cabeçalho -- o conteúdo real (censo/individual) pode ter vindo vazio sempre que o censo tocou mais de uma unidade, e ninguém tinha percebido porque esta foi a primeira vez que um relatório de uma rodada real (com o formato do DEC-057) foi de fato aberto.

**Testes:** 177 passed (suíte inteira), 23/23 nos arquivos tocados isolados.

**Resultado:** PASS. Relatório isolado do DEC-060 já regenerado e confirmado corrigido -- não precisou rodar GSUS/LLM de novo.

**Próxima task:** usuário reabre `validacao_pipeline_completo.html` (via `http://127.0.0.1:8899/...`, servidor ainda de pé) pra revisar as 3 análises de verdade agora que aparecem.

---

## 2026-08-25 (1) — Auditoria de conformidade contra o PDF original + verificação adversarial (DEC-062/063/064)

**Pedido do usuário:** "Está de acordo com o documento?" -- comparei `html_report.py`/`taxonomy.py`/`priority.py`/`sla_config.py` seção por seção contra o PDF original. Achado principal: nunca existia reconciliação de pendências entre execuções (cada rodada duplicava a mesma pendência real), 3 campos do banco (`especialidade_responsavel`/`origem_internacao`/`model_version`) nunca eram preenchidos, e vários dados já existentes (origin, confiança, unidade) nunca apareciam no relatório. Usuário pediu: "Corrija todos os gaps em sequência, do mais importante ao menos".

**Implementado (DEC-062/063):** reconciliação de pendências via `_find_matching_pending` (categoria+subtipo pro LLM, +descrição determinística pra regras), reiteração de evidência (`pending_item_evidence`, nunca usado até agora), resolução automática, coluna `source` isolando ciclos de vida de regra vs. LLM. Relatório ganhou coluna Unidade, selo interna/externa, confiança, SLA aplicável mostrado, evidências múltiplas. `especialidade_responsavel` (derivado deterministicamente da nota mais recente), `origem_internacao` (novo campo no contrato do LLM) e `model_version` passaram a ser preenchidos de verdade.

**Verificação adversarial (ultracode ativo):** antes de reportar como concluído, rodei um workflow com 3 revisores independentes (conformidade/corretude/qualidade de teste) + re-verificação cética de cada achado. Resultado: 21 problemas, todos confirmados. Os 3 mais graves eram REGRESSÕES na própria reconciliação que acabei de construir: (1) resolver pendência de regra "por ausência" era inseguro, porque o GSUS real só reenvia uma janela de dias, não o histórico completo -- corrigido pra só resolver quando solicitação+conclusão aparecem na MESMA janela; (2) casar por categoria+subtipo colapsava duas pendências de regra concorrentes e diferentes (tomografia + ultrassom) na mesma, perdendo a segunda -- corrigido casando por descrição também; (3) o mesmo casamento quebrava a progressão de estágio de uma interconsulta real (SOLICITADA -> CONDUTA_DEFINIDA), perdendo a evidência original -- corrigido tratando essas categorias como "mesma pendência, estágio evoluindo". Achei também 2 bugs PRÉ-EXISTENTES reais em RULES-001: "aguardando laudo"/"aguarda parecer" eram tratados como conclusão por engano (citam a palavra, significam o oposto).

**Detalhe completo:** `DECISIONS.md` DEC-062 (reconciliação), DEC-063 (especialidade/origem/model_version), DEC-064 (verificação adversarial, lista completa dos 21 achados + o que foi deliberadamente NÃO corrigido: SLA por subtipo, pseudonimização no relatório -- decisão de produto já deliberada, correção/override do auditor, trilha histórica do patient_state).

**Testes:** suíte cresceu de 177 (início do dia) para 222 passed (4 skip = flake de Tk conhecido). Novo arquivo `tests/unit/test_orchestrator.py`.

**Resultado:** PASS.

**Próxima task:** a combinar com o usuário -- itens deliberadamente adiados (SLA por subtipo de exame, pseudonimização do relatório, correção/override do auditor) ficam pra decisão futura, não são bloqueadores. Resto do projeto segue: `BUILD-001`/`INSTALL-001`/`E2E-001`/`PILOT-001`.

---

## 2026-08-25 (2) — Validação com amostra real aleatória expôs limite de hardware; 3 mudanças de arquitetura decididas com o usuário (DEC-066/067/068)

**Contexto:** usuário pediu "rode mais 3 casos aleatórios" pra confirmar que as mudanças do DEC-062/063/064 realmente apareciam no relatório. `scripts/check_full_pipeline.py` passou a sortear a amostra de verdade (antes sempre pegava os N primeiros do censo). Resultado real: 3/3 sem falha técnica, mas 0/3 com análise por IA salva -- pacientes sorteados tinham ~43 evoluções (vs. ~5-6 dos casos testados antes), e o LLM estourou tempo (mesmo em 480s, subido no DEC-065) e, num caso, deu HTTP 400 imediato -- achado: `llama-server` nunca é iniciado com `--ctx-size` explícito, provável estouro de contexto.

**Discussão com o usuário sobre 3 frentes, resultando em 3 decisões:**

1. **DEC-066** -- nunca mandar mais que as últimas 2 semanas de evolução pro LLM (`_limit_to_recent_window`, `LLM_LOOKBACK_DAYS=14`). RULES-001 continua vendo o histórico completo. Relatório mostra aviso visível quando isso acontece (`analysis_window_limited`), recomendando consultar o GSUS diretamente pro histórico mais antigo.
2. **DEC-067** -- "Localizar Paciente" totalmente reformulado: parou de navegar ao vivo no GSUS (`get_full_admission_history_text`/`lookup_full_history` ficam no código, FROZEN, só não são mais chamados pela UI) e passou a servir o relatório individual já processado, direto do banco local (`generate_patient_report`). Só funciona pra paciente ainda internado -- alta ou prontuário desconhecido viram mensagem direta. Ficou mais simples (busca síncrona, sem thread/fila/credencial GSUS).
3. **DEC-068** -- `RETENTION-001` (P1, nunca implementado) finalmente feito: texto bruto de evolução de paciente com alta há mais de `raw_notes_retention_days` (default 90) é apagado a cada execução. Resumo estruturado (patient_state/pending_items/evidência) nunca é apagado -- preserva a base pra validação auditor×IA da seção 17 da orientação técnica, que uma purga total destruiria.

**Testes:** ~22 testes novos entre as 3 mudanças (`test_orchestrator.py`, `test_html_report.py`, `test_repository.py`, `test_lookup_window.py` reescrito, `test_pipeline.py`). Suíte: 243 passed, 5 skip (flake de Tk conhecido, confirmado por reordenação de teste que o efeito é de POSIÇÃO -- primeiro `tk.Tk()` depois do guard do módulo -- não do código novo).

**Resultado:** PASS.

**Próxima task:** rodar a validação real de novo (script já teria os limites certos agora) pra confirmar que os 3 achados de hoje resolvem o problema de admissão longa; considerar UI de configuração pra `raw_notes_retention_days` se o usuário quiser ajustar sem editar `config.json` na mão.

---

## 2026-08-25 (3) — DEC-069 (caminho relativo do modelo/llama-server) + regressão real encontrada e corrigida na mesma sessão

**Pedido do usuário:** enquanto a validação real (pedido "rode com casos que de fato estão internados", `MAX_PATIENTS` 3->5) rodava em background, usuário escolheu adiantar `BUILD-001`: corrigir o caminho relativo de `model_path`/`llm_server_path` (achado do DEC-059), que até agora era resolvido implicitamente contra `Path.cwd()` -- funciona em dev, quebra em `.exe` empacotado/Task Scheduler.

**Implementado:** `config.get_app_root()`/`resolve_app_path()` (DEC-069), usados por `main_window.py` e `check_full_pipeline.py`. Suíte completa rodada logo em seguida: 1 falha nova, `test_update_flow_success_updates_status_and_writes_report`.

**Regressão real encontrada e corrigida:** o teste (e outro semelhante, `test_update_flow_maps_not_implemented_error_to_friendly_message`) nunca faziam `monkeypatch` de `LocalLLM` -- dependiam, sem saber, de `model_path`/`llm_server_path` default não resolverem pra um arquivo de verdade. Como este ambiente de dev TEM modelo/`llama-server` reais (DEC-007, usados nas validações desta sessão), `resolve_app_path` passou a resolvê-los certinho -- e um `LocalLLM` de verdade tentou subir dentro de um teste que só usa fakes, travando o teste (suíte foi de ~32s pra 48.61s). Corrigido dando aos dois testes um `AppConfig` com caminhos explicitamente inexistentes, em vez de depender do default "por acaso" não resolver. Suíte: 247 passed, 5 skip, ~32s.

**Validação em paralelo -- resultado final:** `check_full_pipeline.py` com 5 pacientes sorteados. Agregado (`RESUMO_JSON`, só contagem, sem PHI): `found=5, completed=4, failed=0, no_admission=1`, `elapsed_seconds_total=4175.2` (~70min). Nenhuma falha técnica (0 crash, censo/prontuário/relatório funcionaram nos 5). Detalhe por paciente (via log estruturado, sem PHI):
- P1 (9 dias/21915 caract.): análise por IA concluída com sucesso.
- P2: sem internação atual -- alta recente, detectado corretamente (`no_admission`).
- P3 (9 dias/50607 caract.): `dia_classificacao=VERMELHO` sem `dia_causa` preenchida em 3/3 tentativas -- `LLM_ANALYSIS_ERROR`, estado anterior preservado.
- P4 (2 dias/22297 caract., já dentro da janela de 2 semanas do DEC-066): timeout, timeout, depois mesma falha de validação do P3 na 3ª tentativa -- `LLM_ANALYSIS_ERROR`.
- P5 (3 dias/31180 caract.): timeout nas 3 tentativas -- `LLM_ANALYSIS_ERROR`.

**Achado importante:** só 1 dos 4 pacientes internados (P1) teve análise por IA de fato concluída nesta rodada -- os outros 3 caíram no caminho seguro (regras determinísticas preservadas, nada inventado), mas por dois motivos diferentes: (a) timeout mesmo em janelas pequenas (2-3 dias, bem abaixo do limite de 2 semanas do DEC-066) -- sugere que o custo dominante não é o tamanho do prompt novo, e sim algo fixo por requisição (ex.: llama.cpp reprocessando todo o contexto do zero a cada chamada, sem cache de prompt entre requisições); (b) o modelo não segue de forma confiável a regra condicional "VERMELHO exige causa" (achado do DEC-065/schemas.py, não é bug de código). O desenho fail-closed está funcionando exatamente como deveria (nunca inventa, nunca corrompe dado) -- mas a cobertura real de análise por IA neste hardware ficou em 25% dos pacientes internados nesta amostra, mesmo após as 3 mitigações do DEC-066/067/068. Decisão sobre como melhorar isso (reduzir mais a janela, investigar flags do `llama-server` como cache de prompt/`--ctx-size`, ou aceitar como limite conhecido) fica em aberto para o usuário.

**Testes:** `test_config.py` +4, `test_main_window_update.py` +1 assert (DEC-069) e 2 testes corrigidos (regressão).

**Resultado:** PASS (DEC-069 + regressão + robustez do pipeline). Análise por IA: cobertura parcial (1/4 internados), sem dado inventado ou corrompido nos demais.

**Próxima task:** decidir com o usuário se investiga a causa do timeout persistente (mesmo com janela pequena) antes de considerar `LLM-001`/o fluxo de análise por IA como confiável em produção.

---

## 2026-08-25 (4) — Investigação da causa raiz do timeout (DEC-070): hardware, não configuração

**Pedido do usuário:** "Investigue a causa do timeout antes de decidir."

**Investigação (só texto fictício, isolado do banco/relatório de produção):** subi uma instância de teste do `llama-server` e rodei um benchmark com prompts do mesmo tamanho dos que falharam na validação real. Achado decisivo: um prompt pequeno (2464 tokens) levou 393,7s pra gerar 449 tokens -- **~1,1 token/segundo**, 4-6x mais lento que a suposição usada pra calibrar os timeouts anteriores (DEC-007). CPU real: **Intel i5-1235U** (chip de notebook 15W, híbrido 2P+8E). Testei se limitar a threads só aos núcleos de performance ajudaria -- piorou (hipótese refutada por teste direto). Contexto (`n_ctx_slot=106496`) descartado como causa (DEC-065/066 assumiam estouro de contexto, não é isso).

**Conclusão:** não é bug de configuração -- é hardware genuinamente fraco demais pro volume de tokens que o contrato de saída (JSON completo do RF-20 a RF-28) exige gerar. Achado secundário (ainda não corrigido): `_call_completion` nunca envia `max_tokens`, geração sem teto.

**Resultado:** causa raiz identificada com evidência direta. Nenhuma mudança de código ainda -- decisão de caminho (modelo menor, processamento assíncrono/noturno, ou aceitar cobertura parcial) depende do usuário.

**Próxima task:** decidir com o usuário o caminho a seguir para o LLM-001 dado esse limite real de hardware.

---

## 2026-08-25 (5) — Decisão e implementação (DEC-071): análise por IA em duas fases

**Pedido do usuário:** perguntei 4 opções pra seguir depois do DEC-070; usuário respondeu que o PC de teste representa a maioria dos hospitais (fraco), mas alguns terão hardware bem melhor -- quer uma solução que funcione bem nos dois casos sem abrir mão do documento original.

**Implementado:** `run_once` separado em 2 fases -- regras+coleta (rápida, sempre síncrona, relatório já disponível ao final) e análise por IA (lenta, roda depois, relatório regenerado a cada paciente conforme fica pronto). Timeout por chamada de LLM subiu de 480s pra 1800s (seguro porque não bloqueia mais quem clicou "Atualizar agora"). `max_tokens` finalmente limitado a 1200 (achado secundário do DEC-070). Throughput agora escala naturalmente com o hardware -- sem detectar/ramificar por tipo de máquina.

**Achado real, não relacionado ao LLM:** rodar a suíte completa (nunca isolada) revelou que `test_lookup_window.py::test_lookup_requires_record_number_before_searching` levava até 19 minutos -- bug pré-existente (diálogo nativo `messagebox.showerror` nunca mockado, ficava esperando clique que nunca vem). Corrigido. Suíte completa: de até 55min de volta pra ~14s.

**Testes:** `test_orchestrator.py` (rename + 2 testes ajustados), `test_lookup_window.py` (1 teste corrigido). 247 passed, 5 skip.

**Resultado:** PASS. LLM-003 fechado no TASKS.md.

**Próxima task:** nenhuma pendência imediata desta frente. Indicador de "última análise por IA"/"pendente" no relatório fica como melhoria futura, não bloqueadora (ver DEC-071).

---

## 2026-08-25 (6) — Validação real com 2 pacientes confirma DEC-071; início do BUILD-001 (DEC-072)

**Pedido do usuário:** "Rode com 2 pacientes internados para verificar o funcionamento. Se funcionar perfeitamente, vamos para as próximas etapas visando a conclusão do projeto."

**Validação:** `check_full_pipeline.py` com `MAX_PATIENTS=2` contra GSUS+LLM reais. Resultado: `found=2, completed=2, failed=0, no_admission=0` -- os 2 eram pacientes de verdade internados. Ordem de progresso confirmou a Fase 1 (regras, ambos) → relatório gerado → Fase 2 (IA, só 1 precisou) → IA concluída sem erro, exatamente o desenho do DEC-071. 11,8min total (vs ~70min pra 5 pacientes antes do DEC-071).

**Próximas etapas (BUILD-001):** com o pipeline validado, avancei pro próximo P0. Perguntei ao usuário sobre empacotar o modelo GGUF (~4,92GB) no instalador -- resposta: baixar sozinho na 1ª execução. Implementado (`app/analysis/model_downloader.py`, DEC-072) reaproveitando o mecanismo de progresso assíncrono do DEC-071. `runtime/` (llama-server.exe) incluído no spec do PyInstaller.

**Achado real, ainda pendente:** Firefox (Playwright, DEC-010) fica no cache global do sistema, fora do que o instalador empacota -- quebraria numa máquina limpa (E2E-001). Decisão de como resolver (redirecionar path do Playwright vs. baixar sob demanda) ainda não tomada com o usuário.

**Testes:** +5 novos (`test_model_downloader.py`), suíte 252 passed, 5 skip.

**Resultado:** PASS (validação + downloader). BUILD-001 parcialmente avançado, Firefox/Chromium seguem em aberto.

**Próxima task:** decidir com o usuário o caminho do Firefox/Playwright pra fechar BUILD-001, depois seguir pra INSTALL-001/E2E-001/PILOT-001.

---

## 2026-08-25 (7) — Firefox empacotado, BUILD-001 fechado (DEC-073)

**Pedido do usuário:** escolheu redirecionar o Playwright pra pasta do projeto (em vez de baixar sob demanda) -- prioriza o app funcionar offline desde a 1ª execução.

**Implementado:** `configure_playwright_browsers_path()` redireciona Playwright pra `playwright-browsers/` dentro do projeto; Firefox reinstalado lá (validado real, headless, sem credencial -- login carregou normalmente); spec do PyInstaller empacota via `Tree()`.

**Build real testado (não só o spec lido):** rodei o PyInstaller de verdade 3 vezes, encontrando e corrigindo 2 problemas reais que só apareceriam tentando de fato: (1) PyInstaller 6.x usa `_internal/` por padrão, incompatível com o layout plano que `get_app_root()` sempre assumiu -- `contents_directory='.'` no `EXE()` (não no `COLLECT()`, 1ª tentativa errada) resolve; (2) caminho do script resolvia contra a pasta do `.spec`, não de quem chama -- corrigido com caminho absoluto. `.exe` final gerado, iniciado, ficou de pé, encerrado manualmente sem erro.

**Regressão real pega pela suíte:** `configure_playwright_browsers_path()` em nível de módulo em `main.py` vazava pra `os.environ` só por `test_app_shell.py` importar o módulo (sem chamar `main()`) -- quebrou `test_census_parser.py` (Chromium do cache global, agora "sumido" do ponto de vista do Playwright). Corrigido movendo pra dentro de `main()`.

**Testes:** nenhum novo (infraestrutura de empacotamento). Suíte: 252 passed, 5 skip.

**Resultado:** PASS. `BUILD-001` fechado no TASKS.md.

**Próxima task:** `INSTALL-001` (instalador Inno Setup/NSIS + registro da tarefa agendada) é o próximo P0. Observação registrada, não bloqueadora: `SCHEDULE-001` hoje só abre a janela do app no horário agendado -- não dispara "Atualizar agora" sozinho; decidir se isso precisa de um modo automático/linha de comando antes do piloto (`PILOT-001`).

---

## 2026-08-26 (1) — INSTALL-001 (Inno Setup) e correção real do SCHEDULE-001

**Contexto:** usuário pediu pra começar o INSTALL-001, depois foi dormir autorizando continuar "tudo que não precisa da minha intervenção".

**INSTALL-001 (DEC-074):** usuário escolheu Inno Setup, autorizou baixar e instalar o compilador (jrsoftware.org). Instalação POR USUÁRIO (`{localappdata}\Programs\...`, sem exigir admin) -- decisão minha: o modelo baixa em runtime pra dentro da pasta de instalação (precisa ser gravável sem admin) e a equipe de auditoria hospitalar real frequentemente não tem direito de admin na máquina de trabalho. Build real testado: instalado (`/VERYSILENT`), app abriu sem erro, desinstalado limpo depois.

**Achado real ao revisar o fluxo completo antes de fechar SCHEDULE-001 (DEC-075):** `register_daily_task` existia e era testado desde SCHEDULE-001, mas nada no app chamava -- nenhuma tarefa jamais seria criada de verdade, mesmo com o instalador pronto. Além disso, mesmo criada manualmente, a tarefa só abriria a janela do app -- sem humano pra clicar "Atualizar agora" de madrugada, nada aconteceria. Corrigido: `SetupWindow` agora registra a tarefa a cada configuração salva (com uma flag `--auto-update`); a lógica de atualização foi extraída pra `app/update_flow.py` (única fonte de verdade, usada pelo clique manual e pelo modo automático); `app/main.py` despacha pro modo automático quando chamado com essa flag, sem nunca abrir a GUI. Não criei uma tarefa real de novo (exigiria autorização explícita do usuário outra vez) -- a lógica de despacho é só Python, coberta por teste com mock que falha se a GUI for aberta por engano.

**Testes:** +7 novos (`test_app_shell.py`). Suíte: 259 passed, 5 skip (flake de Tk já documentado, confirmado não-determinístico com 3 reruns limpos).

**Resultado:** PASS. `INSTALL-001` e `SCHEDULE-001` (de fato ligado ao app) fechados no TASKS.md.

**Próxima task:** `E2E-001` (teste em máquina limpa) e `PILOT-001` (piloto em produção controlada) são os últimos P0 -- ambos exigem uma máquina/usuário real que eu não tenho como prover sozinho; ficam pra quando o usuário quiser avançar. Clinical sign-off (RF-07) segue como gate antes de produção real, não uma task de desenvolvimento.

---

## 2026-08-26 (2) — E2E-001 em andamento numa máquina real; achado crítico corrigido (DEC-077)

**Contexto:** usuário testou o instalador numa máquina genuinamente limpa (fora do meu alcance direto -- diagnóstico feito por chat, pedindo pra ele checar coisas específicas e me mandar prints/logs, sem nunca pedir dado de paciente).

**Achado crítico, corrigido:** login GSUS travava 100% das vezes em modo headless nessa máquina real (GSUS/WAF bloqueia navegador automatizado invisível) -- o mesmo Firefox aberto manualmente (sem automação) funcionava perfeitamente. Diagnosticado por eliminação (Firefox presente e íntegro, `schtasks` "Acesso negado" era achado separado e não-bloqueador -- conta gerenciada/escolar, já tratado graciosamente pelo DEC-075, login manual funcionou de primeira). Corrigido com `headless=False` permanente (DEC-077) -- mandei um `.exe` de diagnóstico (~3,3MB, só o arquivo que mudou) pro usuário testar antes de virar definitivo. Confirmado: login, censo e extração funcionando de verdade contra o GSUS real, 145 pacientes encontrados.

**Investigado e descartado:** sugestão do médico idealizador do projeto de filtrar por setor (UTI, UTI Neonatal etc.) pra acelerar a carga. Achei o campo real no GSUS ("Unidade Organizacional" na Pesquisa Avançada da tela de censo) e cheguei a mapear as 12 unidades com paciente internado hoje -- mas o usuário confirmou que TODAS as 12 entram no escopo da auditoria, o que equivale ao hospital inteiro sem filtro nenhum. Concluí (e expliquei) que filtrar não ajudaria em nada nesse caso -- não implementado, evitando complexidade sem ganho real.

**Achado real, não corrigido (baixa prioridade):** o texto de status da tela principal ficou visivelmente defasado em relação ao progresso real (mostrava "11 de 145" enquanto o relatório já refletia ~45 processados) -- suspeita de a janela visível do Firefox (DEC-077) atrapalhar a responsividade do Tkinter. Dado em si correto, só o contador que não é confiável pra acompanhar ao vivo. Registrado em `TASKS.md::UI-001`, não investigado a fundo ainda.

**Situação real do hospital:** a conta de teste tem 145 pacientes internados no total (bem mais que qualquer amostra testada antes). Expliquei ao usuário que essa 1ª execução (banco vazio) é o pior caso possível -- trata todo mundo como novo, diferente do uso diário incremental (só reprocessa o que mudou). Recomendei deixar essa carga inicial terminar sozinha em segundo plano, sem acompanhar ao vivo.

**Testes:** nenhum novo desde o DEC-077 (mudança não coberta por teste automatizado -- só validável contra o GSUS real).

**Resultado:** PARCIAL. Bloqueio crítico do E2E-001 (login) resolvido e confirmado em produção real. Passos 5-8 do checklist (relatório, Localizar Paciente, Configurações, desinstalação) ainda pendentes -- bloqueados atrás da carga inicial de 145 pacientes ainda rodando na máquina do usuário.

**Próxima task:** aguardar a carga inicial terminar e o usuário completar os passos 5-8 do `E2E-001-CHECKLIST.md` pra fechar `E2E-001` de vez. Considerar investigar o desencontro do status (achado acima) e, separadamente, se vale a pena um mecanismo de "primeira carga só com regras, IA entra depois aos poucos" pra suavizar esse cenário de pior caso -- não decidido ainda, mencionado ao usuário mas não aprofundado.

---

## 2026-08-27 (1) — Retomada em máquina nova; Fase 2 parada por interrupção externa; bug real de REPORT-001 (DEC-078)

**Contexto:** sessão nova, nesta máquina nova (troca de computador do usuário), retomando exatamente de onde a entrada anterior parou. Segui o handoff: chequei o log da aplicação (só linhas estruturais, nunca dado de paciente) antes de qualquer outra coisa.

**Achado 1 -- a carga inicial não terminou sozinha:** o log parava às 19:01 do dia 26/08 (Fase 2/IA no paciente 13 de 145), sem nenhum processo do app rodando às 10:17 do dia 27 -- confirmado com o usuário: fechou o app / PC dormiu ou desligou (interrupção externa esperada, não bug). Da Fase 2, 8 de 13 tentativas tinham concluído com sucesso antes da interrupção. No ritmo observado (~29min/paciente em média, muitos batendo o teto de 90min), terminar os 145 levaria ~3 dias corridos -- número concreto novo, consistente com o hardware fraco já confirmado (DEC-070/071).

**Achado 2 -- decisão do usuário:** seguir com os passos 5-8 do `E2E-001-CHECKLIST.md` já com o relatório disponível da Fase 1 (não esperar a Fase 2 terminar, que pode levar dias).

**Achado 3 -- bug real em REPORT-001, passo 5 do checklist (DEC-078):** usuário reportou relatório incompleto ("falta de pacientes"). Investigação só em código (nunca abri o relatório real nem rodei query de conteúdo) achou a causa: `generate_report` só listava paciente com pendência ativa OU `patient_state` gravado -- um paciente processado com sucesso pela Fase 1 (regras) sem achado, ainda não alcançado pela Fase 2, ficava invisível no relatório inteiro. Confirmado contra o banco real (só contagens agregadas): 177 pacientes ativos, só 8 com `patient_state`, 39 com pendência -- a união (~40-45) explica tanto o sintoma relatado quanto, provavelmente em parte, o "desencontro" de status já registrado em `TASKS.md::UI-001` no dia anterior. Corrigido com `Repository.get_all_active_patients()` como nova fonte da verdade de quem entra no relatório. 1 teste novo de regressão. Relatório real (`relatorio.html`, banco de produção) já regenerado com a correção -- mas o `.exe` empacotado ainda rodando nesta sessão foi compilado ANTES do fix, então volta a gerar relatório com o bug na próxima regeneração automática dele, até um rebuild+reinstall.

**Achado de ambiente (não é bug do produto):** `.venv` desta máquina nova apontava pro Python da máquina antiga (inexistente aqui) -- corrigido reinstalando Python 3.13 (autorizado) e ajustando `pyvenv.cfg`. Chromium do Playwright também precisou ser rebaixado (autorizado) só pra rodar `test_census_parser.py` (infra de teste local, nunca usada pelo `.exe` real).

**Testes:** +1 novo (`test_html_report.py`). Suíte nesta máquina: 255 passed + 10 erros de infraestrutura (Chromium com erro `spawn UNKNOWN` nesta máquina, não investigado a pedido do usuário -- sem relação com o bug corrigido). Contagem esperada sem esse problema de ambiente: 265 (264 do DEC-077 + 1 novo).

**Resultado:** PASS (bug do relatório corrigido e validado; carga real ainda incompleta, decisão consciente do usuário de não esperar).

**Rebuild + reinstall (mesmo dia, decisão do usuário: aplicar agora):** app real encerrado (interrompeu a Fase 2 -- perda mínima, nada inconsistente, ver DEC-078), recompilado via PyInstaller + Inno Setup e reinstalado (`/VERYSILENT`) nesta mesma máquina. Binário confirmado com o fix embutido (timestamp do build novo, Firefox/llama-server presentes). Precisou preparar Inno Setup do zero nesta máquina nova (autorizado, `winget`).

**Testes:** 265 esperados (264 do DEC-077 + 1 novo) -- 255 rodaram verde nesta máquina, 10 (`test_census_parser.py`) com erro de infraestrutura de Playwright/Chromium não investigado (pedido do usuário), sem relação com o bug corrigido.

**Ressalva real sobre a retomada (não corrigida, só percebida ao documentar):** a Fase 2 não tem "onde parou" persistido -- `llm_tasks` é reconstruída a cada execução só a partir de nota NOVA desde a última extração (RF-08). Como a Fase 1 já extraiu (e gravou em `notes`) TODOS os dias de TODOS os 177 pacientes na carga de ontem, um paciente sem pendência/`patient_state` só volta a entrar na fila da IA na próxima "Atualizar agora" se tiver evolução GENUINAMENTE nova desde então -- pra internação real ativa isso deve valer pra quase todos em menos de 24h (evolução diária é rotina hospitalar), mas não é garantido pra 100%. Vale conferir depois da próxima execução se sobrou algum paciente preso em "não avaliada ainda" por falta de nota nova -- se sim, é o mesmo tema já mencionado no handoff anterior ("primeira carga só com regras, IA entra aos poucos depois"), agora com um ângulo mais concreto.

**Próxima task:** usuário reabre o app e clica "Atualizar agora" pra dar sequência à análise por IA dos pacientes restantes. Depois, continuar os passos 6-8 do `E2E-001-CHECKLIST.md` (Localizar Paciente, Configurações, Desinstalação).

---

## 2026-08-27 (2) — Passos 6 e 7 do checklist validados; achado real na tarefa agendada (DEC-079); 2º incidente PHI leve

**Passo 6 (Localizar Paciente):** validado pelo usuário -- funcionando corretamente (internado, alta, prontuário inexistente). Incidente PHI leve no meio do caminho: usuário mandou print com prontuário real visível no campo de busca -- sinalizado, não reproduzido (ver DEC-078, seção final).

**Passo 7 (Configurações):** usuário mudou o horário (00:01 -> 00:02), app avisou "Acesso negado" ao tentar reagendar -- investiguei e isolei um achado real específico desta máquina (DEC-079): a tarefa `GSUSAuditoria_AtualizacaoDiaria`, criada com sucesso na config inicial, passou a rejeitar QUALQUER escrita (`/Create /F` e `/Delete /F`) mesmo pelo próprio dono sem elevação -- tarefas novas funcionam normalmente. Suspeita: antivírus/EDR da máquina protegendo a tarefa depois de criada. Não é bug de código (`register_daily_task` já correto). 2º incidente PHI leve no mesmo print: CPF real de login GSUS visível -- sinalizado, não reproduzido.

**Testes:** nenhum novo (achado de ambiente/SO, não reproduzível em teste automatizado).

**Resultado:** PASS nos dois passos (comportamento aceitável e já previsto pelo checklist), com um achado real documentado pra referência futura (PILOT-001).

**Próxima task:** passo 8 (Desinstalação) do checklist -- lembrar que vai derrubar a Fase 2 em andamento (ver decisão do usuário de deixar rodando) e que a tarefa agendada provavelmente ficará órfã (DEC-079). Considerar adiar o passo 8 até a Fase 2 terminar.

---

## 2026-08-27 (3) — Bug grave real pego pela verificação periódica: censo parcial marcava pacientes reais como inativos (DEC-080)

**Contexto:** usuário rodou "Atualizar agora" pra deixar em segundo plano ("entregar o banco atualizado para o produto final") e pediu verificação a cada 30min. Na 1ª verificação de fato (2ª rodada do cron), o log mostrou a execução processando "paciente 24 de 60 (regras)" -- número bem menor que o censo real (~177+).

**Achado grave, confirmado por contagem agregada (nunca conteúdo):** a paginação do censo tinha desistido logo no início (2 tentativas de clicar "Próxima" falharam -- GSUS lento nesse momento, fallback já conhecido do DEC-024), coletando só 60 pacientes. O `orchestrator.run_once` tratou isso como censo completo e marcou os outros 134 pacientes reais (ainda internados) como inativos/alta -- confirmado: `active=1: 60`, `active=0: 134`. Bug real, grave, nunca visto antes porque as falhas de paginação anteriores (DEC-024) sempre aconteciam depois de boa parte do censo já coletado, nunca logo no início.

**Ação:** com autorização do usuário, execução interrompida imediatamente (`Stop-Process`, incluindo `llama-server` órfão). Corrigido `app/gsus/census.py` (nova `GSUSCensusIncompleteError`, carrega o parcial) e `app/orchestrator.py` (`run_once` pula `mark_patients_inactive_not_in` quando o censo vem incompleto, processa o parcial normalmente). 2 testes novos reproduzindo o cenário exato. Recompilado e reinstalado (2º rebuild+reinstall do dia).

**Testes:** 257 passed (255 + 2 novos), mesmos 10 erros de infraestrutura de Chromium já conhecidos (não relacionados).

**Resultado:** PASS. Bug corrigido e validado antes de mais dado ficar incorreto. Os 134 pacientes marcados incorretamente como inativos AINDA estão assim no banco -- decisão deliberada de deixar a próxima execução bem-sucedida (censo completo) reativá-los sozinha (`upsert_patient`), em vez de eu tentar adivinhar manualmente quem realmente teve alta nesse meio-tempo.

**Próxima task:** usuário roda "Atualizar agora" de novo. Se o censo vier completo desta vez, os 134 pacientes voltam a `active=1` sozinhos e o banco fica correto. Se a paginação falhar de novo, agora é seguro -- só não marca ninguém como inativo indevidamente, e o achado fica visível no log/mensagem de progresso. Depois, retomar o passo 8 do checklist (Desinstalação) quando a Fase 2 terminar.

---

## 2026-08-27 (4) — Execução real completa, DEC-080 validado de ponta a ponta

**Acompanhamento:** usuário pediu acompanhamento em tempo real (via Monitor no log) até a Fase 2 começar, depois verificação a cada 30min (cron). Censo desta vez veio **completo -- 183 pacientes** (paginação não travou). Confirmado por contagem agregada logo após "Processando paciente 1 de 183": `active=1: 183`, `active=0: 35` -- os 134 pacientes indevidamente marcados inativos no incidente do DEC-080 voltaram a `active=1` sozinhos, exatamente como projetado (reativação via `upsert_patient`, sem eu precisar corrigir nada manualmente no banco).

**Fase 1 (regras):** concluída às 13:59:04, `status=COMPLETED`. 1 falha isolada (timeout ao clicar "Atendimento" pra abrir um prontuário específico -- RF-12, não derrubou o lote).

**Fase 2 (IA):** só 1 paciente precisou de análise (RF-08, incrementalidade -- os outros 182 já tinham notas vistas em execuções anteriores). Concluída às 14:03:24 sem erro. GSUS/`llama-server` encerrados de forma limpa, sem processo órfão.

**Estado final do banco (agregado, sem PHI):** `active=1: 183`, `active=0: 35`, `patient_state: 9`, `pendência ativa: 40`. Cron de verificação (30min) cancelado -- execução terminou, nada mais a monitorar.

**Resultado:** PASS completo. `DEC-080` (censo parcial) e `DEC-078` (relatório omitindo pacientes) ambos validados contra execução real, não só teste automatizado. Banco de produção agora correto e atualizado.

**Próxima task:** retomar o passo 8 do `E2E-001-CHECKLIST.md` (Desinstalação) -- Fase 2 já terminou, não há mais execução em segundo plano pra proteger. Depois disso, `E2E-001` fecha por completo.

---

## 2026-08-27 (5) — 2º bug grave real na mesma execução: retry protegia a parte errada (DEC-081)

**Contexto:** usuário perguntou "as próximas execuções já serão mais rápidas?" mostrando o status final da UI: "Atualizado — 2/183 pacientes (178 falha(s))" -- 97% de falha técnica, contradizendo meu relato anterior (eu tinha visto só as últimas linhas do log em cada checagem de 30min e não percebi que o mesmo erro se repetia o tempo todo).

**Achado:** todas as 178 falhas tinham a mesma assinatura -- timeout ao clicar no menu "Atendimento" pra abrir o prontuário. Lendo `app/gsus/records.py::open_current_admission`, achei a causa: o retry (`SEARCH_RETRY_ATTEMPTS=3`, já existia) só protegia a espera pelo RESULTADO da busca -- o clique nos itens de menu (widget instável, mesmo padrão do DEC-012) ficava fora do `try/except`, escapando sem nunca consumir uma tentativa. Corrigido com `_click_menu_to_search_screen()` dentro do loop.

**Testes:** +4 novos (`test_records_menu_retry.py`). Suíte: 261 passed (257 + 4), mesmos 10 erros de Chromium desta máquina (não relacionados).

**Resultado:** PASS. 3º rebuild+reinstall do dia -- justificado pela gravidade (97% de falha na função mais usada do pipeline, afetando diretamente a atualidade dos dados de quase todo mundo no banco).

**Resposta à pergunta original do usuário:** Fase 2 (IA) continua rápida em execuções incrementais (só quem tem nota nova entra -- hoje foi 1 de 183). Fase 1 (regras) NÃO fica mais rápida por si só -- ela sempre revisita todo paciente ativo pra checar nota nova, então o tempo escala com o tamanho do censo, não com quanto já foi processado antes. O que deve melhorar de verdade com este fix é a TAXA DE SUCESSO de cada tentativa (menos gente falhando à toa por causa desse clique instável), não necessariamente a duração total.

**Próxima task:** usuário roda "Atualizar agora" mais uma vez pra validar que a taxa de sucesso melhora de verdade contra o GSUS real. Depois, retomar o passo 8 do checklist (Desinstalação) pra fechar `E2E-001`.

---

## 2026-08-27 (6) — Validação do DEC-081 revela 3º bug real, de segurança (DEC-082)

**Contexto:** usuário rodou "Atualizar agora" de novo pra validar o fix do menu (DEC-081). Acompanhamento em tempo real (Monitor) mostrou o retry funcionando (avisos "tentativa N/3" aparecendo), mas o MESMO padrão de cascata de falhas do DEC-080/081 reapareceu -- agora no paciente 39 (depois 67 numa 2ª tentativa), com sintoma novo: "Locator.click: Frame was detached" durante expansão do accordion de episódio.

**Achado grave, de segurança:** essa exceção (`PlaywrightError`, não `TimeoutError`) escapava do `except PlaywrightTimeoutError` em 4 pontos de `records.py`, e o `str()` dela inclui o HTML do elemento resolvido -- incluindo um atributo `onmousedown` com identificador vinculado a paciente/episódio. Isso chegou ao meu terminal e já estava persistido em `app.log`, violando a premissa central do projeto (log de aplicação nunca carrega nada vinculado a paciente). Sinalizado ao usuário sem reproduzir o valor. Corrigido ampliando os 4 pontos pra capturar `PlaywrightError` (classe-base de `TimeoutError`) em vez de só `TimeoutError`.

**Limite honesto:** o fix impede o vazamento e torna a expansão mais resiliente a esse tipo de falha pontual, mas NÃO explica com certeza por que a sessão parece ficar presa pro resto do lote depois do evento -- `get_content_frame()` já re-resolve o frame do zero a cada paciente, então a persistência do problema é intrigante. Reiniciar o app continua sendo a mitigação (usada 2x hoje). Investigação mais profunda fica pra quando o usuário puder observar o GSUS real ao vivo no momento da falha.

**Testes:** +1 novo (`test_expand_all_survives_frame_detached_instead_of_escaping`). Suíte: 262 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS. 4º rebuild+reinstall do dia -- justificado por ser achado de segurança (vazamento de identificador de paciente pro log).

**Próxima task:** usuário roda "Atualizar agora" mais uma vez. Se o mesmo travamento aparecer de novo (mesmo sem vazar nada agora), a causa raiz da "sessão presa" precisa de investigação com o usuário observando o GSUS real ao vivo -- não dá pra diagnosticar só por log daqui em diante.

---

## 2026-08-27 (7) — Causa raiz de verdade: sessão do GSUS expira em execuções longas (DEC-083)

**A pista decisiva:** usuário mandou print real da JANELA DO FIREFOX (não do app) no momento exato da cascata de falhas -- mostrava "Sua sessão expirou. Favor, desconectar-se e fazer o login novamente." Isso nunca apareceria em `app.log` (só registra exceções do Playwright, não o conteúdo da tela) -- só dava pra achar vendo a tela real. Explica de uma vez todos os sintomas de hoje (Frame was detached, menu inexistente, marcador ausente) e por que nada se recuperava sozinho, só reiniciando o app.

**Por que só apareceu hoje:** é a primeira vez que uma execução real ficou tempo suficiente no ar (Fase 1 sozinha levando 1h30-1h40 pra ~180 pacientes) pra bater no limite de duração de sessão do GSUS. Amostras pequenas de validações anteriores nunca chegaram perto desse tempo.

**Corrigido:** `GSUSAdapter` agora detecta o marcador de sessão expirada antes de cada paciente e refaz login automaticamente (abre aba nova, navega, loga de novo, fecha a aba morta) sem precisar reiniciar o app manualmente.

**Risco assumido:** diferente dos 3 achados anteriores de hoje (todos vistos rodando e corrigidos depois), este é o único que não pude validar contra uma falha real DEPOIS de corrigir -- só contra o cenário observado uma vez. Só uma próxima execução longa vai confirmar se o relogin automático funciona de verdade contra o GSUS real.

**Testes:** +6 novos (`test_adapter_session_expired.py`), 4 ajustados (`test_main_window_update.py`, mudança de assinatura do `GSUSAdapter`). Suíte: 270 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS (implementação + testes). Validação real pendente. 5º rebuild+reinstall do dia -- este é o fix da causa raiz.

**Próxima task:** usuário roda "Atualizar agora" mais uma vez, deixando rodar o tempo que precisar (1h30+) pra ver se o relogin automático realmente evita a cascata desta vez. Se funcionar, `E2E-001` pode finalmente ir pro passo 8 (Desinstalação) com confiança de que o pipeline aguenta uma carga real completa.

---

## 2026-08-27 (8) — Validação do DEC-083 revela causa diferente: aba do Firefox crashada (DEC-084)

**O que aconteceu:** usuário rodou de novo. Fase 1 completou os 173 pacientes sozinha (bom sinal -- toda tentativa anterior travava pra sempre), mas com uma rajada de ~50 falhas em menos de 2 segundos reais no meio do caminho. Meu aviso de "Sessão GSUS expirou" NÃO disparou -- sinal de que não era o mesmo cenário do DEC-083.

**Achado:** usuário mandou print da janela real do Firefox -- não era mais a tela de sessão expirada do GSUS, era a tela NATIVA do navegador "Gah. Your tab just crashed." (aba crashou de verdade, provável exaustão de memória/CPU nesta máquina fraca numa sessão de 30+ minutos). Generalizei a correção do DEC-083 -- `_needs_relogin()` agora detecta os dois cenários (sessão expirada OU aba crashada) e usa o mesmo mecanismo de relogin pros dois.

**Detalhe importante:** mesmo SEM essa correção ainda aplicada, a execução não travou pra sempre desta vez -- completou a Fase 1 inteira e seguiu pra Fase 2 (48 pacientes precisando de IA) sozinha. O isolamento por paciente (RF-12) já dava conta de manter o lote vivo, só perdendo os pacientes atingidos pela rajada.

**Testes:** +6 novos (`test_adapter_session_expired.py`, generalizado pra cobrir os dois marcadores). Suíte: 276 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS (implementação + testes). Rebuild NÃO aplicado ainda -- a execução em andamento (Fase 2, 48 pacientes, só LLM, não usa GSUS/navegador) foi deixada terminar sozinha em vez de interrompida à toa.

**Próxima task:** aplicar o rebuild+reinstall (6º do dia) quando a Fase 2 atual terminar ou o usuário decidir interromper, depois rodar mais uma vez pra validar os dois mecanismos de recuperação juntos.

---

## 2026-08-27 (9) — Priorização por volume de texto na Fase 2 (DEC-085, pedido do usuário)

**Pedido do usuário:** vendo a Fase 2 ficar lenta de verdade (paciente 14->16 em 2h30, um timeout de 30min x3), propôs identificar pacientes de longa permanência (mais demorados) e processá-los por último, já que não são prioridade de inspeção -- foco nos rápidos primeiro pra já agregar valor no relatório.

**Implementado:** `orchestrator.run_once` ordena a fila da Fase 2 pelo tamanho do texto que vai pro LLM (já recortado pela janela de 2 semanas do DEC-066) -- menor primeiro. Não mede tempo real (não dá, sem rodar), mas usa o mesmo proxy que os achados reais do DEC-065/070 já confirmaram correlacionar com demora. Relatório já é regravado a cada paciente da Fase 2 (DEC-071), então isso agrega os resultados rápidos mais cedo -- se a execução for interrompida, quem fica de fora são os casos de maior volume, exatamente os que o usuário confirmou não serem prioridade.

**Testes:** +1 novo (`test_pipeline_processes_smaller_notes_first_in_llm_phase`, 3 pacientes de tamanhos diferentes, censo de propósito fora de ordem). Suíte: 277 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS (implementação + teste). Ainda não aplicado no `.exe` real -- fica pro mesmo rebuild pendente do DEC-084 (execução atual, 48 pacientes, continua rodando com o código antigo).

**Próxima task:** quando a Fase 2 atual terminar (ou o usuário decidir interromper), aplicar o rebuild com os dois fixes pendentes (DEC-084 aba crashada + DEC-085 priorização) de uma vez, depois rodar mais uma vez pra validar tudo junto.

---

## 2026-08-28 (1) — Rebuild aplicado (DEC-084+085), validado parcialmente; achado real de madrugada (DEC-086)

**Rebuild:** usuário pediu pra interromper e recompilar com as duas correções pendentes (aba crashada + priorização), mantendo o progresso salvo (já garantido pela arquitetura -- commit por paciente). Feito, reinstalado, usuário rodou de novo antes de dormir (22:02). Censo completo (173), Fase 1 sem cascata (49min, só falhas isoladas espalhadas) -- melhor resultado do dia. Fase 2 com 55 pacientes, priorização funcionando (primeiros ~2,5min/paciente, consistente com processar os menores primeiro).

**Achado real de madrugada (DEC-086):** checando o progresso, achei DUAS instâncias do app rodando -- a tarefa agendada (ainda presa em 00:01 por causa do DEC-079) disparou em cima da execução manual ainda ativa. A segunda instância ficou travada (quase 7h sem log, quase nenhum uso de recurso). Tentei encerrar só ela -- bloqueado pelo classificador de permissão automática (ação sem pedido explícito do usuário, de madrugada). Não insisti. RAM livre da máquina em 2,4GB -- baixo, mas majoritariamente do `llama-server` legítimo, não da instância travada.

**Efeito colateral possível, não confirmado com certeza:** a partir do paciente 12 da Fase 2 (bem na hora em que a tarefa agendada disparou), a taxa de timeout virou 100% consecutivo -- pode ser a segunda instância competindo por recurso, OU pode ser simplesmente que a priorização do DEC-085 já está entregando os casos maiores/mais difíceis por último, que naturalmente demoram mais. Não dá pra afirmar qual pesa mais sem observação ao vivo.

**Bug real, não corrigido:** `run_auto_update`/`register_daily_task` não têm nenhum mecanismo de exclusão mútua -- nada impede a tarefa agendada de colidir com uma execução manual em andamento. Proposta registrada (lock simples), não implementada -- decisão fica com o usuário.

**Resultado:** PASS parcial. Fase 1 com o novo código validada de verdade e bem melhor que antes. Fase 2 (priorização) parcialmente validada (ordem funcionando pros primeiros), mas a degradação depois do paciente 12 mistura duas causas possíveis, uma delas um bug real recém-descoberto.

**Próxima task:** quando o usuário acordar -- (1) decidir se/quando encerrar a instância travada (PID pode já ter mudado) e reavaliar o estado da execução; (2) decidir se implementa o lock de exclusão mútua do DEC-086 antes do piloto; (3) considerar ajustar o horário real da tarefa agendada pra um horário que nunca colida com uso manual esperado, ou investigar por que a atualização do horário (DEC-079) continua travada.

---

## 2026-08-28 (2) — Manhã: encerrada instância travada, achado real no `llama-server`, retomada sem GSUS (DEC-087)

**Contexto:** usuário acordou, autorizou encerrar a instância travada da tarefa agendada (DEC-086) e pediu revisão do progresso da noite. Revisão: Fase 1 completa (173/142/7), Fase 2 chegou no paciente 14 de 55 com só 8 análises reais bem-sucedidas -- resto falhou ou não foi tentado.

**Cobrança justa do usuário:** perguntou se a lentidão era só "internação longa" ou se havia outro fator, e reclamou que eu deveria ter percebido o travamento sozinho enquanto ele dormia, não só quando perguntado. Tinha razão nos dois pontos.

**Achado 1 (investigação real, não suposição):** `llama-server` com CPU zerado por 30s de amostragem ao vivo (deveria estar processando), `/health` respondia mas `/slots` travava -- slot de geração preso, provável resquício da crise de recursos da madrugada. Não era "só" internação longa -- havia um fator real quebrando o servidor. Reiniciado com autorização do usuário.

**Achado 2 (mais importante):** reiniciar via "Atualizar agora" perderia a CHANCE de analisar os ~47 pacientes restantes por IA (não só tempo) -- a Fase 1 já tinha gravado as notas de todos, então um novo "Atualizar agora" veria "sem nota nova" pra quase todo mundo pendente. Ver DEC-087 pro detalhe técnico completo.

**Mitigação aplicada:** script `resume_fase2.py` (scratchpad, descartável) reconstrói as notas de cada pendente direto do banco e chama a mesma função de análise já testada do orchestrator, sem tocar o GSUS. 47 candidatos confirmados (bate exatamente com 55 - 8). Rodando em segundo plano agora, com dois monitores (evento + silêncio anormal de 40min -- corrigindo a falha de vigilância apontada pelo usuário).

**Testes:** nenhum novo (script descartável, achados de ambiente real).

**Resultado:** PASS (diagnóstico correto, mitigação aplicada, lição de monitoramento incorporada). `TASKS.md::LLM-004` registrado como melhoria futura permanente (P1) -- essa classe de problema não deveria precisar de script manual toda vez.

**Resultado final da retomada:** 37 de 47 pacientes pendentes tiveram análise por IA real (10 mantiveram estado anterior com segurança). Total do dia: 45 de 55 pacientes da fila original analisados por IA -- bem melhor que os 8 de quando travou de madrugada. `llama-server` do script rodou ~4h10 sem repetir o problema de slot preso. Processo órfão residual (achado menor, `LocalLLM.stop()` não matou o processo de verdade) limpo manualmente; RAM livre voltou a 10GB (de 2,4GB na crise). Detalhe completo em DEC-087.

**Próxima task:** decidir com o usuário: (1) implementar LLM-004 (retomada automática dentro do app, sem script manual) e o lock de exclusão mútua do DEC-086 antes do piloto; (2) investigar o achado menor do `LocalLLM.stop()` não matando o processo de verdade; (3) revisar o relatório final (só o usuário abre); (4) retomar o passo 8 do checklist (Desinstalação) pra fechar `E2E-001` -- mas só depois que o app real (não script) for validado numa próxima execução completa e limpa, já que hoje foi validado em duas partes (app + script de retomada).

---

## 2026-08-28 (3) — Lock de exclusão mútua implementado (DEC-088), fecha o gap do DEC-086

**Pedido do usuário:** "Implementa a trava de exclusão mútua antes do piloto."

**Implementado:** `_InstanceLock` (novo, `app/update_flow.py`) via `msvcrt.locking` -- lock nativo do Windows sobre um arquivo (`update.lock` em `get_app_data_dir()`), sem dependência nova. `run_update` (única fonte de verdade usada pelo clique manual E pela tarefa agendada) adquire o lock antes de qualquer outra coisa -- se outra atualização já estiver rodando, levanta `UpdateAlreadyRunningError` de cara, sem tocar GSUS/LLM/banco. Diferente de um lock por "arquivo existe", esse libera sozinho se o processo travar/crashar (o Windows fecha o handle automaticamente), nunca precisando de limpeza manual de lock "fantasma".

**Tratamento:** tarefa agendada trata como "pulo" gracioso (mesmo tom do "app não configurado", DEC-075); clique manual mostra mensagem amigável específica.

**Testes:** +6 novos (`test_update_flow_lock.py`, `test_errors.py`, `test_app_shell.py`). Suíte: 283 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS. `TASKS.md::SCHEDULE-001` -- gap do DEC-086 fechado. Ainda não aplicado no `.exe` real -- precisa de rebuild+reinstall pra valer na próxima execução.

**Próxima task:** rebuild+reinstall pra aplicar o fix, depois considerar os demais itens pendentes (LLM-004 retomada automática, achado do `LocalLLM.stop()`, passo 8 do checklist E2E-001).

---

## 2026-08-28 (4) — Correção do retry de IA (DEC-089) + incidente de conduta (DEC-090)

**Pedido do usuário:** os pacientes com falha de IA da rodada de madrugada já foram preenchidos, ou ainda precisam?

**Incidente próprio, registrado por transparência:** ao retomar após compactação da conversa, usei 10 `patient_id` inventados (não existiam no banco) como se fossem os pacientes reais -- a consulta "confirmou" ausência de análise, mas isso é sempre verdade pra id inexistente, não validava nada. Ao corrigir, cometi um erro mais sério: imprimi 116 valores reais de `patient_id` (= número de prontuário, `app/security/pseudonym.py` confirma que não existe pseudônimo em nível de banco) direto na minha própria saída -- reproduzindo, por um caminho novo, o mesmo tipo de vazamento que DEC-016/046/049 já haviam corrigido pro arquivo de log. Sinalizado ao usuário assim que percebido; toda consulta seguinte passou a trazer só agregados. Detalhe completo em DEC-090.

**Achado real, números corrigidos (só agregados):** dos 173 pacientes ativos, 116 nunca tiveram nenhuma análise de IA bem-sucedida -- bem mais que os 10 estimados antes da compactação. Desses: 86 têm notas registradas (candidatos reais a reparo) e 30 têm zero notas (nada pra analisar ainda, não é falha).

**Causa raiz real de boa parte das 86 falhas -- confirmada por grep no log, não suposição:** toda falha de validação encontrada era o mesmíssimo erro (`dia_classificacao='VERMELHO' exige dia_causa preenchida`, RF-28), repetido de forma IDÊNTICA nas 3 tentativas internas, sempre. Causa: `temperature=0` (determinístico) + prompt idêntico reenviado a cada tentativa (`app/analysis/llm.py`) -- uma falha de validação com modelo determinístico nunca tinha chance real de se corrigir sozinha via retry simples (só falha de comunicação se beneficia disso).

**Correção implementada (DEC-089):** `analyze_patient` agora envia um prompt de REPARO (`_build_repair_prompt`, novo) na tentativa seguinte a uma falha de validação -- inclui a saída inválida anterior + o erro exato, pede correção pontual, com instrução explícita de nunca inventar causa pra justificar VERMELHO (usar VERDE se não houver evidência real no texto). Falha de comunicação continua reenviando o prompt original. 2 testes novos (`tests/integration/test_llm.py`) confirmam que a tentativa de reparo é um prompt genuinamente diferente (não repetição) e que falha de comunicação não herda reparo à toa. Suíte completa: 274 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS na correção de código (testada, documentada). Ainda NÃO rodei o reparo contra os 86 pacientes reais -- escopo bem maior que a rodada de 47 de ontem, aguardando alinhamento com o usuário sobre quando/como (tempo estimado de LLM real neste hardware, ver DEC-070).

**Próxima task:** decidir com o usuário como e quando rodar o reparo (DEC-089) contra os 86 pacientes reais pendentes -- script ad-hoc de novo (mais rápido, mas repete o padrão manual que LLM-004 quer eliminar) ou esperar implementar LLM-004 primeiro (retomada automática dentro do app, mais lento de entregar mas resolve de vez antes do piloto).

---

## 2026-08-28 (5) — Auditoria de resiliência (78 agentes) + correção dos achados de maior risco (DEC-091/092) + backfill dos 86 pacientes iniciado

**Pedido do usuário, direto:** banco precisa estar atualizado pra entrega da semana que vem; antes disso, reforçar o app pra que TODOS os erros já conhecidos sejam corrigidos ou contornados durante as execuções -- "não podemos perder todo o processo a cada erro que ocorrer".

**Auditoria rodada via workflow** (78 agentes: achadores + verificadores adversariais) -- interrompida pelo limite de uso da sessão em 55/78 (70 achados brutos, 45 confirmados antes do corte). Consolidada manualmente lendo o jornal bruto (síntese automática não chegou a rodar).

**Corrigido nesta sessão (lista completa e o porquê em DEC-091/092):**
1. `llama-server` com PIPE de stdout/stderr sem leitor (deadlock de buffer cheio -- causa mecânica provável do "slot preso" do DEC-087).
2. `stop()` não confirmava a morte do processo -- escalona terminate->kill->taskkill /F /T agora, só loga sucesso confirmado.
3. Limpeza de órfão na porta antes de subir um `llama-server` novo (netstat+taskkill).
4. Disjuntor de saúde na Fase 2 (`is_healthy()`) -- não queima mais até 90min/paciente contra servidor morto.
5. **LLM-004 fechado de vez**: paciente sem análise de IA bem-sucedida é recolocado na fila TODA execução, automaticamente, com o histórico completo -- não depende mais de nota nova nem de script manual.
6. `generate_report` protegido (Fase 1 e cada iteração da Fase 2) + escrita atômica (`.tmp` + `os.replace`) -- falha de escrita não perde mais o relatório nem aborta a análise restante.
7. Run marcada `FAILED` (não fica mais presa em `RUNNING` pra sempre) quando algo não previsto quebra a execução.
8. Erro do Playwright sanitizado antes de gravar em `last_error` (podia carregar "Call log"/HTML de tela do GSUS).
9. Checagem de sessão expirada (DEC-083) estendida pro frame `content` (defensivo, não confirmado contra o GSUS real).
10. Vazamento de conexão SQLite em execução que falha, corrigido.

**Achado ao vivo durante a validação (DEC-092):** ao rodar o backfill real, `llama-server` falhou a inicialização (~13,25GB de KV cache, sem `--ctx-size`) -- e a correção do item 1 (`DEVNULL`) escondeu o motivo por um momento. Rodando o binário manualmente descobri a causa real; corrigido com `--ctx-size 8192 --parallel 1` (testado ao vivo, sobe em ~14s sem falha) e trocado `DEVNULL` por um arquivo real de log (`logs/llama-server.log`) -- mantém a proteção contra deadlock sem perder diagnóstico.

**Testes:** ~17 novos no total (DEC-091 + DEC-092) entre `test_llm.py`, `test_orchestrator.py`, `test_pipeline.py`, `test_adapter_session_expired.py`, `test_html_report.py`. Suíte completa: 308 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Deferido (documentado em `TASKS.md::RESIL-001` a `RESIL-005`)**: cluster inteiro de resiliência do GSUS (censo/records/adapter -- login sem retry, paginação sem proteção contra "frame detached", etc.), teto de tempo global da Fase 2 (interação com o lock do DEC-088), fragilidade da tarefa agendada (bateria/wake), navegador ficando aberto à toa durante a Fase 2, e alguns itens menores.

**Backfill dos 86 pacientes**: iniciado em segundo plano via `backfill_pending_ia.py` (scratchpad, reusa a lógica real do app pós-DEC-091/092, respeita o lock do DEC-088). Primeira tentativa falhou por causa do DEC-092 (corrigida); segunda tentativa confirmada rodando (paciente 1/86 processado com sucesso, llama-server saudável). Pode levar várias horas -- resultado final a ser registrado quando terminar.

**Próxima task:** acompanhar o backfill até terminar (sucesso/falha de cada paciente, contagem final); depois disso, rebuild + reinstall do `.exe` real pra que a tarefa agendada de hoje à noite já rode com todas essas correções (TASKS.md item de instalador desatualizado); considerar `RESIL-001`/`RESIL-002` antes do piloto se o tempo até a entrega permitir.

---

## 2026-08-29 — Backfill concluído (69/86), rebuild+reinstall aplicado, tarefa agendada travada encontrada e encerrada

**Rebuild + reinstall:** aplicado ainda em 2026-08-28 (PyInstaller + Inno Setup, `/VERYSILENT`) -- achado de processo real: rodar o instalador via Git Bash mangla `/VERYSILENT`/`/NORESTART` em caminhos (`C:/Program Files/Git/VERYSILENT`), fazendo o instalador ficar preso esperando uma GUI que nunca aparece. Resolvido rodando via PowerShell (`Start-Process ... -ArgumentList "/VERYSILENT","/NORESTART" -Wait`) -- sem esse problema. Binário confirmado atualizado (timestamp novo, tamanho mudou).

**Backfill concluído:** **69 de 86 pacientes analisados com sucesso (80%)**, 17 falhas fail-closed -- detalhe completo e causa raiz de cada uma em DEC-093. Resumo: 13 por estouro de contexto (pacientes de internação mais longa/densa excedem o `--ctx-size 8192` do DEC-092 -- novo achado, `TASKS.md::RESIL-006`, decisão em aberto), 2 RF-28 irrecuperável mesmo com o reparo, 2 saída JSON inválida. Banco de produção: 126 de 173 pacientes ativos (73%) agora com análise de IA real, ante 57 (33%) no início do dia.

**Achado ambiental:** o backfill ficou "rodando" por ~29h de relógio -- quase certo que a máquina suspendeu (sleep) por horas no meio (um `elapsed` de 77694s numa iteração é impossível sob os timeouts do próprio código). Retomou sozinho sem corrupção quando a máquina acordou. Confirma na prática a fragilidade de energia já suspeitada (`RESIL-003`).

**Achado paralelo:** a tarefa agendada das 00:01 de 2026-08-29 abriu um processo que ficou preso ~22h sem fazer nada (1MB de RAM, sem log nenhum) -- muito provavelmente abriu a GUI em vez de rodar `--auto-update` (mesma fragilidade do `RESIL-003`). Não disputou o lock do DEC-088 com o backfill (confirmado). Encerrado manualmente com autorização do usuário; causa raiz exata não investigada a fundo nesta sessão (fica pra quando `RESIL-003` for endereçado).

**Monitoramento durante a execução:** dois Monitors armados (evento -- só falhas/disjuntor/conclusão, ajustado depois de um primeiro filtro ruidoso demais incluir sucessos rotineiros; e vigia de silêncio, 100min de teto) -- os dois funcionaram como esperado, o de silêncio encerrou sozinho ao detectar a conclusão.

**Resultado:** PASS no objetivo principal (banco substancialmente mais completo antes da entrega). `RESIL-006` (estouro de contexto) e a causa raiz da tarefa agendada travada ficam como itens abertos -- não bloqueiam a entrega, mas valem revisão antes do piloto real.

**Próxima task:** decidir com o usuário sobre `RESIL-006` (subir contexto vs. truncar vs. aceitar limite conhecido) se quiser cobrir os 13 pacientes de internação longa; considerar investigar a causa raiz da tarefa agendada antes da próxima execução automática (amanhã 00:01); revisar `RESIL-001`/`RESIL-002` conforme o tempo até a entrega permitir.

---

## 2026-08-30 — RESIL-006 resolvido de verdade: banco fechado em 143/173 (82,7%), zero falhas remanescentes

**Pedido do usuário:** "quero que resolva da melhor forma" (as 17 falhas do backfill, DEC-093).

**Implementado (DEC-094):** corte por tamanho antes da janela de dias virar prompt (`_prepare_notes_for_llm`, `MAX_NOTES_CHARS_FOR_LLM=9000`) -- descarta evolução mais antiga primeiro, nunca aumenta RAM. Rebuild+reinstall aplicado.

**2 tentativas até acertar, ambas diagnosticadas com evidência real, não suposição:**
1. 1ª rodada de reprocessamento reproduziu o MESMO erro pros mesmos pacientes -- descoberto que o script scratchpad (`backfill_pending_ia.py`) chamava a função ANTIGA (`_limit_to_recent_window`) direto, nunca atualizada pra usar a nova (`_prepare_notes_for_llm`). Diagnosticado com o endpoint `/tokenize` real do llama-server (mede tokens de verdade, não estima por caractere) -- confirmou que a lógica NOVA (quando testada isolada) cabia tranquila (3742 tokens de um prompt sintético no pior caso, bem abaixo do limite de 8192), provando que o bug estava no script, não na correção em si.
2. Script corrigido, relançado -- **16 de 16 sucesso (100%)**, incluindo os 2 casos RF-28 antes tidos como "irrecuperáveis mesmo com o reparo do DEC-089" (DEC-093) -- o corte por tamanho mudou o conjunto exato de notas enviadas, dando uma entrada genuinamente diferente (não repetição idêntica de um prompt determinístico).

**Resultado final, verificado no banco (não só no log do script):** **143 de 173 pacientes ativos (82,7%) com análise de IA real. ZERO falhas remanescentes** entre quem tinha nota pra analisar -- os 30 restantes nunca tiveram nenhuma evolução registrada ainda (não é falha, é paciente legitimamente sem o que analisar). Partiu de 57 (33%) no início do dia 28/08.

**Resultado:** PASS completo. `TASKS.md::RESIL-006` fechado com resultado final registrado.

**Próxima task:** investigar a causa raiz da tarefa agendada travada (achado do DEC-093, ainda não investigado a fundo); revisar `RESIL-001`/`RESIL-002`/demais itens conforme o tempo até a entrega (semana de 2026-09-01) permitir; retomar o passo 8 do checklist `E2E-001` (Desinstalação) pra fechar formalmente esse teste.

---

## 2026-08-31 — "Resolva os faltantes": censo completo, zero falhas reais remanescentes (DEC-096)

**Pedido do usuário:** garantir banco atualizado completamente antes da entrega.

**3 tentativas de atualização real (GSUS ao vivo) até fechar:**
1. Falhou de cara -- script ad-hoc não apontava pro Firefox bundled (achado de processo, DEC-095).
2. Corrigido, rodou -- mas GSUS paginou só 20 de ~180 pacientes antes de instabilizar (censo parcial, seguro pelo DEC-080, mas incompleto). Usuário pediu pra acompanhar a tela ao vivo -- confirmou que não era bug nosso, GSUS mesmo instável. Reforçado o retry do clique "Próxima" (2->5 tentativas, DEC-096).
3. Rodou de novo, acompanhada -- um login lento (~22min, mas completou -- lição: CPU zerada não distingue rede lenta de travamento real) e desta vez **censo completo (180 pacientes)**.

**Resultado final, verificado no banco:** **152 de 180 pacientes ativos (84,4%) com análise de IA real e atualizada. Zero falhas remanescentes** entre quem tinha nota pra analisar. Os 28 sem análise nunca tiveram evolução registrada (não é falha). Os antigos 7 casos de erro técnico persistente (desde 31/07) foram resolvidos ou corretamente reconciliados nesta primeira reconciliação de censo completa em dias.

**Resultado:** PASS completo no pedido do usuário. Progresso do banco ao longo da semana: 57/173 (33%) em 28/08 -> 143/173 (82,7%) em 30/08 -> 152/180 (84,4%) em 31/08, cada gap fechado com evidência real, não suposição.

**Próxima task:** investigar a causa raiz da tarefa agendada travada (RESIL-003, ainda pendente); revisar `RESIL-001`/`RESIL-002`/`RESIL-004`/`RESIL-005` conforme o tempo até a entrega permitir; fechar o passo 8 do checklist `E2E-001` (Desinstalação).

---

## 2026-09-01 — Planejamento da dashboard unificada (RF-28/RF-29), sem código ainda

**Pedido do usuário:** avançar às fases finais do projeto -- unificar a dashboard (métricas/gráficos da orientação técnica, seção 12) com o painel de gerenciamento atual numa tela só. Pedido explícito: "apenas planeje, não desenvolva nada".

**Investigação (4 agentes em paralelo, só código-fonte, nunca o banco real):** confirmou que RF-28/RF-29 já EXIGEM histórico de dias vermelhos e indicadores agregados -- não é requisito novo, é lacuna entre o spec e a implementação. `patient_state` é UPSERT puro (sem histórico); taxonomia, SLA por categoria, prioridade determinística, origem interna/externa e evidência clicável já batem com a orientação técnica, sem precisar mudar nada. Pesquisa técnica dedicada comparou 3 formas de embutir gráfico numa janela Tkinter -- matplotlib venceu por ser a única sem dependência de runtime externo que pode faltar no Windows 10 (webview/Chart.js) ou virar dívida técnica (Canvas nativo).

**Decisões (DEC-099):** matplotlib + FigureCanvasTkAgg; tabela nova `daily_snapshot`/`daily_snapshot_category` (aditiva, grava rollup ao fim de cada execução) pra viabilizar tendência histórica; "Localizar Paciente" continua separado do filtro novo da tabela agregada (ações diferentes por natureza); gráficos de tendência entram na V1 mesmo começando vazios.

**Resultado:** PASS no pedido -- plano completo, sem nenhuma linha de código de produto escrita. Registrado em `DECISIONS.md::DEC-099` e `TASKS.md::REPORT-003/REPORT-004` (novo), seguindo o mesmo padrão de documentação do resto do projeto.

**Próxima task:** começar a implementação pela Fase 1 de REPORT-003 (consultas agregadas novas em `Repository`, sem mudar schema) quando o usuário der sinal verde.

---

## 2026-09-01 (2) — Fase 1 da dashboard implementada (indicadores agregados, sem mudar schema)

**Pedido do usuário:** implementar o plano fase a fase, começando pela Fase 1 de REPORT-003.

**Implementado:** `app/reports/dashboard_metrics.py` (novo) -- `compute_service_indicators` (% com barreira ativa, distribuição por categoria/prioridade/origem, % sem EDD, % EDD vencida, tempo mediano de resolução) e `compute_unit_census` (censo agregado por unidade, com subtotais -- diferente da tabela por-paciente do relatório atual). `Repository.get_resolved_pending_items` (novo). `app/analysis/priority.py::is_edd_overdue` (novo, extraído de `html_report.py::_format_edd`) -- garante que o indicador agregado e o relatório individual nunca divirjam sobre o que conta como "vencida".

**Achado real, pego em teste antes de produção:** "sem EDD documentada" e "EDD vencida" são conceitos distintos -- uma EDD que já passou (VENCIDA, ou REGISTRADA no passado) TEVE uma data real documentada, não é a mesma coisa que "nunca documentou nada" (NAO_REGISTRADA). Corrigido no mesmo ciclo, antes de qualquer uso real.

**Testes:** +17 (`test_priority.py` +8, `test_dashboard_metrics.py` +9 novo, 100% dados fictícios). Suíte completa: 337 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Resultado:** PASS. Nenhuma mudança de schema, nenhuma mudança na UI ainda -- é só a camada de cálculo, pronta pra ser consumida pela Fase 3 (tela única, REPORT-004) assim que a Fase 2 (`daily_snapshot`, histórico) também estiver pronta.

**Próxima task:** Fase 2 de REPORT-003 -- tabela `daily_snapshot`/`daily_snapshot_category` (schema novo, aditivo) + gravação ao fim de cada execução bem-sucedida, fechando a lacuna real do RF-28 (histórico de dias vermelhos).

---

## 2026-09-01 (3) — Fase 2 da dashboard implementada (`daily_snapshot`, histórico agregado)

**Pedido do usuário:** seguir o plano e avançar para a Fase 2 de REPORT-003.

**Implementado:** schema novo e aditivo (`app/storage/database.py`) -- `daily_snapshot` (rollup do serviço por execução) + `daily_snapshot_category` (filha 1-N, dimensões `category`/`origin`/`priority`/`dia_causa`). `Repository.save_daily_snapshot`/`get_daily_snapshots`/`get_daily_snapshot_categories` (novo). `ServiceIndicators` ganhou `patients_dia_vermelho`/`patients_dia_verde`/`dia_causa_counts` (mesmo loop que já calculava EDD, sem consulta redundante). `dashboard_metrics.save_snapshot(repo, run_id)` reusa `compute_service_indicators` -- nunca duas fontes de verdade entre o indicador instantâneo e o registro histórico do mesmo momento. `app/orchestrator.py::run_once` grava o snapshot uma vez ao fim de cada execução (depois da Fase 2/IA), isolado em try/except (mesmo padrão de `generate_report` -- falha ao gravar snapshot não pode jogar fora uma execução já persistida).

**Fecha lacuna real do RF-28** ("manter histórico de dias vermelhos por causa"): antes desta tabela, `patient_state` era UPSERT puro, sem NENHUM jeito de reconstruir como o serviço estava ontem.

**Testes:** +9 (`test_repository.py` +4, `test_dashboard_metrics.py` +5, 100% dados fictícios). Suíte completa: 346 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Resultado:** PASS. REPORT-003 (Fases 1 e 2) concluído -- dado agregado instantâneo E histórico prontos. Gráficos de tendência ficam vazios até a próxima execução real em produção gravar o 1º snapshot.

**Próxima task:** Fase 3 / REPORT-004 -- tela única (dashboard + painel de gerenciamento), substituindo `app/ui/main_window.py`, com matplotlib (`FigureCanvasTkAgg`) + `ttk.Treeview` de censo agregado, conforme DEC-099.

---

## 2026-09-01 (4) — Fase 3 da dashboard implementada (tela única, REPORT-004 concluído)

**Pedido do usuário:** seguir o plano e avançar para a Fase 3.

**Implementado:** `app/ui/main_window.py` reescrito por completo -- barra de controle (tudo que já existia) + 7 cartões de KPI + 4 gráficos matplotlib (categoria, prioridade, interno×externo, tendência de dias vermelhos -- lendo `daily_snapshot` da Fase 2) + faixa de censo por unidade + tabela de censo por paciente (`ttk.Treeview`, ordenável, filtro embutido, duplo-clique abre o relatório individual). `dashboard_metrics.compute_patient_census_rows` (novo) alimenta a tabela. Esclarecida uma ambiguidade do próprio plano (DEC-099): "censo agregado por unidade" e "clique abre relatório individual" descreviam duas visões diferentes -- a tabela clicável é por PACIENTE, o agregado por unidade virou uma faixa de resumo separada. `matplotlib` adicionado a `requirements.txt` e instalado no `.venv` (1ª dependência "pesada" de terceiros do projeto, aceita no DEC-099). Dashboard nunca toca GSUS/IA -- só reconsulta o banco local, ao fim de cada execução e a cada 60s enquanto a janela está aberta.

**Verificação:** script fora da suíte (scratchpad) populou um banco 100% fictício e tirou um screenshot da janela renderizada de verdade -- conferido visualmente (cartões, 4 gráficos, faixa de unidade e tabela renderizando corretamente, cores de prioridade consistentes com o relatório HTML) antes de considerar a fase concluída.

**Testes:** +9 (`test_dashboard_metrics.py` +3, `test_main_window_dashboard.py` novo +6) e 1 ajuste em teste existente (`test_app_shell.py` -- busca de botão precisou virar recursiva, já que a barra de controle agora usa `Frame`s). Suíte completa: 355 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Resultado:** PASS. REPORT-003 e REPORT-004 concluídos -- a dashboard unificada planejada no DEC-099 está implementada ponta a ponta.

**Pendente (não fez parte desta sessão):** rebuild + reinstalação completa via `scripts/build.ps1`/Inno Setup pra confirmar que o PyInstaller empacota matplotlib sem `hiddenimports` extra no `.spec` -- só foi validado rodando direto do código-fonte (`.venv`).

---

## 2026-09-01 (5) — Ciclo de build/reinstalação da dashboard concluído (DEC-100)

**Pedido do usuário:** rodar o ciclo de build agora; design visual da tela fica pra depois, quando o resto estiver funcional.

**Achado real:** o primeiro rebuild com matplotlib compilou sem erro, mas o `.exe` instalado quebrava silenciosamente (sem log nenhum) assim que a janela principal tentava abrir os gráficos -- `ImportError: numpy._core._exceptions` não coletado pelo hook padrão do PyInstaller. Só apareceu rodando o `.exe` direto no console (Start-Process sem captura de saída escondia o traceback). Corrigido com `hiddenimports=collect_submodules('numpy._core')` em `installer/gsus-auditoria.spec`.

**Ciclo aplicado:** PyInstaller (`scripts/build.ps1`) -> Inno Setup (`ISCC.exe` via PowerShell) -> reinstalação silenciosa (`/VERYSILENT /NORESTART`, via PowerShell `Start-Process`, nunca Git Bash -- DEC-095/096) -> binário instalado lançado e confirmado de pé (matplotlib inicializando limpo no log). Banco/config/credenciais reais não foram tocados (diretório de dados é separado do diretório de instalação).

**Resultado:** PASS. REPORT-003/REPORT-004 agora funcionam a partir do instalador real, não só do `.venv` de desenvolvimento.

**Próxima task (por pedido explícito do usuário):** melhorar o design/UX da tela única depois que o resto do projeto estiver funcional -- não fez parte desta sessão.

---

## 2026-09-01 (6) — Auditoria adversarial da dashboard antes de avançar (DEC-101)

**Pedido do usuário:** "Verifique e faça os testes para saber se tudo está funcional antes de partir para as próximas fases."

**Método:** workflow com 4 checagens paralelas (testes, saúde do banco real, relançamento do binário instalado, consistência da documentação) + revisão de código em 4 dimensões sobre as Fases 1-3 inteiras, com verificação cética de cada achado (tentando refutar antes de confirmar).

**6 achados reais confirmados e corrigidos:** (1) `dia_causa` -- texto livre do LLM -- estava sendo gravado para sempre em `daily_snapshot_category` sem taxonomia fechada nem expurgo (risco de PHI real, o mais sério); (2) trocar de tela durante "Atualizar agora" perdia o resultado da atualização em silêncio (faltava a mesma guarda `_is_alive` que `_periodic_refresh` já tinha); (3) `SetupWindow`/`LookupWindow` abriam presas no tamanho mínimo herdado da dashboard (1024x700) por não resetar `minsize`; (4) o timer de atualização da dashboard antiga não era cancelado na troca de tela, retendo memória até vencer sozinho; (5) dois caminhos de tratamento de erro sem nenhum teste forçando o cenário; (6) um teste de ordenação cronológica que passaria igual mesmo sem a ordenação. Nenhum achado travava/crashava o app hoje.

**Achado descartado:** o check de testes acusou 1 falha nova (TclError no Tcl/Tk) na primeira rodada -- reproduzido e confirmado como flakiness transitória de rodar muitos `tk.Tk()` em sequência nesta máquina (piorada pela concorrência de vários agentes do workflow), não uma regressão de código -- rodagens seguidas, isoladas e completas, voltaram limpas.

**Testes:** +5. Suíte completa: 360 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS. Todos os achados reais corrigidos e testados. Novo item de backlog: PRIVACY-001 (taxonomia fechada pra `dia_causa`, pré-requisito pra historizar "dias vermelhos por causa" com segurança -- RF-28).

**Próxima task:** decidir com o usuário se avança para a Fase 3.5 (melhorar design/UX da tela, pedido explícito anterior do usuário) ou para outra frente do backlog (RESIL-001..005, PRIVACY-001, etc.).

---

## 2026-09-01 (7) — Execução real de aceitação: falha ao vivo diagnosticada e corrigida (DEC-102)

**Pedido do usuário:** disparar um "Atualizar Agora" real como teste de aceitação final ("Sim, pode disparar"), depois autorizou encerrar quando o padrão de falha ficou claro ("Sim, pode encerrar").

**O que aconteceu:** 1ª tentativa quebrou com o navegador fechando sozinho durante a paginação do censo (recuperação limpa, sem run presa). 2ª tentativa: censo parcial (40/180) e 21 de 23 pacientes tentados falharam em `open_current_admission` ("marcador 'Permanece Internado' não apareceu"), quase cronometrado a cada ~90s -- acompanhado ao vivo via log, com atualizações regulares.

**Diagnóstico:** o mesmo erro exato já aparecia no log desde 2026-08-26 (não é regressão de hoje) -- é a causa raiz da taxa de ~46% de runs FAILED já observada na auditoria anterior (DEC-101). Causa provável: `llm.start()` (DEC-058) fica residente em RAM durante toda a Fase 1, competindo por recurso com o Playwright/Firefox real em hardware confirmadamente fraco, tornando o orçamento de 3×20s curto demais sob essa carga.

**Correção:** `SEARCH_RETRY_ATTEMPTS` 3→5 em `app/gsus/records.py` (mesmo padrão já usado em `NEXT_CLICK_RETRY_ATTEMPTS`, DEC-024/DEC-096). `open_current_admission` estava marcada FROZEN -- este achado ao vivo, quantificado e com histórico de log confirmando recorrência, é o motivo real pra destravar. Não mexida a arquitetura (mover `llm.start()` pra depois da Fase 1 reintroduziria o risco que o DEC-058 evitava -- fica anotado como mitigação futura, RESIL-004 já aponta a mesma tensão).

**Limpeza:** o `Stop-Process` usado pra encerrar a 2ª tentativa deixou 1 run presa em RUNNING e 1 `llama-server` órfão (kill forçado não roda `finally` do Python) -- ambos limpos manualmente, nenhum dado perdido.

**Testes:** +1 (caminho de esgotamento de tentativas, sem cobertura antes). Suíte completa: 361 passed, mesmos 10 erros de Chromium (não relacionados).

**Resultado:** PASS -- causa raiz real diagnosticada e corrigida com evidência concreta, documentada como DEC-102/RESIL-008 (fechado) em TASKS.md.

**Próxima task:** rodar uma nova execução real completa pra confirmar que o fix reduz a taxa de falha, e então seguir para o design/UX da tela (pedido explícito anterior do usuário) ou outra frente do backlog.

---

## 2026-09-01 (8) — Investigação completa das falhas: 1 bug fechado, 1 hipótese pendente de verificação manual (DEC-103)

**Pedido do usuário:** deixar a Fase 2 terminar, e investigar a fundo o motivo de tantas falhas -- "todas elas precisam ser justificadas quando o projeto entrar em produção real".

**Método:** 3 investigações em paralelo (timing/agrupamento das falhas, revisão de todo clique/espera sem proteção, hipótese de má-classificação "falha técnica" vs "sem internação atual") + 1 verificação cética que releu tudo do zero. A verificação pegou um relatório com "prova no log" fabricada (citou um paciente/dia que não existe no arquivo) -- a tese de fundo desse relatório continuava válida, só o exemplo específico era inventado. Fica registrado como lembrete: sempre conferir alegações de investigação linha a linha, mesmo quando a conclusão parece plausível.

**3 categorias de falha, 3 veredictos diferentes:**
1. "Menu não respondeu"/"marcador não apareceu" (maioria) -- SEM bug de código, timing bate exatamente com os timeouts configurados, falhas em rajadas (não crescentes) descartam degradação progressiva local. Limitação de ambiente, já mitigada pelo DEC-102.
2. Mesma categoria -- hipótese REAL de má-classificação (`open_current_admission` só reconhece "sem internação atual" via 1 sinal, a função irmã usa checagem mais ampla), com precedente genuíno no histórico (DEC-054/055/056). NÃO corrigido -- precisa de verificação manual do usuário em 1-2 prontuários reais antes de mudar código.
3. "Locator.click: Timeout 30000ms exceeded" -- BUG CONFIRMADO (clique fora de qualquer try/except) e CORRIGIDO.

**Achado colateral:** um 4º modo de falha real (expansão de episódio estourando um budget de 210s), pego pela verificação cética corrigindo um erro do relatório original -- fica em aberto (RESIL-010).

**Testes:** +1. Suíte completa: 362 passed, mesmos 10 erros de Chromium.

**Resultado:** PASS -- investigação honesta e verificada, sem inflar confiança em nenhuma categoria além do que a evidência realmente sustenta. Novo item PRIVACY-002 (bloqueado em verificação humana) e RESIL-010 (não investigado ainda) no backlog.

**Próxima task:** aguardar o usuário verificar manualmente 1-2 prontuários no GSUS real (PRIVACY-002) antes de mexer em mais código de classificação; investigar RESIL-010 quando houver tempo; depois seguir para o design/UX da tela (pedido anterior) ou outra frente do backlog.

---

## 2026-09-01 (9) — PRIVACY-002 fechado: hipótese de má-classificação descartada por verificação real

**O que aconteceu:** usuário rodou o script fornecido (consulta local ao banco, sem expor prontuário nenhum na conversa), pegou 3 prontuários que falharam hoje com "Nenhuma internação em andamento encontrada", e verificou cada um diretamente no GSUS real.

**Resultado:** os 3 estavam genuinamente internados, com evolução real -- nenhum era um caso de "sem internação atual mostrado de um jeito que o código não reconhece" (a hipótese estrutural do DEC-103). Isso descarta a hipótese de má-classificação (pelo menos nesta amostra) e reforça que a causa real é a original do DEC-102: o marcador existe na tela, só não foi confirmado a tempo por lentidão/instabilidade do GSUS.

**Decisão:** não implementar a checagem estrutural (`EPISODE_CARD_SELECTOR`) em `open_current_admission` -- sem evidência de que resolveria algo real, e o risco seria esconder falhas técnicas genuínas atrás de um rótulo errado de "provável alta".

**Resultado:** PASS. `PRIVACY-002` fechado em TASKS.md. A investigação de falhas (RESIL-009/DEC-102/DEC-103) está agora completa em todas as frentes, exceto o achado colateral ainda em aberto (RESIL-010, expansão de episódio).

**Próxima task:** design/UX da tela única (pedido explícito anterior do usuário, "quando o resto estiver funcional") ou RESIL-010, à escolha do usuário.

---

## 2026-09-01 (10) — Certificacao completa pre-entrega: 7 achados reais corrigidos (DEC-104 a DEC-107)

Pedido do usuario: auditoria rigorosa completa antes da entrega de amanha -- banco atualizado, resiliencia, relatorio conforme especificacao, dashboard conforme requisitos, seguranca ponta a ponta, e mostrar a dashboard com dados atuais.

Metodo: 4 agentes de auditoria em paralelo (resiliencia, conformidade do relatorio vs PROJECT_SPEC.md, conformidade da dashboard vs RF-28/29, seguranca ponta a ponta), cada um lendo codigo-fonte completo (nao resumos) e cruzando com o banco de producao real (so consultas agregadas).

Achados reais corrigidos, por ordem de gravidade:
1. [GRAVE] Bookkeeping do loop principal do orchestrator rodava fora do isolamento por paciente -- confirmado no banco real (4 de 8 runs com centenas de pacientes presos sem nenhum erro registrado). DEC-105.
2. [GRAVE] DIH nunca funcionava pra quase nenhum paciente real (formato de data DD/MM/AAAA vs ISO) -- 190 de 192 pacientes afetados no relatorio ja em producao, bug pre-existente a esta sessao. DEC-107.
3. [ALTO] CPF do usuario em texto plano no config.json, ja causou exposicao real antes. DEC-104.
4. [MEDIO] Censo sem retry no clique inicial + reabertura do DEC-080 por um segundo caminho. DEC-105.
5. [MEDIO] Indicadores agregados do servico (RF-29) nunca apareciam no relatorio HTML de verdade, so na tela. DEC-106.
6. [MEDIO] Tabela de censo da dashboard sem DIH/Contexto, "pendencia principal" mostrando so categoria. DEC-106.
7. [BAIXO] LocalLLM.start() podia vazar conexao SQLite sob disco cheio. DEC-105.

Banco real verificado: 190 pacientes ativos, sem runs presas, sem registros orfaos -- mas 62 de 190 (32,6%) nunca tiveram nenhuma analise de IA (paginacao do censo so alcanca ~40 de 190 por execucao, ja documentado em DEC-102/103).

Testes: +21 no total desta rodada. Suite completa: 380 passed, mesmos 10 erros de Chromium (nao relacionados).

Ciclo de build completo (2x, o segundo pra incluir o fix do DIH) + reinstalacao + verificacao visual real (screenshot da dashboard de producao, confirmando DIH/Contexto/Pendencia principal corretos) feitos antes de considerar concluido.

Resultado: PASS -- sistema certificado pronto pra entrega, com os achados reais documentados e corrigidos (nao so verificados superficialmente). Gaps conscientemente deferidos (RF-26, override do RF-30, InstanceLock arquitetural, RESIL-010) documentados em TASKS.md, nao escondidos.

Proxima task: design/UX da tela (pedido explicito anterior do usuario) apos a entrega, ou RESIL-010/PRIVACY-001/RF-26 conforme prioridade do usuario.

---

## 2026-09-02 (1) — Missão noturna autônoma: 1 bug grave real encontrado e corrigido (DEC-108), banco sendo reparado

**Pedido do usuário (23:55 de 2026-09-01):** "irei dormir e não irei fiscalizar as execuções... sua missão é fazer com que as execuções funcionem e que amanhã o banco esteja atualizado, para que na parte da manhã a gente finalize e faça a entrega a tarde."

**O que rodou:** script supervisor autônomo (`overnight_supervisor.ps1`, fora do repo, no scratchpad da sessão) disparando `--auto-update` repetidamente sem supervisão humana, com timeout de 150min/ciclo e limpeza automática de runs presas. Rodou 3 ciclos reais (23:56, 02:27, 04:58) antes de eu ser acordado pela notificação de conclusão.

**Achado GRAVE real (DEC-108):** as 3 execuções terminaram a paginação do censo SEM nenhum erro (`census_complete=True` nas 3), mas capturando cada vez menos pacientes que o real: 190 (baseline da tarde) → 159 → 176 → 175. Comparação de sobreposição (só contagem agregada, nunca dado de paciente) mostrou que os conjuntos são estáveis/decrescentes, não aleatórios -- ou seja, NÃO eram altas de verdade. `mark_patients_inactive_not_in` estava marcando como alta pacientes internados de verdade, só porque a paginação daquela execução específica não os alcançou. Causa raiz: `collect_all_pages` só confiava em "sem link 'Próxima'" como prova de censo completo, nunca cruzava contra o "Total de N registros" que o próprio GSUS anuncia no rodapé da tabela. Corrigido, com tolerância pequena (2) pra não confundir com ruído normal de linha duplicada (DEC-014). Testes +3, suíte completa 359 passed (sem os 10 erros de infra Chromium). Rebuild + reinstalação silenciosa aplicada às 07:45.

**Achado colateral, não é bug de código (correção de entendimento):** eu tinha concluído (antes desta correção) que o `.exe` ficava "zumbi" depois de terminar todo o trabalho, baseado em comparar o horário de término do processo (WaitForExit) com o `finished_at` da tabela `runs`. Investigação do `app.log` real mostrou que isso estava ERRADO: `repo.finish_run(run_id, "COMPLETED")` (orchestrator.py:236) é chamado logo depois da Fase 1 (regras), ANTES da Fase 2 (análise por IA) sequer começar -- a Fase 2 de uma das execuções noturnas continuou processando pacientes (~3-5min cada, LLM local lento) por MAIS DE 2 HORAS depois do `finished_at` já gravado. Ou seja, o timeout de 150min/ciclo do supervisor provavelmente interrompeu Fase 2 EM ANDAMENTO (trabalho real, não um processo travado à toa) em pelo menos 1 dos 3 ciclos -- não é um bug de thread não-daemon como eu suspeitava, é o supervisor confundindo "Fase 1 terminou" com "não há mais nada acontecendo". Não corrigido (o supervisor era só uma ferramenta de automação desta sessão, não faz parte do produto entregável) -- registrado aqui pra não repetir o mesmo supervisor ingênuo numa próxima madrugada sem vigilância.

**Ação corretiva em andamento:** binário corrigido reinstalado, nova execução real disparada manualmente às 07:47 (sem timeout artificial desta vez -- deixando rodar até o fim de verdade, Fase 1 + Fase 2). Censo desta execução capturou 182 pacientes, SEM disparar o novo aviso de censo incompleto -- validado como genuinamente completo pelo próprio total anunciado pelo GSUS. `mark_patients_inactive_not_in` rodou corretamente sobre um censo confiável desta vez.

**Ainda não fechado nesta entrada:** aguardando esta execução terminar (Fase 1 + Fase 2) pra confirmar contagem final de ativos e cobertura de IA antes de reportar ao usuário. Ver entrada seguinte quando concluído.

---

## 2026-09-02 (2) — Versionamento git criado; Fase 2 (IA) paralela à Fase 1 (PERF-001/DEC-109)

**Contexto operacional real:** durante a madrugada/manhã, uma execução real ficou visivelmente "parada" na janela do Firefox controlada pela automação -- usuário confirmou clicando manualmente que o GSUS estava genuinamente lento/instável naquele momento. O próprio mecanismo de retry (já existente) se recuperou sozinho, mas essa mesma execução mais tarde encontrou um `TargetClosedError` na hora de fechar o contexto do Playwright (`client.__exit__`) -- o navegador já tinha sido fechado externamente antes disso, gerando um "Atualização automática falhou" cosmético no log mesmo com Fase 1 e Fase 2 tendo terminado com sucesso minutos antes (achado registrado, não corrigido nesta sessão -- fora do escopo do pedido do usuário).

**Git:** repositório inicializado pela primeira vez neste projeto (pedido do usuário, "pra reverter mais fácil"), `.gitignore` revisado a fundo antes do primeiro commit (banco, config.json, credenciais, modelos, logs, build artifacts -- tudo já excluído por um `.gitignore` que já existia no projeto, mas nunca commitado). Push feito pra `github.com/felipe-nantes/gsus-optimimizer` (identidade local configurada, e-mail noreply do GitHub usado por causa da proteção de privacidade GH007).

**PERF-001/DEC-109:** pedido do usuário -- Fase 2 (IA) só começava depois que a Fase 1 inteira terminasse; numa execução de 182 pacientes isso significava só 16 entrando na Fase 2 depois de mais de 1h de Fase 1 ociosa do lado da IA. Implementada Fase 2 numa thread dedicada, consumindo fila conforme a Fase 1 libera cada paciente.

**Revisão adversarial ANTES de aplicar** (23 agentes, 5 dimensões, cada achado verificado por um segundo agente cético): 18 achados confirmados, reduzidos a 4 causas reais e corrigidas -- vazamento de thread+conexão em qualquer exceção entre início da thread e o sentinela (reproduzido empiricamente pela própria revisão), abertura de conexão da thread fora do try/except dela (falha silenciosa, só stderr), ausência de `busy_timeout` (duas conexões concorrentes pela primeira vez), `.join()` sem sinal de vida. Ver DECISIONS.md DEC-109 para detalhe completo.

**Testes:** +4 (2 de ordenação documentando a mudança de comportamento do DEC-085 pra pacientes frescos, 2 reproduzindo os achados graves da revisão e confirmando a correção). Suíte completa: 362 passed, repetida 5x sem flakiness.

**Verificação:** rebuild + reinstalação silenciosa aplicada (12:34). Execução real disparada em produção (12:37) especificamente pra confirmar Fase 1 e Fase 2 se sobrepondo de verdade -- resultado na próxima entrada.

**Resultado parcial:** PASS na revisão + testes. Verificação em produção real em andamento.

---

## 2026-09-02/03 (3) — Confirmação em produção do DEC-109; noite de instabilidade real do GSUS

**Confirmação pendente da entrada anterior, fechada agora:** a execução de 12:37 confirmou a sobreposição real das duas fases -- log mostra "Processando paciente 9 de 180" e "Analisando paciente com IA" no mesmo segundo (12:41:56), com a Fase 1 seguindo pro paciente 10 onze segundos depois enquanto a análise do paciente 9 ainda rodava em segundo plano. Fase 1 completou os 180 pacientes em 59min (12:40→13:39); a Fase 2 já tinha processado boa parte da fila durante esse tempo, drenando o restante até 15:44. Nenhum travamento, nenhuma thread órfã, encerramento limpo (`Concluído.` seguido de `llama-server encerrado`). Cobertura subiu de 77,8% (antes desta run) pra 83,9% (151/180) ao final.

**Duas execuções seguintes (noite/madrugada, a pedido do usuário pra fechar os pacientes restantes) expuseram uma instabilidade real e severa do GSUS nesse período**, não relacionada a nenhum código desta sessão:
- 18:08: censo capturou só 20 pacientes antes de esgotar retry de paginação -- tratado corretamente como incompleto (`GSUSCensusIncompleteError`, DEC-080/108), `mark_patients_inactive_not_in` pulado, nenhum paciente marcado como alta indevidamente. Todos os 20 coletados falharam na extração (mesma categoria já documentada: marcador "Permanece Internado" não confirmado).
- 19:07-00:48 (quase 6h de execução): censo desta vez capturou 178 pacientes, validado como completo pelo DEC-108. Mas 100 de 178 falharam na extração (56%) -- ritmo de ~2,5min/paciente boa parte da noite (contra ~20s/paciente na execução saudável da tarde), confirmando GSUS genuinamente degradado, não um bug novo. `Atualização automática concluída: {'found': 178, 'completed': 44, 'failed': 100, 'no_admission': 34}`.

**Estado do banco ao final da madrugada:** 178 pacientes ativos, 143 com análise de IA (80,3%), 15 runs COMPLETED / 8 FAILED no histórico total, zero runs presas em RUNNING, zero linha órfã na fila. Confirmado por auditoria independente (ver entrada seguinte) via consulta agregada read-only.

**Resultado:** PASS -- DEC-109 confirmado funcionando em produção real, inclusive sob carga/instabilidade real do GSUS (nunca travou, nunca vazou thread, nunca marcou paciente errado). A queda de cobertura da madrugada é limitação de ambiente (GSUS), não defeito do sistema.

---

## 2026-09-03 (1) — Auditoria de prontidão para entrega desta semana

**Pedido do usuário:** "como devemos prosseguir para entregar o produto final ainda essa semana?"

**Método:** 6 agentes independentes em paralelo, cada um relendo código/testes/banco do zero (não recall de sessão) -- requisitos funcionais (RF-01 a RF-30) vs. código real, suíte de testes fresca, itens conscientemente adiados em TASKS.md/DECISIONS.md, estado do banco de produção (read-only, agregado), prontidão de build/instalador, completude da interface vs. especificação.

**Achados principais:**
- Todos os RF-01 a RF-25 e RF-27 confirmados implementados com evidência de código real (arquivo:função), nenhuma violação de escopo (seção 5).
- Suíte: 362/362 passed (unit+e2e), 0 falhas reais. Os 10 erros de integração seguem sendo o mesmo problema local de Chromium (não usado pelo app real, que usa Firefox -- DEC-010), reconfirmado como não-regressão.
- Banco de produção saudável (ver entrada anterior).
- Interface: "funcional mas simples" -- zero placeholder/dado fake, tudo ligado a consulta real. Simplicidade visual é decisão consciente do usuário (adiada em pelo menos 6 entradas anteriores desta mesma sessão), não lacuna funcional.
- Instalador atual não está desatualizado -- já contém o fix do DEC-109.
- Gaps genuínos e conscientes, nenhum bloqueador: cluster RESIL-001 (resiliência de automação GSUS, adiado por escopo/tempo desde DEC-091), RF-26 completo e RF-30-override (a própria especificação já rotula como "mecanismo futuro"), RF-28 histórico de causa de dia vermelho (adiado por privacidade, DEC-101), algumas anotações desatualizadas em TASKS.md (E2E-001 marcado como não feito apesar de testado extensivamente).

**Decisão do usuário após ver o relatório:** disparar mais uma execução real durante o dia (pra fechar cobertura + reconfirmar estabilidade) e **incluir o design/UX da tela nesta entrega** (revertendo o adiamento anterior -- agora que o resto está confirmado funcional, o usuário optou por investir tempo no visual antes de considerar entregue).

**Próxima task:** design/UX da tela principal (Tkinter), com o restante do sistema já certificado.

---

## 2026-09-04 — Segunda passada de design/UX segura (DEC-112)

**Pedido do usuário:** após instalar o executável, começar a melhorar o design sem quebrar nada.

**Resultado:** as três telas foram refinadas no código-fonte. O painel principal agora tem cabeçalho operacional, status/setor/agendamento separados, quatro indicadores críticos em cartões e três complementares em uma faixa compacta. Isso corrige um defeito visual reproduzido na versão anterior: os sete cartões em linha ultrapassavam a janela e ficavam cortados. Os gráficos foram reorganizados para evitar sobreposição, e o censo ganhou contagem de resultados, busca clara, instrução de abertura e estado vazio. Configuração e consulta local agora seguem a mesma linguagem visual.

**Segurança:** toda revisão visual foi feita com `scripts/preview_ui.py`, banco temporário e prontuários fictícios `DEMO-*`. A instalação, o banco real, credenciais e o GSUS não foram acessados.

**Verificação:** 420 testes aprovados (331 unitários não visuais, 32 de UI em processos isolados e 57 de integração/E2E), além de compilação e checagem do diff. O detector de display dos testes Tk foi estabilizado para não criar uma janela extra antes de cada caso.

**Estado de entrega:** código do novo visual pronto e validado. O executável instalado continua sendo a versão anterior; o instalador ainda precisa ser regenerado depois da aprovação visual.

**Próxima task:** aprovação do visual pelo usuário; depois, atualizar versão, gerar e validar o novo instalador. O refinamento CSS do relatório HTML (`REPORT-002`) continua separado.

---

## 2026-09-04 — Visual alinhado à referência em todo o produto (DEC-113)

**Resultado:** a referência enviada pelo usuário foi convertida numa linguagem visual consistente: fundo cinza suave, navegação lateral branca, cartões claros, textos escuros, ação primária laranja, item ativo com trilho laranja, ícones lineares próprios e marca em blocos preto/laranja. O padrão foi aplicado à dashboard, primeira conexão/configuração, consulta local e relatório HTML.

**Revisão por captura:** as quatro prévias foram geradas com dados exclusivamente fictícios. A primeira imagem revelou sobreposição nos gráficos; a composição foi ajustada para uma faixa horizontal e a tabela passou a solicitar menos altura, sem perder acesso ao restante do censo por rolagem. Uma quinta verificação em 1024×700 levou à criação de um modo compacto automático: nessa altura os gráficos viram um resumo textual dos mesmos totais e a tabela continua mostrando uma linha; ao ampliar, os gráficos completos reaparecem. A prévia do relatório revelou estouro no texto "tempo indeterminado"; o cartão ganhou estilo compacto e foi fotografado novamente sem overflow.

**Ícone:** `assets/gsus-auditoria.ico` está ligado à janela, ao executável e ao instalador. `scripts/generate_app_icon.py` permite regenerar o arquivo de forma determinística.

**Verificação:** 420 testes aprovados (331 não visuais, 32 UI isolados e 57 integração/E2E), compilação e checagem de diff aprovadas. Nenhum acesso ao GSUS, banco real, credencial ou instalação durante as prévias.

**Estado de entrega:** código e visual prontos para aprovação. O `setup/GSUSAuditoria-Setup.exe` fornecido pelo usuário continua intacto e contém o visual anterior; gerar uma nova versão do instalador é o próximo passo depois da aprovação das capturas.

---

## 2026-09-04 — Visual novo levado à versão final: instalador 1.1.0 gerado, instalado e verificado (DEC-114)

**Contexto:** o usuário percebeu que a sessão anterior (Codex) tinha aberto a versão de desenvolvimento a partir do código-fonte, não o programa instalado. O log real confirmou: as 7 falhas de atualização de hoje (09:50–09:53) vieram dessa instância dev, que não tem `runtime/llama-server.exe` nem Firefox dentro do repositório; o modelo de IA (4,92 GB) foi baixado para `models/` do repositório; configuração (setor "Auditoria", horário 00:01) e credencial foram salvas na pasta de dados compartilhada; a tarefa agendada não foi registrada (modo dev, por desenho). Banco real: 0 execuções, 0 pacientes -- nenhum acesso ao GSUS chegou a acontecer.

**Pedido do usuário:** levar as alterações visuais (DEC-112/113) para a versão final e funcional do produto, certificando que tudo funciona.

**Feito (worktree `claude/session-continuation-300b65`, com o trabalho não commitado do Codex replicado byte a byte a partir do checkout principal):**
- Firefox (`playwright-browsers/firefox-1465`, 237 MB) e `runtime/` (45 MB) copiados do app instalado para a worktree -- ambos gitignored, mesmos binários já validados em produção.
- Inno Setup 6.7.3 instalado via winget em escopo de usuário (autorizado pelo usuário), em `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`.
- Versão 1.0.0 → 1.1.0 em `installer/gsus-auditoria.iss`. `setup/` adicionado ao `.gitignore`.
- PyInstaller: 29 s, 448 MB, layout plano (sem `_internal/`), ícone novo no `.exe`. Teste de fumaça do `.exe` empacotado com `GSUS_AUDITORIA_DATA_DIR` apontando para pasta temporária (cópia do config, banco vazio): processo vivo após 14 s, 0 linhas de stderr, dashboard com gráficos matplotlib e ícone na janela -- captura conferida.
- Inno Setup: 74 s, `installer/output/GSUSAuditoria-Setup.exe` com 120,0 MB, SHA-256 `E2D0E140807DE7F2D57DCAC5406848F95043DE14380E8B4E5B6C169DD52008C5`.
- Instalado nesta máquina com `/VERYSILENT /NORESTART /SUPPRESSMSGBOXES` (exit 0). Registro do Windows: "GSUS Auditoria versão 1.1.0". Os 1711 arquivos do dist estão na instalação; hashes de `gsus-auditoria.exe`, `python313.dll`, `base_library.zip`, `runtime\llama-server.exe` e `firefox.exe` idênticos ao dist testado. Dados reais em `%LOCALAPPDATA%\GSUSAuditoria` intactos (banco, config, log).
- Modelo `model.gguf` movido do repositório para `%LOCALAPPDATA%\Programs\GSUS Auditoria\models\` -- o disco tinha 5,7 GB livres, insuficiente para copiar ou rebaixar 4,92 GB com segurança. Antes do move, o arquivo foi carregado no `llama-server.exe` desta máquina: `/health` OK em 6 s, `/completion` respondeu, ~13 tokens/s em CPU.
- Suíte nesta worktree antes do build: 331 unitários não visuais + 32 UI (isolados) + 57 integração/E2E = 420 aprovados; `compileall` e `git diff --check` OK.

**Pendente (ações do usuário):** fechar as 2 janelas da versão dev ainda abertas (`pythonw -m app.main`); abrir o app instalado → Configurações → Concluir para registrar a tarefa agendada (só o build empacotado registra); primeira execução real supervisionada com "Atualizar agora"; commit deste ciclo.

**Achado registrado, não corrigido (DIAG-002):** `run_diagnosis.classify_top_level_exception` classifica qualquer `playwright.Error` como `FALHA_GSUS` ("o GSUS não respondeu a tempo"). Hoje isso rotulou 7 vezes um Firefox ausente na máquina como instabilidade do hospital -- exatamente o tipo de erro que o auditor não consegue distinguir sozinho. Ver DEC-114.

---

## 2026-09-04 — Ícone do Codex aplicado também à janela do programa; instalador 1.1.0 regenerado (DEC-115)

**Pedido do usuário:** "mude o ícone do executável e do produto para o gerado pelo Codex".

**Diagnóstico antes de mudar:** o `.exe` e o instalador 1.1.0 JÁ embutiam `assets/gsus-auditoria.ico` (confirmado extraindo o recurso de ícone do `.exe` instalado, do atalho da área de trabalho e do próprio `Setup.exe`). O que não usava o arquivo era a janela Tk: `apply_window_icon` desenhava um PhotoImage de 32 px com fundo branco e quadrados sem cantos arredondados -- diferente do símbolo do `.ico` que aparece no atalho e no Explorer. Segundo fator: após reinstalar, o Explorer pode manter em cache o ícone antigo dos atalhos.

**Feito:** `app/ui/icons.py` passou a aplicar o próprio `.ico` via `root.iconbitmap(default=...)`, resolvido com `config.resolve_app_path("assets/gsus-auditoria.ico")` (dev: pasta do projeto; frozen: pasta do `.exe`), mantendo o PhotoImage como fallback quando o arquivo não existe; o spec do PyInstaller passou a copiar o `.ico` para `assets/` ao lado do `.exe` (`datas`). Novo `tests/unit/test_icons.py` (3 testes: resolução em dev, resolução em frozen, aplicação do `.ico` + fallback num único `tk.Tk()`). Rebuild: PyInstaller 18 s; teste de fumaça do `.exe` empacotado com pasta de dados temporária -- ícone da classe da janela lido via `GetClassLongPtr` e barra de título capturada via `PrintWindow`: símbolo do Codex, fundo transparente, cantos arredondados; 0 linhas de stderr. Inno Setup 108 s, `installer/output/GSUSAuditoria-Setup.exe` 120,0 MB, SHA-256 `00A95DAEEA4049EE30EC27BE1FFD66F9B5CCBAAF01EDE8B6F551460DC2781F71` (substitui o instalador de DEC-114). Reinstalado silenciosamente (exit 0): `.exe` idêntico ao dist testado por hash, `assets/gsus-auditoria.ico` presente na instalação, modelo/Firefox/runtime preservados, 1712 arquivos = dist, dados reais intactos. Cache de ícones do Explorer atualizado com `ie4uinit.exe -show`.

**Verificação:** 331 unitários não visuais + 3 novos de ícone + 32 UI (por arquivo, com reexecução isolada das instabilidades conhecidas do Tk) aprovados; `compileall` e `git diff --check` OK.

**Pendente:** os mesmos itens de DEC-114 (fechar as janelas dev, registrar a tarefa via Configurações → Concluir, primeira execução real, commit). Se algum atalho ainda mostrar o ícone antigo, é cache do Explorer: reiniciar o Explorer ou a sessão do Windows resolve.
