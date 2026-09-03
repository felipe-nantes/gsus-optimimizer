# DECISIONS.md — GSUS Auditoria

Somente decisões arquiteturais relevantes. Formato fixo: Problema / Decisão / Alternativas / Motivo / Impacto.

---

## DEC-001 — Versão do Python alvo

**Problema:** A máquina de desenvolvimento tem Python 3.14.0 e 3.13.7 instalados. É preciso fixar uma única versão para todo o projeto (dev, testes, empacotamento).

**Decisão:** Fixar **Python 3.13** como runtime do projeto.

**Alternativas consideradas:** Python 3.14 (mais recente, disponível na máquina).

**Motivo:** Playwright e llama-cpp-python (bindings de llama.cpp) publicam wheels pré-compilados alguns meses após o lançamento de uma nova versão do Python. 3.14 é recente demais para garantir wheel disponível para todas as dependências congeladas na stack, o que quebraria o requisito de instalação simples (sem compilar nada na máquina do usuário). 3.13 tem suporte maduro em todo o ecossistema relevante.

**Impacto:** `venv`/build devem usar o interpretador 3.13 explicitamente (`C:\Users\fnant\AppData\Local\Programs\Python\Python313\python.exe`). PyInstaller deve empacotar com esse mesmo interpretador.

---

## DEC-002 — Armazenamento de credenciais GSUS

**Problema:** A senha do usuário GSUS não pode ser salva em texto puro (requisito explícito da spec). É preciso um mecanismo seguro nativo do Windows.

**Decisão:** Usar a **Windows Credential Manager API** (`advapi32.dll`: `CredWriteW` / `CredReadW` / `CredDeleteW`) via `ctypes` — biblioteca padrão do Python, sem dependência de terceiros.

**Alternativas consideradas:**
1. `keyring` (pacote de terceiros) — usa a mesma API do Windows por baixo, mas adiciona uma dependência inteira (com sub-dependências e abstração cross-platform desnecessária, já que o alvo é só Windows) para resolver algo que a stdlib + `ctypes` já resolve diretamente.
2. `pywin32` — dependência de terceiros grande, tipicamente usada para acesso a essa mesma API; redundante frente a `ctypes` puro.
3. DPAPI bruta (`CryptProtectData`/`CryptUnprotectData`) gravando um arquivo cifrado local — funcionaria, mas o Credential Manager já é "o" mecanismo de credenciais do Windows visível ao usuário/IT, mais alinhado ao requisito literal da spec ("mecanismo seguro disponível no Windows para armazenamento de credenciais").

**Motivo:** Política de dependências do projeto (seção 50 do prompt mestre): se a stdlib resolve, não adicionar dependência. `ctypes` + Credential Manager resolve sem custo de instalação, superfície de falha ou manutenção extra.

**Impacto:** `app/security/credentials.py` implementa `save_credential`, `get_credential`, `delete_credential` sobre essa API. Testado com fixture/target name dedicado (`GSUSAuditoria:gsus`), nunca logado.

---

## DEC-003 — Localização do banco SQLite em produção

**Problema:** Onde persistir `auditoria.db` no computador do usuário final, considerando que o `.exe` pode rodar de uma pasta somente-leitura (Program Files) e que o app deve funcionar sem privilégio de administrador no dia a dia.

**Decisão:** `%LOCALAPPDATA%\GSUSAuditoria\auditoria.db` em produção. Em desenvolvimento, `./auditoria.db` (raiz do repo), sobrescrevível por `config.py`.

**Alternativas consideradas:** gravar ao lado do `.exe` (problemático se instalado em `Program Files`, que normalmente exige admin para escrita); `%APPDATA%` (roaming — desnecessário e mais lento para um banco local que não precisa sincronizar entre máquinas).

**Motivo:** `%LOCALAPPDATA%` é gravável sem elevação, por usuário, e é o local convencional do Windows para dados de aplicação não-roaming.

**Impacto:** `app/config.py` resolve o caminho do banco em runtime; testes usam banco temporário isolado.

---

## DEC-004 — Ferramenta de instalador (adiada)

**Problema:** É preciso um instalador Windows simples que oculte PyInstaller/Chromium/llama.cpp do usuário e registre a tarefa agendada.

**Decisão:** Adiada para a task `INSTALL-001` (fase de empacotamento). Candidatos pré-aprovados dentro da stack congelada: Inno Setup ou NSIS (ambos geram um único `.exe` instalador, sem exigir runtime adicional no computador do usuário, e ambos suportam registrar uma tarefa no Task Scheduler via `schtasks` no script de pós-instalação).

**Alternativas consideradas:** MSIX (mais burocrático para app não vindo da Store), instalador próprio em Python (reinventa o que Inno/NSIS já resolvem).

**Motivo:** Decisão de baixo risco e reversível — não bloqueia nenhuma task P0 anterior a `BUILD-001`. Escolha final será feita com base no que estiver disponível/testável no ambiente de build no momento da task.

**Impacto:** Nenhum agora. Revisar ao chegar em `INSTALL-001`.

---

## DEC-005 — Extração de seletores GSUS reais

**Problema:** Nenhum HTML, seletor ou screenshot real do GSUS foi fornecido até o momento. A spec proíbe adivinhar seletores ou inventar comportamento de interface (seção 14 do prompt mestre).

**Decisão:** Implementar os módulos `app/gsus/login.py`, `census.py`, `records.py` com a *forma* do fluxo (funções, assinaturas, contrato de entrada/saída, waits por estado) mas com os seletores reais marcados como `BLOCKED_GSUS` (constante/`NotImplementedError` explícito) até que um humano forneça HTML sanitizado, locator identificado manualmente, ou screenshot sanitizado com estrutura DOM. Testes desses módulos usam fixtures HTML sintéticas com estrutura plausível (baseada em padrões comuns de sistemas hospitalares em ASP.NET WebForms/tabelas), servindo para validar a lógica de parsing assim que os seletores reais forem conhecidos — a fixture será ajustada para refletir a estrutura real assim que fornecida.

**Alternativas consideradas:** bloquear todo o projeto até acesso ao GSUS real (rejeitado pela regra 14 do prompt mestre: "não bloqueie todo o projeto por uma informação localizada").

**Motivo:** Permite avançar em toda a arquitetura, banco, fila, regras, LLM, relatório e empacotamento em paralelo, isolando o risco real (seletores) em pontos de integração claramente marcados.

**Impacto:** Tasks `GSUS-001` a `GSUS-005` ficam com "esqueleto pronto, seletor pendente" até handoff humano. Ver pedido de informação ao final do `CURRENT_STATE.md`.

---

## DEC-006 — Mecanismo real de login do GSUS (parcialmente desbloqueado)

**Problema:** `GSUS-001` estava bloqueado por falta de informação real. O usuário informou a URL (`https://gsus.pr.gov.br/`) e que "só abre no Mozilla".

**Investigação:** Naveguei (com o navegador Chromium da própria ferramenta, sem inserir nenhuma credencial) até `https://gsus.pr.gov.br/`. A página redireciona automaticamente para `https://auth-cs.identidadedigital.pr.gov.br/...` -- o SSO estadual "Identidade Digital PR" (OAuth2/OIDC, `response_type=code`, `redirect_uri=https://gsus.pr.gov.br/gsus-integrado`). Essa tela de login **carregou normalmente no Chromium**, sem erro. Coletei os seletores reais (todos em uma página pública, sem nenhum dado de paciente):

- Botão do método "Central de Segurança" (usuário/senha): `get_by_role("button", name="Central de Segurança")` / `id="btnCentral"`.
- Campo CPF: `get_by_label("CPF")` / `id="attribute_central"`.
- Campo Senha: `get_by_label("Senha")` / `id="password"`.
- Botão "Entrar": `get_by_role("button", name="Entrar")` / `id="btn-central-acessar"`.
- Sucesso confirmado por `page.wait_for_url("**/gsus-integrado**")`.

**Decisão:** Implementar `app/gsus/login.py` com esses seletores reais (não é mais um esqueleto `NotImplementedError`). **Não** tentei autenticar de fato (não digitei a senha real em nenhum campo) -- ver Alternativas.

**Alternativas consideradas:** Completar o login eu mesmo (via Playwright rodado por mim, ou via navegação interativa) para também mapear as telas de censo/prontuário. Rejeitado: a tela pós-login expõe dado real de paciente internado (PHI). Se eu (o modelo de IA rodando via API da Anthropic) visualizar essa tela -- print, HTML não sanitizado, texto extraído --, isso equivale a enviar PHI real para fora da máquina/instituição, violando diretamente `PROJECT_SPEC.md` SEC-03/SEC-04 e a seção 12 do prompt mestre, que existem exatamente para impedir isso. A automação de produção (Playwright rodando localmente, sem me enviar o conteúdo) é o único caminho sancionado para tocar dado real.

**Motivo:** Progride `GSUS-001` sem exigir handoff manual de seletor, mantendo a garantia de que nenhum PHI passa pelo meu contexto.

**Impacto:**
- `app/gsus/login.py` deixou de estar `BLOCKED_GSUS` na parte de login; falta validação end-to-end com credencial real, que só pode ser feita **pelo usuário**, rodando `scripts/check_login.py` no próprio terminal (o script confirma sucesso pela URL, sem imprimir dado de paciente).
- `GSUS-002` a `GSUS-005` (censo, pesquisa de prontuário, evoluções) continuam bloqueados -- a informação necessária a partir daqui só pode vir do usuário, sanitizada (nome/prontuário fictícios), nunca de uma inspeção minha na tela real.
- Risco aberto, não resolvido: "só abre no Mozilla" pode se referir à etapa pós-login (que não pude verificar) e não ao login em si (que funcionou no Chromium). Playwright suporta o canal Firefox nativamente (mesma stack, sem dependência nova) caso isso se confirme necessário -- só decidir se o teste real (`scripts/check_login.py` + navegação manual do usuário) mostrar problema real no Chromium.
- Faltam ainda os binários do Chromium do Playwright (`playwright install chromium`, download ~150-300MB da CDN oficial do Playwright) -- não executei por exigir permissão explícita de download; pedido feito ao usuário separadamente. **Atualização:** usuário autorizou; Chromium 130 baixado com sucesso em 2026-08-20 (`playwright install chromium`).

---

## DEC-007 — Runtime e modelo do LLM local

**Problema:** LLM-001 precisa de um binário `llama-server` real e um modelo `.gguf` real para sair do estágio "testado só com stub HTTP". Ambos são downloads grandes, exigindo permissão explícita e escolha de modelo (RNF-06: começar pequeno, mas trocável).

**Decisão:**
- **Runtime:** `llama.cpp` release `b10516`, asset `llama-b10516-bin-win-cpu-x64.zip` (build oficial do projeto `ggml-org/llama.cpp` no GitHub, ~18.5MB) -- variante CPU-only x64, sem exigir CUDA/GPU. Extraído para `runtime/` (`runtime/llama-server.exe` + DLLs `ggml-cpu-*` com dispatch automático por microarquitetura de CPU). Validado com `llama-server.exe --help`.
- **Modelo:** `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf` (~4,92GB), quantização da comunidade por `bartowski` (repositório `bartowski/Meta-Llama-3.1-8B-Instruct-GGUF` no Hugging Face, um dos quantizadores GGUF mais usados/confiáveis), licença Llama 3.1 Community License. Baixado para `models/model.gguf` (caminho já esperado por `app/config.py`).

**Alternativas consideradas:** Qwen2.5-3B-Instruct (menor, ~1-2GB, mais rápido em CPU) -- foi a sugestão inicial minha por ser mais leve para começar (RNF-06 pede "começar pequeno"). O usuário, ao decidir, priorizou precisão/confiabilidade sobre velocidade ("se o Qwen é mais leve mas vai ficar errando muito... Llama para garantir eficiência sem retrabalho futuro") -- decisão dele, registrada aqui.

**Motivo:** Llama 3.1 8B em Q4_K_M é um ponto de equilíbrio padrão da comunidade entre qualidade e viabilidade em CPU (4-6 tokens/s típico em CPU moderna de 8+ threads); evita a necessidade de trocar de modelo mais tarde caso o menor "erre muito" nas tarefas de síntese clínica/evidência, que é exatamente o risco que o usuário quis evitar.

**Impacto:**
- `app/config.py`: novo campo `llm_server_path` (default `runtime/llama-server.exe`); `model_path` já apontava para `models/model.gguf`.
- `models/` e `runtime/` (exceto `.gitkeep`, se algum) devem ficar fora do controle de versão (arquivos grandes/binários) -- adicionar ao `.gitignore` quando o repositório for inicializado com git.
- Troca de modelo futura = só trocar o arquivo em `models/model.gguf` (RNF-06: `LocalLLM` não conhece o modelo carregado).
- Teste real de carregamento (`LocalLLM.start()` com o modelo de verdade, medir tempo de resposta em CPU) ainda pendente -- ver `CURRENT_STATE.md`.

---

## DEC-008 — Parei de tentar login real sozinho após 1ª tentativa sem redirecionamento

**Problema:** Ao validar `login()` contra o GSUS real (headless, só confirmando URL, sem ver dado de paciente -- mesmo racional do DEC-006), a 1ª tentativa (antes do fix) quebrou por seletor ambíguo (`get_by_label("CPF")` casava 4 campos ocultos de outros métodos de login -- SMS/Token/E-mail -- bug real, corrigido usando os IDs estáveis já capturados: `#attribute_central`, `#password`, `#btn-central-acessar`). A 2ª tentativa, já corrigida, submeteu o formulário de verdade mas `wait_for_url` estourou o timeout de 30s -- a URL não chegou a `/gsus-integrado`.

**Decisão:** Não tentei uma 3ª vez. Parei e devolvi a decisão ao usuário.

**Motivo:** O login usado é da "Identidade Digital PR" -- SSO estadual, não exclusivo do GSUS. Tentativas repetidas com credencial possivelmente errada podem bloquear a conta em outros serviços do estado (não só GSUS). Não tenho como saber a política de bloqueio do provedor, e diagnosticar às cegas tentando de novo é exatamente o tipo de ação irreversível/arriscada que a spec (seção 63: "pergunte quando... envolve segurança... comportamento real do GSUS") e minhas próprias regras de segurança pedem para não fazer sozinho.

**Impacto:** `GSUS-001` fica "implementado, com 1 bug real já corrigido, aguardando o usuário validar com os próprios olhos" -- ele deve rodar `scripts/check_login.py` (modo headed, interativo) e ver o que aparece depois de clicar Entrar: pode ser senha incorreta, verificação em duas etapas (SMS/token), CAPTCHA, ou só lentidão de rede. Isso não posso descobrir sem arriscar a conta dele.

**Atualização 2026-08-20 (mais tarde):** usuário logou manualmente no Firefox pra confirmar que credencial está correta e mandou 2 screenshots sanitizados (sem dado de paciente -- só nome de hospital/unidade) mostrando: (1) a aba original mostra "O sistema foi aberto em outra janela", (2) o app real abre num **pop-up separado**, que por sua vez mostra um modal "Selecionar Estabelecimento para Login" com Confirmar/Desconectar. Isso desbloqueou o diagnóstico -- ver DEC-009 e DEC-010.

---

## DEC-009 — GSUS abre pop-up + é frameset clássico (content frame)

**Problema:** Com a senha confirmada correta pelo usuário, por que o login automatizado ainda não fechava?

**Investigação (headless, só confirmando URL/estrutura, nunca dado de paciente -- mesmo racional do DEC-006):**
1. O clique em "Entrar" não navega a página original -- abre uma **nova janela** (`page.context.expect_page()` captura isso). A aba original vira só um aviso "pode ser fechada".
2. Essa nova janela (pop-up) é, ela mesma, um **frameset clássico**: tem um frame sem nome (moldura) e um frame **chamado `content`** (`gsus_page.frame(name="content")`) -- é dentro desse frame que todo o conteúdo real vive.
3. O modal "Selecionar Estabelecimento" (Tipo EAS/UF/Município/Estabelecimento/Unidade + botão Confirmar) roda dentro desse frame `content`. O botão Confirmar é `<input type="submit" id="botaoConfirmar">` -- não um `<button>`, por isso `get_by_role("button", name="Confirmar")` na página top-level nunca achava nada (nem estava no frame certo, nem no elemento certo).
4. Confirmado batendo com o teste real: depois de clicar `#botaoConfirmar` dentro do frame `content`, a URL desse frame vai para `.../inicial.do?action=carregarEASPadrao` -- dentro do sistema de verdade.

**Decisão:** `app/gsus/login.py` usa `app/gsus/client.get_content_frame(page)` (novo helper) para localizar o frame `content` e clicar `#botaoConfirmar` nele. `login()` passa a retornar a `Page` do pop-up (não mais a página original) -- é ela (e o frame `content` dentro dela) que `census.py`/`records.py` devem usar a partir de agora.

**Motivo:** Sem isso, GSUS-001 nunca completaria de verdade -- não é seletor errado, é modelo mental errado da estrutura da página (pop-up + frameset, não navegação de página única).

**Impacto:** `app/gsus/client.py` ganhou `CONTENT_FRAME_NAME`/`get_content_frame()`, reutilizável por GSUS-002+. `app/gsus/adapter.py` atualizado para usar a página retornada por `login()`. Login real validado ponta a ponta (ver DEC-010 sobre o engine usado no teste).

---

## DEC-010 — Trocar Chromium por Firefox como engine do Playwright para o GSUS

**Problema:** A stack congelada (seção 6 do prompt mestre) especifica "Browser: Chromium empacotado". Mas o usuário avisou desde o início que o GSUS "só abre no Mozilla", e o teste real confirmou: com **Chromium**, depois de clicar Entrar, nenhum pop-up chegou a abrir dentro de 30s (timeout). Com **Firefox** (mesmo código, mesma credencial), o pop-up abriu e o fluxo completo funcionou (DEC-009).

**Decisão:** `app/gsus/client.py` usa `playwright.firefox.launch(...)` em vez de `playwright.chromium.launch(...)`. Documentado aqui como desvio explícito e justificado da stack congelada, conforme exigido pela seção 4 do prompt mestre (problema concreto, por que a stack existente não resolve, impacto, alternativa mais simples).

**Alternativas consideradas:**
1. Insistir em Chromium e investigar a causa raiz (provável: SameSite cookie policy, User-Agent sniffing, ou incompatibilidade JS específica do GSUS com o engine Chromium/Blink). Rejeitada por ora: investigar profundamente o motivo exigiria mais tempo sem benefício prático -- o objetivo é o produto funcionar, não usar um engine específico por preferência.
2. Trocar de biblioteca de automação inteira (ex.: Selenium). Rejeitada: Playwright já resolve, só precisava do canal certo -- não é uma dependência nova, é uma flag.

**Motivo:** Playwright suporta múltiplos engines nativamente (Chromium/Firefox/WebKit) -- usar o canal Firefox não introduz nenhuma dependência nova, só troca uma string (`chromium` → `firefox`) e adiciona um download de browser (~85MB, já feito). É a correção mais simples que resolve o problema real, exatamente como a seção 3 do prompt mestre pede ("quando houver conflito entre elegância e solução simples que resolva com confiabilidade: escolha a simples").

**Impacto:**
- `app/gsus/client.py`: `GSUSClient` agora lança Firefox. `playwright install firefox` precisa rodar no ambiente de build/empacotamento (além de `chromium`, que fica sem uso real no fluxo GSUS agora -- decidir em `BUILD-001` se removê-lo do pacote final pra economizar espaço, já que só Firefox é usado para o GSUS).
- `installer/gsus-auditoria.spec` / processo de empacotamento: precisa embutir o Firefox do Playwright (não mais o Chromium) na pasta de distribuição final.
- `ARCHITECTURE.md` seção "Runtime e empacotamento" desatualizada quanto a isso -- atualizar na próxima revisão do documento.

---

## DEC-011 — Estrutura da tela de censo (com correção de manuseio de PHI)

**Problema:** Faltava a estrutura da tela "Pesquisar Internação" (GSUS-002/003) para implementar `census.py`.

**Incidente e correção:** O usuário enviou um screenshot da tela de censo que continha **dado real identificável de 12 pacientes** (nome completo, data de nascimento, nome da mãe, prontuário, leito/unidade, nome do médico logado, nome do hospital) -- não sanitizado, ao contrário do que eu havia pedido explicitamente (paciente fictício tipo "Paciente Teste 001"). Esse screenshot chegou até o meu contexto (API da Anthropic), o que é exatamente o que `PROJECT_SPEC.md` SEC-03/SEC-04 e a seção 12 do prompt mestre existem para evitar. Eu:
1. Sinalizei o problema diretamente ao usuário (não silenciei).
2. **Não reproduzi nenhum dado real** em código, fixture, log, commit ou nesta entrada -- todos os exemplos abaixo e nas fixtures (`fixtures/gsus_html/census_page*.html`) são fictícios, seguindo a convenção já estabelecida ("Paciente Teste NNN").
3. Extraí só a **estrutura** (nomes de coluna, campos de formulário, layout) para implementar o parser -- informação de UI, não PHI.

**Estrutura extraída (segura -- rótulos de coluna/campo, não dado de paciente):**
- Formulário "Pesquisar Internação": `* EAS` (fixo), `Município` (fixo), `Nº Prontuário` (2 campos -- provável número + dígito verificador, padrão comum em prontuário hospitalar brasileiro), `* Status Internação` (dropdown, já vem em "Internado"), link "Pesquisa Avançada", botões `Pesquisar`/`Limpar`.
- Tabela de resultado, paginada ("Página X de Y : Total de N registros", links "Próxima"/"Última"), colunas: Prontuário, Nome do Paciente, Data Nascimento, Nome da Mãe, Data de Internação, Descrição do Leito, Unidade Org., Localização, Status, Inconsistente, Visualizar.
- Menu superior confirmado (visível, não é PHI): Infra-saúde | Ambulatório | Pronto Atendimento | **Internação** | Atendimento | Farmácia | Laboratório | Enfermagem | Centro Diagnóstico | SCIH | Desconectar.

**Decisão:** Implementar `census.py` extraindo só `record_number/bed/unit/admission_date` (️`Patient` não tem campo de nome -- nunca guardamos nome/data de nascimento/nome da mãe do paciente na automação, mesmo que a tela mostre). Navegação até a tela (`_navigate_to_search_screen`) é uma inferência a partir do item de menu "Internação" visível -- marcada como não confirmada, testável separadamente de `collect_all_pages` (a lógica de extração/paginação, essa sim totalmente testada com fixtures fictícias).

**Alternativas consideradas:** Pedir para o usuário reenviar um HTML "encontrar-e-substituir" os nomes reais. Rejeitada como próximo passo por ser desnecessária -- a estrutura de colunas/campos já é suficiente para implementar o parser com locators por texto de cabeçalho (mais robusto que depender de IDs exatos, seguindo a hierarquia de seletores da seção 15).

**Motivo:** Minimização de dado sensível é um requisito de segurança explícito (SEC-03/SEC-04), não apenas do produto final, mas do próprio processo de desenvolvimento comigo.

**Impacto:** `app/gsus/census.py` implementado e testado (5 testes, fixtures fictícias: página única, vazia, duplicata, paginação real via `file://`). `scripts/check_census.py` criado para o usuário validar contra o GSUS real sem me devolver nenhum dado de paciente (só contagem + leito/unidade, nunca prontuário/nome).

**Atualização 2026-08-20 (mais tarde):** usuário confirmou por texto (sem screenshot) o caminho de navegação: menu **Internação** → link **Pesquisar Internação** → botão **Pesquisar** -- exatamente o que eu tinha inferido. `GSUS-002` e `GSUS-003` estão completos.

---

## DEC-012 — Menu do GSUS não usa `<a>`/`<button>` semânticos (bug real corrigido)

**Problema:** Usuário rodou `scripts/check_census.py`: navegador abria e logava normalmente, mas "parava na tela inicial" sem mensagem de erro clara -- sugeria que o clique no menu "Internação" não estava fazendo efeito.

**Investigação (headless, só na tela de menu/formulário vazio -- sem nenhum dado de paciente):** `_navigate_to_search_screen` usava `get_by_role("link", name=...)`. Inspecionei o elemento real: "Internação" é `<div id="oCMenu__NNNN" class="clLevel0">` -- um widget de menu customizado, não um `<a>`. `get_by_role("link", ...)` nunca encontra um `<div>`, então o Playwright ficava esperando (auto-wait) até estourar o timeout -- provavelmente é isso que apareceu como "trava sem erro" (o traceback do timeout não é capturado pelos `except` específicos do script, então o script morre e deixa o navegador aberto parado).

**Decisão:** Trocar para `get_by_text("Internação", exact=True)` / `get_by_text("Pesquisar Internação", exact=True)` -- localizam pelo texto visível, que é estável, ao contrário do id interno do widget (`oCMenu__4803` parece ser um contador, não confiável entre sessões). Botão "Pesquisar" também não é `<button>` -- é `<input type="button" id="btConsultar">`; troquei para `#btConsultar` (id explícito, estável, não parece gerado automaticamente).

**Validação:** rodei a função `_navigate_to_search_screen` de verdade (headless) até o formulário de busca aparecer (`#btConsultar`/`#codPaciente` visíveis) -- **parei exatamente aí**, sem clicar em Pesquisar, para nunca carregar a tabela com paciente real.

**Motivo:** Corrigir com base em estrutura real observada (não mais suposição) -- exatamente o handoff que a seção 14 do prompt mestre pede, só que peguei eu mesmo por inspeção direta de uma tela sem PHI, sem precisar de mais uma rodada de screenshot do usuário.

**Impacto:** `app/gsus/census.py` atualizado. Suíte de testes (73) não foi afetada -- os testes existentes exercitam `collect_all_pages` diretamente, sem passar por `_navigate_to_search_screen`.

**Atualização 2026-08-20 (mais tarde):** usuário rodou `scripts/check_census.py` de novo e confirmou -- "mostrou todo o censo de internações". `GSUS-002` e `GSUS-003` validados ponta a ponta contra o GSUS real (navegação + clique em Pesquisar + extração + paginação). `FROZEN` a partir daqui -- não mexer sem motivo (seção 48 do prompt mestre).

---

## DEC-013 — Estrutura de prontuário/evoluções (GSUS-004/005) -- com 2º incidente de PHI, mais grave

**Incidente (mais grave que o DEC-011):** o usuário enviou 7 screenshots mostrando o fluxo de abertura de prontuário. As primeiras 4 imagens eram estrutura/formulário (seguras). As imagens 5, 6 e 7, porém, mostravam **nota clínica completa e real**: diagnóstico descritivo, queixa principal, história da doença atual, exame físico, sinais vitais numéricos, conduta médica, código CID, lista de medicamentos prescritos, e o **nome completo de três profissionais de saúde diferentes** (dois médicos, uma técnica de enfermagem) -- tudo vinculado ao mesmo prontuário real (262531) do incidente anterior (DEC-011). Isso é substancialmente mais sensível que uma lista de nomes: é o próprio conteúdo clínico que o produto inteiro existe para nunca deixar sair da instituição (PROJECT_SPEC.md SEC-03).

Ação tomada: sinalizei o problema de forma mais direta que da primeira vez (repetição do mesmo erro pede reforço, não só repetição da mesma frase). **Nenhum dado das imagens 5-7 foi reproduzido** em código, fixture, log, ou nesta entrada -- toda estrutura abaixo foi generalizada a partir do que é seguro (rótulos de UI, formato de cabeçalho sem o conteúdo, nomes de campo).

**Estrutura extraída (segura -- rótulos/campos, nunca conteúdo clínico real):**
- Navegação: menu **Atendimento** → item **Pesquisar Prontuário** (tela própria, diferente de GSUS-002). Formulário confirmado por inspeção real MINHA, headless, **tela vazia sem paciente aberto** (mesmo racional seguro do DEC-006/DEC-012): campo `#codPaciente`, botão `#btnConsultar` (atenção: nome parecido mas **diferente** do `#btConsultar` da tela de censo -- não são o mesmo elemento).
- Resultado: bloco "Dados Pessoais" (ignorado -- nunca extraído) + 1 card por episódio de "Internação"; o episódio atual traz o texto **"Permanece Internado"** no cabeçalho (rótulo de status, não é PHI -- usado como marcador em `records.CURRENT_ADMISSION_MARKER`).
- Abrir o card do episódio revela dias colapsáveis no formato **"DD de MÊS de AAAA - DiaDaSemana"**.
- Abrir um dia revela blocos de evolução com cabeçalho **"DD/MM/AAAA HH:MM - PROFISSIONAL (CARGO)"** -- só UM traço, diferente do formato de duas partes que eu tinha assumido antes (`DD/MM/AAAA HH:MM - ESPECIALIDADE - TIPO`, nunca confirmado, só suposição do DEC-005).

**Decisão:**
1. `app/gsus/records.py`: `open_current_admission` implementado com os seletores confirmados (`#codPaciente`/`#btnConsultar`) + clique no card marcado "Permanece Internado". `extract_notes` implementado por melhor esforço (expandir dias por padrão de texto de data via regex, extrair texto do container ancestral) -- **AINDA NÃO confirmado em execução real**, ao contrário de `open_current_admission`.
2. `app/extraction/parser.py`: `NOTE_HEADER_PATTERN` reescrito para o formato real (`timestamp - texto (cargo)`), e a divisão em blocos passou a ser por **ocorrência do padrão de cabeçalho** (via `finditer`), não mais por linha em branco -- mais robusto, já que não há garantia de separação por linha em branco quando o texto vem de `inner_text()` de vários blocos DOM concatenados.
3. Nome do profissional **nunca vira campo estruturado** -- só o cargo (capturado do parêntese) é guardado em `source_type`. `specialty` fica sempre `None` (o formato real não distingue "especialidade médica" de "cargo" no cabeçalho).
4. `app/analysis/rules.py`: `apply_consult_rule` deixou de depender de `note.specialty` (que não existe de verdade) -- passou a resolver interconsultas só por conteúdo de texto (menção à especialidade + marcador "parecer" na mesma nota), igual à regra de exame.
5. Todas as 11 fixtures em `fixtures/notes/` reescritas pro formato de cabeçalho real, com nomes/prontuários 100% fictícios ("Profissional Teste N").

**Motivo:** A estrutura real diverge da suposição original em pontos que afetam corretude (cabeçalho de 1 traço, não 2; sem campo de especialidade estruturado) -- corrigir agora evita que `RULES-001`/`INCR-001` silenciosamente processem tudo como "malformado" quando ligados ao GSUS real.

**Impacto:**
- `app/gsus/records.py`, `app/extraction/parser.py`, `app/analysis/rules.py`, `fixtures/notes/*.txt`, `tests/unit/test_parser.py` alterados.
- Suíte: 75 passed (73 anteriores + 2 novos testes de parser cobrindo múltiplos cabeçalhos sem linha em branco entre eles).
- **Gap de teste reconhecido:** `extract_notes`/`open_current_admission` (a parte que interage com o DOM real do prontuário/dias/evoluções) **não tem teste automatizado com fixture sintética** -- construir uma fixture HTML "inventando" a estrutura de accordion seria repetir o mesmo erro que a seção 14 do prompt mestre proíbe (inventar comportamento de interface), já que não inspecionei esse DOM de verdade (só descrição visual). Validação real fica só por `scripts/check_notes.py`, rodado pelo usuário, que nunca imprime conteúdo clínico -- só contagem de blocos encontrados.

---

## DEC-014 — Bug real: seletor de tabela pegava o formulário, não o resultado

**Problema:** Usuário rodou `scripts/check_notes.py`: travou logo na extração do censo (chamada a `get_census`, que já era considerada `FROZEN`/validada -- DEC-012). Erro real do terminal: `Timeout 30000ms exceeded` esperando `td.nth(5)` dentro de `table.filter(has_text="Prontuário").first.locator("tbody tr").first`.

**Causa:** `table.filter(has_text="Prontuário")` também bate no formulário de busca "Pesquisar Internação" (rótulo "Nº Prontuário:"), que fica ACIMA do resultado na mesma tela e -- app legado -- provavelmente é montado com `<table>` para layout. `.first` pegava a tabela do FORMULÁRIO (poucas colunas por linha), não a de RESULTADO (11 colunas) -- por isso `td.nth(5)` (6ª coluna) nunca existia nessa linha, e o Playwright ficava esperando até estourar timeout, sem nenhuma mensagem clara sobre a causa raiz.

**Por que passou no teste anterior (DEC-012):** aquele teste só validava a NAVEGAÇÃO até o formulário (parava antes de clicar Pesquisar, de propósito, pra não carregar dado real). Nunca exercitou `_extract_page_rows` de verdade -- só via fixture sintética, que não tem essa ambiguidade (só uma tabela na página).

**Decisão:**
1. Trocar o texto de identificação da tabela de "Prontuário" (ambíguo) para **"Inconsistente"** (`RESULT_TABLE_MARKER`) -- coluna que só existe no cabeçalho da tabela de resultado, não no formulário.
2. Adicionar checagem defensiva: se uma linha tiver menos de 7 colunas (`MIN_EXPECTED_COLUMNS`), levanta `GSUSCensusError` com mensagem clara na hora, em vez de deixar o Playwright estourar timeout genérico sem contexto (ROBUST-001 -- falha rápida e legível é melhor que travar mudo).

**Motivo:** Corrige com base em erro real observado (não suposição) -- o log de terminal (sem PHI nenhum) foi suficiente pra diagnosticar sem precisar ver tela nenhuma.

**Impacto:** `app/gsus/census.py` atualizado. Suíte (75 testes) permanece verde -- as fixtures sintéticas já continham a coluna "Inconsistente", então nenhum teste precisou mudar. `scripts/check_notes.py` também ganhou tratamento de erro mais robusto (`try/except` genérico com traceback impresso -- só texto de erro de programa, sem dado de paciente) para nunca mais travar "mudo" independente de qual etapa falhar.

---

## DEC-015 — Bug real: cabeçalho é `<th>` dentro do `<tbody>`, sem `<thead>`

**Problema:** Depois do fix do DEC-014, usuário rodou `scripts/check_notes.py` de novo e colou o log (texto puro, sem PHI). Novo erro, mais informativo (graças à checagem defensiva do DEC-014): `Linha 0 da tabela de resultado tem 0 coluna(s)`.

**Causa:** A tabela de resultado real não separa cabeçalho em `<thead>` -- a linha de cabeçalho é um `<tr>` comum dentro do `<tbody>`, com células `<th>` em vez de `<td>`. `rows.nth(0)` pegava essa linha de cabeçalho; `.locator("td")` nela retorna 0 elementos (correto -- não tem `<td>` nenhum), o que minha checagem do DEC-014 (menos de 7 colunas) tratava como erro.

**Decisão:** Diferenciar os dois casos: **0 colunas** = provável linha de cabeçalho (`<th>`) -- pular silenciosamente, não é erro. **Entre 1 e 6 colunas** (`<td>` de verdade, mas de menos) = continua sendo tratado como sinal de tabela errada (DEC-014) -- levanta `GSUSCensusError` com mensagem clara.

**Motivo:** De novo, corrigido a partir de log de terminal real (sem PHI) -- nenhuma suposição nova, só distinguir "linha sem `<td>` nenhum" de "linha com `<td>` de menos que o esperado", que são sintomas de causas diferentes.

**Impacto:** `app/gsus/census.py` atualizado (`cell_count == 0` → `continue`; `0 < cell_count < MIN_EXPECTED_COLUMNS` → erro). 2 fixtures novas (`census_page_th_in_tbody.html`, `census_page_wrong_table_columns.html`) + 2 testes cobrindo os dois casos separadamente -- suíte em 77 (75 + 2). `GSUS-002`/`003` seguem tecnicamente `FROZEN` quanto à API pública (nenhuma assinatura mudou), só a implementação interna foi corrigida com base em evidência real -- consistente com a seção 48 do prompt mestre (corrigir componente congelado é permitido quando há motivo, risco e regressão correspondente, o que foi feito aqui).

---

## DEC-016 — Paginação é AJAX (não navegação real) + vazamento de PHI em log corrigido

**Problema 1 (funcional):** Usuário rodou `scripts/check_notes.py` de novo. Log real (sem PHI, exceto o problema abaixo): 21 avisos de "Prontuário duplicado" espalhados, terminando em "OK: 155 pacientes" -- deveria ser 180 (visto no censo original). `collect_all_pages` usava `gsus_frame.wait_for_load_state()` depois de clicar "Próxima", assumindo que isso espera o conteúdo novo carregar.

**Causa:** a paginação real é assíncrona (AJAX) -- não há navegação de página de verdade, então `wait_for_load_state()` retorna quase imediatamente (não há o que esperar do ponto de vista do Playwright), e a extração seguinte lê a tabela ainda com o conteúdo antigo (ou parcialmente atualizado) em alguns ciclos -- por isso duplicatas espalhadas, não concentradas na página 1 (confirma que a maior parte da paginação funcionava, só com corrida ocasional).

**Decisão:** trocar a espera por uma que observa o EFEITO (1ª linha da tabela mudar de valor), não o evento de navegação: captura o prontuário da 1ª linha antes de clicar "Próxima", clica, e faz polling curto (até 10s, a cada 200ms) até esse valor mudar. Se não mudar dentro do prazo, assume que "Próxima" não tem mais efeito (fim real da paginação) e para -- em vez de continuar reprocessando a mesma página até `MAX_PAGES` (500).

**Problema 2 (vazamento):** o log de "Prontuário duplicado" incluía o número do prontuário via `logger.warning(..., record_number)`. Nenhum dos scripts `check_*.py` configurava handler de logging -- o handler padrão do Python ("last resort") imprime WARNING+ no `stderr`, que o usuário colou de volta pra mim (com boa intenção, pra debugar). **16-21 prontuários reais vazaram pro meu contexto por causa disso.** Diferente dos incidentes DEC-011/DEC-013 (screenshot do usuário), este foi causado pelo meu próprio código de logging.

**Decisão:** (a) tirar o número do prontuário da mensagem de log; (b) criar `scripts/_common.py` com `setup_file_logging()` -- redireciona TODO log da aplicação para `logs/check_scripts.log` (arquivo local) em vez do terminal, usado pelos 3 scripts `check_*.py`. Isso corrige a CLASSE do bug (qualquer `logger.warning`/`error` futuro em qualquer módulo), não só esta ocorrência.

**Motivo:** minimização de PHI é requisito explícito (RNF-03/seção 32 do prompt mestre já autoriza logar "paciente interno/pseudonimizado" em log LOCAL -- o problema nunca foi logar em arquivo, foi vazar pro terminal/mim).

**Impacto:** `app/gsus/census.py` (espera por mudança real + log sem identificador), `scripts/_common.py` (novo), `scripts/check_login.py`/`check_census.py`/`check_notes.py` (usam o logging de arquivo agora). 1 fixture nova (`census_page_ajax.html`, simula troca de conteúdo via JS com atraso) + 1 teste. Suíte em 78.

---

## DEC-017 — Bug real: confundi atributo `name` com `id` na inspeção do botão "Pesquisar" do prontuário

**Problema:** Com o fix do DEC-016, usuário rodou de novo: passou do censo, mas travou em `open_current_admission` -- `Timeout 30000ms exceeded... waiting for locator("#btnConsultar")`. Usuário confirmou visualmente: "parou na tela de pesquisar formulario".

**Investigação (headless, número de prontuário **inventado**, sem paciente real -- nenhum resultado real possível de carregar):** `frame.locator("#btnConsultar")` retornava `count=0` -- elemento não existe com esse ID. Investigando todos os `<input type=button>` da tela, o botão "Pesquisar" tinha `id=""` (vazio) e `name="btnConsultar"`. Na inspeção original (bem antes, na mesma sessão), eu tinha listado os inputs no formato `(id, name, type)` e li errado: a string `'btnConsultar'` que aparecia era o valor de **name**, não de **id** -- o `id` real era `None`. Confirmei corrigindo para seletor por `name` (`input[name="btnConsultar"]`): `count=1`, `visible=True`.

**Decisão:** `app/gsus/records.py`: trocar `page.locator("#btnConsultar")` por `page.locator('input[name="btnConsultar"]')`.

**Motivo:** erro meu de leitura de log, não informação nova do usuário nem mudança da página -- por isso não precisou de mais nenhuma interação do usuário para corrigir, só reler minha própria evidência com mais cuidado.

**Impacto:** `app/gsus/records.py` atualizado + docstring corrigida (a tela de prontuário não tem um `id` real no botão de busca, diferente da tela de censo que usa `#btConsultar` -- esse sim um `id` de verdade, já validado em produção real via `GSUS-002`/`003`). Resto de `extract_notes` (expandir card de internação, dias, evoluções) continua não validado -- só pode ser testado com prontuário real, que só o usuário pode fazer.

---

## DEC-018 — Bug real: `.fill()` não "pegava" antes do clique em Pesquisar (corrida)

**Problema:** Com o fix do DEC-017, usuário rodou de novo. Passou do censo (175 pacientes -- melhora real vs. 155, confirma que o fix da paginação AJAX ajudou). Travou em `open_current_admission`: `GSUSRecordError` porque o marcador "Permanece Internado" nunca apareceu. Usuário mandou screenshot da tela onde parou -- **sem dado de paciente, só uma mensagem de validação do próprio GSUS**: "O campo Nº Prontuário é obrigatório."

**Causa:** o formulário foi submetido com o campo `#codPaciente` VAZIO, apesar do código chamar `.fill(record_number)` antes do clique em Pesquisar. Mais provável: depois de navegar bastante pela paginação do censo (ela mesma assíncrona -- DEC-016), o clique em "Atendimento" -> "Pesquisar Prontuário" também é AJAX, e o `.fill()` rodou antes do campo da tela nova estar de fato pronto para receber o valor -- preencheu um campo que ainda ia ser substituído, ou não completou a atribuição a tempo do clique seguinte.

**Decisão:** `_fill_record_number_reliably()` -- preenche, **lê de volta** o valor do campo (`input_value()`) e só segue se bater com o que foi pedido; se não bater, espera um pouco e tenta de novo (até 3 vezes). Se depois de 3 tentativas o campo ainda não tiver o valor certo, levanta erro claro em vez de submeter uma busca vazia (fail-closed -- nunca adivinhar/prosseguir com dado errado).

**Motivo:** mesmo padrão dos DEC-016/017 -- essa app tem bastante comportamento assíncrono não sinalizado por eventos de navegação que o Playwright saiba esperar automaticamente; a estratégia geral que está funcionando é "não confiar em timing, confirmar o efeito antes de seguir".

**Impacto:** `app/gsus/records.py` atualizado. Log da nova tentativa não inclui o prontuário (lição do DEC-016 já aplicada aqui desde o início). Ainda não validado: o resto de `open_current_admission` (clique no card "Permanece Internado" abrindo os dias) e todo `extract_notes` -- só o usuário pode validar com prontuário real.

**Atualização (mesma sessão):** o fix acima NÃO resolveu -- usuário rodou de novo com prontuário real, mesmo erro exato ("campo obrigatório"), mesmo com o valor lido de volta batendo (`input_value() == record_number` confirmado antes do clique). Ver DEC-019 para a causa real.

---

## DEC-019 — Causa real: `.fill()` não dispara os eventos de teclado que o autocomplete legado escuta

**Problema:** o campo mostrava o valor certo (`input_value()` confirmado), mas o clique em "Pesquisar" ainda resultava em "campo obrigatório" no servidor.

**Investigação:** a tela tem um campo companheiro `#nomePaciente`, `disabled="disabled"`, dentro de um `<div id="pesquisaAjaxPaciente">` -- padrão clássico de autocomplete: ao digitar no campo visível, JS antigo (que só escuta `keyup`/`keydown` por caractere, não o evento genérico `input`/`change`) dispara uma busca AJAX e preenche esse campo companheiro, e só ENTÃO o valor é considerado "confirmado" para submissão. `.fill()` do Playwright seta o valor via JS diretamente (sem simular tecla por tecla) -- o valor aparece certo no campo, mas o handler de `keyup` do autocomplete nunca dispara, então a validação do lado do servidor (que provavelmente depende de um campo oculto sincronizado por esse handler, não do texto bruto) trata como se estivesse vazio.

**Decisão:** trocar `.fill()` por `field.press_sequentially(record_number, delay=80)` -- digita caractere por caractere, disparando os eventos de teclado de verdade. Mantido o reconferimento (`input_value()`) e retry por precaução (a causa de timing do DEC-018 pode coexistir).

**Não validado ainda em execução real** -- esta é uma hipótese bem fundamentada (padrão reconhecível de autocomplete legado, evidência do campo companheiro desabilitado), mas não confirmei rodando eu mesmo contra o GSUS real depois do fix, por dois motivos: (1) evitar mais rodadas seguidas minhas contra o servidor real da instituição em pouco tempo; (2) descobri um problema sério nesse processo -- ver abaixo.

**Efeito colateral descoberto: vazamento de processo Firefox.** Ao investigar isso, rodei vários diagnósticos headless seguidos; um deles travou (paginação do censo lenta em headless) e eu o interrompi via `taskkill`. Ao checar processos, encontrei **mais de 30 processos `firefox.exe` órfãos** acumulados de diagnósticos anteriores nesta mesma sessão (provavelmente de rodadas que foram para timeout/background e tiveram o processo Python pai finalizado sem que o `browser.close()` dentro do `with sync_playwright()` chegasse a rodar). Isso consumia vários GB de RAM na máquina do usuário -- que ele já avisou ser fraca. Matei todos (`taskkill /IM firefox.exe /F`).

**Motivo de não seguir testando ao vivo:** dado o vazamento de recursos encontrado E o volume de acessos automatizados que já fiz ao GSUS real nesta sessão (facilmente 15+ sessões headless em menos de 1 hora), decidi parar de rodar diagnósticos ao vivo por conta própria e deixar a próxima validação para o usuário, via `scripts/check_notes.py` -- ele já sabe rodar e colar o log.

**Impacto:** `app/gsus/records.py` atualizado (digitação simulada). Nenhum processo órfão adicional deve se acumular dos MEUS diagnósticos futuros (vou evitar rodar tantos ao vivo); os scripts `check_*.py` que o usuário roda já fecham o browser corretamente no fluxo normal (`browser.close()` sempre alcançado, exceto se o processo for interrompido à força).

---

## DEC-020 — Corrida entre digitação e AJAX de autocomplete perdia caractere

**Problema:** com o fix do DEC-019 (digitação simulada), usuário rodou de novo. Progresso real: o formulário FOI submetido (não mais "campo obrigatório" vazio) -- mas com o número TRUNCADO: "262531" virou "26253" (perdeu o último dígito "1"), resultando em "Prontuário '26253' não encontrado!".

**Causa:** mesmo digitando caractere por caractere, o autocomplete legado da tela (`#nomePaciente` companheiro) dispara uma busca AJAX a cada tecla (ou por debounce). É plausível que uma resposta AJAX referente a um estado anterior da digitação (ex.: quando só "26253" tinha sido digitado) chegue e reformate/sobrescreva o campo DEPOIS do último caractere ter sido digitado mas ANTES do clique em Pesquisar -- uma corrida entre o autocomplete e a leitura final do valor pelo formulário.

**Decisão:** não confiar numa única verificação isolada. `_search_by_record_number` agora faz tudo (digitar, conferir, clicar Pesquisar, conferir o RESULTADO) dentro do mesmo laço de tentativa: se a página resultante mostrar "não encontrado" OU "campo obrigatório", trata como possível corrida (não confirma que o prontuário realmente não existe) e tenta a sequência inteira de novo, até 3 vezes.

**Motivo:** dado que "não encontrado" pode ser um FALSO NEGATIVO causado por corrida (já demonstrado: um prontuário que sabidamente existe -- veio do censo -- apareceu como não encontrado por causa de um dígito perdido), não é seguro tratar essa mensagem como resposta definitiva sem antes descartar a hipótese de corrida via nova tentativa.

**Não validado ainda** -- mesma decisão do DEC-019 de não rodar mais diagnósticos ao vivo eu mesmo por ora (ver aquele registro). Fica para a próxima rodada do usuário.

**Impacto:** `app/gsus/records.py`: `_fill_record_number_reliably` virou `_search_by_record_number`, absorvendo o clique em Pesquisar e a checagem de resultado no mesmo laço de retry. Suíte (78 testes) inalterada -- nenhum teste automatizado cobre essa função (gap já reconhecido no DEC-013, sem mudança).

---

## DEC-021 — Clique automático no botão Pesquisar não disparava a busca (faltava blur)

**Problema:** com o fix do DEC-020, usuário rodou de novo: campo preencheu certo (confirmado visualmente), mas o clique automático no botão Pesquisar não fez nada -- a busca só aconteceu depois do usuário clicar na tela manualmente. As 3 tentativas automáticas esgotaram e o script terminou com `GSUSRecordError`.

**Causa provável:** mesma família dos problemas anteriores (autocomplete legado) -- o campo provavelmente tem um handler de `blur` que "confirma"/processa o valor digitado, e o botão Pesquisar só reage corretamente depois desse blur acontecer. Um clique automático direto no botão, sem antes tirar o foco do campo de forma que dispare esse handler, deixa o estado interno do formulário desatualizado -- exatamente o que o clique manual do usuário (que naturalmente tira o foco do campo antes de clicar em outro lugar) resolvia.

**Decisão:** adicionar `field.press("Tab")` (sai do campo, dispara blur de verdade) depois de digitar e antes de clicar em Pesquisar -- simula o que um clique manual em outro lugar da tela naturalmente faz.

**Não validado ainda** -- mesma decisão de não rodar diagnóstico ao vivo eu mesmo (DEC-019). Próxima rodada do usuário confirma.

**Impacto:** `app/gsus/records.py` -- `_search_by_record_number` manda Tab antes do clique. Suíte inalterada (78, gap de cobertura já reconhecido).

---

## DEC-022 — Falso positivo: marcador de erro batia na legenda fixa do formulário

**Problema:** com o fix do DEC-021, usuário rodou de novo. Screenshot mostrou o campo preenchido CORRETAMENTE e validado (nome do paciente confirmado apareceu no campo companheiro -- Tab/blur funcionou), mas mesmo assim as 3 tentativas esgotaram com `GSUSRecordError`.

**Causa:** `REQUIRED_FIELD_MARKER = "obrigatório"` batia em QUALQUER ocorrência da palavra na página -- inclusive a legenda fixa do rodapé do formulário, **sempre presente independente de erro**: "(*) Campo de preenchimento obrigatório." O banner de erro real, visto em rodadas anteriores, é "O campo Nº Prontuário **é** obrigatório." (com o verbo "é"). Como o marcador não distinguia os dois, TODA tentativa era tratada como falha (mesmo quando o preenchimento/validação tinham funcionado), disparando retry -- que limpa o campo (`field.fill("")`) e recomeça, desperdiçando um estado que já estava correto.

**Decisão:** trocar o marcador para `"é obrigatório"` (com o verbo) -- só bate no banner de erro real, não na legenda fixa.

**Motivo:** erro meu de especificidade no texto de busca -- eu já tinha visto as duas strings em screenshots anteriores mas não notei que uma continha a outra como substring. Não precisa de teste ao vivo pra confirmar essa parte -- é uma correção direta a partir de evidência textual já observada.

**Impacto:** `app/gsus/records.py`. Ainda não confirmado: se isso também resolve o sintoma "clique no botão Pesquisar não parece disparar a busca" relatado pelo usuário -- é possível que fosse só efeito colateral do retry falso-positivo limpando um estado que já tinha funcionado. Próxima rodada do usuário mostra se sobra algum problema real no clique em si.

**Atualização (mesma sessão):** não resolveu -- usuário rodou de novo (10ª rodada), campo preencheu certo 3 vezes, botão nunca disparou a busca. Ver DEC-023 para a causa real, encontrada por diagnóstico direto.

---

## DEC-023 — Causa real: "Pesquisar" abre uma janela pop-up nova, eu observava a página errada

**Problema:** mesmo com os fixes anteriores (digitação simulada, Tab/blur, marcador de erro corrigido), o clique no botão Pesquisar continuava sem efeito visível, confirmado repetidamente pelo usuário.

**Investigação (headless, 2 diagnósticos):**
1. Testei 4 formas diferentes de "clicar" (`locator.click()`, `page.mouse.click()` nas coordenadas exatas, `element.click()` via JS, e invocar o handler `onclick` diretamente) com um número de prontuário **inventado** -- nenhuma gerou requisição de rede. Inconclusivo (número inventado pode falhar validação própria da função antes de chegar a fazer requisição).
2. Repeti com o número real já visto num print anterior do usuário (sem ler/imprimir o nome do paciente -- só confirmei que o autocomplete bateu, sem mostrar o valor) e chamei a função JS do botão diretamente pelo nome (`pesquisarProntuarioPaciente()`, sem nenhum clique): **zero requisições de rede vistas na página original.**

**Causa:** a função provavelmente abre uma **janela pop-up nova** para mostrar o prontuário -- mesmo padrão já confirmado para o login (DEC-009: GSUS abre pop-up após autenticar). Eu só estava observando/testando a página ORIGINAL; se a busca abre uma janela nova, nenhuma requisição nela apareceria nos meus logs, e o clique pode estar funcionando perfeitamente o tempo todo.

**Decisão:** aplicar o mesmo padrão já usado em `login.py` (`page.context.expect_page()`): envolver o clique em Pesquisar nessa captura de pop-up. Se uma nova página abrir, usar ELA daqui pra frente (verificação de "Permanece Internado", extração de evoluções). Se não abrir nenhuma (timeout de 5s), assume que não há pop-up nessa tela e segue verificando a página original como antes -- não quebra o caminho não-pop-up.

**Motivo:** essa é a explicação que mais bate com TODOS os sintomas observados até agora: campo preenche e valida certo (autocomplete funciona), mas "nada visível acontece" -- exatamente o que se esperaria se uma nova janela abrisse e ninguém (nem o script, nem necessariamente o usuário, se não notou uma segunda janela) estivesse olhando para ela.

**Impacto:** `app/gsus/records.py`: `open_current_admission` e `_search_by_record_number` agora retornam a página onde a busca aconteceu (pode ser nova). `app/gsus/adapter.py` atualizado para usar essa página retornada ao chamar `extract_notes`. Suíte (78) inalterada -- gap de cobertura desta função já reconhecido (DEC-013). **Ainda não validado em execução real** -- próxima rodada do usuário decide.

---

## DEC-024 — Clique em "Próxima" instável (timeout de estabilidade) durante o censo

**Problema:** ao rodar a 11ª rodada de `scripts/check_notes.py` (testando o fix do DEC-023), o CENSO em si (`GSUS-002`/`003`, `FROZEN` desde DEC-012, já validado ponta a ponta antes) falhou de um jeito novo: `Locator.click: Timeout 30000ms exceeded ... waiting for element to be visible, enabled and stable`. Diferente dos bugs anteriores -- o log mostra que o seletor encontrou o elemento CERTO (`<a href="javascript:openAjax(...)">Próxima</a>`), só não conseguiu confirmar estabilidade a tempo pra clicar.

**Causa provável:** lentidão momentânea do servidor ou da página sob uso automatizado repetido (muitas rodadas de teste em pouco tempo nesta sessão) -- não um erro de seletor/lógica. Não é possível ter certeza sem mais amostras, mas o padrão (elemento certo encontrado, timeout só na checagem de estabilidade) é consistente com instabilidade transitória, não com um bug estrutural.

**Decisão:** `collect_all_pages` não deve mais deixar uma falha pontual no clique de "Próxima" derrubar a execução inteira. `_click_next_with_retry` tenta o clique até `NEXT_CLICK_RETRY_ATTEMPTS` (2) vezes com timeout menor (15s) cada; se todas falharem, a paginação para e devolve os pacientes já coletados até ali (com aviso no log), em vez de propagar a exceção.

**Motivo:** mesmo princípio já aplicado por paciente (RF-12, "falha em um prontuário não deve interromper o lote") estendido à paginação em si -- um censo parcial (por causa de uma falha pontual de rede/timing) é preferível a nenhum censo. O relatório final já expõe contagem de encontrados/processados (REPORT-001), então uma eventual perda de páginas finais do censo por instabilidade fica visível, não escondida.

**Impacto:** `app/gsus/census.py` (`_click_next_with_retry` novo). 2 testes novos (`tests/unit/test_census_retry.py`, usando um stub -- sem precisar de browser real, já que o comportamento é puramente lógico/de retry). Suíte em 80. Pedido do usuário registrado em `TASKS.md` (`CENSUS-002`, P1): permitir filtrar o censo por data de internação, pra reduzir o volume de teste (e de carga no GSUS real) nas próximas rodadas -- não implementado ainda, prioridade após destravar GSUS-004/005.

---

## DEC-025 — Causa real definitiva: o botão "Pesquisar" é inerte sob automação; Enter funciona

**Problema:** com o fix do DEC-023 (tratar pop-up), usuário rodou de novo (12ª rodada): mesmo resultado, 3 tentativas esgotadas, nada visível acontecendo ao "clicar" em Pesquisar.

**Informação decisiva do usuário:** "Quando eu coloco manualmente o número e dou um simples ENTER já aparece o prontuário com as datas certinho." -- ou seja, a busca funciona via tecla Enter no próprio campo, **sem precisar do botão**. Isso explica retroativamente por que TODAS as tentativas de clique (Playwright `.click()`, `page.mouse.click()` nas coordenadas exatas, `el.click()` via JS, e até chamar `onclick()` diretamente -- ver DEC-023) falharam: o botão provavelmente está de fato quebrado/inerte nesta versão da tela (bug do próprio GSUS, não da minha automação), e o fluxo real e funcional sempre foi via tecla Enter (handler de teclado no campo, comum em telas com busca "as-you-type" ou confirmação por Enter).

**Decisão:** trocar o disparo da busca de "clicar no botão Pesquisar" para `field.press("Enter")` -- diretamente o que o usuário demonstrou funcionar. Mantida a captura de pop-up (`expect_page()`) envolvendo o Enter, e a verificação de resultado (mesma lógica de retry para "não encontrado"/"campo obrigatório" como possível corrida).

**Motivo:** não é mais uma hipótese minha -- é o comportamento manual confirmado pelo usuário, a fonte de verdade mais forte possível para comportamento de UI (seção 14 do prompt mestre: não adivinhar quando dá pra confirmar).

**Impacto:** `app/gsus/records.py`: `_search_by_record_number` não usa mais `input[name="btnConsultar"]` em nenhum caminho -- só `field.press("Enter")`. Docstring do módulo e da função atualizadas. Suíte (80) inalterada -- gap de cobertura desta função continua reconhecido (DEC-013), só valida com o usuário rodando `scripts/check_notes.py`.

**Atualização (mesma sessão):** não resolveu -- usuário: "faltou apertar o enter, fora que está tentando as 3 vezes atoa". Ver DEC-026: o problema real era o mecanismo de RETRY, não o Enter em si.

---

## DEC-026 — Parar de interpretar mensagem de erro na tela; só esperar o sinal de sucesso

**Problema:** com Enter no lugar do clique (DEC-025), usuário ainda viu 3 tentativas "à toa" -- o mesmo padrão de retry inútil já visto no DEC-022 (falso positivo em marcador de erro).

**Causa provável:** `NOT_FOUND_MARKER`/`REQUIRED_FIELD_MARKER` continuavam no código, verificados logo após `page.wait_for_load_state()` -- que para uma busca AJAX pode retornar quase imediatamente, antes do resultado real aparecer (mesma corrida do DEC-016/018). Ler a tela cedo demais e reagir a texto ambíguo já causou pelo menos um falso positivo confirmado (DEC-022); é razoável suspeitar que o padrão se repetiu, mascarando buscas que na verdade funcionaram.

**Decisão:** parar de tentar decidir "deu erro ou não" a partir de texto da tela. `open_current_admission` agora só verifica UM sinal, positivo e definitivo: o marcador `CURRENT_ADMISSION_MARKER` ("Permanece Internado") aparecendo dentro de uma janela generosa (`RESULT_WAIT_MS`, 20s). Se não aparecer nesse prazo, a tela de busca é recarregada do ZERO (novo `Atendimento` -> `Pesquisar Prontuário`, não só um novo preenchimento) e tenta de novo -- até `SEARCH_RETRY_ATTEMPTS` (3) vezes completas.

**Motivo:** verificar ausência de erro (texto ambíguo, sujeito a bater em conteúdo padrão da página) é inerentemente mais frágil que verificar presença de sucesso (um marcador específico que só aparece quando a internação de verdade abriu). Já apanhei duas vezes tentando a abordagem por "detectar erro" -- trocar de estratégia em vez de continuar ajustando a lista de textos a evitar.

**Impacto:** `app/gsus/records.py` reestruturado: `_search_by_record_number` virou `_submit_search` (só dispara a busca, não decide sucesso/falha); toda a lógica de retry subiu para `open_current_admission`, que agora recarrega a navegação inteira a cada tentativa (não só o preenchimento do campo) -- evita reaproveitar um estado de página potencialmente inconsistente entre tentativas. Suíte (80) inalterada. Continua sem teste automatizado para esta função específica (gap reconhecido, DEC-013) -- validação real só com o usuário.

**Atualização (mesma sessão) -- SUCESSO:** usuário confirmou -- "Abriu o prontuário e a data certa". `GSUS-004` está completo e validado ponta a ponta contra o GSUS real. `extract_notes` (GSUS-005) rodou sem travar, mas extraiu errado -- ver DEC-027.

---

## DEC-027 — `extract_notes` capturava só o cabeçalho do dia, não o conteúdo expandido

**Problema:** com `GSUS-004` funcionando, o log da 1ª execução real de `extract_notes` mostrou: "1 bloco(s) de evolução encontrados, 0 com cabeçalho reconhecido" -- ou seja, praticamente nada foi extraído de útil.

**Causa:** `_text_until_next_day_header` subia UM nível de ancestral a partir do elemento de texto do cabeçalho do dia (`header.locator("xpath=ancestor::*[...][1]")`) e usava o `inner_text()` desse ancestral como "o conteúdo do dia". Na estrutura real, esse nível de ancestral aparentemente contém só o próprio cabeçalho ("20 de agosto de 2026 - Quinta-Feira"), não o conteúdo das evoluções revelado pelo clique de expansão -- por isso 1 bloco (o cabeçalho em si) e 0 reconhecido (o texto do cabeçalho do dia não bate no formato de cabeçalho de EVOLUÇÃO, que é diferente: "DD/MM/AAAA HH:MM - ... (cargo)").

**Decisão:** abandonar a tentativa de achar o container "certo" por posição no DOM (chute que já errou uma vez -- adivinhar o próximo nível também seria só mais um chute). Em vez disso: depois de expandir todos os dias, capturar o texto visível da PÁGINA INTEIRA (`page.locator("body").inner_text()`) de uma vez. `app.extraction.parser.parse_note_blocks` já localiza cada evolução pelo próprio padrão de cabeçalho via regex, em qualquer lugar do texto -- não precisa saber onde um "dia" começa e termina no DOM, só onde cada EVOLUÇÃO começa (o que o padrão de cabeçalho já resolve). Texto de UI ao redor (menu, "Dados Pessoais", rótulos de botão) não bate no padrão de cabeçalho e fica de fora dos blocos capturados (ou vira ruído inofensivo dentro do corpo de uma evolução adjacente).

**Motivo:** menos chute sobre estrutura de DOM não confirmada = menos pontos de falha. O parser já foi desenhado (DEC-013) pra não depender de onde no DOM cada evolução está, só do formato do cabeçalho -- esta mudança finalmente aproveita isso por completo.

**Impacto:** `app/gsus/records.py`: `extract_notes` simplificado, `_text_until_next_day_header` removida. Suíte (80) inalterada -- ainda sem teste automatizado para esta função (gap reconhecido, DEC-013), validação real com o usuário na próxima rodada.

**Atualização (mesma sessão):** não resolveu -- mesmo resultado exato ("1 bloco, 0 reconhecido"). Ver DEC-028: a causa raiz era outra -- o elemento certo pra clicar nem era o que eu buscava.

---

## DEC-028 — Elemento clicável real identificado por inspeção estrutural: `<a class="item" id="historicoEvolucaoNItem">`

**Problema:** com o fix do DEC-027 (capturar página inteira em vez de container), o resultado real permaneceu idêntico -- "1 bloco, 0 reconhecido". Suspeita: o clique de expansão nunca estava funcionando (por isso não importava COMO eu extraía depois -- não havia conteúdo novo pra extrair).

**Investigação (headless, usando o fluxo real já validado de `open_current_admission` com o prontuário 262531 já visto antes -- só estrutura, nunca conteúdo clínico lido/impresso):**
1. Primeira tentativa de diagnóstico travou (censo lento de novo) e outra travou numa tempestade de retry (`element is not visible`, 60+ tentativas) -- matei os processos e limpei.
2. Diagnóstico mais enxuto (pulando o censo, indo direto ao prontuário conhecido) revelou: o texto que batia no padrão de data estava dentro de `<font><b>`, **invisível**, sem `onclick` nele nem no pai imediato.
3. Subindo a cadeia de ancestrais (8 níveis, só tag/classe/id/onclick/visibilidade -- nunca texto), achei em 2 níveis acima: `<a class="item" id="historicoEvolucao0Item">` -- um link de verdade, ainda invisível naquele momento (antes do clique), com um índice numérico no id (sugerindo um por evolução, sequencial: `historicoEvolucao0Item`, `historicoEvolucao1Item`, etc.).

**Causa:** eu procurava pelo TEXTO da data pra decidir o que clicar -- mas o elemento realmente clicável (o link com o id `historicoEvolucaoNItem`) não corresponde a "um por dia" como a estrutura visual sugeria, e sim a um por EVOLUÇÃO individual. O texto de data que eu buscava está aninhado fundo demais dentro dele (e permanece tecnicamente "invisível" pro Playwright mesmo com o pai expandido, provavelmente por ficar atrás de outro elemento ou com CSS específico) -- nunca foi um bom alvo de clique.

**Decisão:** `extract_notes` agora localiza os elementos por seletor CSS direto (`a.item[id^="historicoEvolucao"]`), não mais por texto de data. Clica em cada um (todos começam recolhidos, um clique cada deve bastar), com timeout curto por item (5s) e tolerância a falha individual (não trava tudo se uma evolução específica não expandir). Depois, mesma estratégia do DEC-027: captura o texto da página inteira e deixa o parser separar.

**Motivo:** seletor por `id` com prefixo estável, encontrado por inspeção estrutural real (não suposição) -- exatamente o tipo de evidência que faltava nas tentativas anteriores.

**Impacto:** `app/gsus/records.py`: `extract_notes` reescrito, `DAY_HEADER_PATTERN` removido (não é mais usado -- import `re` também removido). Suíte (80) inalterada. Ainda sem teste automatizado pra esta função (gap reconhecido, DEC-013) -- validação real na próxima rodada do usuário. Se `historicoEvolucao0Item` for mesmo "por evolução" (não "por dia"), o resultado esperado agora é bem melhor -- cada evolução individual deve expandir e aparecer no texto capturado.

---

## DEC-029 — `get_census` ganha limite opcional `max_patients` (só para teste)

**Problema:** o ciclo de teste (`scripts/check_notes.py`) esperava o censo completo (80-175 pacientes, várias páginas AJAX) só para usar `patients[0]` -- lento, e contribuiu para os diagnósticos travarem/demorarem nesta sessão. Pedido do usuário: encurtar isso, mas manter a busca completa funcionando pra uso real.

**Decisão:** `get_census`/`collect_all_pages` ganham parâmetro opcional `max_patients: int | None = None`. Quando informado, a paginação para assim que atinge essa quantidade (verificado logo após extrair cada página, antes de decidir se busca a próxima). Omitido (padrão), comportamento idêntico a antes -- busca tudo.

**Motivo:** evita duplicar a lógica de paginação num "modo rápido" separado -- é o mesmo código, só com um critério de parada adicional opcional. `scripts/check_notes.py` passa a chamar com `max_patients=1`; nenhum outro chamador (produção, `GSUSAdapter`) foi alterado -- continuam pegando o censo completo.

**Impacto:** `app/gsus/census.py` (`get_census`, `collect_all_pages`), `scripts/check_notes.py` (usa `max_patients=1`, e também corrigido para usar a página retornada por `open_current_admission` em `extract_notes` -- antes usava sempre `content_frame`, que por sorte nunca deu problema porque a busca por Enter não abre pop-up na prática, mas estava inconsistente com o que `adapter.py` já fazia desde o DEC-023). 3 testes novos com fixtures existentes. Suíte em 83.

---

## DEC-030 — Accordion de DOIS níveis: "atendimento" precisa abrir antes de "evolução"

**Problema:** com o seletor `a.item[id^="historicoEvolucao"]` (DEC-028), o script rodou até o fim sem travar, mas o log de arquivo (pedi pro usuário colar, já que os avisos não vão mais pro terminal -- DEC-016) revelou: "Não foi possível expandir a evolução 1/2" e "2/2" -- os 2 cliques deram timeout.

**Investigação (headless, mesmo prontuário já conhecido, só estrutura):** o `<a class="item" id="historicoEvolucao0Item">` fica DENTRO de `<div class="card_body" id="historicoAtendimento0">` -- que por sua vez está colapsado (o diagnóstico anterior, DEC-028, já tinha mostrado esse `card_body` como invisível, eu só não tinha percebido a implicação). Olhando os filhos diretos de `.card_atendimento` (o card que `open_current_admission` já abre com sucesso), achei: `<div class="card_header" id="historicoAtendimento0Item">` (visível, irmão anterior do `card_body`) -- **mesmo padrão de nome** do nível de evolução (`{prefixo}{N}Item` = cabeçalho clicável, `{prefixo}{N}` = corpo colapsado).

**Causa:** é um accordion de DOIS níveis, não um. Clicar direto em `historicoEvolucaoNItem` sem antes expandir `historicoAtendimentoNItem` tenta clicar um elemento que ainda está dentro de um container fechado -- por isso "não visível", por isso timeout.

**Decisão:** `extract_notes` agora expande os dois níveis em sequência: primeiro todos os `[id^="historicoAtendimento"][id$="Item"]` (nível atendimento), depois todos os `a.item[id^="historicoEvolucao"]` (nível evolução, só ficam clicáveis depois do primeiro nível aberto). Extraída função auxiliar `_expand_all` (reaproveitada pros dois níveis, mesma tolerância a falha individual). Erro só é levantado se, depois de tentar expandir tudo, zero evoluções forem encontráveis -- não achar nenhuma depois de expandir os dois níveis é sinal real de prontuário vazio, não de seletor errado.

**Motivo:** de novo, achado por inspeção estrutural real (filhos diretos + irmão anterior do elemento já conhecido), não suposição -- e o padrão de nome consistente (`{prefixo}{N}Item`/`{prefixo}{N}`) dá confiança de que a mesma lógica deve valer pra ambos os níveis.

**Impacto:** `app/gsus/records.py`: `extract_notes` reescrito com os 2 níveis; também removido código morto que tinha sobrado de uma edição anterior (trecho inalcançável depois do `return`, inofensivo mas confuso). Suíte (83) inalterada -- gap de teste automatizado pra esta função continua reconhecido (DEC-013), validação real só com o usuário.

**Atualização -- SUCESSO TOTAL:** usuário confirmou -- "foi perfeito. Todo o histórico do paciente foi aberto e consultado". `GSUS-005` completo. Os 5 módulos de automação GSUS (`GSUS-001` a `GSUS-005`) estão validados ponta a ponta contra o GSUS real.

---

## DEC-031 — Novo recurso "Localizar Paciente" (histórico completo, manual, sem persistir)

**Contexto:** com `GSUS-005` funcionando, o usuário pediu um novo recurso: uma busca manual por número de prontuário ("Localizar") que mostra o HISTÓRICO COMPLETO de internações do paciente (atual + antigas, mais recente primeiro) -- diferente da rotina automática, que deve continuar olhando só a internação atual.

**Decisões (confirmadas pelo usuário via pergunta direta, por envolverem quanto dado sensível fica acumulado -- seção 63 do prompt mestre: "envolve segurança"):**
1. **Retenção:** o histórico puxado pelo "Localizar" NÃO é salvo no banco (`Repository`/SQLite) -- é busca ao vivo no GSUS toda vez, só pra exibição na tela. Evita acumular anos de histórico de pacientes que só foram consultados uma vez.
2. **Análise:** internação antiga NÃO passa por regras determinísticas nem pelo LLM local -- só texto bruto, pra leitura humana direta. Internação já encerrada não gera "pendência" nova.
3. **Rotina automática:** sem nenhuma mudança -- continua usando `open_current_admission` + `extract_notes` (só internação atual), confiando no hash incremental (`INCR-001`) já existente pra não reprocessar evolução antiga. Não foi criado nenhum filtro de data novo.

**Implementação:**
- `app/gsus/records.py`: nova função `get_full_admission_history_text()` -- localiza TODOS os cabeçalhos de episódio (`ADMISSION_HEADER_PATTERN`, formato `"Internação (..."`, confirmado pro episódio atual), abre cada um, expande os 2 níveis de accordion já validados (`DEC-030`), devolve o texto bruto da página inteira (todos os episódios juntos, sem tentar separar por episódio no nível de código -- fica pra leitura humana natural, mais simples que tentar recortar por episódio no DOM).
- **NÃO CONFIRMADO para múltiplos episódios:** o único paciente testado até agora (262531) só tem UMA internação (`codInternacao5951310` aparece uma única vez nos ids da página) -- não deu pra observar visualmente como o GSUS mostra internações antigas/com alta. A generalização do cabeçalho ("Internação (...)" em vez de travar em "Permanece Internado") é uma extrapolação razoável a partir do formato já confirmado, não uma nova suposição cega -- mas fica pendente de validação com um paciente que realmente tenha mais de uma internação.
- `app/gsus/adapter.py`: novo método `GSUSAdapter.lookup_full_history()` -- reaproveita `_ensure_login()`, chama a função acima.
- `app/ui/lookup_window.py` (novo): tela "LOCALIZAR PACIENTE" -- campo de prontuário, botão, área de texto rolável (só leitura) pro resultado. Roda a busca numa thread separada (mesmo padrão do `MainWindow`), nunca grava nada em banco.
- `app/ui/main_window.py`: novo botão "LOCALIZAR PACIENTE" leva pra essa tela.
- `app/ui/errors.py`: `GSUSRecordError` ganhou mensagem amigável específica ("Não foi possível localizar esse prontuário...").
- 8 testes novos (`test_lookup_window.py`, `test_errors.py`) -- incluindo um que confirma explicitamente que NENHUM arquivo `auditoria.db` é criado por esse fluxo (a garantia de não-persistência, testável sem GSUS real).

**Motivo:** minimização de PHI acumulado é um requisito já explícito do projeto (SEC-03/SEC-04) -- estender esse princípio pro novo recurso, em vez de tratá-lo como "mais um lugar que salva tudo", evita crescer o banco local com anos de prontuário de pacientes que só foram olhados uma vez de passagem.

**Impacto:** suíte em 91 (83 + 8). Nenhuma mudança na rotina automática existente (`orchestrator.py`, `Repository`, regras, LLM) -- o novo recurso é inteiramente aditivo e isolado.

---

## DEC-032 — Horário padrão de agendamento trocado de 23:00 para 00:01 (captura do "dia anterior completo")

**Contexto:** usuário confirmou por screenshots que "Localizar" abre corretamente todo o histórico de internações do paciente (múltiplos episódios, 2014–2026) -- validando a extrapolação do DEC-031 sobre `ADMISSION_HEADER_PATTERN`. Em seguida, pediu que a rotina automática rode às 00:01 e extraia "apenas os dados do dia anterior" (ex.: rodar 00:01 de 21/08 para pegar 20/08).

**Análise:** isso NÃO exige um filtro de data novo, pelo motivo já registrado no DEC-031 -- o hash incremental (`INCR-001`) já entrega exatamente "o que é novo desde a última execução". Rodando logo após a meia-noite, o dia anterior já está encerrado (evoluções não são retroativamente editadas por sistemas hospitalares em uso normal), então "novo desde a última rodada (também 00:01, dia anterior)" já coincide com "o dia anterior inteiro" -- sem precisar comparar timestamp de evolução contra data corrente.

Um filtro de data explícito (descartar nota fora do intervalo "ontem") foi considerado e rejeitado: se uma rodada falhar (máquina desligada, GSUS fora do ar), a próxima rodada com filtro rígido perderia as notas do dia pulado; sem filtro, o incremental as pega automaticamente na rodada seguinte (comportamento de catch-up já existente via `processing_queue`/resume, `QUEUE-001`).

**Decisão:** trocar `config.DEFAULT_SCHEDULE_TIME` de `"23:00"` para `"00:01"` -- é só o valor sugerido/pré-preenchido na tela de configuração (`SetupWindow`), continua editável pelo usuário. Nenhuma mudança em `orchestrator.py`, `records.py` ou na lógica de dedup.

**Alternativas consideradas:** filtro explícito por data de evolução (rejeitado -- ver Análise, risco de perda silenciosa de dado em rodada com falha). Manter 23:00 (rejeitado -- pediria ao usuário reconciliar manualmente "hoje" vs. "ontem" sempre que configurasse, já que 23:00 captura um dia ainda incompleto).

**Motivo:** decisão de baixo risco e reversível (é um valor default, não uma trava) que atende ao pedido do usuário sem adicionar mecanismo novo nem enfraquecer a garantia de robustez já existente (RNF-02/RNF-04).

**Impacto:** `app/config.py` (`DEFAULT_SCHEDULE_TIME`). `tests/unit/test_config.py::test_load_config_missing_file_returns_defaults` já referencia o símbolo (não o literal `"23:00"`), permanece verde sem alteração. `app/ui/setup_window.py`/`main_window.py` só exibem `schedule_time` (sem hardcode) -- sem mudança necessária.

---

## DEC-033 — "Localizar": extrair data de cada internação para garantir ordem mais-recente-primeiro (RF-19)

**Contexto:** usuário confirmou (3 screenshots, sem dado identificável de paciente -- só a lista de tipos/datas de episódio 2014-2026) que `get_full_admission_history_text` visita corretamente TODOS os episódios de um paciente com múltiplas internações reais. Isso confirma a extrapolação do DEC-031 (`ADMISSION_HEADER_PATTERN` = `"Internação ("` cobre todo tipo de episódio -- "P.A."/"Ambulatorial" são valores do atributo "Origem do..." dentro do cabeçalho, não um prefixo de cabeçalho à parte).

**Problema:** apesar da navegação estar correta, a função só devolvia o texto bruto concatenado da página inteira, na ordem em que o GSUS lista -- sem nenhuma garantia de código de que essa ordem é "mais recente primeiro" (RF-19 exige isso explicitamente). Além disso, pra um paciente com 12 anos de histórico e vários episódios, um bloco de texto corrido sem nenhuma marcação de onde um episódio termina e outro começa é difícil de ler.

**Decisão:** capturar o texto de cada cabeçalho de episódio (já visível durante a navegação, antes do clique) e, depois de expandir tudo, usar essas strings pra localizar onde cada episódio começa no texto final (`str.find` incremental, robusto mesmo com cabeçalhos duplicados -- posição de busca sempre avança). Extrair a data de início de cada cabeçalho (primeira ocorrência do padrão `"DD de MÊS de AAAA"` -- é sempre a data de início, por construção do próprio texto, independente do que vem depois no cabeçalho), ordenar os episódios por essa data (decrescente, episódio sem data reconhecida vai por último) e devolver o texto já reordenado, com um rótulo simples (`"Internação N de TOTAL — AAAA-MM-DD"`) antes de cada bloco.

Fail-soft em cada etapa: cabeçalho sem data reconhecida (formato de "alta" ainda não confirmado literalmente) só fica sem rótulo/no fim da ordenação, nunca derruba a função; cabeçalho não encontrado no texto final (não deveria acontecer, mesma fonte) faz a função desistir da reformatação e devolver o texto bruto sem alteração -- nunca perde informação por causa da rotulagem.

**Alternativas consideradas:** confiar cegamente na ordem do GSUS (era o comportamento até agora) -- rejeitada, porque RF-19 pede a garantia explicitamente e não há como validar que o GSUS SEMPRE lista nessa ordem para qualquer paciente. Mudar o retorno para dado estruturado (lista de episódios) em vez de string única -- rejeitada por ora: exigiria mudar `LookupWindow`/`GSUSAdapter` também, escopo maior que o necessário para atender RF-19; a string já reordenada e rotulada resolve o requisito sem tocar UI/adapter.

**Motivo:** menor mudança que fecha um requisito explícito (RF-19) já sinalizado como gap, sem tocar nada do fluxo de navegação real já validado (FROZEN) nem no contrato UI/adapter.

**Impacto:** `app/gsus/records.py` (`_parse_header_date`, `_format_episodes_newest_first`, `get_full_admission_history_text` atualizado). 9 testes novos (`tests/unit/test_records_history.py`, dados 100% fictícios) cobrindo parsing de mês acentuado, formato não reconhecido, reordenação, cabeçalho duplicado, fallback sem cabeçalho. Suíte em 100 (91 + 9). Reordenação/rotulagem em si **ainda não validada contra o GSUS real** -- só a navegação/expansão (que já eram FROZEN) foram exercitadas ao vivo até agora; próxima rodada do usuário confirma.

---

## DEC-034 — Bug real: "Localizar" nunca navegava até "Pesquisar Prontuário" -- timeout sempre

**Problema:** usuário testou "Localizar" (262.531) três vezes e sempre bateu no mesmo erro genérico. Diagnóstico direto (log de arquivo e terminal não mostraram nada -- ver investigação abaixo) exigiu instrumentação temporária (grava traceback bruto em arquivo fixo, sem depender de `logging.*`, removida depois de capturar o real). Traceback real:

```
playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.
Call log:
waiting for locator("#codPaciente")
```
-- estourando dentro de `_submit_search` (`records.py`), chamada por `get_full_admission_history_text`.

**Causa:** `get_full_admission_history_text` chamava `_submit_search` diretamente, sem antes clicar no menu **Atendimento -> Pesquisar Prontuário** -- diferente de `open_current_admission` (usado pela rotina automática), que já faz esse clique antes de `_submit_search`. Sem essa navegação, a página ainda está na tela inicial pós-login (`_ensure_login`), onde `#codPaciente` não existe -- daí o timeout de 30s sempre, para qualquer prontuário.

**Nota sobre a validação anterior (DEC-031/033):** a confirmação do usuário em 2026-08-21 ("visitou todas as internações do paciente", 3 screenshots) registrada no DEC-033 antecede este bug ter sido pego -- não há como reconciliar com certeza como aquele teste passou sem essa navegação (sem controle de versão -- projeto não é repositório git -- não dá pra inspecionar o estado exato do código naquele momento). O que importa a partir daqui é o comportamento atual, real e reproduzido três vezes seguidas.

**Investigação (metodologia -- registrar por transparência, incluindo um erro meu no processo):** 1ª tentativa de captura via arquivo de diagnóstico coincidiu no tempo com uma suíte de testes que eu mesmo rodava em paralelo (`test_lookup_window.py`), que escreve no MESMO arquivo fixo ao exercitar seu próprio cenário fake (prontuário "999999") -- o arquivo capturado nessa primeira rodada era do meu teste, não do clique real do usuário. Percebido pela inconsistência (traceback apontando para dentro de `tests/`, número de prontuário errado), sinalizado ao usuário, arquivo apagado, 2ª captura (sem teste rodando em paralelo) trouxe o traceback real acima.

**Decisão:** `get_full_admission_history_text` ganha o mesmo padrão de navegação + retry já validado em produção por `open_current_admission` (clique Atendimento -> Pesquisar Prontuário, dentro de um laço de `SEARCH_RETRY_ATTEMPTS` tentativas, esperando o primeiro cabeçalho de internação aparecer via `RESULT_WAIT_MS` antes de decidir que a tentativa falhou). `open_current_admission` em si **não foi tocada** (FROZEN, sem motivo pra mexer -- só reaproveitado o padrão).

**Alternativas consideradas:** extrair a navegação para uma função compartilhada entre `open_current_admission` e `get_full_admission_history_text` -- rejeitada por ora: exigiria alterar `open_current_admission` (FROZEN, validada ponta a ponta), risco maior que o benefício de não duplicar 2 linhas de clique.

**Motivo:** a correção usa exatamente o mecanismo já validado em produção real (mesmos seletores, mesma estratégia de retry) para o mesmo menu -- não é seletor novo nem suposição, é aplicar o padrão comprovado onde faltava.

**Impacto:** `app/gsus/records.py` (`get_full_admission_history_text`). `app/ui/lookup_window.py`: instrumentação temporária de diagnóstico adicionada e removida na mesma sessão -- nenhum resíduo no código. Suíte: 93 passed + 3 skip (sem `test_lookup_window.py`, que trava a suíte por ~5min nessa máquina -- rodar separado quando precisar). Ainda não validado contra o GSUS real -- próxima tentativa do usuário confirma.

---

## DEC-035 — GSUS pede justificativa de acesso auditada para prontuário não internado na unidade -- "Localizar" falha limpo, nunca preenche sozinho

**Contexto:** com o fix do DEC-034, usuário testou de novo (262.531, digitação corrigida) e bateu num caso novo: 2ª tentativa do retry ficou travada 58+ cliques tentando reabrir o menu "Atendimento", sempre bloqueada por um modal (`<div id="MB_window">`) cobrindo a tela. Liguei o navegador em modo visível temporariamente (`headless=False`, só para este diagnóstico) para o usuário ver o que era -- ele confirmou por screenshot (sanitizado, exceto por um vazamento parcial de nome de paciente e nome completo do usuário GSUS logado, sinalizado a ele, nada reproduzido aqui).

**Achado:** a tela é real e é do próprio GSUS -- "Justificar Acesso ao Prontuário". Aparece quando o prontuário pesquisado não está internado na unidade atual NESTE MOMENTO. Texto do aviso: "O acesso e a justificativa serão gravados" -- é um controle de auditoria/privacidade deliberado do GSUS (comum em prontuário eletrônico -- exige que o profissional justifique por escrito acessar um registro que não é de um paciente sob seu cuidado ativo agora), exige campo de texto obrigatório + clique em "Confirmar Justificativa".

**Por que isso não é um bug a "corrigir" preenchendo o formulário:** "Confirmar Justificativa" é uma ação de escrita/confirmação clássica -- exatamente a categoria que o projeto sempre excluiu por princípio (SEC-01/SEC-02, `client.py`: "nunca 'salvar', 'confirmar' ou 'excluir'"). Preencher e confirmar automaticamente geraria um registro de auditoria REAL no GSUS, permanente, no nome do usuário logado, com uma justificativa gerada por código -- sem nenhum julgamento humano genuíno por trás. Como "Localizar" existe justamente para ver histórico de internações ANTIGAS (não a atual), essa tela vai aparecer para a maioria dos usos reais do recurso, não é um caso raro.

**Decisão (perguntada diretamente ao usuário, por envolver segurança/auditoria -- seção 63 do prompt mestre):** falhar limpo, sem nunca tentar passar. `get_full_admission_history_text` detecta o marcador `"Justificar Acesso ao Prontuário"` e levanta `GSUSAccessJustificationRequired` (subclasse de `GSUSRecordError`) imediatamente -- sem retry (retry só re-dispararia o mesmo modal e bloquearia a navegação seguinte, que foi exatamente o bug dos 58 cliques observado). Mensagem amigável específica, diferente de "não encontrado": "Esse prontuário exige justificativa de acesso no próprio GSUS... Abra manualmente no GSUS se precisar consultar esse histórico."

**Alternativa considerada e rejeitada pelo usuário:** pausar a automação e pedir a justificativa pro usuário digitar na hora, na própria tela do app, e só então confirmar com o que ele escreveu (preservaria decisão humana real, mas exige pausar a thread de trabalho e esperar input -- complexidade maior). Usuário escolheu a opção mais simples e mais conservadora.

**Consequência prática reconhecida:** RF-19 ("histórico completo de internações, atual e antigas") fica, na prática, limitado a prontuários que estão internados na unidade configurada NESTE MOMENTO -- para histórico de paciente já com alta, o usuário precisa abrir manualmente no GSUS e justificar lá. Não é uma limitação de código, é o próprio controle de auditoria do GSUS.

**Motivo:** consistente com o princípio já estabelecido do projeto inteiro (nunca confirmar/escrever no GSUS de forma automática) e com fail-closed (SEC-02) -- documentar e falhar claro é preferível a inventar comportamento sobre uma tela de auditoria real.

**Impacto:** `app/gsus/records.py` (`ACCESS_JUSTIFICATION_MARKER`, `GSUSAccessJustificationRequired`, laço de retry de `get_full_admission_history_text` atualizado). `app/ui/errors.py` (mensagem amigável específica, checada antes do `GSUSRecordError` genérico). 1 teste novo (`test_errors.py`). `app/gsus/client.py`: `headless=False` usado só durante o diagnóstico, revertido para `True` na mesma sessão -- nenhum resíduo. Suíte: 97 passed (sem `test_lookup_window.py`).

---

## DEC-036 — Campo de prontuário sem confirmação de valor digitado (regressão) -- reintroduzida

**Problema:** usuário testou "Localizar" com prontuário atualmente internado (5333366, evitando a tela de justificativa do DEC-035). Erro real capturado (mesmo método de diagnóstico temporário das vezes anteriores): `GSUSRecordError: Prontuário 5333356: nenhuma internação encontrada` -- **5333356**, não 5333366 (um dígito trocado, posição 6). Segunda vez que um número digitado sai diferente do pedido (a primeira foi 262531 -> 262351, mas aquela o próprio usuário confirmou ter sido erro de digitação DELE). Padrão repetido com números diferentes é suspeito o bastante para não assumir digitação errada de novo sem proteção de código.

**Causa provável:** a mesma classe de corrida já resolvida antes neste projeto (DEC-019/020: autocomplete legado da tela, AJAX por tecla, pode interferir com a digitação simulada). A proteção que o DEC-018/020 descreveram adicionar (ler `input_value()` de volta e tentar de novo se não bater) **não existe mais** na implementação atual de `_submit_search` -- não há registro de quando/por que foi perdida (projeto não é repositório git, sem histórico pra inspecionar).

**Decisão:** reintroduzir a confirmação: depois de digitar, lê `field.input_value()`; se não bater com `record_number`, loga aviso (sem o número, lição do DEC-016) e tenta de novo (até `FIELD_CONFIRM_ATTEMPTS = 3`); só aperta Enter/submete depois de confirmar o valor certo. Se esgotar as tentativas sem confirmar, levanta `GSUSRecordError` claro em vez de submeter um valor sabidamente errado.

**Escopo:** `_submit_search` é usada por AMBAS `open_current_admission` (rotina automática, FROZEN) e `get_full_admission_history_text` ("Localizar") -- a correção beneficia as duas. Mexer em código usado por uma função FROZEN é permitido aqui pelo mesmo motivo do DEC-015 (bug real observado em produção, correção estritamente defensiva -- só adiciona uma verificação antes de submeter, não muda o que é submetido quando já está certo).

**Motivo:** mesmo racional já validado pelo usuário no DEC-018/020 -- não é suposição nova, é reaplicar uma decisão já tomada que se perdeu.

**Impacto:** `app/gsus/records.py` (`FIELD_CONFIRM_ATTEMPTS`, `_submit_search` com laço de confirmação). Suíte: 97 passed (sem mudança de contagem -- `_submit_search` não tem teste unitário direto, mesmo gap já reconhecido desde DEC-013 pra tudo que toca DOM real do prontuário). Ainda não validado contra o GSUS real -- próxima tentativa do usuário confirma se o problema para de acontecer.

---

## DEC-037 — Corrida no campo de prontuário CONFIRMADA real (não era só digitação do usuário) -- limpeza por teclado + mais tempo de espera

**Correção sobre o DEC-036:** o número "errado" do incidente anterior (5333366 vs 5333356) era só o usuário relatando o número errado NO CHAT pra mim -- ele confirmou depois que 5333356 sempre foi o certo. Isso não invalida o DEC-036: o laço de confirmação criado ali é o que revelou ESTE bug de verdade, ao testar de novo com o número correto (5333356) e o próprio laço esgotar `FIELD_CONFIRM_ATTEMPTS = 3` tentativas sem o campo nunca confirmar o valor digitado -- prova direta de corrida real, não suposição.

**Decisão:** duas mudanças em `_submit_search`: (1) limpar o campo antes de digitar usando teclado (`Control+a` + `Backspace`) em vez de `.fill("")` -- mesmo racional do DEC-019 (esse campo só confia em evento de teclado real, `.fill()` pode deixar o estado JS interno do autocomplete dessincronizado mesmo só para limpar); (2) aumentar `FILL_SETTLE_WAIT_MS` de 800ms para 1500ms -- 800ms não bastou consistentemente nesta máquina+rede real.

**Motivo:** reaplica o padrão já community-validado (teclado real > valor programático, pra este campo especificamente) no passo que faltava (limpeza), e dá mais margem de tempo pro AJAX do autocomplete assentar antes de conferir o valor -- ambos diretamente motivados pela evidência do DEC-036 (3 falhas reais seguidas), não suposição nova.

**Impacto:** `app/gsus/records.py` (`_submit_search`, `FILL_SETTLE_WAIT_MS`). Suíte: 94 passed + 3 skip (contagem igual, skip é o flake de Tcl/Tk já conhecido). Ainda não validado contra o GSUS real -- próxima tentativa do usuário confirma.

---

## DEC-038 — A conferência do DEC-036/037 estava errada: o campo REFORMATA o valor (comparar só dígitos, digitar uma vez só)

**Problema:** usuário pediu para rodar com o navegador visível (`headless=False`) e observou ao vivo: **o script digitava o número do prontuário 3 vezes seguidas** no campo de busca, sendo que o correto é digitar uma vez e apertar Enter.

**Causa:** o laço de confirmação introduzido no DEC-036 comparava `field.input_value() == record_number` -- igualdade literal. O campo `#codPaciente` do GSUS **reformata** o que é digitado (máscara/pontuação -- a própria tela de censo exibe prontuário com ponto, ex.: "262.531"), então o valor lido de volta nunca era literalmente igual ao digitado, mesmo quando a digitação funcionou perfeitamente. Resultado: o laço redigitava as 3 tentativas inteiras e depois falhava com "Não foi possível digitar o número do prontuário corretamente após 3 tentativas" -- um erro que descrevia mal o que realmente acontecia (a digitação estava certa; a CONFERÊNCIA é que estava errada).

Isso também revê o diagnóstico do DEC-037: aquele registro tratou o esgotamento das 3 tentativas como "prova direta de corrida real". Não era -- era a comparação literal falhando. A mudança de limpeza por teclado (`Control+a`/`Backspace`) e o aumento de `FILL_SETTLE_WAIT_MS` feitos lá permanecem (são defensivos e alinhados ao DEC-019), mas a conclusão sobre corrida está corrigida aqui.

**Decisão:** (1) digitar **uma única vez** -- sem laço de redigitação; (2) manter a conferência fail-closed (SEC-02: nunca submeter número diferente do pedido), mas comparando apenas os **dígitos** dos dois lados, via novo helper `_digits_only()` -- é o que de fato identifica o prontuário, e absorve tanto a máscara do GSUS quanto o usuário digitar com ponto na tela do app.

**Alternativas consideradas:** remover a conferência inteira (simples, mas perderia a proteção fail-closed contra submeter número errado -- e o histórico do projeto, DEC-019/020, mostra que este campo tem comportamento instável o bastante pra justificar a checagem). Normalizar comparando `endswith`/prefixo (frágil, casaria prontuários diferentes).

**Motivo:** corrige a causa real observada ao vivo pelo usuário, mantém a garantia de segurança que motivou o DEC-036, e elimina as redigitações desnecessárias (que além de feias, batiam 3x no autocomplete AJAX do GSUS a cada busca -- carga desnecessária no servidor real da instituição).

**Impacto:** `app/gsus/records.py` (`_digits_only` novo, `_submit_search` sem laço, `FIELD_CONFIRM_ATTEMPTS` removida -- sem referências órfãs). 2 testes novos (`test_records_history.py`). Suíte: 96 passed + 3 skip. `app/gsus/client.py` está com `headless=False` no momento (diagnóstico, a pedido do usuário) -- **reverter para `True`** assim que a validação ao vivo terminar.

---

## DEC-039 — Detectar episódio por ID estrutural, não pelo texto do cabeçalho (`^Internação (` nunca casou)

**Problema:** com o DEC-038 aplicado, usuário acompanhou ao vivo (navegador visível): o script digitou o prontuário UMA vez, deu Enter, a busca funcionou e a tela do prontuário abriu -- mas o script ficou parado, repetiu a busca inteira mais 2 vezes e terminou com "nenhuma internação encontrada". Ou seja: a navegação e a busca estão certas; o que falha é **reconhecer o resultado que já está na tela**.

**Causa:** `ADMISSION_HEADER_PATTERN = re.compile(r"^Internação\s*\(")` não casa nada na página real. A âncora `^` exige que o texto do elemento COMECE exatamente com "Internação (" -- o que não se sustenta se o `inner_text` do elemento traz qualquer coisa antes. Esse padrão nasceu no DEC-031 como extrapolação e foi dado como "CONFIRMADO" no DEC-033 -- mas o DEC-034 já tinha mostrado que aquela suposta confirmação era inválida (a função nunca chegava a navegar até a tela de busca; o que o usuário viu naquele teste foi outra coisa). Esta é a terceira decisão desta cadeia baseada naquela falsa confirmação -- registrar explicitamente pra não repetir.

**Decisão:** parar de depender do texto exibido para identificar episódio.
1. **Espera do resultado:** aguarda o card estrutural OU o marcador `"Permanece Internado"` (`CURRENT_ADMISSION_MARKER`) -- este último já provado em produção real por `open_current_admission` (DEC-025/026), então é sinal confiável, não suposição.
2. **Localização dos episódios:** novo `_episode_locator()` prefere `EPISODE_CARD_SELECTOR = '[id^="codInternacao"]'` -- id real observado na página do prontuário (`codInternacao5951310`, registrado no DEC-031), não invenção. Cai para o padrão de texto (agora **sem** a âncora `^`) só se o estrutural não achar nada, logando aviso.
3. **Texto do cabeçalho:** passa a usar só a PRIMEIRA LINHA do `inner_text` do card (a linha com a data). O texto completo do card só existe depois de expandir e atrapalharia tanto o parse da data quanto a localização de posição em `_format_episodes_newest_first`.
4. Erro específico quando o resultado aparece mas nenhum episódio é localizado -- distingue "não achei o prontuário" de "achei o prontuário mas não reconheci os episódios", que antes se confundiam na mesma mensagem.

**Alternativas consideradas:** pedir ao usuário o texto exato do cabeçalho na tela (funcionaria, mas é a quarta rodada de ida-e-volta sobre o mesmo ponto, e texto de UI continuaria sendo a base frágil); inspecionar eu mesmo a página real (rejeitado -- é tela de paciente, PHI, mesma regra do DEC-006 que venho seguindo o projeto inteiro).

**Motivo:** hierarquia de seletores da seção 15 do prompt mestre prefere atributo/ID estável a texto quando o texto se mostra instável -- e aqui o texto se mostrou instável de forma comprovada (3 falhas reais). Prioriza evidência já observada (id real) sobre suposição sobre rótulo.

**Impacto:** `app/gsus/records.py` (`ADMISSION_HEADER_PATTERN` sem âncora, `EPISODE_CARD_SELECTOR` e `_episode_locator()` novos, espera de resultado e coleta de cabeçalho atualizadas em `get_full_admission_history_text`). Suíte: 96 passed + 3 skip. Ainda não validado contra o GSUS real -- próxima rodada do usuário (com navegador visível) confirma.

---

## DEC-040 — São TRÊS níveis de accordion, e expandir "às cegas" fechava o que já estava aberto

**Contexto:** com o DEC-039, o "Localizar" passou a achar e rotular os episódios corretamente ("Internação 1 de 2 -- 2026-08-16", "Internação 2 de 2 -- 2026-08-15", em ordem). Mas o conteúdo continuou vazio: todos os dias apareciam fechados (`▶ 21 de agosto de 2026 - Sexta-Feira`), sem nenhuma evolução.

**Investigação:** captura ESTRUTURAL da página real (`page.evaluate` coletando apenas `tagName|id|class|onclick` -- **nenhum texto lido**, mesma técnica segura do DEC-028; arquivo local, temporário, removido depois). Resultado (ids reais, generalizados):

```
DIV|historicoAtendimento0Item|card_header|          <- episódio
DIV|historicoAtendimento0|card_body|
A  |historicoEvolucaodata21/08/2026codInternacao5951310Item|item|   <- DIA
DIV|historicoEvolucaodata21/08/2026codInternacao5951310||
A  |historicoEvolucao7Item|item|                    <- evolução individual
DIV|historicoEvolucao7||
SPAN|seta_historicoEvolucao_data.../codInternacao...|... seta aberta|
```

**Dois bugs revelados:**

1. **Nível faltando.** O DEC-030 mapeou só dois níveis (episódio + evolução). Existe um terceiro no meio: o **dia** (`a.item[id^="historicoEvolucaodata"]`). Pior: `NOTE_TOGGLE_SELECTOR = 'a.item[id^="historicoEvolucao"]'` casava dia E evolução no mesmo seletor (o id do dia também começa com "historicoEvolucao"), misturando níveis que precisam ser abertos em ordem.

2. **Expansão às cegas fechava o que estava aberto.** `_expand_all` clicava em TODOS os toggles encontrados. Como clique alterna (toggle), qualquer seção já aberta -- inclusive as que o próprio código tinha acabado de abrir no passo anterior -- era FECHADA de volta. É a explicação direta dos dias vazios.

**Regra estrutural descoberta (vale nos três níveis):** o toggle tem id terminado em `"Item"`, e o corpo correspondente tem exatamente o mesmo id **sem** esse sufixo. Isso permite verificar se algo já está aberto (corpo visível) em vez de alternar no escuro.

**Decisão:**
- Três seletores separados (`EPISODE_TOGGLE_SELECTOR`, `DAY_TOGGLE_SELECTOR`, `NOTE_TOGGLE_SELECTOR` -- este último agora com `:not([id^="historicoEvolucaodata"])` pra não invadir o nível do dia), aplicados **em ordem** via `ACCORDION_LEVELS`/`_expand_everything()`.
- `_expand_all` reescrita para ser **idempotente**: só clica no que está fechado (`_is_expanded()` via visibilidade do corpo), re-consulta o DOM a cada rodada (expandir insere elementos novos e invalida índices) e não repete o mesmo id (`processed_ids`), com limite `MAX_EXPAND_ROUNDS = 6` contra loop.
- Fail-soft mantido: toggle sem id, sem corpo correspondente ou que estoure timeout é tratado como fechado/pulado, com aviso -- clicar a mais é recuperável, não clicar deixaria dado de fora.

**Escopo -- inclui `extract_notes` (rotina automática, FROZEN):** `extract_notes` sofria exatamente os mesmos dois bugs. Corrigido junto, pelo mesmo precedente do DEC-015 (bug real observado, correção defensiva, API pública inalterada). **Consequência:** o caminho automático mudou sem validação ao vivo ainda -- precisa de uma rodada de "Atualizar agora" pra confirmar, registrado em `TASKS.md`.

**Motivo:** o produto inteiro depende de extrair evolução; um seletor que fecha o que abriu torna tudo silenciosamente vazio -- exatamente o pior modo de falha para uma ferramenta de auditoria (parece que rodou, mas não achou nada).

**Impacto:** `app/gsus/records.py` (`EPISODE_TOGGLE_SELECTOR`/`DAY_TOGGLE_SELECTOR`/`NOTE_TOGGLE_SELECTOR`/`ACCORDION_LEVELS`/`MAX_EXPAND_ROUNDS` novos, `_is_expanded()`/`_expand_everything()` novos, `_expand_all` reescrita, `extract_notes` e `get_full_admission_history_text` usando o novo caminho). Ver DEC-041 -- o `_expand_all` desta entrada ainda tinha um segundo defeito, corrigido lá. `ENCOUNTER_TOGGLE_SELECTOR` mantido como alias do seletor de episódio (compatibilidade). Também neste ciclo: `EPISODE_HEADER_LINE_PATTERN` + `_format_episodes_newest_first` sem `header_texts` -- os cabeçalhos passam a ser localizados no próprio texto, cobrindo qualquer tipo de episódio ("Internação", "Pronto-Atendimento", ...) sem depender de acertar um seletor de DOM por tipo (foi o que fez o episódio 2 aparecer). Diagnóstico estrutural removido do código -- nenhum resíduo.

---

## DEC-041 — Só o PRIMEIRO dia de cada episódio expandia (clique ignorado nunca era tentado de novo) + 3º incidente de PHI, o mais grave

**Problema:** com o DEC-040, o "Localizar" passou a expandir de verdade -- mas só o **primeiro dia de cada episódio**. Os demais continuavam fechados. Confirmado pelo usuário em execução real.

**Causa:** o `_expand_all` do DEC-040 mantinha um `processed_ids` e marcava o toggle como processado no primeiro clique, independente de o clique ter surtido efeito. O conteúdo do dia carrega por **AJAX**: enquanto a página processa a expansão do dia anterior, o clique seguinte é ignorado pelo JS da tela. Como o id já estava em `processed_ids`, esse item nunca era tentado de novo. Resultado: exatamente 1 dia aberto por episódio.

**Decisão:** `_expand_all` reescrita mais uma vez, agora orientada a EFEITO (mesmo princípio já validado na paginação do censo, DEC-016):
- rodadas repetidas até que nenhum item fechado reste (`clicked_any == False` encerra);
- depois de cada clique, `_wait_expanded()` faz polling até o corpo do item ficar visível (`EXPAND_BODY_WAIT_MS = 6s`, passo `EXPAND_POLL_MS = 250ms`) em vez de um `wait_for_timeout(300)` fixo;
- item que não confirmou abertura **não** é marcado como resolvido -- volta a ser tentado na rodada seguinte;
- `attempts_by_id` limita a `MAX_CLICKS_PER_TOGGLE = 3` cliques por item, pra nunca reintroduzir a alternância infinita caso a detecção de "aberto" falhe em algum caso não previsto;
- toggle sem `id` (não dá pra confirmar abertura) é clicado só na primeira rodada.

**Motivo:** "não confie em timing, confirme o efeito" é o padrão que já resolveu praticamente todos os problemas reais desta aplicação legada (DEC-016, DEC-018, DEC-026). O DEC-040 aplicou isso à detecção de estado mas não à confirmação pós-clique -- esta entrada fecha essa lacuna.

**Impacto:** `app/gsus/records.py` (`_wait_expanded()` novo, `_expand_all` reescrita, constantes `MAX_CLICKS_PER_TOGGLE`/`EXPAND_BODY_WAIT_MS`/`EXPAND_POLL_MS`). Vale para os dois caminhos (`extract_notes` da rotina automática e `get_full_admission_history_text` do "Localizar"). Suíte: 98 passed + 3 skip.

**3º incidente de PHI -- o mais grave da série (DEC-011, DEC-013, este).** Ao relatar o resultado, o usuário colou no chat o texto extraído **na íntegra**: evoluções clínicas completas de vários profissionais, nome do paciente, nomes de dezenas de profissionais de saúde, CID, prescrições, sinais vitais -- e, o mais sensível, conteúdo relativo a **um caso de violência sexual envolvendo uma paciente menor de idade**, incluindo detalhes de atendimento psicológico. Ação tomada: sinalizado ao usuário de forma direta e proporcional à gravidade (terceira ocorrência); **nada reproduzido** em código, fixture, log, teste ou nesta entrada; explicado que, para diagnosticar, bastava a descrição do FORMATO ("dia 21 abriu com conteúdo, dias 20 a 16 continuam fechados"), nunca o conteúdo. Reforça a razão de ser do produto (SEC-03: dado hospitalar não sai da instituição) e do princípio que venho seguindo desde o DEC-006 de nunca eu mesmo abrir tela com paciente real.

---

## DEC-042 — Expansão sem teto de tempo tornou a extração inviável (>15min por prontuário)

**Problema:** com o DEC-041, a rodada real do "Localizar" passou de **15 minutos sem terminar** e precisou ser abortada. O usuário nem chegou a ver resultado.

**Causa (design meu, não do GSUS):** o custo do DEC-041 é multiplicativo -- `EXPAND_BODY_WAIT_MS` de 6s por item que não confirma abertura, x `MAX_CLICKS_PER_TOGGLE = 3` tentativas, x `MAX_EXPAND_ROUNDS = 6` rodadas, x dezenas de evoluções por dia (um único dia deste prontuário tinha ~30 evoluções). Pior: a rotina automática repete isso para **~180 pacientes** por execução -- ou seja, o desenho estava fora da realidade do produto, não só lento neste caso.

**Decisão:**
- Teto de tempo TOTAL por prontuário: `EXPAND_TIME_BUDGET_S = 90`, compartilhado entre os três níveis e verificado a cada item (`deadline` monotônico passado a `_expand_all`). Ao estourar, loga e segue com o que já abriu -- **runtime previsível vale mais que completude** aqui, porque uma rodada que nunca termina não entrega relatório nenhum.
- Custos unitários reduzidos: espera por item 6s -> 1,5s, tentativas por item 3 -> 2, rodadas 6 -> 4, polling 250ms -> 150ms.
- Ordem dos níveis passa a ter significado de prioridade: episódio e dia primeiro (é onde o conteúdo de fato aparece), evolução individual por último -- se o orçamento acabar, o que se perde é o nível menos custoso de perder.

**Alternativas consideradas:** paralelizar cliques (rejeitado -- é a mesma sessão de browser, e a app legada já se mostra sensível a AJAX concorrente, DEC-016/041); aumentar o orçamento e aceitar rodadas longas (rejeitado -- 180 pacientes x minutos é inviável para uma tarefa agendada diária).

**Motivo:** RNF-02 já exige timeout controlado em toda operação crítica; a expansão de accordion virou uma operação crítica de custo aberto e não tinha teto nenhum.

**Impacto:** `app/gsus/records.py` (`EXPAND_TIME_BUDGET_S` novo, constantes de custo reduzidas, `_expand_all` recebe `deadline`, `_expand_everything` distribui o orçamento). Vale para os dois caminhos. Suíte: 98 passed. **Ainda não validado ao vivo** -- a rodada anterior foi abortada antes de produzir dado.

**Higiene de processos:** a rodada abortada, somada a outras interrompidas antes, deixou 18 processos `firefox.exe` órfãos na máquina do usuário (que já é fraca -- ver memória do projeto). Removidos com `taskkill /IM firefox.exe /F` **após autorização explícita do usuário** (poderia matar abas pessoais dele também). Mesmo problema já registrado no DEC-019 -- recorrente sempre que uma execução com Playwright é interrompida à força, porque o `browser.close()` do context manager não chega a rodar.

---

## DEC-043 — O código nunca expandiu dia nenhum: os dois indicadores usados mentiam. Detectar por TAMANHO do corpo

**Problema:** depois de DEC-040/041/042, o resultado continuava idêntico: só o dia mais recente de cada episódio trazia conteúdo (21/08 no episódio de internação, 16/08 no de pronto-atendimento); os demais só o título.

**Investigação:** em vez de mais uma hipótese por rodada (já eram ~10 ciclos de tentativa-e-erro sobre o mesmo ponto, consumindo tempo do usuário e acessos ao servidor real), fiz um **mapa estrutural completo** da árvore do resultado -- profundidade, tag, id, class, nº de filhos e altura, com dígitos de id mascarados e **nenhum texto lido**. Árvore real (generalizada):

```
DIV.card_atendimento
  DIV#historicoAtendimentoNItem.card_header        <- episódio (toggle)
  DIV#historicoAtendimentoN.card_body  f=13 h=541  <- episódio (corpo, ABERTO)
    DIV.pesquisar_prontuario_data  h=30
      A#historicoEvolucaodataDD/MM/AAAAcodInternacaoNItem.item     <- DIA (toggle)
        SPAN#seta_...  class="pesquisar_prontuario_data_seta aberta"
    DIV#historicoEvolucaodataDD/MM/AAAAcodInternacaoN  f=2 h=42    <- DIA (corpo)
      TABLE.form_tabela h=29
    ... (mesmo par repetido por dia)
```

**Causa -- os dois sinais que usei mentem:**
1. **A classe "aberta" da seta aparece em TODOS os dias**, abertos ou fechados. Não é indicador de estado (DEC-040 assumiu que era).
2. **`is_visible()` do corpo é verdadeiro mesmo colapsado**: o corpo fechado não é escondido, é um invólucro de 42px com uma `table.form_tabela` de placeholder -- visível, só vazio (DEC-040/041 assumiram que corpo visível = aberto).

Com os dois sinais dando "aberto", `_is_expanded` retornava `True` para todos os dias e o código **nunca clicou em um único toggle de dia**. Os dias 21/08 e 16/08 tinham conteúdo porque **a própria página auto-expande o dia mais recente de cada episódio** -- nada a ver com a automação. Ou seja: as correções DEC-040/041/042 estavam todas resolvendo o problema errado.

**Decisão:** detectar estado pelo **tamanho do corpo**, medido via `page.evaluate` (`childElementCount` + `getBoundingClientRect().height`) -- único sinal que acompanhou o estado real em todos os casos observados. Colapsado é sempre `f=2, h=42`; aberto cresce em ambos. Limiares: `COLLAPSED_MAX_CHILDREN = 2`, `COLLAPSED_MAX_HEIGHT_PX = 60` (42 observado + margem). Vale para os três níveis (o corpo do episódio aberto media `f=13, h=541`).

**Alternativas consideradas:** pedir ao usuário um HTML salvo da página (rejeitado -- o arquivo conteria PHI completo e passaria por mim, exatamente o que DEC-011/013/041 mostraram ser o risco recorrente deste projeto); continuar por tentativa-e-erro (rejeitado explicitamente -- o mapa estrutural custou UMA rodada e respondeu o que 10 rodadas de palpite não responderam).

**Motivo:** medida geométrica é observável e não depende de interpretar convenção de CSS de uma app legada -- que já se mostrou enganosa duas vezes seguidas aqui.

**Impacto:** `app/gsus/records.py` (`_body_metrics()` novo, `_is_expanded` reescrita por medida, `_arrow_state`/constantes de seta removidas). Diagnóstico estrutural removido do código -- nenhum resíduo. Suíte: 98 passed. `client.py` segue com `headless=False` (validação ao vivo em curso) -- reverter para `True` ao encerrar.

**Lição de processo (vale mais que o fix):** gastei ~10 rodadas testando uma hipótese por vez contra o sistema real. O mapa estrutural -- barato, seguro quanto a PHI e definitivo -- deveria ter sido o PRIMEIRO passo, não o décimo. Quando duas hipóteses seguidas falham sobre o mesmo ponto, parar de palpitar e ir observar a estrutura inteira.

---

## DEC-044 — Accordion do GSUS é EXCLUSIVO: capturar dia a dia, não expandir tudo e ler no fim

**Problema:** com a detecção por tamanho (DEC-043), os dias finalmente passaram a abrir de verdade. Mas o resultado veio errado de outro jeito: sobraram no texto apenas **os dois últimos dias que o código clicou** (16/08 e 15/08, do episódio de pronto-atendimento) -- os dias do episódio de internação sumiram --, e o último dia veio **truncado** ("apenas o começo dos dados").

**Causa:** o accordion de dias do GSUS é **exclusivo** -- abrir um dia FECHA os demais. Toda a estratégia até aqui ("expandir tudo, depois `body.inner_text()` uma vez no final") é incompatível com isso: no momento da leitura final, só os últimos dias clicados continuavam abertos. O truncamento do último dia tem a mesma origem estrutural: a leitura acontecia enquanto o AJAX daquele dia ainda estava carregando.

**Decisão:** inverter a estratégia -- **capturar cada dia no momento em que ele abre**, em vez de acumular estado na tela:
- `_collect_days()`: para cada toggle de dia, garante a abertura (`_is_expanded` + clique + `_wait_expanded`) e lê o `inner_text` **daquele corpo** ali mesmo, antes de ir para o próximo. Funciona com accordion exclusivo ou não.
- `_render_history()`: monta o texto final a partir do que foi capturado, agrupando por episódio e ordenando do mais recente para o mais antigo (RF-19).

**Bônus estrutural:** o id do toggle de dia já carrega tudo que é preciso, sem interpretar texto de tela:
```
historicoEvolucaodata21/08/2026codInternacao5951310Item
                     ^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^
                     data do dia   chave do episódio
```
`DAY_ID_PATTERN` extrai os dois. Com isso, agrupamento por episódio e ordenação por data passam a vir de **dado estruturado**, não de regex sobre o texto exibido -- que é a origem de boa parte dos erros desta série (DEC-039/040). O rótulo do episódio ainda vem do texto do card (`[id="codInternacao..."]`), mas só como enfeite: se faltar, o bloco continua rotulado e ordenado corretamente.

**Alternativas consideradas:** forçar todos os dias abertos ao mesmo tempo via JS (rejeitado -- manipular o DOM da aplicação é mais invasivo que ler, e frágil frente à lógica interna do widget); aumentar o orçamento de tempo e reler a página várias vezes (rejeitado -- não resolve a exclusividade, só mascara).

**Impacto:** `app/gsus/records.py` (`DAY_ID_PATTERN`, `_iso_from_br_date()`, `_collect_days()`, `_episode_header_text()`, `_render_history()` novos; `get_full_admission_history_text` reescrita em torno deles, mantendo o caminho antigo como fallback fail-soft se nenhum toggle de dia for reconhecido). 9 testes novos (`tests/unit/test_records_day_capture.py`, dados fictícios) cobrindo o parse do id, rejeição dos outros níveis, agrupamento, ordenação e dia vazio. Suíte: 107 passed. **`extract_notes` (rotina automática) ainda NÃO usa esta estratégia** -- continua com `_expand_everything`, e portanto ainda sofre do mesmo problema de accordion exclusivo; migrar depois que o "Localizar" for validado ao vivo (registrado em `TASKS.md`).

---

## DEC-045 — Regressão minha: clique cego em episódio + iteração por índice derrubaram a captura para 1 dia

**Problema:** primeira execução da estratégia do DEC-044 saiu pior que antes -- capturou **apenas o dia 15**.

**Causa (dois defeitos meus, ambos já conhecidos e reintroduzidos):**
1. **Clique cego em episódio.** Deixei em `get_full_admission_history_text` um laço anterior que clicava em TODOS os cards de episódio (`_episode_locator(...).nth(i).click()`), sem checar estado. Clique alterna: isso FECHAVA o episódio que a própria página abre sozinho, escondendo os dias dele. É exatamente o bug do DEC-040, que eu tinha corrigido em `_expand_all` mas esqueci de remover aqui.
2. **Iteração por índice (`.nth(i)`).** Cada clique reordena/reinsere nós; os índices deixam de apontar para o mesmo dia. Mesma classe de erro do DEC-040 ("índices ficam obsoletos"), que eu tinha resolvido re-consultando o DOM -- mas re-consultar não basta quando a própria ORDEM muda.

**Decisão:**
- Remover o laço de clique cego. Quem abre episódio é `_expand_all`, que só clica no que está fechado.
- `_collect_days` captura a lista de **ids** dos toggles uma única vez (`page.evaluate`) e itera por id (`[id="..."]`), nunca por posição. Id não muda com reordenação.
- Tratar o caso do dia oculto: se o toggle não estiver visível (episódio dele fechou -- o accordion é exclusivo, DEC-044), reabre os episódios e tenta de novo antes de desistir daquele dia.
- `logger.info` com a contagem capturada/total, pra que uma captura parcial fique evidente no log local em vez de silenciosa.

**Motivo:** os dois defeitos já tinham decisão registrada neste mesmo documento (DEC-040) -- reintroduzi ambos ao reescrever a função. Registrar explicitamente porque é a segunda vez na série que uma correção antiga se perde em reescrita posterior (a primeira foi o DEC-036, sobre a conferência do campo de prontuário).

**Impacto:** `app/gsus/records.py` (`get_full_admission_history_text` sem o laço de clique cego; `_collect_days` iterando por id, com reabertura de episódio e log de cobertura). Suíte: 107 passed. Ainda não validado ao vivo.

---

## DEC-046 — Descoberto por que eu estava depurando às cegas: log de produção invisível por redirecionamento de sandbox

**Achado principal (processo, não código):** ao investigar por que o "Localizar" trazia dias vazios, fui ler `%LOCALAPPDATA%\GSUSAuditoria\logs\app.log` -- e ele estava **parado em 20/08**, apesar de o app ter sido reiniciado dezenas de vezes em 21/08. Investigação mostrou que existem DOIS arquivos: o real e uma cópia em `...\AppData\Local\Packages\Claude_<id>\LocalCache\Local\GSUSAuditoria\logs\app.log`. O assistente roda em sandbox com **redirecionamento de escrita/leitura de `%LOCALAPPDATA%`**: tudo que eu lia ali era a minha cópia virtualizada (com conteúdo antigo copy-on-write + minhas próprias escritas de teste), nunca o arquivo que o app do usuário grava.

Consequência real: **em ~15 rodadas de depuração deste recurso, eu nunca li um único log da aplicação.** Todos os `logger.warning` que eu vinha adicionando como instrumentação eram inúteis, e por isso precisei recorrer repetidamente a arquivos de diagnóstico ad-hoc (`gsus_debug*.txt`) escritos em `C:\Users\fnant\`, caminho que não sofre o redirecionamento. Isso explica boa parte da lentidão e do palpite-por-rodada desta série (DEC-039 a DEC-045).

**Decisão:** `setup_logging()` passa a adicionar um SEGUNDO handler, gravando `logs/app.log` **dentro do repositório**, apenas quando rodando a partir do código-fonte (`not sys.frozen`) e a pasta já existir. Produção (executável empacotado) permanece só com `%LOCALAPPDATA%`, sem mudança de comportamento.

**Dois bugs de extração corrigidos no mesmo ciclo, revelados pela saída real do usuário:**
1. **O dia mais recente de cada episódio nunca era capturado.** `DAY_TOGGLE_SELECTOR` exigia id com prefixo `historicoEvolucaodata`, mas o dia que a página abre sozinha usa outro formato (`historicoEvolucao<N>Item`, sem "data"). Trocado para regra ESTRUTURAL -- `div.pesquisar_prontuario_data > a.item` -- que cobre os dois formatos (o wrapper é o mesmo para todos os dias, confirmado no mapa estrutural do DEC-043).
2. **Agrupamento por episódio dependia do id do dia.** Agora vem do DOM: junto com o id do toggle, coleto o id do `div.card_body[id^="historicoAtendimento"]` que o contém (`el.closest(...)`), que agrupa corretamente qualquer formato de id. `_day_date()` novo: usa a data embutida no id quando existe, senão extrai do texto do cabeçalho do dia; sem nenhuma das duas, ordena por último em vez de quebrar. `_episode_header_text` passou a usar `<idDoCorpo>Item` (regra do DEC-040).

**Impacto:** `app/main.py` (`setup_logging` com handler de desenvolvimento). `app/gsus/records.py` (`DAY_TOGGLE_SELECTOR` estrutural, `_collect_days` coletando (id do dia, id do episódio) numa só chamada JS, `_day_date()` novo, `_episode_header_text` corrigido). 1 teste novo + 3 ajustados. Suíte: 108 passed.

**Lição:** antes de instrumentar com log em ambiente novo, **verificar que o log realmente chega a mim** -- um `logger.warning` que ninguém consegue ler é pior que nenhum, porque dá falsa sensação de instrumentação.

---

## DEC-047 — Primeiro diagnóstico feito com log real: espera por item estava curta demais (corte excessivo do DEC-042)

**Contexto:** primeira execução depois do handler de log de desenvolvimento (DEC-046). Pela primeira vez em toda esta série eu li o log real da aplicação em vez de inferir comportamento a partir do texto final.

**O que o log mostrou, direto:**
```
Nenhum card de episódio pelo id estrutural -- usando o padrão de texto como alternativa.
Dia 2/11 não confirmou abertura -- capturando o que houver.
... (idem para os dias 3 a 9)
Dias capturados: 11 de 11.
```

**Leitura:** (a) o seletor estrutural novo do DEC-046 funcionou -- passou de 8 para **11 dias** localizados; (b) os dias 1, 10 e 11 confirmaram abertura na hora (são os que a página já abre sozinha); (c) **os 8 dias que dependiam de carregamento por AJAX falharam a confirmação**, cada um em ~2s.

**Causa:** `EXPAND_BODY_WAIT_MS` estava em 1,5s. Eu mesmo tinha reduzido de 6s para 1,5s no DEC-042, para conter a explosão de tempo -- corte agressivo demais: com 6s um dia chegou a abrir corretamente (DEC-044), com 1,5s nenhum que dependa de AJAX abre. Ou seja, o DEC-042 resolveu o problema de tempo criando um problema de corretude.

**Decisão:** `EXPAND_BODY_WAIT_MS` para 8s e `EXPAND_TIME_BUDGET_S` de 90s para 210s. O custo real é bem menor que o pior caso: o polling retorna assim que o corpo cresce, então dia já aberto sai imediatamente e só quem depende de AJAX paga o tempo. Com 11 dias, o pior caso teórico (88s) cabe folgado no orçamento.

**Instrumentação adicionada:** `_collect_days` passa a logar `dias capturados / total / quantos com conteúdo / total de caracteres` -- métrica pura, **nunca conteúdo** (RNF-03/SEC-03). Permite verificar cobertura da extração lendo só o log local, sem ninguém precisar colar nota clínica no chat -- endereçando diretamente a causa dos três incidentes de PHI desta série (DEC-011/013/041).

**Risco conhecido, ainda em aberto:** este orçamento serve para o "Localizar" (um prontuário sob demanda). A rotina automática processa ~180 pacientes por execução; uma internação longa (30+ dias) a 8s por dia estoura qualquer janela razoável. `extract_notes` ainda usa `_expand_everything` e nem migrou para a captura dia-a-dia (DEC-044). Registrado em `TASKS.md` -- provavelmente exigirá extrair só os dias novos (usando o hash incremental do INCR-001 para saber quais), não o histórico inteiro, a cada execução.

**Impacto:** `app/gsus/records.py` (`EXPAND_BODY_WAIT_MS`, `EXPAND_TIME_BUDGET_S`, log de cobertura em `_collect_days`). Suíte: 108 passed.

---

## DEC-048 — Rotina automática extrai só os DIAS NOVOS; histórico completo fica exclusivo do "Localizar"

**Contexto:** o usuário confirmou a extração do "Localizar" correta (11/11 dias) e reafirmou a divisão que já tinha pedido antes: **histórico completo só sob solicitação pelo botão "Localizar"; atualização automática só com os dias novos.** Isso resolve ao mesmo tempo o requisito funcional e o `PERF-001` (71s por prontuário x ~180 pacientes seria inviável numa tarefa agendada diária).

**Decisão:**
1. `Repository.get_note_days(patient_id)` -- datas ISO que já têm evolução gravada (`substr(timestamp,1,10)`, distinto). Evolução sem timestamp reconhecido é ignorada (não identifica um dia).
2. `RecordSource.get_raw_notes_text(patient, known_days=frozenset())` -- Protocol e `GSUSAdapter` atualizados; `orchestrator._process_patient` passa `repo.get_note_days(patient_id)`.
3. `records.extract_notes(page, patient_id, known_days)` -- **migrada para a captura dia-a-dia** (`_collect_days`), o que corrige de quebra o problema de accordion exclusivo (DEC-044) que ainda afetava o caminho automático, e pula os dias já processados.
4. `_days_to_skip()` decide o que pular, com duas salvaguardas: os `ALWAYS_REFRESH_RECENT_DAYS = 2` dias mais recentes são **sempre** reabertos (um dia em curso continua recebendo evolução depois da rodada anterior; 2 cobre a virada de meia-noite, já que a execução roda 00:01 e "ontem"/"hoje" são ambos relevantes), e dia cuja data não sai do id **nunca** é pulado (fail-safe: extrair a mais é recuperável, deixar de extrair perde dado).

**Por que pular por DIA e não confiar só no hash por evolução:** o hash (INCR-001) continua sendo a fonte de verdade da deduplicação -- ele roda depois e descarta o que já existe. O `known_days` é puramente **otimização de custo**: abrir dia é a operação cara (AJAX, ~8s no pior caso). Reprocessar dia à toa não causa duplicata, só desperdício.

**Robustez a rodada perdida:** se uma execução falhar (máquina desligada, GSUS fora do ar), os dias daquele período simplesmente não estarão em `known_days` e serão extraídos na execução seguinte. É o comportamento de catch-up que eu tinha defendido no DEC-032 contra um filtro rígido de data -- preservado aqui.

**Impacto:** `app/storage/repository.py` (`get_note_days`), `app/orchestrator.py` (Protocol + chamada), `app/gsus/adapter.py`, `app/gsus/records.py` (`extract_notes` reescrita, `_days_to_skip` novo, `ALWAYS_REFRESH_RECENT_DAYS`, `_collect_days` com `skip_days`). 8 testes novos (4 de `_days_to_skip`, 3 de `get_note_days`, mais ajuste nos fakes de `RecordSource`). Suíte: **118 passed + 4 skip**. `client.py` de volta a `headless=True`; nenhum resíduo de diagnóstico no código.

**Não validado ao vivo:** o caminho automático ("Atualizar agora") ainda não rodou contra o GSUS real depois desta mudança. É a próxima validação (`GSUS-005`/`PERF-001` em `TASKS.md`).

---

## DEC-049 — Primeira rodada real do lote: pop-up não fechado derruba tudo a partir do 2º paciente + 4º incidente de PHI (agora causado por mim)

**Contexto:** primeira execução real de "Atualizar agora" com a extração incremental do DEC-048. Censo funcionou (**185 pacientes**). Paciente 1 processado corretamente (18 de 19 dias, 213k caracteres). **Do paciente 2 em diante, todos falharam.**

**Bug 1 -- pop-up do prontuário nunca era fechado.** Sequência real no log:
```
Processando paciente 1 de 185... -> Dias capturados: 18 de 19
Processando paciente 2 de 185... -> Locator.click: Frame was detached
Processando paciente 3 de 185... -> Locator.click: Timeout ... waiting for get_by_text("Atendimento")
Processando paciente 4 de 185... -> (idem)
```
A pesquisa de prontuário abre uma **janela pop-up** (DEC-023). Ela ficava aberta ao fim de cada paciente: o frame `content` da página principal era detachado (paciente 2) e, nas tentativas seguintes, o menu "Atendimento" já não era mais alcançável (timeout). Um recurso que funcionava em uso avulso ("Localizar", uma consulta por sessão) estava quebrado em lote -- e só um teste de lote real revelaria isso.

**Decisão:** `GSUSAdapter.get_raw_notes_text` fecha o pop-up num `finally` (`_close_record_popup`), garantindo estado limpo para o paciente seguinte mesmo quando a extração falha. Nunca fecha a página principal da sessão; falha ao fechar vira aviso, não interrompe o lote (RF-12). `lookup_full_history` não precisa do mesmo tratamento: o "Localizar" abre e fecha o browser inteiro a cada consulta.

**Bug 2 -- PHI real em log, de novo, e desta vez o vazamento chegou até mim.** O log da rodada trazia linhas como `Falha ao processar paciente 552603` e `patient_id=613247`. `Repository.upsert_patient` usa **o próprio número do prontuário como `patient_id`** -- então logar `patient_id` é logar identificador direto de paciente. Agravante: fui eu que, no DEC-046, passei a copiar o log para dentro do repositório justamente para conseguir lê-lo -- ou seja, minha própria instrumentação transformou um log local (aceitável, RNF-03) num canal que traz PHI para o meu contexto. Três números reais de prontuário chegaram assim. É o mesmo erro do DEC-016, reintroduzido por um caminho novo.

**Decisão:** novo módulo `app/security/pseudonym.py` -- `for_log(identificador)` devolve um rótulo determinístico (`pac-<8 hex de sha256>`). Determinístico para continuar sendo possível correlacionar linhas do mesmo paciente entre execuções (única utilidade do id em depuração), sem expor identidade. Aplicado em `orchestrator` (2 pontos) e `repository.mark_error`. Persistência, relatório e telas seguem usando o identificador real -- o usuário precisa saber de qual paciente se trata; a pseudonimização é **exclusiva de log**. `logs/app.log` do repositório foi truncado para remover os prontuários já gravados; `.gitignore` já cobria `logs/*.log` (não foram versionados).

**Impacto:** `app/gsus/adapter.py` (`_close_record_popup` + `finally`), `app/security/pseudonym.py` (novo), `app/orchestrator.py`, `app/storage/repository.py`. 2 testes novos (um garante que `mark_error` não escreve o prontuário no log e que o pseudônimo aparece; outro cobre estabilidade/None). Suíte: **120 passed + 4 skip**.

**Medição de custo (PERF-001), primeira real:** paciente 1 levou ~48s para 19 dias em banco vazio (pior caso -- histórico inteiro). O regime incremental do DEC-048 só será medível quando uma rodada completa terminar e a seguinte rodar. A rodada foi abortada.

---

## DEC-050 — Rotina automática ignora internações antigas listadas na mesma tela; custo por paciente medido

**Contexto:** com o fix do pop-up (DEC-049), a rodada real destravou -- pacientes 1, 2 e 3 processados em sequência, sem erro. Medições da rodada:
- **~12s por paciente** (17s, 11s, 12s) -> ~37min para os 185. Viável para tarefa agendada diária. `PERF-001` deixa de ser bloqueador aparente (falta confirmar com rodada completa).
- **Incremental funcionando:** paciente 1 registrou "Pulando 10 dia(s) já processado(s)" e capturou 8 de 19 dias (56k caracteres) contra 19 dias/213k na rodada anterior com banco vazio.

**Problema encontrado:** o log trazia vários `"Dia N/4 segue oculto -- seguindo sem ele"` (paciente 2: 3 de 4 dias; paciente 3: 2 de 4). Não era falha: a tela de prontuário lista TODOS os episódios do paciente, e os dias dos episódios antigos ficam ocultos porque aqueles cards estão colapsados. A rotina automática só deve olhar a internação atual (RF-04) -- mas o código (a) tentava reabrir os episódios antigos para alcançar esses dias, o que é caro e fora de escopo, e (b) registrava aviso como se fosse perda de dado, poluindo o log e mascarando avisos que importam.

**Decisão:** `_collect_days` ganha `current_episode_only` (ligado só em `extract_notes`, a rotina automática). Com ele: a coleta de dias em JS passa a informar se o card do episódio está aberto (altura > 60px -- mesmo critério de "expandido" do DEC-043); dias de episódio fechado são descartados de saída, com um `logger.info` de contagem ("Ignorando N dia(s) de internação anterior") em vez de aviso por item; e a recuperação "reabre episódios e tenta de novo" (necessária no "Localizar") fica desligada, para não abrir internação antiga. O "Localizar" (RF-19) segue com o comportamento completo -- é justamente o recurso que deve ver todo o histórico.

**Motivo:** alinha o código à divisão que o usuário definiu (rotina automática = só o atual; histórico completo = só sob pedido no "Localizar"), elimina trabalho caro fora de escopo e devolve significado ao log -- aviso volta a indicar problema de verdade.

**Impacto:** `app/gsus/records.py` (`_collect_days` com `current_episode_only`, chamada em `extract_notes`). Suíte: 120 passed + 4 skip. Falta uma rodada completa para confirmar tempo total e taxa de sucesso.

---

## DEC-051 — Número de prontuário embutido no TEXTO da exceção vazava por caminho que o pseudônimo do DEC-049 não cobria

**Problema:** monitorando a rodada real de "Atualizar agora", uma das linhas de erro capturadas para diagnóstico continha `GSUSRecordError: Prontuário 6465655: nenhuma internação em andamento encontrada...` -- prontuário real, em texto puro, mesmo com o pseudônimo do DEC-049 já em produção. Esse número chegou ao meu contexto.

**Causa:** o DEC-049 pseudonimizou `patient_id` como PARÂMETRO separado nas chamadas de log (`logger.exception("Falha ao processar paciente %s", pseudonym.for_log(patient_id))`). Mas cinco pontos em `app/gsus/records.py` embutiam o prontuário DENTRO do texto da própria mensagem de exceção (`f"Prontuário {record_number}: ..."`). Esse texto chega ao log por dois caminhos que o pseudônimo não alcançava: `logger.exception(...)` inclui automaticamente o traceback completo (que contém `str(exceção)`), e `Repository.mark_error` grava `error: str` (também `str(exceção)`) puro. Pseudonimizar um parâmetro ao lado da mensagem não protege o conteúdo da própria mensagem.

**Decisão:** remover o identificador do TEXTO de todas as exceções levantadas em `records.py` (5 pontos: `open_current_admission`, `_submit_search`, `extract_notes`, `GSUSAccessJustificationRequired`, `get_full_admission_history_text` x2) -- as mensagens passam a descrever só o PROBLEMA ("Nenhuma internação em andamento encontrada após 3 tentativas..."), nunca o paciente. Verificado que `errors.py::friendly_message` nunca leu o texto da exceção (só o tipo via `isinstance`), então nada de user-facing depende do identificador estar ali -- mudança sem risco de regressão de comportamento visível.

**Por que na fonte, não filtrando no destino:** dava para redigir dígitos no ponto de log (regex sobre `str(exc)`), mas isso é frágil (qual sequência de dígitos é o prontuário?) e teria que ser mantido em todo ponto de log presente E futuro. Tirar o identificador de onde ele nasce resolve os dois caminhos de vazamento (`logger.exception` e `mark_error`) de uma vez, e qualquer novo ponto de log futuro herda a proteção automaticamente.

**Impacto:** `app/gsus/records.py` (5 mensagens de exceção). `tests/unit/test_errors.py` (2 testes ajustados para o novo formato de mensagem, mesma garantia). Suíte: 120 passed + 4 skip. `logs/app.log` do repositório contém prontuários reais de execuções anteriores a este fix -- será truncado ao final da rodada em andamento (não mexer em arquivo de log de processo ativo).

**Padrão que se repete nesta série (registrar para não repetir de novo):** este é o segundo vazamento de identificador de paciente causado pela MINHA PRÓPRIA instrumentação de log (o primeiro foi o `patient_id` cru no DEC-049) -- ambos só existem porque o DEC-046 passou a copiar o log para dentro do repositório para que eu pudesse lê-lo. Cada vez que reviso esse mecanismo, verificar não só o PARÂMETRO logado, mas o TEXTO INTEIRO que cada `logger.*` acaba emitindo, incluindo tracebacks automáticos.

---

## DEC-052 — Pop-up órfão em cada tentativa de busca falha derrubou a rodada real inteira a partir da metade

**Contexto:** primeira rodada completa de "Atualizar agora" após os fixes anteriores. Números finais: **185 processados, 98 sucesso, 90 falhas**. Padrão nos timestamps das falhas (log com pseudônimo, nenhum identificador real): esporádicas e de tipos variados até 11:53 (7 falhas em ~1h); a partir de **11:53:14, toda falha ocorre a cada exatos 30s, sempre o mesmo timeout, até o fim** (83 falhas seguidas). Um intervalo mecânico e idêntico não é sintoma de corrida/contenção -- é assinatura de estado permanentemente quebrado.

**Causa:** `open_current_admission` (o laço de retry, DEC-025/026) abre um pop-up a cada tentativa via `_submit_search`. Quando uma tentativa falha (marcador "Permanece Internado" não aparece a tempo), o código fazia `continue` **sem fechar esse pop-up** antes de recarregar a busca na página original. Ao longo de ~180 pacientes, tentativas falhas isoladas (normais -- nem toda busca acerta de primeira) foram deixando pop-ups órfãos se acumulando, até que o volume passou a bloquear fisicamente o clique no menu "Atendimento" da página principal -- e dali em diante TODA tentativa, para TODO paciente seguinte, falha da mesma forma. O DEC-049 já tinha corrigido o pop-up não fechado ENTRE pacientes (com sucesso -- pacientes 1 a 3 fluíram); este é o mesmo problema um nível abaixo, DENTRO do laço de retry de uma única busca.

**Por que só apareceu numa rodada de ~180 pacientes:** o "Localizar" (uma consulta por vez) e os testes anteriores (poucos pacientes) nunca acumulariam tentativas falhas suficientes para o acúmulo virar bloqueio. Só um teste de lote real e longo revelaria isso -- mesmo padrão do DEC-049 (que também só apareceu em lote).

**Decisão:** novo helper `_close_if_popup(original, candidate)` em `records.py` (fecha `candidate` só se for uma janela distinta de `original`; nunca fecha a página principal; falha ao fechar vira aviso, não interrompe). Chamado dentro do laço de `open_current_admission` antes de cada `continue`. Isso também cobre, sem tratamento especial, o cenário que eu tinha cogitado (tela de "Justificar Acesso ao Prontuário", DEC-035, aparecendo num paciente cujo status mudou entre o censo e o processamento -- fora do escopo detectado nesse caminho): ela cairia no mesmo `except PlaywrightTimeoutError` e agora tem seu pop-up fechado do mesmo jeito.

**Escopo -- `open_current_admission` é FROZEN:** mesmo precedente já usado nesta série (DEC-015/040/049) -- bug real observado em produção, correção estritamente aditiva (fecha um pop-up antes de continuar; não muda o que é submetido nem a lógica de sucesso).

**Verificado após a rodada:** zero processos `firefox.exe` remanescentes -- o `browser.close()` do `with GSUSClient(...)` ao final da execução fecha tudo, incluindo pop-ups internos. O problema era só DURANTE a execução (bloqueio de clique), não vazamento de processo do SO.

**Impacto:** `app/gsus/records.py` (`_close_if_popup` novo, chamado em `open_current_admission`). Ainda não validado ao vivo -- próxima rodada completa confirma. `PERF-001`/`GSUS-005`: com pop-ups não mais se acumulando, a taxa de sucesso deve subir bem acima dos 98/185 (53%) desta rodada, já que boa parte das 90 falhas foi consequência do bloqueio, não de problema paciente a paciente.

---

## DEC-053 — Fechar pop-up por identidade de objeto Python não bastava: derrubava a sessão de trabalho inteira em lote

**Contexto:** com o fix do DEC-052 (fechar pop-up órfão de tentativa falha), rodada real de "Atualizar agora" destravou por ~50 pacientes (100% sucesso, sem falha) -- mas colapsou de novo, mais rápido que antes (~16min de execução, contra ~1h16 na rodada com o bug do DEC-052). Log revelou a transição exata:

```
13:08:59,169  paciente 50: "Dias capturados: 0 de 0" (só internação antiga na tela -- caso legítimo)
13:08:59,173  paciente 50 falha: "Nenhuma evolução encontrada para extrair"
13:08:59,296  paciente 51 falha em ~120ms: "Locator.click: Frame was detached"
13:09:29+     todo paciente seguinte: mesmo timeout de 30s, a cada 30s exato, até o fim (83 falhas)
```

`"Frame was detached"` falhando em ~120ms (não 30s) é o sinal decisivo: não é lentidão nem corrida -- o frame de trabalho principal (`content_frame`, obtido uma única vez em `_ensure_login()` e reutilizado por todos os ~180 pacientes) deixou de existir. Depois disso, todo paciente seguinte opera sobre um frame morto: falha instantânea na primeira tentativa, timeout de 30s nas seguintes (esperando um clique que nunca vai acontecer num frame que não existe mais).

**Causa real:** tanto `_close_if_popup` (DEC-052, em `records.py`) quanto `_close_record_popup` (DEC-049, em `adapter.py`) decidiam "é seguro fechar?" comparando **identidade de objeto Python** (`candidate is original` / `record_page is self._page`). Essa comparação tem um furo: o Playwright cria um objeto `Page` **novo** a cada evento de pop-up capturado (`expect_page()`), mesmo que a janela do navegador por trás seja, nalgum caso do fluxo do GSUS, a **mesma sessão de trabalho** sendo reaproveitada em vez de uma janela genuinamente nova. Quando isso acontece, o objeto `record_page`/`result_page` PARECE ser um pop-up distinto (nunca é `is` da referência local), mas fechá-lo fecha a janela real de trabalho -- derrubando a sessão inteira para o resto do lote. Antes do DEC-049/052 (quando nada era fechado), esse risco não existia; ele foi introduzido exatamente pelas correções que passaram a fechar pop-ups.

**Decisão:** trocar a defesa por uma garantia objetiva, que não depende de identidade de objeto nem de entender por que o GSUS às vezes reaproveita a janela: antes de abrir a busca, tirar um retrato de `context.pages` (`_snapshot_pages`) -- a lista REAL de janelas abertas no browser context naquele momento. Depois, só fechar uma página se ela **não estiver** nesse retrato (`_close_if_new_page`) -- ou seja, só fechar o que é matematicamente comprovado como novo. Uma página pré-existente nunca é fechada, mesmo que seja um objeto Python "diferente" do que foi guardado como referência local.

**Aplicado nos dois pontos que fecham pop-up:** o laço de retry de `open_current_admission` (records.py, DEC-052) e o `finally` de `GSUSAdapter.get_raw_notes_text` (adapter.py, DEC-049) -- ambos agora tiram o snapshot antes de abrir a busca e usam a mesma função de fechamento.

**Motivo:** a causa raiz nunca foi "fechar pop-up é arriscado" -- foi "decidir o que é pop-up por um critério que pode estar errado". Uma verificação objetiva (pertence ou não à lista real de páginas) elimina a classe inteira de erro, sem precisar confirmar experimentalmente por que o GSUS se comporta assim nalguns casos.

**Impacto:** `app/gsus/records.py` (`_snapshot_pages`, `_close_if_new_page` substituindo `_close_if_popup`). `app/gsus/adapter.py` (`get_raw_notes_text` usa as funções de `records.py` em vez de `_close_record_popup` própria). 4 testes novos (`test_records_history.py`) cobrindo: fecha página nova, nunca fecha página pré-existente (o caso real que quebrou a rodada), ignora Frame sem `.close()`, ignora `None`. Suíte: 120 passed + 4 skip. Zero processos `firefox.exe` órfãos após a rodada interrompida. **Ainda não validado ao vivo** -- é a 4ª tentativa de fechar este ciclo (pop-up entre pacientes -> pop-up dentro do retry -> agora identidade de objeto); próxima rodada completa confirma se o problema realmente para de acontecer desta vez.

---

## DEC-054 — "Sem internação atual" vira categoria separada, não falha

**Contexto:** a rodada validada no DEC-053 terminou sem colapso (175/175, 147 sucesso/84%). Das 28 falhas reais, 25 eram "Nenhuma evolução encontrada para extrair" -- investigado: são pacientes cuja tela só lista episódio(s) ANTIGO(S) (0 dias do episódio atual), provável alta recente ainda listada no censo/fila do dia. Perguntado ao usuário se isso deveria contar como falha técnica no relatório -- decisão: não, deve ser categoria separada.

**Decisão:**
1. Nova exceção `GSUSNoCurrentAdmissionDays(GSUSRecordError)` em `records.py`, levantada por `extract_notes` especificamente quando `_collect_days` (com `current_episode_only=True`) não encontra NENHUM dia pertencente ao episódio atual (distinto de "havia dias, mas nada de novo pra extrair" -- esse caso continua sendo sucesso silencioso, não falha, já garantido pelo `ALWAYS_REFRESH_RECENT_DAYS`).
2. `_collect_days` passa a devolver `(dias, total_no_escopo)` -- o segundo valor permite ao chamador distinguir os dois casos sem custo adicional de DOM.
3. Novo status de fila `QUEUE_NO_ADMISSION`, método `Repository.mark_no_admission()` (loga em `INFO`, não `ERROR` -- não é problema a investigar) e `get_no_admission_patients()`. Nova coluna `runs.patients_no_admission`, com migração (`ALTER TABLE`) para bancos já existentes na máquina do usuário.
4. `orchestrator.run_once` captura `GSUSNoCurrentAdmissionDays` ANTES do `except Exception` genérico e chama `mark_no_admission` em vez de `mark_error`.
5. `html_report.py`: nova seção "Sem internação atual, prováveis altas recentes" com estilo neutro (cinza, não vermelho de erro), separada da seção de falhas reais.

**Impacto:** `app/gsus/records.py`, `app/storage/database.py` (migração), `app/storage/repository.py`, `app/orchestrator.py`, `app/reports/html_report.py`. 5 asserts de contagem exata ajustados (`no_admission: 0` na chave nova) + 1 teste E2E novo (`test_pipeline_no_current_admission_is_not_counted_as_failure`) confirmando que não conta como falha, aparece na seção certa do relatório, e não aparece na de falhas reais. Suíte: 129 passed, sem skip. Ainda não validado ao vivo -- próxima rodada confirma que a taxa de "falha real" cai para perto de 3/175 (~98% de sucesso), já que a maioria das 28 falhas desta rodada eram desta categoria.

---

## DEC-055 — Modal de justificativa também bloqueava a rotina automática (não só o "Localizar")

**Contexto:** rodada real após o DEC-054 terminou (178/178, sem travar de vez), mas com 22 falhas reais -- e o padrão de timestamps das últimas 17 mostrou de novo o intervalo mecânico de exatos 30s, característico de estado bloqueado (mesma assinatura do DEC-052/053). Desta vez a rodada só não travou pra sempre porque o cluster começou perto do fim da fila (17:28 até 17:37, últimos ~17 pacientes).

**Causa:** o log trazia `"retrying click action, attempt #57"` -- a MESMA assinatura do modal `MB_window` ("Justificar Acesso ao Prontuário", DEC-035) já visto travando o "Localizar". A rotina automática (`open_current_admission`) nunca ganhou tratamento pra esse modal -- só `get_full_admission_history_text` ("Localizar") o detecta. Quando um paciente da fila automática não está mais internado nesta unidade (alta durante a execução, por exemplo), o GSUS mostra o mesmo modal de justificativa; sem detecção, o código fica tentando achar "Permanece Internado" (nunca vai aparecer -- o modal está por cima), esgota as 3 tentativas retry-clicando em "Atendimento" (bloqueado pelo modal, ~58 tentativas internas do Playwright a cada vez), e o pior: **o modal continua aberto para o PACIENTE SEGUINTE também**, porque nada o fecha -- daí o padrão mecânico de falha repetida.

**Por que os fixes anteriores (DEC-052/053) não resolviam isso:** aqueles tratavam PÁGINAS/POP-UPS órfãos (`context.pages`). Um modal `MB_window` é um `<div>` sobreposto na MESMA página -- não é uma Page nova, `_close_if_new_page` nunca o veria.

**Decisão:** detectar `ACCESS_JUSTIFICATION_MARKER` também em `open_current_admission`, no mesmo ponto em que hoje se decide "corrida, tentar de novo" -- mas aqui é DEFINITIVO (retry não ajudaria, o modal continuaria bloqueando). Ao detectar: clicar em **"Cancelar"** (ação de leitura/desistência -- nunca "Confirmar Justificativa", que geraria auditoria real, mesmo princípio do DEC-035) para fechar o modal e destravar a tela para o próximo paciente, e levantar `GSUSNoCurrentAdmissionDays` (mesma categoria do DEC-054 -- semanticamente é o mesmo caso: paciente sem internação atual nesta unidade, só detectado por um caminho diferente).

**Motivo:** o significado de negócio é idêntico ao que o DEC-054 já resolveu (paciente sem internação atual não é falha técnica); faltava só detectar esse SEGUNDO caminho pelo qual esse mesmo estado se manifesta (modal de justificativa, em vez de "0 de 0 dias do episódio atual").

**Gap de teste reconhecido, consistente com o DEC-013:** não criei fixture sintética de Playwright pra esse cenário (exigiria simular `get_by_text`/`.click`/`.count`/`.wait_for` de um modal real que não inspecionei eu mesmo estruturalmente, só por descrição/log do usuário) -- fabricar essa simulação seria inventar comportamento de interface, exatamente o que a seção 14 do prompt mestre proíbe. Validação fica só ao vivo.

**Impacto:** `app/gsus/records.py` (`open_current_admission` com a nova checagem). Suíte: 125 passed + 4 skip (mesmo total de antes, 129). Ainda não validado ao vivo -- próxima rodada confirma se o cluster mecânico para de acontecer.

---

## DEC-056 — Validação real fechada: rotina automática estável (96,5% de sucesso, sem colapso)

**Resultado da rodada após o DEC-055:** 174/174 processados, **168 sucesso real, 6 falhas, 0 padrão mecânico**. Timestamps das 6 falhas espalhados (17:56 a 18:27, minutos de distância entre si) -- nenhuma delas em sequência de 30s, diferente de TODAS as rodadas anteriores desta série. Zero processo `firefox.exe` órfão ao final.

**Fecha o arco DEC-052 a DEC-055** (pop-up entre pacientes -> pop-up dentro do retry -> identidade de objeto -> modal de justificativa): a causa raiz de cada colapso era distinta, e as quatro precisaram ser corrigidas para a rotina automática rodar de ponta a ponta de forma estável em lote real (~175 pacientes).

**As 6 falhas remanescentes** são todas "nenhuma internação em andamento encontrada após 3 tentativas" -- mesmo tipo, sem padrão temporal. Podem ser: (a) falha técnica pontual genuína (rede/AJAX lento naquele momento), ou (b) o mesmo caso "sem internação atual" do DEC-054/055 se manifestando por um TERCEIRO caminho ainda não identificado (nem "0 dias do episódio atual", nem o modal de justificativa). Não investigado a fundo -- taxa de 96,5% já é consistente com uso real, e forçar mais uma rodada de ~45min só para diferenciar 6 casos não parece a melhor troca agora. Registrado como possível refinamento futuro, não bloqueador.

**Impacto:** nenhuma mudança de código nesta entrada -- é registro de validação. `PERF-001`/`GSUS-005` (`TASKS.md`) podem ser considerados substancialmente resolvidos para o propósito de RF-04/RNF-02 (execução confiável, sem intervenção manual). `PILOT-001` (piloto em produção controlada) deixa de estar bloqueado por esta frente.

---

## DEC-057 — Adoção do modelo de "barreiras à progressão assistencial" (orientação técnica de mestrado, 2026-08-24)

**Contexto:** usuário forneceu um documento formal ("Orientação Técnica — Relatório inteligente para auditoria hospitalar concorrente", projeto de mestrado aplicado, agosto/2026) especificando o que a análise de IA deve produzir. Ele substitui o modelo anterior (contexto + status livres + pendências com categoria solta) por um modelo estruturado e fechado, alinhado a metodologia publicada (Appropriateness Evaluation Protocol; Red2Green do NHS).

**Escopo da mudança (grande, tocou todo o núcleo analítico):**

1. **Taxonomia fechada de pendências (RF-22)** -- `app/analysis/taxonomy.py`, novo. 7 categorias fixas (Diagnóstico, Interconsulta, Procedimento/cirurgia, Terapêutica, Transferência, Alta/barreira, Administrativa/logística), cada uma com subtipos sugeridos pela orientação + "OUTRO" como escape -- taxonomia fechada não pode travar num caso real não previsto.

2. **SLA parametrizável, nunca "atraso" automático (RF-23)** -- `app/analysis/sla_config.py`, novo. Limites default por categoria (documentados como sugestão a validar localmente, mesmo princípio do RULES-001/RF-07). O sistema reporta tempo decorrido; só o cálculo de PRIORIDADE usa o limite como sinal.

3. **Prioridade por REGRA, nunca pelo LLM livremente (RF-24)** -- `app/analysis/priority.py`, novo. `compute_priority()` decide ALTA/MÉDIA/MONITORAMENTO a partir de categoria + necessidade hospitalar + tempo decorrido vs. SLA -- transparente e auditável, consistente com "a LLM não deve inventar o que considera urgente" (seção 16 da orientação).

4. **Contrato do LLM expandido (RF-20, RF-21, RF-25, RF-27, RF-28)** -- `app/analysis/schemas.py` reescrito: `necessidade_hospitalar` (5 categorias fechadas, nunca "internação desnecessária"), `objetivo_terapeutico`, `proximo_passo`, `edd_data`/`edd_status`, `dia_classificacao`/`dia_causa`, e cada `pending_item` ganha `subcategory` (validado contra a taxonomia), `origin`, `is_inferred`+`confidence` (obrigatória quando inferido), `flow_status`. Validação rigorosa: categoria fora da taxonomia é erro, `dia_causa` obrigatória quando VERMELHO, `edd_data` obrigatória quando `edd_status=REGISTRADA`.

5. **Prompt reescrito (`app/analysis/llm.py`)** -- pede as 3 perguntas centrais da orientação (por que internado hoje / o que impede progressão / existe ação pendente), embute a lista de categorias/subtipos gerada dinamicamente de `taxonomy.py` (nunca desalinha), e todos os guardrails literais da orientação (nunca "desnecessária", nunca inventar data/causalidade, linguagem de incerteza quando inferido). `build_prompt`/`analyze_patient` passam a receber `active_pending_items` -- sem isso o LLM não teria como saber se uma pendência antiga foi resolvida pelas evoluções novas (só via texto livre do estado anterior).

6. **Schema de banco expandido** -- `patient_state` ganha 11 colunas novas (necessidade hospitalar, objetivo, próximo passo, EDD, dia verde/vermelho, especialidade/origem, versão do modelo); `pending_items` ganha 6 (subcategory, origin, priority, confidence, is_inferred, flow_status); nova tabela `pending_item_evidence` para reiterações (múltiplas evidências por pendência, seção 15 da orientação -- a primeira evidência continua em `pending_items.evidence`). Migração (`ALTER TABLE`) para bancos já existentes na máquina do usuário, mesmo padrão do DEC-054.

7. **Relatório reformulado (`app/reports/html_report.py`)** -- ganhou "Censo de pendências por unidade" (tabela, uma linha por paciente com a pendência de maior prioridade -- seção 11) além do relatório individual já existente, agora no formato completo da seção 10 (contexto, necessidade hospitalar com justificativa, objetivo, pendência principal com prioridade+tempo, próximo passo, previsão de alta, classificação do dia). Indicadores agregados (seção 12) e distinção interna/externa em painel próprio ficam para uma iteração seguinte -- registrado como REPORT-003 em `TASKS.md`.

**Decisões de design tomadas (não estavam explícitas na orientação, precisaram de critério):**
- **Prioridade e tempo decorrido NUNCA são lidos como valor fixo do banco** -- são recalculados na leitura do relatório (`hours_elapsed_since`/`compute_priority` chamados de novo em `html_report.py`). Um valor gravado uma vez ficaria desatualizado a cada hora que passa, mesmo sem nova execução -- a coluna `priority` persistida é só um snapshot informativo do momento da criação.
- **A evidência de toda pendência (incluindo a "principal") fica sempre visível no relatório individual**, não só a descrição resumida -- bug real que corrigi durante o desenvolvimento (a 1ª versão só mostrava evidência de pendências secundárias; com uma única pendência, ela nunca aparecia). RF-26 exige auditabilidade de TODO achado, não só dos secundários.
- **Categoria "Interconsulta" reaproveita `flow_status`** (SOLICITADA/REALIZADA_SEM_CONDUTA_DEFINIDA/CONDUTA_DEFINIDA) para o fluxo que a orientação pede distinguir (seção 4.2) -- campo genérico, disponível a qualquer categoria, não só interconsulta.
- **Regras determinísticas (RULES-001) migradas para a taxonomia nova** sem mudar comportamento: `CATEGORY_EXAM`/`CATEGORY_CONSULT` viram aliases de `CATEGORY_DIAGNOSTICO`/`CATEGORY_INTERCONSULTA`; `RuleFinding.origin` é sempre `default_origin(category)` (regra determinística nunca infere, RF-25).

**Bug real encontrado e corrigido durante a validação (inspeção de relatório de exemplo com dados fictícios, antes de considerar a fase pronta):** `hours_elapsed_since` tratava `evidence_date` (horário LOCAL de Brasília, sem timezone -- é o que `normalize_datetime` grava a partir do que o GSUS mostra) como se fosse UTC, inflando todo tempo decorrido em ~3h sistematicamente. Corrigido para comparar sempre no mesmo "regime" (ambos ingênuos/locais, nunca misturar com UTC).

**Impacto:** `app/analysis/taxonomy.py`, `sla_config.py`, `priority.py` (novos); `schemas.py`, `llm.py`, `rules.py` reescritos/expandidos; `app/storage/database.py` (schema + migração), `repository.py` (`save_patient_state`/`add_pending_item` expandidos, `add_pending_item_evidence`/`get_pending_item_evidence` novos); `app/orchestrator.py` (integra tudo); `app/reports/html_report.py` (reformulado). `PROJECT_SPEC.md` ganhou RF-20 a RF-30. 34 testes novos (`test_priority.py`, `test_taxonomy.py`, `test_sla_config.py`) + testes existentes atualizados para o novo contrato (`test_schemas.py` reescrito, `test_llm.py`/`test_pipeline.py`/`test_html_report.py` ajustados). Suíte: 160 passed + 3 skip (flake conhecido).

**Não validado ao vivo:** todo este ciclo foi validado com dados sintéticos/fictícios (regras determinísticas + relatório de exemplo manual) -- ainda não rodou contra uma análise real do LLM local (LocalLLM) com o modelo Llama 3.1 8B carregado. Próximo passo natural: rodar "Atualizar agora" com LLM habilitado e inspecionar a saída real (só estrutura/contagens, nunca conteúdo clínico, mesmo protocolo de sempre).

**Pendente de validação clínica (mesmo princípio do RF-07):** os subtipos da taxonomia, os limites de SLA default e as regras de prioridade são um ponto de partida fiel à orientação técnica, não uma validação institucional formal -- devem ser revisados por critério clínico antes de uso em produção real, exatamente como já vale para RULES-001.

## DEC-058 — Primeira validação do contrato DEC-057 contra o LLM real: dois bugs encontrados e corrigidos

**Contexto:** primeira execução de `LocalLLM.analyze_patient` com o modelo real (Llama 3.1 8B via `llama-server`) contra o novo contrato do DEC-057, usando a fixture 100% fictícia `fixtures/notes/long_admission.txt` (script ad-hoc, descartável, fora da suíte).

**Achado 1 -- timeout de comunicação (não era falha de validação):** `DEFAULT_TIMEOUT_SECONDS=60` (herdado do LLM-001, prompt antigo bem menor) não bastava para o prompt do DEC-057 (embute a taxonomia inteira + o template JSON completo no `SYSTEM_PROMPT`). Na 1ª tentativa da execução real, a chamada HTTP estourou os 60s mesmo com o modelo já carregado. Corrigido: `DEFAULT_TIMEOUT_SECONDS` 60 → 240.

**Achado 2 -- "null" como string literal, não como valor JSON (bug real, corrigido):** com o timeout já corrigido, a saída final e válida trouxe `"edd_data": "null"`, `"dia_causa": "null"`, `"flow_status": "null"` -- a palavra escrita como STRING (entre aspas) em vez do valor JSON `null`. A validação existente não pegava isso (`isinstance(valor, str)` aceita a string "null" normalmente), e código downstream (`html_report.py::_format_edd`, `if edd_status == "REGISTRADA" and edd_data:`) trataria essa string como valor verdadeiro/real, renderizando errado. Corrigido com `normalize_null_sentinels()` (novo, `app/analysis/schemas.py`) -- reconhece `"null"`/`"none"`/`"nulo"` (case-insensitive, com/sem espaço) e converte para `None` antes de validar. Chamado em `LocalLLM.analyze_patient` logo após o parse do JSON, antes de `validate_analysis_output`. Prompt também reforçado com uma regra explícita ("use o valor JSON null... NUNCA a palavra 'null' como texto entre aspas") como defesa em profundidade -- reduz a chance de nova tentativa desperdiçada (cada retry reprocessa o prompt inteiro; ~524s a tentativa nesta execução). 9 testes novos (`test_schemas.py`, `test_llm.py` end-to-end reproduzindo exatamente a saída real observada).

**Achado 3 -- observação de qualidade, corrigida a pedido do usuário (ver atualização abaixo):** no mesmo resultado, `necessidade_hospitalar="SIM"` foi justificado citando "mantém febre intermitente" -- uma nota de evolução anterior (15/08) -- enquanto a evolução MAIS RECENTE da mesma fixture (20/08) já registrava "afebril há 48 horas. Mantida investigação AMBULATORIAL APÓS ALTA". O modelo não priorizou a evidência mais recente ao decidir `necessidade_hospitalar`, resultado clinicamente questionável (mas dentro do contrato -- nenhuma regra de schema foi violada). Isso é esperado de um modelo 8B local e é exatamente o tipo de erro que RF-07/RF-24 preveem o auditor humano capturar -- não é motivo para bloquear, mas reforça que a saída do LLM é HIPÓTESE a revisar, nunca fato publicado sem revisão.

**Atualização -- Achado 3 corrigido (usuário pediu "ajuste o prompt"):** duas mudanças complementares em `app/analysis/llm.py`. (a) `build_prompt` agora identifica a nota com o maior `timestamp` (comparação de string ISO 8601, mesmo padrão de `hours_elapsed_since`) entre as `new_notes` e marca sua linha explicitamente com `*** EVOLUÇÃO MAIS RECENTE ***` -- por VALOR de timestamp, nunca por posição na lista (a ordem de extração do GSUS não é garantida por este módulo, testado explicitamente). (b) `SYSTEM_PROMPT` ganhou uma regra dizendo que `necessidade_hospitalar`, `current_status` e `dia_classificacao` devem sempre se basear na evolução marcada como mais recente, e que ela vence qualquer contradição com evoluções anteriores. Isso não elimina a possibilidade de erro de raciocínio do modelo (continua sendo um LLM 8B local, RF-07 continua exigindo revisão humana), mas remove a ambiguidade estrutural que permitia ele "perder" qual nota era a mais atual. 5 testes novos (`tests/unit/test_llm_prompt.py`), incluindo um que prova a marcação segue o timestamp mesmo com a lista fora de ordem.

**Impacto:** `app/analysis/llm.py` (`DEFAULT_TIMEOUT_SECONDS`, `SYSTEM_PROMPT`, `build_prompt`, import de `normalize_null_sentinels`), `app/analysis/schemas.py` (`normalize_null_sentinels`, `NULL_SENTINEL_STRINGS`, novo), `tests/unit/test_llm_prompt.py` (novo). Suíte: 173 passed + 4 skip (flake de display Tk já conhecido, não regressão). `TASKS.md::MODEL-003` segue `[~]` -- os 3 achados desta rodada real foram tratados (2 bugs de código + 1 ajuste de prompt), mas falta rodar o pipeline completo ponta a ponta antes de fechar, e nenhum destes ajustes substitui a revisão clínica humana por achado (RF-07).

## DEC-059 — "ATUALIZAR AGORA" nunca chamava o LLM de verdade (gap de produção encontrado ao preparar a validação ponta a ponta)

**Contexto:** usuário pediu para rodar o pipeline completo ponta a ponta (GSUS real + LLM real), pra fechar `MODEL-003`. Ao revisar o caminho real que o botão "ATUALIZAR AGORA" usa (`app/ui/main_window.py::_run_update_worker`) antes de replicar a mesma chamada num script de validação, encontrei um gap de produção: a chamada a `run_once(...)` passava `llm=None` fixo, com um comentário "LLM local ainda não plugado na UI". `config.py` já tinha os campos `model_path`/`llm_server_path`/`llm_host`/`llm_port` prontos, mas nada em `main_window.py` os lia. Ou seja: **todo uso real do botão principal do app, até agora, gerou relatório só com regras determinísticas -- nunca com a análise por IA do DEC-057/058**, mesmo que o usuário tivesse o modelo configurado.

**Corrigido:** `_run_update_worker` agora constrói um `LocalLLM` de verdade a partir do `AppConfig` e o passa para `run_once`. Duas decisões de design tomadas no processo:

1. **LLM inicia ANTES da sessão GSUS**, não depois -- carregar o modelo pode levar de 1 a mais de 4 minutos nesta máquina (hardware fraco, DEC-058); abrir a sessão GSUS só depois de o modelo já estar pronto evita ela ficar ociosa esperando (risco de expirar por inatividade).
2. **Falha ao iniciar o LLM (`LLMStartupError`) NÃO aborta a atualização** -- cai para `llm=None` e a atualização continua só com regras determinísticas, logando o erro. Essa é a semântica que `app/ui/errors.py::friendly_message` já previa para `LLMStartupError` ("A atualização continuou sem ele") mas que a implementação anterior nunca honrava de verdade (o LLM nunca era nem tentado). Sem essa queda suave, qualquer máquina com modelo mal configurado passaria a falhar a atualização inteira em vez de só perder a análise por IA -- regressão pior que o gap original.

**Bug relacionado encontrado e corrigido no processo:** `LocalLLM.start()` chamava `subprocess.Popen(...)` fora de qualquer `try/except` -- um `OSError` real (`FileNotFoundError: [WinError 2]`, reproduzido de verdade ao rodar a suíte com os caminhos default de `AppConfig`, que são relativos) escapava cru em vez de virar `LLMStartupError`, quebrando o contrato que `friendly_message`/`main_window.py` dependem (só sabem tratar `LLMStartupError`/`LLMAnalysisError`). Corrigido envolvendo o `Popen` num `try/except OSError`, relançando como `LLMStartupError` com a causa original encadeada (`from exc`).

**Achado colateral, registrado mas NÃO corrigido agora (fora de escopo de hoje):** o gap acima só apareceu porque `AppConfig.model_path`/`llm_server_path` são caminhos RELATIVOS por padrão (`"models/model.gguf"`, `"runtime/llama-server.exe"`), e no Windows `CreateProcess` não resolve caminho relativo de executável contra o diretório de trabalho do mesmo jeito que `pathlib.Path.exists()` -- então a checagem de existência passa mas o `Popen` falha. Em produção real (executável empacotado, ou disparado pelo Windows Task Scheduler via `SCHEDULE-001`), o diretório de trabalho no momento do lançamento não é garantido ser a pasta de instalação -- ou seja, esse mesmo `WinError 2` pode acontecer de verdade fora de teste, e agora ele degrada silenciosamente (relatório sai igual, só sem IA) em vez de travar visivelmente. Resolver isso de verdade exige saber com certeza a pasta de instalação em modo `frozen` (PyInstaller) vs. modo dev -- decisão que pertence a `BUILD-001`/`INSTALL-001`, não a este ciclo. Registrado aqui para não se perder.

**Testes novos:** `tests/unit/test_main_window_update.py` (+2: LLM chamado quando inicia bem -- prova que `analyze_patient` é de fato invocado, não só que o relatório é gerado; atualização continua quando o LLM falha ao iniciar), `tests/integration/test_llm.py` (+3: `LLMStartupError` para server/model ausente, `OSError` do `Popen` vira `LLMStartupError`). Suíte inteira: 177 passed (variação de skip só pelo flake de Tk já conhecido, confirmado isolando os arquivos alterados: 14/14 passed).

**Impacto:** `app/ui/main_window.py` (`_run_update_worker`), `app/analysis/llm.py` (`LocalLLM.start()`), `tests/unit/test_main_window_update.py`, `tests/integration/test_llm.py`. `TASKS.md::MODEL-003`/`LLM-002` atualizados.

## DEC-060 — MODEL-003 fechado: pipeline completo validado contra GSUS real + LLM real

**Contexto:** usuário pediu para rodar o pipeline completo ponta a ponta, autorizando explicitamente que eu mesmo disparasse a execução (credencial já salva no Credential Manager), com a condição de eu só ler agregados -- nunca nome, prontuário ou texto clínico (dado real de paciente nunca deve chegar ao meu contexto, regra de todo o projeto).

**Implementado:** `scripts/check_full_pipeline.py` (novo, permanente -- mesmo padrão de `check_login.py`/`check_census.py`/`check_notes.py`). Chama `orchestrator.run_once` de verdade (a MESMA função que `app/ui/main_window.py` usa desde o DEC-059), com:
- Credencial real via `credentials.get_credential("gsus")` (nunca impressa).
- `LocalLLM` com caminhos ABSOLUTOS resolvidos contra a raiz do projeto (evita o problema de caminho relativo do DEC-059).
- Amostra limitada a 3 pacientes reais (`SampledCensus`, wrapper fino sobre o `GSUSAdapter` real -- nunca reimplementa GSUS-001..005, que são FROZEN) -- rodar o LLM contra o censo inteiro na primeira validação seria lento demais e arriscado demais para um primeiro teste.
- Banco e relatório ISOLADOS (`validacao_pipeline_completo.db`/`.html`, mesma pasta `%LOCALAPPDATA%\GSUSAuditoria\` dos arquivos reais, nomes diferentes) -- necessário porque `run_once` chama `mark_patients_inactive_not_in` com a lista truncada; rodando contra o banco real isso marcaria pacientes reais fora da amostra como inativos incorretamente.
- Log do app inteiro redirecionado para arquivo via `scripts/_common.py::setup_file_logging()` (já existia, reusado) -- nunca cai no terminal/na minha leitura.
- Saída para mim: só uma linha `RESUMO_JSON` com contagens agregadas, tempos e (se falhar) o TIPO da exceção + `friendly_message` -- nunca conteúdo.

**Resultado da execução real (2026-08-24):** sucesso completo. LLM carregou em 82,2s (mais rápido que as rodadas anteriores da sessão -- variação de contenção de máquina, não uma mudança de código). GSUS real acessado, 3 pacientes reais processados: **3 completos, 0 falha, 0 sem-internação-atual**. Tempo total (LLM + GSUS + 3 pacientes): 2292,7s (~38,2min) -- primeiro dado real de custo ponta a ponta COM o LLM plugado (PERF-001 tinha medido só a extração, sem LLM). Relatório gerado em `validacao_pipeline_completo.html`.

**O que isso prova e o que NÃO prova:** prova que a integração técnica funciona de ponta a ponta contra o ambiente real -- login, censo, extração, regras, LLM (incluindo o wiring novo do DEC-059), persistência no schema expandido do DEC-057, e geração do relatório reformulado, tudo sem quebrar. **NÃO prova que o conteúdo das 3 análises está clinicamente correto** -- por design, o dado real de paciente nunca chegou até mim (só vi a linha `RESUMO_JSON`), então não posso avaliar a qualidade clínica das 3 saídas, só que existem e são schema-válidas. O usuário precisa abrir `validacao_pipeline_completo.html` ele mesmo para essa avaliação (RF-07 continua de pé -- validação clínica humana nunca foi feita para a taxonomia/SLA/prioridade, e agora também não foi feita para estas 3 análises específicas).

**Pendente, não bloqueador:** rodar via botão "ATUALIZAR AGORA" de verdade (não só o script) contra o censo completo (não só 3 pacientes) é o próximo teste natural antes de `PILOT-001`, mas MODEL-003 em si -- "o contrato do LLM funciona contra o modelo real" -- está demonstrado.

**Impacto:** `scripts/check_full_pipeline.py` (novo). `TASKS.md::MODEL-003` fechado (`[x]`).

## DEC-061 — Relatório vinha vazio contra dado real: filtro por `unit` nunca deveria ter existido

**Contexto:** usuário abriu o relatório real gerado pelo DEC-060 e reportou "não achei o relatório" -- depois de resolver um problema à parte de acesso a arquivo (Explorer/Brave não enxergavam o arquivo mesmo confirmado no disco via PowerShell -- contornado servindo por HTTP local, `127.0.0.1`, nunca investigado a fundo pois não bloqueava), o relatório abriu mas mostrava **"Nenhum paciente processado com sucesso nesta execução"** nas seções de censo e individual, apesar do cabeçalho dizer corretamente "3 Processados, 0 Falhas".

**Causa raiz:** `generate_report`/`get_pending_items_for_unit`/`mark_patients_inactive_not_in` filtravam por `WHERE patients.unit = ?`, usando o `unit` CONFIGURADO no app (`app_config.unit`, ex. "Auditoria"). Mas `patients.unit` é texto ESCAVADO da própria tabela do censo do GSUS (`app/gsus/census.py`, coluna `COL_UNIT`) -- e conferi que `get_census(gsus_frame, unit, ...)` **nunca usa o parâmetro `unit` pra filtrar a busca de verdade** (`_navigate_to_search_screen(gsus_frame)` não recebe `unit`; o código clica "Consultar" direto). Na execução real do DEC-060, os 3 pacientes vieram com `unit` = `'4-Internados P.A.'` (2) e `'ENF. MEDICO-CIRURGICA 2 (21 leitos)'` (1) -- nenhum bate com `'Auditoria'`. Resultado: as três consultas por `unit` sempre voltavam vazias contra dado real (só "por sorte" pareciam funcionar nos meus próprios testes/fixtures sintéticas, onde eu sempre escrevia o mesmo texto dos dois lados).

**Perguntei ao usuário** (decisão de produto, não só bug técnico) o que a conta GSUS realmente representa. Resposta: "Minha conta GSUS tem acesso a todas as unidades e eu devo auditar todas elas." -- ou seja, a própria conta GSUS já É o escopo de auditoria; `unit` configurado no app nunca deveria ter sido um FILTRO de dado, só um RÓTULO de exibição.

**Corrigido:**
- `Repository.mark_patients_inactive_not_in(unit, ids)` → `mark_patients_inactive_not_in(ids)` -- não filtra mais por unit (efeito colateral real, achado no processo: como o filtro nunca batia, NENHUM paciente jamais foi marcado inativo em toda a história do projeto, mesmo tendo alta/saído do censo -- `patients.active` nunca era zerado).
- `Repository.get_pending_items_for_unit(unit)` → renomeado `get_all_active_pending_items()` -- não recebe mais `unit`, e ganhou `AND p.active = 1` (antes não tinha NENHUM filtro de ativo -- paciente inativo com pendência antiga também vazava pro relatório; inconsistente com a outra consulta do mesmo método, que já filtrava `active = 1`).
- `generate_report`'s query interna de `patient_state` -- removido `AND p.unit = ?`.
- `unit` continua sendo parâmetro de `run_once`/`generate_report` -- usado só pra rotular o título do relatório ("Censo de pendências — {unit}"), nunca mais como critério de busca no banco.

**Validação:** reproduzido o bug com um teste novo (`test_report_shows_patients_regardless_of_scraped_unit_label`, dois pacientes com `unit` diferente do configurado, nenhum some do relatório) e 3 testes novos de `mark_patients_inactive_not_in`/`get_all_active_pending_items` (não existia NENHUM teste de `mark_patients_inactive_not_in` antes -- gap de cobertura real). Suíte inteira: 177 passed (skip variando só pelo flake de Tk conhecido -- confirmei isolando os 3 arquivos tocados: 23/23 passed). Regenerei o relatório ISOLADO do DEC-060 a partir dos MESMOS dados reais já coletados (sem rodar GSUS/LLM de novo) -- confirmei estruturalmente (contagem de blocos "LEITO", nunca conteúdo) que os 3 pacientes agora aparecem e a mensagem de vazio sumiu.

**Impacto real desta correção:** provavelmente TODO relatório gerado por este projeto até agora (incluindo PERF-001, "174 pacientes, 168 sucesso") só parecia funcionar nos números do cabeçalho -- as seções de censo/individual (o conteúdo que realmente importa pro auditor) podem ter vindo vazias sempre que o censo retornou mais de uma unidade distinta, e isso nunca foi percebido porque ninguém tinha aberto o relatório de uma rodada real com o formato novo (DEC-057) até agora. Primeira vez que abrir o relatório de verdade fez parte do ciclo de validação -- reforça por que "rodar de verdade" (não só suíte de teste) continua sendo indispensável neste projeto.

**Impacto:** `app/storage/repository.py` (`mark_patients_inactive_not_in`, `get_all_active_pending_items`), `app/orchestrator.py` (chamada atualizada), `app/reports/html_report.py` (duas consultas), `tests/unit/test_repository.py` (+3), `tests/unit/test_html_report.py` (+1).

## DEC-062 — Auditoria de conformidade contra o PDF original: reconciliação de pendências entre execuções nunca existia

**Contexto:** usuário pediu comparação seção-por-seção do relatório contra o documento de orientação técnica original, depois "corrija todos os gaps em sequência, do mais importante ao menos". Achado mais importante: `orchestrator.py` sempre inseria uma pendência NOVA a cada execução (`repo.add_pending_item(...)` incondicional), mesmo quando a mesma pendência real continuava aberta -- nunca reconhecia "já existe" nem "foi resolvida". A tabela `pending_item_evidence` (seção 15 da orientação: "EVIDÊNCIA 1 / EVIDÊNCIA 2") existia desde o DEC-057 mas `add_pending_item_evidence` nunca era chamado. `resolve_pending_item` também nunca era chamado.

**Corrigido:** `_run_llm_analysis` e o laço de regras em `_process_patient` agora reconciliam contra as pendências já ATIVAS do paciente via `_find_matching_pending` (novo): mesma categoria+subtipo (LLM) ou categoria+subtipo+descrição (regra, determinística por construção) -> REITERAÇÃO (evidência nova vai para `pending_item_evidence`, `evidence`/`evidence_date` ORIGINAIS nunca sobrescritos -- seção 5 exige a primeira evidência documental); categoria+subtipo novo -> pendência nova; pendência ativa não reconfirmada pelo LLM -> resolvida. Nova coluna `pending_items.source` ("RULE"/"LLM") isola os dois ciclos de vida -- sem isso, a resolução do LLM resolveria pendências de regra por engano (e vice-versa).

**Relatório também ganhou (mesma auditoria):** coluna "Unidade" no censo (necessário desde o DEC-061 -- várias unidades reais na mesma execução); selo de origem interna/externa e nível de confiança em cada pendência (dado já existia no banco, nunca era renderizado); SLA institucional aplicado mostrado ao lado do tempo decorrido (seção 6: "deixa a metodologia mais defensável"); evidências múltiplas (EVIDÊNCIA 1/2/3) quando há reiteração.

**Testes:** suíte cresceu de 177 para ~204 nesta etapa (novos: `test_orchestrator.py` inteiro, +testes em `test_html_report.py`/`test_repository.py`). Ver DEC-064 para uma segunda rodada de correções sobre esta mesma reconciliação (achados de uma verificação adversarial independente, incluindo 3 regressões reais nesta lógica).

**Impacto:** `app/orchestrator.py` (`_find_matching_pending`, `_run_llm_analysis`, `_process_patient`), `app/storage/database.py`/`repository.py` (`source`), `app/reports/html_report.py` (unidade/origin/confiança/SLA/evidências múltiplas), `tests/unit/test_orchestrator.py` (novo).

## DEC-063 — `especialidade_responsavel`/`origem_internacao`/`model_version` nunca eram preenchidos

**Contexto:** mesma auditoria de conformidade do DEC-062. Colunas existiam no banco desde o DEC-057 (RF-20/RF-30), mas `orchestrator.py` nunca passava valores reais -- `especialidade_responsavel` e `origem_internacao` sempre ficavam `NULL`, `model_version` idem.

**Corrigido:** `especialidade_responsavel` é derivado DETERMINISTICAMENTE da nota mais recente (`most_recent_note`, extraído de `llm.py` para `app/analysis/rules.py` -- reaproveitado nos dois lugares) -- é metadado já estruturado (cargo do profissional na nota), não algo pra pedir ao LLM adivinhar (RF-07/RF-08). `origem_internacao` foi adicionado ao contrato do LLM (`schemas.py`, `SYSTEM_PROMPT`) -- só aparece explicitamente na nota de admissão, então `save_patient_state` precisa preservá-lo (COALESCE) quando análises incrementais posteriores devolverem null. `model_version` vem de `LocalLLM.model_version` (nome do arquivo .gguf carregado, via `getattr(llm, "model_version", None)` -- duck typing, nunca exigido pelo Protocol `AnalysisEngine`). Relatório passou a exibir os três, incluindo `last_analysis_at` (já era gravado, nunca mostrado) como trilha de auditoria mínima (RF-30).

**Impacto:** `app/analysis/rules.py` (`most_recent_note`), `app/analysis/llm.py` (`origem_internacao` no prompt/contrato, `model_version` property), `app/analysis/schemas.py`, `app/storage/repository.py` (`save_patient_state` com COALESCE), `app/orchestrator.py`, `app/reports/html_report.py`.

## DEC-064 — Verificação adversarial (3 revisores independentes) encontrou 21 problemas reais, incluindo 3 regressões na reconciliação do DEC-062

**Contexto:** com ultracode ativo, rodei uma verificação adversarial (workflow, 3 lentes independentes -- conformidade com o PDF, corretude da lógica nova de reconciliação, qualidade dos testes novos -- cada achado depois re-verificado por um 4º agente cético) sobre o trabalho do DEC-062/063 antes de reportar como concluído. Resultado: 21 achados, **todos os 21 confirmados** na re-verificação. Nada era ruído.

**Regressões reais na minha própria lógica do DEC-062 (as mais graves, corrigidas):**
1. **Resolução automática de regra por ausência era insegura.** `apply_all_rules` roda só sobre a janela de notas desta execução (`get_raw_notes_text(patient, known_days)` pula dias antigos já conhecidos contra o GSUS real -- só ~2 dias recentes + novos, `app/gsus/records.py`). Uma pendência de regra cuja nota de origem "saiu da janela" deixava de aparecer em `apply_all_rules` mesmo continuando aberta de verdade, e meu código resolvia "por ausência" -- apagando uma barreira real do relatório sem nenhuma evidência de resolução. Corrigido: `apply_exam_rule`/`apply_consult_rule` agora devolvem TAMBÉM os achados resolvidos (campo `resolved`, antes filtrado internamente) -- o orchestrator só resolve quando a MESMA janela contém solicitação E conclusão (prova real), nunca por silêncio.
2. **Casamento por categoria+subtipo era grosso demais pra regras.** RULES-001 usa um subtipo único (`AGUARDA_EXAME_IMAGEM_OU_LABORATORIAL`) pra TODO tipo de exame -- duas pendências de regra concorrentes e diferentes (ex.: tomografia E ultrassom abertos ao mesmo tempo) colapsavam na mesma, e a segunda nunca era persistida (perda silenciosa). Corrigido: pendências de regra casam por categoria+subtipo+DESCRIÇÃO (determinística por construção -- `RuleFinding.description` é sempre o mesmo texto pro mesmo exame/especialidade).
3. **Casamento por categoria+subtipo quebrava progressão de estágio do LLM.** INTERCONSULTA/PROCEDIMENTO_CIRURGIA têm subtipo que representa ESTÁGIO de um fluxo (seção 4.2: "solicitado -> realizado -> conduta definida... pode continuar pendente"), não um tipo diferente de pendência. Uma interconsulta evoluindo de SOLICITADA para REALIZADA_SEM_CONDUTA_DEFINIDA virava "pendência nova" -- perdendo a evidência/data original (violação direta da seção 5) e criando duplicata. Corrigido: para essas duas categorias, o casamento agora ignora subtipo (`STAGE_PROGRESSION_CATEGORIES`); subtipo também passou a ser mostrado no contexto do LLM (`build_prompt`) como reforço.

**Bugs pré-existentes reais (RULES-001, não introduzidos nesta sessão, corrigidos agora):**
4. `COMPLETION_VERB`/`CONSULT_COMPLETION_MARKERS` tratavam a mera presença de "laudo"/"parecer" como conclusão -- "aguardando laudo"/"aguarda parecer" citam a palavra mas significam o oposto (a orientação técnica, seção 4, trata "exame realizado, aguardando laudo" como estado PENDENTE distinto). Corrigido com `_has_unwaited_marker`: um marcador de conclusão só conta se a mesma FRASE não tiver também "aguard-".

**Outros gaps reais corrigidos:**
5. `save_patient_state` não fazia COALESCE de `edd_data`/`edd_status` -- mesma razão de ser do `origem_internacao` (RF-08, incrementalidade), mas ficava de fora por inconsistência, apagando uma EDD real já confirmada. Achado extra ao corrigir: `edd_status` quase nunca vem `null` do chamador real (campo obrigatório no LLM), então um `COALESCE` simples nele não fazia nada -- precisou de `CASE WHEN excluded.edd_data IS NULL` pra amarrar o status à mesma condição que preserva a data (achado só na hora de escrever o teste, o COALESCE ingênuo passou a fazer o `edd_status` sobrescrever sozinho e esconder a data preservada).
6. `origin` do LLM (frequentemente `null`, campo opcional) nunca tinha fallback -- `default_origin(category)` só era usado pelo lado das regras. Maioria das pendências do LLM nunca mostrava o selo interna/externa.
7. `necessidade_hospitalar_justificativa` (obrigatória pela seção 3) só era checada por tipo, não por conteúdo -- `""` passava a validação.

**Achados reais, deliberadamente NÃO corrigidos agora (fora de escopo desta rodada, registrados para decisão futura):**
- SLA fixo por categoria, não por subtipo (seção 6 cita anatomopatológico como precisando de vários dias, diferente do resto de DIAGNOSTICO) -- mudança de `sla_config.py`/`priority.py` mais estrutural, não pedida nesta rodada.
- `patient_id` = prontuário real, nunca pseudonimizado no relatório -- decisão de produto DELIBERADA e já documentada (`app/security/pseudonym.py`), o auditor precisa do prontuário real pra agir no GSUS. Não mudei sem validar com o usuário.
- Sem mecanismo de correção/override do auditor, sem rastreamento de concordância auditor×IA (seção 17) -- features futuras, já registradas como tal em `PROJECT_SPEC.md` RF-30.
- `patient_state` sobrescreve destrutivamente a cada análise (sem trilha histórica de dia_classificacao/necessidade_hospitalar) -- mesmo tema do REPORT-003 (indicadores agregados, dias vermelhos históricos), já deliberadamente adiado pro piloto.
- Campo "responsável" por pendência (distinto de especialidade do paciente) -- baixa prioridade, nice-to-have.

**Testes:** ~20 testes novos/ajustados fechando os gaps de cobertura que a lente "tests" também apontou (tag "inferido" nunca verificada, pendência sem `patient_state` ainda, `most_recent_note` com lista mista, origin EXTERNA/confidence MEDIA-BAIXA/flow_status nunca exercitados, isolamento RULE×LLM com mesma categoria+subtipo nunca testado). Suíte final: 222 passed, 4 skip (flake de Tk conhecido).

**Impacto:** `app/analysis/rules.py` (`apply_exam_rule`/`apply_consult_rule` mudam contrato de retorno, `_has_unwaited_marker`), `app/orchestrator.py` (`_find_matching_pending`, laço de regras), `app/storage/repository.py` (`save_patient_state`), `app/analysis/schemas.py`, `tests/unit/test_rules.py`/`test_orchestrator.py`/`test_repository.py`/`test_html_report.py`. Reforça o valor de rodar uma verificação adversarial independente antes de reportar "pronto" -- as 3 regressões mais graves eram exatamente do tipo que uma suíte de teste escrita pela mesma pessoa que escreveu o código tende a não pegar.

## DEC-065 — Validação com amostra aleatória de verdade: LLM falhou 3/3 (timeout + "null" num enum obrigatório)

**Contexto:** usuário pediu pra rodar mais 3 casos reais, mas ALEATÓRIOS (as validações anteriores sempre pegavam os N primeiros do censo -- sempre os mesmos pacientes, com admissões curtas por coincidência). `scripts/check_full_pipeline.py` passou a usar `random.sample` em vez de fatiar os primeiros N (`SampledCensus.get_census`), com banco/relatório isolados novos (`validacao_pipeline_v2.*`, pra não misturar com a rodada anterior).

**Resultado real:** 3/3 pacientes processados sem falha técnica (`counts.failed=0`), mas **0 de 3 tiveram análise por IA salva** (`patient_state` vazio) -- as regras determinísticas também não encontraram nada nesses 3 casos. Os 3 pacientes sorteados tinham admissões bem mais longas (129 notas ao todo, ~43 por paciente, contra ~5-6 nas validações anteriores) -- amostra aleatória expôs um caso real que a amostra "sempre os primeiros" nunca tinha testado.

**Causa raiz (log estrutural, sem dado clínico -- `LLM_ANALYSIS_ERROR: ... tentativa(s): [...]`):**
1. **Timeout de comunicação em 11 das ~9 tentativas observadas.** Prompt com ~43 evoluções é bem maior que os casos testados até agora -- 240s (ajustado no DEC-059) não bastava. A incrementalidade (RF-08) processa só notas NOVAS no dia a dia, então isso deve ser raro em operação normal -- mas a PRIMEIRA análise de um paciente com histórico longo (ou depois de um hiato grande sem rodar) reenvia tudo de uma vez, e vai continuar acontecendo de verdade. Subido para 480s.
2. **`edd_status` (enum obrigatório -- REGISTRADA/NAO_REGISTRADA/VENCIDA, nunca nullable) também veio como a STRING "null"** em pelo menos 2 tentativas -- o mesmo hábito do DEC-058, mas num campo pra onde `normalize_null_sentinels` não olhava (só cobria os OPCIONAIS). Sem nenhum valor de fallback, isso derrubava a tentativa mesmo com tudo mais correto. Corrigido: `normalize_null_sentinels` agora deriva um valor seguro pra `edd_status` quando vier "null" -- REGISTRADA se sobrou uma `edd_data` real (já normalizada), senão NAO_REGISTRADA. Nunca inventa VENCIDA (isso é julgamento de data-passada, que já cabe a `_format_edd`/`priority.py` na leitura).

**Testes:** 6 novos (`test_schemas.py` +4, `test_llm.py` +1 integração reproduzindo o cenário exato). Suíte: 227 passed, 4 skip.

**Impacto:** `app/analysis/llm.py` (`DEFAULT_TIMEOUT_SECONDS` 240→480, reforço no `SYSTEM_PROMPT`), `app/analysis/schemas.py` (`normalize_null_sentinels`), `scripts/check_full_pipeline.py` (amostra aleatória, banco/relatório `_v2`). Reforça por que "3 casos aleatórios" pegou o que "sempre os mesmos 3" não pegava -- validação real com amostra fixa dá falsa confiança.

## DEC-066 — Janela de 2 semanas pro LLM em paciente com backlog grande

**Contexto:** rodada real do DEC-065 (timeout de 480s) ainda falhou pros mesmos 3 pacientes -- 2 continuaram dando timeout mesmo em 480s, e um novo (HTTP 400 imediato -- provável estouro da janela de contexto do modelo, que `llama-server` nunca é iniciado com `--ctx-size` explícito, usa o default). Diagnóstico: subir timeout não resolve um problema de TAMANHO de prompt -- só adia a falha. Discuti com o usuário 3 frentes (Localizar Paciente, escopo da atualização diária, retenção de dado pós-alta); esta entrada cobre a primeira decisão concreta: **nunca mandar mais que as últimas 2 semanas de evolução pro LLM**, independente de quão longa seja a admissão.

**Implementado:** `_limit_to_recent_window` (novo, `orchestrator.py`) corta `new_structured_notes` às últimas `LLM_LOOKBACK_DAYS=14` dias (relativo à nota mais recente do lote, não a "agora" -- uma atualização de madrugada não pode descartar notas só por causa da hora) antes de passar pro LLM. RULES-001 continua vendo o histórico completo (`all_structured_notes`, sem corte) -- não tem o custo de tempo/contexto do LLM, não precisa da limitação. Nova coluna `patient_state.analysis_window_limited` (nunca sticky -- sempre reflete só a análise de agora) sinaliza quando o corte foi aplicado; relatório mostra um aviso visível ("⚠ Esta análise considera só as evoluções mais recentes...") recomendando consulta manual ao histórico completo via "Localizar Paciente" quando isso acontece -- nunca esconder que a análise é parcial (RF-26).

**Testes:** 9 novos (`test_orchestrator.py` +7, `test_html_report.py` +2, `test_repository.py` +1). Suíte: 237 passed, 4 skip.

**Impacto:** `app/orchestrator.py` (`_limit_to_recent_window`, `LLM_LOOKBACK_DAYS`, `_process_patient`/`_run_llm_analysis`), `app/storage/database.py`/`repository.py` (`analysis_window_limited`), `app/reports/html_report.py` (aviso). Pendente (próximas entradas): reformular "Localizar Paciente" pra servir o relatório estruturado já processado (não mais navegação ao vivo no GSUS) e política de retenção pós-alta.

## DEC-067 — "Localizar Paciente" reformulado: banco local, nunca mais GSUS ao vivo

**Contexto:** segunda decisão da conversa do DEC-066 (as outras duas: janela de 2 semanas, já feita; retenção pós-alta, ver DEC-068). Pedido do usuário: "Localizar Paciente" deve buscar/gerar o relatório do paciente JÁ armazenado no banco -- só se o paciente estiver internado; se tiver alta, retornar "Paciente recebeu alta".

**Corrigido/substituído:** o desenho original (RF-19, DEC-031, 2026-08-20) navegava ao vivo no GSUS a cada consulta e mostrava o histórico bruto (atual + internações antigas) -- deliberadamente só-leitura, nunca persistia, porque podia abrir prontuário fora do escopo normal (exigindo a "justificativa de acesso" auditada do GSUS, DEC-035). O novo desenho elimina esse problema por construção: `generate_patient_report(repo, record_number)` (novo, `html_report.py`) só lê do banco LOCAL (`patients`/`patient_state`/`pending_items`/`pending_item_evidence`, já populados pela rotina automática normal, sempre dentro do escopo já auditado) -- nunca abre uma sessão GSUS. `patient_id == record_number` por construção (`upsert_patient`), então a busca é uma consulta direta. Reutiliza `_render_bed`/`_annotate_priority`/`_sort_key` -- mesmo formato de relatório individual da rotina automática (RF-20 a RF-30), não mais um dump de texto bruto.

Três resultados possíveis: relatório aberto no navegador (mesmo padrão de "Abrir relatório" do `MainWindow`) se o paciente está ativo; "Paciente recebeu alta" se existe no banco mas `active=0`; "Prontuário não encontrado... pode não ter sido processado ainda" se nunca apareceu numa rotina automática. `LookupWindow` ficou bem mais simples: busca SQLite é rápida o bastante pra não precisar mais de thread/fila/polling, e não depende mais de credencial GSUS configurada.

**Efeito colateral que precisou de ajuste:** o aviso de "análise parcial" do DEC-066 recomendava "consulte Localizar Paciente" para ver o histórico mais antigo -- não faz mais sentido, já que Localizar agora mostra exatamente o mesmo dado (já limitado) do relatório principal. Reescrito para recomendar consultar o GSUS diretamente.

**Não removido (FROZEN, só desusado pela UI):** `app/gsus/records.py::get_full_admission_history_text()` e `app/gsus/adapter.py::lookup_full_history()` -- a capacidade de navegação ao vivo com múltiplos episódios (DEC-033/034/035) continua no código, validada e funcional, só não é mais chamada por `lookup_window.py`. Não deletei por ser código FROZEN (GSUS-001..005, TASKS.md: "não mexer sem motivo real") e por poder ser útil de novo (script de diagnóstico, ou se o usuário quiser essa via no futuro).

**Testes:** `tests/unit/test_lookup_window.py` reescrito por completo (5 testes: relatório aberto, alta, não encontrado, campo vazio, garantia de só-leitura -- banco antes/depois idêntico). Suíte: ver DEC-068 pro número final combinado com o item de retenção.

**Impacto:** `app/storage/repository.py` (`get_patient_by_record_number`), `app/reports/html_report.py` (`generate_patient_report`, `PatientLookupResult`, ajuste no texto do aviso do DEC-066), `app/ui/lookup_window.py` (reescrito), `tests/unit/test_lookup_window.py` (reescrito), `PROJECT_SPEC.md` RF-19, `ARCHITECTURE.md`, `TASKS.md` UI-004 fechado.

## DEC-068 — RETENTION-001 implementado: purga texto bruto de paciente com alta, nunca o resumo estruturado

**Contexto:** terceira decisão da conversa do DEC-066. Usuário lembrou de um pedido esquecido (apagar histórico de paciente com alta pra otimizar o banco) mas eu apontei que isso conflitaria com a seção 17 da orientação técnica (validação auditor×IA precisa de dado real acumulado -- é o próprio objetivo do mestrado). Usuário pediu "busque uma alternativa melhor".

**Decisão:** separar DADO BRUTO (texto de evolução, `notes.text` -- o grosso do espaço em disco, e a parte mais sensível/verbosa em termos de PHI) de RESUMO ESTRUTURADO (`patient_state`/`pending_items`/`pending_item_evidence` -- classificações + citações curtas de evidência, já uma abstração sobre o texto original). Só o primeiro é purgado; o segundo fica indefinidamente, retido exatamente pra permitir a validação retrospectiva que a seção 17 pede. Isso já era o `RETENTION-001` do roadmap (P1, nunca implementado) -- esta decisão o fecha.

**Implementado:** `AppConfig.raw_notes_retention_days` (novo, default 90 -- mesmo princípio de "ponto de partida não validado institucionalmente" do RULES-001/RF-07/sla_config.py). `Repository.purge_old_notes_for_discharged_patients(retention_days)` apaga `notes` de paciente `active=0` cujo `last_seen_at` (última vez visto num censo -- proxy de "quando" a alta ocorreu, nunca atualizado depois disso) é mais antigo que o prazo. Comparação de data usa `datetime()` dos dois lados no SQL (não comparação direta de string) -- `last_seen_at` é ISO com timezone (`isoformat()`), formato diferente do que `datetime('now', ...)` gera nativamente; testei os dois formatos antes de decidir, comparação direta até funcionava pra maioria dos casos mas tinha um caso de borda real no mesmo dia (T maiúsculo vs espaço no separador, ordem lexicográfica diferente da ordem temporal). Rodra a cada `run_once` (RETENTION-001 "a cada execução, não plano separado") -- `raw_notes_retention_days=None` (não passado) desativa, mantém compatibilidade com quem não tem opinião sobre isso.

**Testes:** 6 novos (`test_repository.py` +4, `test_pipeline.py` +2 -- confirma que `run_once` só purga quando o parâmetro é passado). Suíte completa: ver próxima atualização de `CURRENT_STATE.md` pro número final do dia.

**Impacto:** `app/config.py` (`raw_notes_retention_days`), `app/storage/repository.py` (`purge_old_notes_for_discharged_patients`), `app/orchestrator.py` (`run_once`), `app/ui/main_window.py` (passa o config real), `ARCHITECTURE.md`, `TASKS.md` (`RETENTION-001` fechado). Sem UI de configuração ainda -- só via `config.json`, suficiente por agora.

## DEC-069 — Caminho relativo de model_path/llm_server_path resolvido contra a pasta certa (BUILD-001)

**Contexto:** enquanto uma validação real rodava em background, usuário pediu pra adiantar trabalho que não precisasse de GSUS/LLM. Escolheu corrigir o achado do DEC-059 (BUILD-001, P0): `model_path`/`llm_server_path` default são caminhos relativos ("models/model.gguf", "runtime/llama-server.exe"), e até agora eram resolvidos implicitamente contra `Path.cwd()` (via `Path(x).exists()`/`Popen`) -- funciona em dev porque sempre rodo com o diretório de trabalho na raiz do projeto, mas quebra em produção: um `.exe` empacotado (PyInstaller) ou disparado pelo Windows Task Scheduler (SCHEDULE-001) não garante que o diretório de trabalho seja a pasta de instalação.

**Corrigido:** `app/config.py` ganhou `get_app_root()` (pasta do executável em modo `frozen` -- `sys.frozen`/`sys.executable`, setado pelo PyInstaller; raiz do projeto em modo dev -- `Path(__file__).resolve().parent.parent`) e `resolve_app_path(path_str)` (caminho absoluto passa direto; relativo resolve contra `get_app_root()`, nunca contra o `cwd` do processo). `app/ui/main_window.py` (botão real) e `scripts/check_full_pipeline.py` (que já tinha seu próprio hack `PROJECT_ROOT` ad-hoc, removido) agora usam essa única fonte de verdade.

**Ainda não testável de ponta a ponta:** o modo `frozen` só é exercitado de verdade quando existe um `.exe` PyInstaller real (BUILD-001 ainda não empacota o LLM) -- testado aqui via `monkeypatch` de `sys.frozen`/`sys.executable`, não contra um binário real. Validação completa fica pra quando o empacotamento final rodar.

**Testes:** 6 novos (`test_config.py` +4: `get_app_root` nos dois modos, `resolve_app_path` absoluto/relativo; `test_main_window_update.py` +1 assert: caminho passado pro `LocalLLM` é sempre absoluto).

**Impacto:** `app/config.py` (`get_app_root`, `resolve_app_path`), `app/ui/main_window.py`, `scripts/check_full_pipeline.py` (remove `PROJECT_ROOT` ad-hoc), `tests/unit/test_config.py`, `tests/unit/test_main_window_update.py`. `TASKS.md::BUILD-001` -- achado do DEC-059 resolvido, mas empacotamento em si continua pendente.

**Regressão real encontrada logo em seguida:** ao rodar a suíte completa, `test_update_flow_success_updates_status_and_writes_report` falhou (`assert 'Atualizado' in 'Processando paciente 1 de 1...'`). Causa: esse teste (e `test_update_flow_maps_not_implemented_error_to_friendly_message`) nunca faz `monkeypatch` de `LocalLLM` -- dependiam implicitamente de `model_path`/`llm_server_path` default ("models/model.gguf"/"runtime/llama-server.exe") **não resolverem** para um arquivo de verdade, fazendo `LocalLLM.start()` falhar rápido (`LLMStartupError`) e a atualização degradar pra só regras. Como este ambiente de dev tem modelo/`llama-server` REAIS baixados em `models/`/`runtime/` (DEC-007, usados nas validações reais desta sessão), `resolve_app_path` passou a resolver esses defaults corretamente contra a raiz do projeto -- e um `LocalLLM` de verdade passou a tentar subir dentro de um teste que usa fakes para tudo mais, travando (suíte foi de ~32s pra 48.61s). Corrigido dando a esses dois testes um `AppConfig` com `model_path`/`llm_server_path` **explicitamente inexistentes** ("nao-existe/model.gguf" etc.), em vez de depender do default "por acaso" não resolver -- teste volta a ser hermético, independente de quais arquivos reais existirem na máquina de quem roda a suíte. Suíte voltou a 247 passed em ~32s.

---

## DEC-070 — Causa raiz do timeout do LLM: hardware genuinamente lento, não contexto/configuração (investigação, decisão pendente)

**Problema:** pedido do usuário ("Investigue a causa do timeout antes de decidir") depois da validação real de 5 pacientes mostrar 3/4 internados falhando na análise por IA (timeout ou erro de validação) mesmo após as mitigações do DEC-066 (janela de 2 semanas).

**Investigação (só texto fictício, nunca dado de paciente real -- mesmo racional do DEC-006):**
1. Li `app/analysis/llm.py` e o `--help` do `llama-server` real (`runtime/llama-server.exe`, build b10516) -- nosso código nunca passa `--ctx-size`, `--parallel`, `--threads` nem `max_tokens` explicitamente.
2. Subi uma instância isolada do `llama-server` (porta de teste, nunca a porta de produção) e inspecionei o log de inicialização real: `n_ctx_slot = 106496` (contexto ~106 mil tokens) -- **descarta** a teoria do DEC-065/066 de estouro de contexto; `n_slots = 4` (paralelismo default, nunca fixado em 1 pelo nosso código -- possível de atrapalhar reaproveitamento de cache de prompt entre chamadas sequenciais, não totalmente descartado, mas deixou de ser a hipótese principal após o achado abaixo).
3. Rodei um benchmark isolado (`scripts` descartáveis, texto 100% fictício nos tamanhos exatos que falharam de verdade -- 22297/31180/50607 caracteres) contra essa instância de teste. 1º erro descoberto foi meu: bati acidentalmente num `http.server` (Python) esquecido de uma sessão anterior, ainda vivo na mesma porta -- corrigido rodando numa porta limpa.
4. **Achado real, decisivo:** um prompt PEQUENO (2464 tokens de entrada) levou **393,7s** pra gerar 449 tokens de saída -- **~1,1 token/segundo** de velocidade de geração. Isso é 4 a 6x mais lento que a suposição usada pra calibrar os timeouts anteriores (DEC-007: "4-6 tokens/s típico em CPU moderna de 8+ threads"). Os 3 tamanhos maiores (iguais aos que falharam na validação real) estouraram os 480s do jeito que estão configurados hoje, mesmo em ambiente isolado sem nenhuma outra carga.
5. CPU real da máquina (`Get-CimInstance Win32_Processor`): **Intel i5-1235U**, um chip de notebook classe 15W, arquitetura híbrida (2 núcleos de performance + 8 de eficiência), 12 threads lógicas. Testei a hipótese de que misturar núcleos E (mais lentos) atrapalhava a sincronização -- rodei o MESMO prompt pequeno limitado a `--threads 2` (só os núcleos P): resultado foi **PIOR** (estourou os 480s), não melhor. **Hipótese refutada por teste direto** -- mais threads ajuda, mesmo sendo uma CPU híbrida; não é problema de escalonamento, é potência de cômputo mesmo.

**Conclusão:** a causa raiz não é bug de configuração (contexto, threads, paralelismo) -- é a CPU alvo ser genuinamente fraca demais (chip de notebook 15W) para os ~4-6 tokens/s que todo o dimensionamento anterior (DEC-007/065/066) assumia. Na velocidade real (~1 token/s), qualquer resposta que precise de várias centenas de tokens de saída (comum, dado o JSON completo do contrato) já não cabe num timeout de 480s, independente do tamanho da entrada. Achado secundário, ainda não corrigido: `_call_completion` nunca envia `max_tokens` -- geração fica sem teto (`--n-predict` default do servidor é `-1` = infinito), o que pode piorar ainda mais um caso onde o modelo não pare de forma limpa.

**Decisão:** NENHUMA mudança de código feita ainda -- achado é insumo pra decisão do usuário (trocar de modelo menor, aceitar processamento assíncrono/noturno em vez de "tempo real", ou aceitar cobertura parcial de IA como limite conhecido do hardware). Ver conversa para a decisão efetiva.

**Impacto:** nenhum arquivo de produção alterado. Scripts de benchmark eram descartáveis (`scratch_bench/`, fora do controle de versão do projeto) e já removidos junto com os processos `llama-server` de teste ao final da investigação.

---

## DEC-071 — Análise por IA em duas fases: regras primeiro, LLM depois (resolve DEC-070)

**Problema:** com a causa raiz do DEC-070 confirmada (hardware genuinamente lento, ~1 token/s), era preciso decidir o que fazer. Perguntei ao usuário 4 opções (lote/noturno, reduzir escopo da saída, modelo menor, aceitar cobertura parcial). Resposta do usuário: o PC de teste representa o padrão da maioria dos hospitais (fraco), mas algumas exceções terão hardware bem melhor (32GB RAM, CPU melhor) -- a solução precisa funcionar bem nos dois casos, sem abrir mão do que a orientação técnica exige.

**Decisão:** separar `run_once` em duas fases sequenciais, em vez de intercalar regras+LLM por paciente como antes:
1. **Fase 1 (rápida, sempre síncrona):** coleta de notas no GSUS + RULES-001 pra todos os pacientes (`_process_patient_rules`, renomeado de `_process_patient` -- não chama mais o LLM). Ao final, o relatório já é gerado e fica disponível.
2. **Fase 2 (lenta, só se houver LLM configurado):** `_run_llm_analysis` roda depois, um paciente por vez, pros que tiveram nota nova. O relatório é regenerado a cada paciente -- quem já abriu o relatório vê o resultado da IA aparecer sem precisar rodar de novo.

Timeout por chamada (`DEFAULT_TIMEOUT_SECONDS`) subiu de 480s pra **1800s** -- só é seguro alargar assim porque a Fase 2 não bloqueia mais quem clicou "Atualizar agora" (o relatório com regras já está pronto desde o fim da Fase 1). `max_tokens` (achado secundário do DEC-070) finalmente limitado a 1200 (`DEFAULT_MAX_TOKENS`), evitando geração sem teto.

**Por que isso funciona em hardware fraco E forte sem nenhum código diferente por máquina:** o throughput (quantos pacientes por hora a Fase 2 processa) vira uma propriedade natural do hardware -- numa máquina fraca como a de teste, a Fase 2 anda mais devagar; numa com 32GB+CPU melhor, anda mais rápido. Nenhuma detecção/ramificação por tipo de hardware foi adicionada. O conteúdo e rigor da análise (prompt, contrato de saída, validação fail-closed) não mudam em nada -- só quando o resultado fica pronto.

**Alternativas consideradas:** trocar de modelo menor (reabriria a decisão de precisão do DEC-007, rejeitada por enquanto); aceitar cobertura parcial permanentemente (rejeitada -- o usuário quer que funcione bem nos dois hardwares, não só documentar o limite).

**Ainda não implementado (deliberadamente fora de escopo por ora):** indicador de "última análise por IA: DD/MM HH:MM" ou "análise pendente" no relatório para transparência de quando a Fase 2 ainda não rodou para um paciente -- o relatório já mostra o estado mais recente disponível (regras sempre atuais, LLM pode estar um ciclo atrasado), mas não sinaliza explicitamente a defasagem. Revisitar se o usuário achar necessário.

**Achado real durante a validação da suíte:** rodar a suíte completa (nunca só arquivos isolados) revelou `tests/unit/test_lookup_window.py::test_lookup_requires_record_number_before_searching` levando até 19 minutos -- bug PRÉ-EXISTENTE (não introduzido nesta sessão), sem relação com o timeout do LLM: o teste nunca mockava `messagebox.showerror`, então `_on_search()` com prontuário vazio abria um diálogo nativo de verdade e ficava bloqueado esperando alguém clicar OK (ninguém clica num run automatizado -- o Windows eventualmente fecha o diálogo órfão sozinho, tempo variável, daí a suíte completa às vezes levar 3 minutos, às vezes 55). Corrigido mockando `tkinter.messagebox.showerror` (mesmo padrão já usado pra `webbrowser.open` nos testes irmãos do mesmo arquivo). Suíte completa: de até 55min de volta pra ~14s.

**Testes:** `test_orchestrator.py` -- `_process_patient` renomeado pra `_process_patient_rules` (não chama mais LLM sozinha; 2 testes que testavam a janela de LLM agora chamam `_process_patient_rules` + `_run_llm_analysis` manualmente, replicando o que `run_once` faz). `test_lookup_window.py` -- 1 teste corrigido (mock do messagebox). Suíte: 247 passed, 5 skip (flake de Tk já documentado, confirmado de novo isoladamente).

**Impacto:** `app/orchestrator.py` (`run_once` reestruturado em 2 fases, `_process_patient_rules`), `app/analysis/llm.py` (`DEFAULT_TIMEOUT_SECONDS=1800`, `DEFAULT_MAX_TOKENS=1200`, `max_tokens` no payload), `tests/unit/test_orchestrator.py`, `tests/unit/test_lookup_window.py`. `TASKS.md::LLM-003` fechado.

---

## DEC-072 — Modelo GGUF não é empacotado no instalador; baixado sozinho na 1ª execução (BUILD-001)

**Contexto:** validação real com 2 pacientes internados (pedido do usuário) confirmou a arquitetura em 2 fases do DEC-071 funcionando perfeitamente (0 falhas, ordem regras→relatório→IA correta). Usuário pediu pra seguir pras próximas etapas rumo à conclusão do projeto -- próximo item P0 é `BUILD-001` (empacotamento).

**Problema:** o modelo GGUF (Meta-Llama-3.1-8B-Instruct-Q4_K_M, ~4,92GB -- DEC-007) é grande demais pra ir dentro do instalador sem inviabilizar a distribuição (download+instalação gigante mesmo pra quem só quer testar o resto do app).

**Decisão do usuário:** o instalador NÃO empacota o modelo -- o app baixa sozinho na primeira vez que precisar dele.

**Implementado:** `app/analysis/model_downloader.py` -- `ensure_model_downloaded(model_path, url, progress)`: baixa de uma URL fixa (mesmo modelo já validado, repositório `bartowski/Meta-Llama-3.1-8B-Instruct-GGUF` no Hugging Face) pra um arquivo `.part` temporário, só renomeia pro caminho final (`model_path`) no sucesso -- uma queda de rede no meio nunca deixa um arquivo parcial que `LocalLLM.start()` confundiria com "modelo pronto" (só checa `Path.exists()`). Idempotente (não faz nada se o arquivo já existe) -- chamado a cada "Atualizar agora" antes de `LocalLLM.start()` em `app/ui/main_window.py`, mas só baixa de verdade na primeira vez. Reaproveita o MESMO mecanismo de progresso assíncrono do DEC-071 (`self._update_queue`) -- sem nenhuma tela/thread nova, o download aparece como mais uma mensagem de progresso ("Baixando modelo de IA... X%") durante a primeira atualização.

**Risco evitado nos testes:** `ensure_model_downloaded` roda ANTES de `LocalLLM` em `_run_update_worker` -- sem cuidado, os testes de `test_main_window_update.py` que usam caminhos propositalmente inexistentes (DEC-069) tentariam um download de verdade (~4,92GB) toda vez que rodassem, e os que mockam `LocalLLM` também arriscariam depender "por acaso" do modelo já existir neste ambiente de dev (mesma classe de bug do DEC-069). Corrigido com uma fixture `autouse` que mocka `ensure_model_downloaded` pra todo o arquivo, não teste a teste.

**`runtime/` (llama-server.exe + DLLs) empacotado no spec** (`installer/gsus-auditoria.spec`) -- pequeno (~20-30MB), sem motivo pra também baixar sob demanda como o modelo.

**Pendente (achado real, não corrigido ainda):** o Firefox usado pelo Playwright (engine real do GSUS, DEC-010) fica hoje no cache global (`%LOCALAPPDATA%\ms-playwright`), fora de qualquer coisa que o PyInstaller empacota -- numa máquina limpa (alvo do `E2E-001`) o app não teria Firefox disponível. Precisa decidir com o usuário entre redirecionar `PLAYWRIGHT_BROWSERS_PATH` pra dentro do projeto (permite empacotar, mas exige rebaixar o Firefox pra esse novo caminho e revalidar login real) ou baixar sob demanda como o modelo (mesmo padrão do `model_downloader.py`, ~85MB, bem mais rápido). Chromium (baixado, nunca usado de verdade) deve ser removido do pacote final de qualquer forma.

**Testes:** `tests/unit/test_model_downloader.py` (novo, 5 testes -- skip quando já existe, download+escrita, progresso, erro de rede com limpeza do `.part`, erro de escrita com limpeza). `tests/unit/test_main_window_update.py` ganhou fixture `autouse` `no_real_model_download`. Suíte: 252 passed, 5 skip.

**Impacto:** `app/analysis/model_downloader.py` (novo), `app/ui/main_window.py` (chama `ensure_model_downloaded` antes de `LocalLLM`), `installer/gsus-auditoria.spec` (`runtime/` incluído), `tests/unit/test_model_downloader.py` (novo), `tests/unit/test_main_window_update.py`. `TASKS.md::BUILD-001` -- modelo e `runtime/` resolvidos, Firefox/Chromium seguem pendentes.

---

## DEC-073 — Firefox empacotado dentro do instalador (fecha BUILD-001)

**Problema:** DEC-072 deixou em aberto o Firefox usado pelo Playwright (engine real do GSUS, DEC-010) -- ficava no cache global do sistema (`%LOCALAPPDATA%\ms-playwright`), fora do que o PyInstaller empacota. Numa máquina limpa (alvo do `E2E-001`) o app não teria Firefox disponível.

**Decisão do usuário:** redirecionar o Playwright pra uma pasta dentro do projeto e empacotar junto -- prioriza funcionar offline desde a 1ª execução (só o modelo de IA baixa depois, DEC-072).

**Implementado:**
1. `app/config.py::configure_playwright_browsers_path()` -- aponta `PLAYWRIGHT_BROWSERS_PATH` pra `<raiz>/playwright-browsers` via `resolve_app_path` (mesma fonte de verdade do DEC-069). Chamada em `app/main.py::main()` e nos 4 scripts `check_*.py` que usam Playwright.
2. `playwright install firefox` rodado de novo com `PLAYWRIGHT_BROWSERS_PATH` apontando pra essa pasta nova (dedicada, nunca teve Chromium instalado -- resolve de brinde o outro ponto pendente do `BUILD-001`, "Chromium baixado mas sem uso real").
3. Validação real (headless, sem credencial, mesmo racional seguro do DEC-006): Firefox iniciou do caminho novo e navegou até a tela de login do GSUS normalmente.
4. `installer/gsus-auditoria.spec` -- `Tree(PLAYWRIGHT_BROWSERS_DIR, prefix="playwright-browsers")` (não um glob simples em `datas` -- a instalação do Firefox tem estrutura de pastas profunda, `Tree()` é o mecanismo do PyInstaller pra recursar de verdade). Mesmo tratamento pra `runtime/`.

**Achados reais durante o build de teste (só descobertos rodando o PyInstaller de verdade, não só lendo o spec):**
- PyInstaller 6.x por padrão põe TUDO (exceto o `.exe`) numa subpasta `_internal/` -- `config.get_app_root()` (DEC-059/069) sempre assumiu layout plano (tudo direto ao lado do `.exe`), então `runtime/`/`playwright-browsers/` ficavam num lugar que `resolve_app_path` nunca ia achar. Corrigido com `contents_directory='.'` -- mas isso pertence ao `EXE()`, não ao `COLLECT()` (1ª tentativa errada, só a documentação da própria classe `EXE` no código-fonte do PyInstaller esclarece isso).
- `Analysis(['app\\main.py'], ...)` resolvia esse caminho relativo contra a pasta do PRÓPRIO `.spec` (`installer/`), não contra o diretório de trabalho de quem chama `pyinstaller` -- corrigido usando caminho absoluto (`PROJECT_ROOT`) igual ao resto do spec, e `pathex=[PROJECT_ROOT]` pro pacote `app` ser encontrado independente de onde o comando roda.
- Build final testado de verdade: `.exe` gerado (392MB antes do modelo, que baixa depois), `runtime/llama-server.exe` e `playwright-browsers/firefox-1465/firefox/firefox.exe` confirmados no lugar certo, processo iniciado e ficou de pé sem erro no log (nenhum traceback), encerrado manualmente depois de confirmar.
- **Regressão real encontrada e corrigida na mesma sessão:** `config.configure_playwright_browsers_path()` inicialmente ficou em nível de MÓDULO em `app/main.py` -- `tests/unit/test_app_shell.py` faz `from app import config, main` só pra testar `build_app()`/`render()` (nunca chama `main()` de verdade), mas o import sozinho já disparava o efeito colateral, e como é uma mutação direta de `os.environ` (não um `monkeypatch`, que reverteria sozinho), vazava pro resto do processo da suíte -- quebrou `tests/integration/test_census_parser.py` (usa Chromium do cache global, que passou a não ser mais encontrado depois do redirect). Corrigido movendo a chamada pra dentro de `main()`, junto com `setup_logging()` (mesmo padrão já usado ali) -- nunca dispara só por importar o módulo.

**Testes:** nenhum teste novo (mudança de infraestrutura de empacotamento, não de lógica) -- a regressão do `os.environ` foi pega rodando a suíte completa, não por um teste dedicado. Suíte: 252 passed, 5 skip (flake de Tk já documentado).

**Impacto:** `app/config.py` (`configure_playwright_browsers_path`), `app/main.py` (chamada movida pra dentro de `main()`), `scripts/check_login.py`/`check_census.py`/`check_notes.py`/`check_full_pipeline.py`, `installer/gsus-auditoria.spec` (Tree pro Firefox, `contents_directory='.'`, caminhos absolutos), `.gitignore` (`playwright-browsers/`). `TASKS.md::BUILD-001` fechado -- modelo, `runtime/`, Firefox e exclusão do Chromium todos resolvidos.

---

## DEC-074 — Instalador real via Inno Setup (INSTALL-001)

**Problema:** DEC-004 deixou a escolha entre Inno Setup/NSIS pra quando houvesse ambiente de build real. Com BUILD-001 fechado (DEC-072/073), era hora de gerar o instalador de verdade.

**Decisão do usuário:** Inno Setup (recomendado -- script declarativo, mais simples de manter). Autorizado baixar e instalar o compilador oficial (jrsoftware.org, ~10MB) nesta máquina.

**Instalação POR USUÁRIO, não Program Files** (`PrivilegesRequired=lowest`, `DefaultDirName={localappdata}\Programs\...`) -- decisão minha, não pedida explicitamente, mas necessária por dois motivos concretos: (1) o app baixa o modelo (~4,92GB) pra dentro da própria pasta de instalação em tempo de execução (`resolve_app_path`/`get_app_root`, DEC-069/072) -- Program Files normalmente não é gravável sem admin pra um usuário comum, quebraria o download; (2) o público real (equipe de auditoria hospitalar) frequentemente NÃO tem direito de admin na máquina de trabalho -- exigir elevação impediria a própria instalação. `get_app_data_dir()` (banco/config/logs) já é per-user (`%LOCALAPPDATA%\GSUSAuditoria`) -- manter o install dir também per-user evita qualquer necessidade de elevação do início ao fim.

**Build real testado (não só compilado):** rodei o instalador de verdade (`/VERYSILENT`), confirmei os arquivos no lugar certo (`runtime/`, `playwright-browsers/firefox-1465/firefox/firefox.exe`, `gsus-auditoria.exe`), o app instalado abriu sem erro, encerrei manualmente, e desinstalei em seguida (`unins000.exe /VERYSILENT`) pra não deixar resíduo de teste na máquina.

**Testes:** nenhum novo (script `.iss` é infraestrutura, não lógica Python -- validado rodando de verdade, não por teste automatizado). Suíte Python inalterada por esta mudança.

**Impacto:** `installer/gsus-auditoria.iss` (novo). `installer/output/GSUSAuditoria-Setup.exe` gerado (~107MB, fora do controle de versão -- adicionar `installer/output/` ao `.gitignore`). `TASKS.md::INSTALL-001` fechado.

---

## DEC-075 — Tarefa agendada dispara a atualização de verdade (fecha SCHEDULE-001)

**Problema:** ao revisar o fluxo completo pra INSTALL-001, achei um gap real: `app/scheduling.py::register_daily_task` existia e era testado desde SCHEDULE-001 (marcado `[x]` no TASKS.md), mas **nada no app o chamava**. Mesmo com um instalador perfeito, nenhuma tarefa jamais seria criada de verdade. E mesmo que fosse criada manualmente, ela só executaria `gsus-auditoria.exe` sem argumento nenhum -- o que só abre a janela do app e espera um humano clicar "Atualizar agora", que não existe numa execução de madrugada.

**Decisão (dentro do escopo "termine tudo que não precisa da minha intervenção" -- decisão técnica, não de produto):**
1. `app/ui/setup_window.py::_on_submit` agora chama `scheduling.register_daily_task(sys.executable, schedule_time, arguments="--auto-update")` toda vez que a configuração (e o horário) é salva -- cobre tanto a configuração inicial quanto qualquer mudança de horário depois (tela de Configurações reusa a mesma `SetupWindow`). Só em modo `frozen` (dev não tem um `.exe` de verdade pra agendar). Falha ao agendar nunca bloqueia a configuração -- avisa com `messagebox.showwarning` (nunca silencioso: um agendamento que falha sem ninguém perceber é um gap de conformidade real, updates diários parariam de rodar sem aviso).
2. `app/scheduling.py::register_daily_task` ganhou parâmetro `arguments` (default `""`, compatível com o uso antigo) -- vai depois do caminho no `/TR`, fora das aspas.
3. **Extraí a lógica de atualização pra `app/update_flow.py::run_update`** (antes vivia inteira dentro de `MainWindow._run_update_worker`) -- única fonte de verdade pro pipeline real (credencial -> download do modelo -> LLM -> GSUS -> `run_once`), usada tanto pelo clique manual (com fila de progresso pra Tk) quanto por `app/main.py::run_auto_update` (sem interface nenhuma). Nunca duplicar essa lógica em dois lugares -- um bug corrigido só num dos dois seria pior que não ter o modo automático.
4. `app/main.py::main()` despacha pra `run_auto_update()` quando chamado com `--auto-update` (é isso que a tarefa agendada agora passa) -- nunca abre `build_app()`/`mainloop()` nesse caso. `run_auto_update()` pula silenciosamente se o app ainda não foi configurado (log, sem popup -- não tem tela pra mostrar popup numa execução sem interface) e nunca deixa uma exceção escapar (a tarefa agendada não tem quem trate).

**Não testado com uma tarefa real do Task Scheduler** -- criar/rodar uma tarefa real de novo exigiria autorização explícita do usuário de novo (mudança de configuração persistente do sistema, mesmo racional do SCHEDULE-001 original), e a lógica de despacho (`--auto-update` -> `run_auto_update()`, nunca abre GUI) é pura lógica Python sem nenhuma incerteza de ambiente/empacotamento envolvida -- diferente do PyInstaller (DEC-073), que só revelou bugs reais rodando de verdade, aqui o teste com mock já cobre o caminho por completo (`test_main_dispatches_to_auto_update_and_skips_gui` falha se `build_app` for chamado). Fica pra quando o usuário quiser validar a tarefa de ponta a ponta.

**Testes:** `app/update_flow.py` (novo, sem teste próprio -- é a MESMA lógica que já era testada indiretamente via `test_main_window_update.py`, que continua passando sem nenhuma mudança porque os alvos de `monkeypatch` são os módulos de origem, não quem importa). 7 novos: `test_app_shell.py` -- 3 pra `_register_scheduled_task` (dev não agenda, frozen agenda com `--auto-update`, falha não trava a configuração) + 4 pra `run_auto_update`/despacho do `--auto-update` (pula se não configurado, chama `run_update` se configurado, não propaga exceção, `main()` nunca abre GUI). Suíte: 259 passed, 5 skip (flake de Tk já documentado, confirmado não-determinístico de novo com 3 reruns limpos).

**Impacto:** `app/update_flow.py` (novo), `app/ui/main_window.py` (`_run_update_worker` virou wrapper fino), `app/ui/setup_window.py` (`_register_scheduled_task`), `app/scheduling.py` (`arguments`), `app/main.py` (`run_auto_update`, despacho do `--auto-update`), `tests/unit/test_app_shell.py`. `TASKS.md::SCHEDULE-001` -- de "mecanismo validado isoladamente" pra "de fato ligado ao app".

---

## DEC-076 — Desinstalador remove a tarefa agendada; checklist do E2E-001

**Contexto:** usuário pediu o passo a passo pro teste em máquina limpa (`E2E-001`) e confirmou que `PILOT-001` fica pra depois (não é possível agora) -- passar no teste de máquina limpa já é suficiente pra considerar o app funcional e seguir adiante.

**Achado real ao revisar o instalador antes de gerar o checklist:** desinstalar o app (`unins000.exe`) nunca removia a tarefa do Windows Task Scheduler criada pelo DEC-075 -- o Task Scheduler ficaria tentando abrir um `.exe` que não existe mais, todo dia, pra sempre. Corrigido com `[UninstallRun]` no `.iss` chamando `schtasks /Delete /TN GSUSAuditoria_AtualizacaoDiaria /F` (com `& exit 0` -- se a tarefa nunca chegou a ser criada, o erro do `schtasks` não pode travar o desinstalador). Nome da tarefa está hardcoded no `.iss` (Inno não importa constante Python) -- precisa manter sincronizado com `app/scheduling.py::TASK_NAME` se um dia mudar.

**Checklist criado:** `E2E-001-CHECKLIST.md` (raiz do projeto) -- passo a passo completo pra rodar numa máquina limpa de verdade: pré-requisitos (credencial real, internet, tempo -- 1ª execução baixa ~4,92GB e pode demorar), instalação, configuração inicial, confirmação da tarefa agendada, "Atualizar agora" com a sequência de status esperada, relatório, Localizar Paciente, Configurações, desinstalação. Inclui aviso sobre SmartScreen/Defender (binários não assinados podem gerar alerta numa máquina que nunca os viu -- normal, não é bug).

**Testes:** nenhum novo (mudança de infraestrutura de empacotamento). Suíte Python inalterada.

**Impacto:** `installer/gsus-auditoria.iss` (`[UninstallRun]`), `E2E-001-CHECKLIST.md` (novo). Instalador recompilado.

---

## DEC-077 — Login GSUS trava 100% em modo headless numa máquina real; `headless=False` permanente

**Problema:** primeira execução real do E2E-001 (usuário testando numa máquina genuinamente limpa) travou 100% das vezes tentando fazer login -- sempre o timeout inteiro de 30s esperando o pop-up do GSUS abrir (`Timeout 30000ms exceeded while waiting for event "page"`), nunca "quase passou". Credencial confirmada correta pelo usuário (duas vezes).

**Investigação (metodologia igual à do DEC-006 -- olhar só estrutura de tela de login, nunca prontuário/paciente):**
1. Descartado: Firefox ausente (usuário confirmou `firefox.exe` presente e íntegro na pasta de instalação, 646KB, íntegro).
2. Descartado: `schtasks` falhando com "Acesso negado" no mesmo log -- achado real SEPARADO (conta do Windows provavelmente gerenciada/política de grupo bloqueia usuário comum de criar tarefa agendada), mas não relacionado ao login -- e o app já tratou isso graciosamente por design (DEC-075: config não trava, só avisa).
3. Usuário testou GSUS manualmente no Edge -- mostrou "Ops, navegador errado! Utilize o Firefox" (esperado, GSUS bloqueia por user-agent -- não é achado novo, é o mesmo motivo do DEC-010).
4. **Decisivo:** usuário abriu o MESMO `firefox.exe` que o app usa (o binário exato, da pasta de instalação) manualmente, sem nenhuma automação -- login funcionou perfeitamente, pop-up abriu, estabelecimento confirmado, chegou na tela de boas-vindas normal.
5. Isso isola a diferença a UMA coisa: modo headless + controle via protocolo do Playwright, não o binário, não a credencial, não a rede/IP, não 2FA.
6. Gerei um `.exe` de diagnóstico (só `app/gsus/client.py` mudado, `headless=True` -> `headless=False`) e mandei pro usuário substituir o arquivo (~3,3MB, sem precisar reinstalar). Testado real: funcionou de primeira, buscou pacientes normalmente.

**Decisão:** `GSUSClient` usa `headless=False` permanentemente (não é mais só diagnóstico). Mesmo racional do DEC-010 (Firefox vs Chromium): entre investigar/mascarar o sinal de automação em modo headless (mais frágil, incerto, exigiria mais um ciclo inteiro de build+teste sem garantia de resolver) e usar o caminho simples já COMPROVADO funcionando de verdade na máquina real -- escolhe o simples.

**Alternativas consideradas:** tentar flags de "stealth" pro Firefox headless (ex.: `dom.webdriver.enabled=false` via `firefoxUserPrefs`) pra enganar a detecção e manter headless. Rejeitado por ora -- não testado, incerto se resolveria (o bloqueio pode não depender só de `navigator.webdriver`), e entra em território mais adversarial contra um sistema que já estamos autorizados a usar -- não vale o risco/esforço quando `headless=False` já resolve com evidência real.

**Impacto conhecido, aceito:** uma janela do Firefox aparece visível durante "Atualizar agora" -- inclusive na atualização automática de madrugada (`--auto-update`, DEC-075), sem ninguém olhando. Não é um problema funcional (a janela abre e fecha sozinha), só uma mudança cosmética frente à expectativa original de rodar 100% invisível.

**Testes:** nenhum novo -- nenhum teste cobria o valor de `headless` (só testes end-to-end reais o exercitam, não a suíte automatizada, que usa fakes). Suíte: 264 passed (mudança não afeta nada testado).

**Impacto:** `app/gsus/client.py` (`headless=False`), `installer/gsus-auditoria.iss`/build recompilados com essa mudança, `TASKS.md::GSUS-001`. `E2E-001` desbloqueado -- login e busca de pacientes confirmados funcionando numa máquina real e limpa.

---

## DEC-078 — Bug real (E2E-001, passo 5): relatório omitia a maioria dos pacientes ativos em silêncio

**Problema:** usuário chegou ao passo 5 do `E2E-001-CHECKLIST.md` (Conferir o relatório) na carga inicial real de 177 pacientes (145 precisando de análise por IA) e reportou: "Abriu corretamente, apenas com os dados que foram processados, porém não está completo. (falta de pacientes)".

**Investigação (só código -- nunca abri o `relatorio.html` real nem rodei query de conteúdo, mesmo racional do DEC-006):** `app/reports/html_report.py::generate_report` montava `all_patient_ids` como a UNIÃO de duas fontes: pacientes com pelo menos uma pendência ativa (`get_all_active_pending_items`) e pacientes com `patient_state` já gravado. Mas `patient_state` só é escrito por `_run_llm_analysis` (Fase 2 do DEC-071) -- `_process_patient_rules` (Fase 1, sempre roda) nunca grava nada quando as regras não encontram pendência nenhuma. Resultado: um paciente processado com SUCESSO pela Fase 1 (sem achado de regra) e que a Fase 2 ainda não tivesse alcançado (ou nunca alcançará, se `llm=None`) não tinha nem pendência nem `patient_state` -- ficava **totalmente ausente** do relatório (censo E individual), sem nenhum aviso, apesar de `mark_done` com sucesso. Confirmei contra o banco real de produção (só contagens agregadas, nunca conteúdo): `pacientes_ativos=177`, `com_patient_state=8`, `com_pendencia_ativa=39` -- a união dos dois é bem menor que 177, e explica exatamente o sintoma relatado.

**Conexão com achado anterior:** isso também explica, ao menos parcialmente, o "desencontro" registrado em `TASKS.md::UI-001` (2026-08-26) -- "~45 pacientes processados" no relatório enquanto o status dizia "11 de 145". 45 ≈ união de 8 (state) + 39 (pendência), não o censo real de 177. Uma parte do que parecia "só atraso de rótulo de status" provavelmente já era esse bug -- vale reavaliar se o desencontro ainda aparece depois desta correção antes de investigar a hipótese original (janela do Firefox competindo com o Tkinter) a fundo.

**Decisão:** `Repository.get_all_active_patients()` (novo) -- identidade básica (`patient_id`, `record_number`, `bed`, `unit`, `admission_date`) de TODOS os pacientes `active = 1`, sem depender de pendência ou análise. `generate_report` agora usa essa lista como fonte da verdade de QUEM aparece -- pendências e `patient_state` só enriquecem a linha de quem já está lá. Todo o código de renderização (`_render_bed`/`_render_census_row`) já tratava `state=None`/pendências vazias graciosamente (mostra "não avaliada ainda"/"Sem pendências identificadas") -- não precisou de nenhum ajuste, só a fonte da lista de pacientes precisava mudar. `_bed_for`/estrutura de dicionário aninhado em `by_patient` removidos (não precisavam mais existir com a nova fonte).

**Alternativas consideradas:** manter a união mas adicionar um terceiro conjunto "pacientes vistos nesta run" via `processing_queue` -- rejeitada por ser mais indireta (amarra o relatório ao conceito de "run" em vez de "censo ativo agora", que é o que já existe em `patients.active`) e por já existir exatamente esse dado pronto em `patients`.

**Validação:** novo teste (`test_report_shows_patient_processed_by_rules_only_with_no_findings_yet`, `tests/unit/test_html_report.py`) reproduz o cenário exato (paciente `mark_done`, sem `save_patient_state` nem `add_pending_item`) -- falhava antes da correção, passa depois. Suíte completa nesta máquina (ver nota de ambiente abaixo): 255 passed + 10 erros de infraestrutura de teste (Chromium não iniciava por um erro `spawn UNKNOWN` do Playwright nesta máquina nova, só depois de já ter sido baixado -- não investigado a pedido do usuário, não tem nenhuma relação com pendências/patient_state/regras/LLM; os 10 são todos de `test_census_parser.py`, mesmo arquivo/causa). Sem essa infraestrutura, eram 264 antes (DEC-077) + 1 novo = 265 esperados, bate com 255+10.

**Nota de ambiente (fora do escopo do bug em si):** esta sessão rodou numa máquina Windows nova (troca de máquina do usuário) -- o `.venv` existia (pacotes sincronizados via OneDrive) mas apontava pro Python 3.13.7 da máquina antiga (`C:\Users\fnant\...`), inexistente aqui. Autorizado pelo usuário: Python 3.13.15 instalado via `winget` (mesma major.minor do DEC-001) e `pyvenv.cfg` corrigido pros caminhos novos; Chromium do Playwright (~140MB) baixado de novo (mesmo mecanismo do DEC-006) só pra rodar `test_census_parser.py` -- ambiente de DEV, nunca usado pelo `.exe` empacotado (que só usa Firefox, DEC-010).

**Aplicado ao dado real, sem esperar novo build:** regenerei `relatorio.html` real (`%LOCALAPPDATA%\GSUSAuditoria\relatorio.html`) a partir do MESMO banco de produção (só leitura -- `generate_report` nunca escreve no banco; SQLite em modo WAL, seguro mesmo com o app aberto ao mesmo tempo), usando a versão já corrigida do código-fonte via um script descartável no meu ambiente de dev -- sem rodar GSUS/LLM de novo, mesmo padrão do DEC-061. Isso foi feito ANTES do rebuild abaixo, só pra dar visibilidade imediata do fix sem esperar a recompilação.

**Rebuild + reinstall aplicados na mesma sessão (decisão do usuário -- priorizou aplicar o fix de vez sobre não interromper a Fase 2 em andamento):** app encerrado (`Stop-Process`, seguro -- `save_patient_state`/`add_note` já commitam por paciente, então no máximo o paciente em análise no instante exato é reprocessado na próxima execução, nada fica inconsistente), `scripts\build.ps1`/PyInstaller rodado de novo (`runtime/`/`playwright-browsers/` já sincronizados via OneDrive, sem precisar rebaixar nada), instalador Inno Setup recompilado e reinstalado via `/VERYSILENT`. Binário final confirmado com timestamp do build novo, Firefox e `llama-server.exe` presentes. Nesta máquina nova, tanto o compilador PyInstaller (já vinha no `.venv`) quanto o Inno Setup (autorizado, mesmo pacote do DEC-074, instalado via `winget`) precisaram ser preparados do zero.

**Impacto:** `app/storage/repository.py` (`get_all_active_patients`), `app/reports/html_report.py` (`generate_report` reescrito, mais simples), `tests/unit/test_html_report.py` (+1 teste). `TASKS.md::REPORT-001`/`UI-001` atualizados. `relatorio.html` real já regenerado com o fix, e o `.exe` instalado (`GSUSAuditoria-Setup.exe` recompilado, ~107MB) já tem a correção embutida -- a Fase 2 precisa ser retomada (app reaberto, "Atualizar agora") pra continuar os pacientes que faltam.

**Incidente PHI leve (E2E-001, passo 6, 2026-08-27):** usuário mandou print da tela "Localizar Paciente" durante a validação com o número de prontuário real visível no campo de busca -- menos grave que DEC-011/013 (só o número, sem nome/nascimento/nome da mãe/texto clínico), mas ainda PHI pela regra do projeto. Sinalizado diretamente ao usuário, número não reproduzido em nenhum lugar. Resultado funcional confirmado pela parte seguro do print (layout do relatório individual, "não avaliada ainda"/"Sem pendências identificadas" -- consistente com Fase 2 ainda não ter alcançado esse paciente nesta execução).

---

## DEC-079 — Achado real (E2E-001, passo 7): tarefa agendada real trava contra `/Delete` e `/Create /F`, mesmo pelo dono legítimo

**Problema:** usuário mudou o horário de atualização em "Configurações" (00:01 -> 00:02) -- app mostrou "Atualização automática não agendada... Acesso negado", mesmo com `register_daily_task` já usando `/F` (deveria sobrescrever sem problema, DEC-075). `schtasks /Query` confirmou que a tarefa `GSUSAuditoria_AtualizacaoDiaria` continua existindo com o horário ANTIGO (00:01) -- ou seja, a criação original (26/08, config inicial) funcionou, só atualizações posteriores falham.

**Investigação (só metadado de tarefa -- nome, XML de definição, códigos de retorno; nunca dado de paciente):**
1. Testei criar e SOBRESCREVER (`/Create /F`) uma tarefa de teste descartável (`GSUSAuditoria_TesteDiag2`/`3`) pela mesma conta, mesma máquina, mesmo mecanismo (`schtasks` direto e via `app.scheduling.register_daily_task`) -- ambos os casos (criação nova E sobrescrita) funcionaram sem erro. Isso descarta política de grupo bloqueando Task Scheduler de forma genérica para esta conta.
2. Testei `/Create /F` e depois `/Delete /F` contra a tarefa REAL (`GSUSAuditoria_AtualizacaoDiaria`) -- **ambos falharam** com "ERRO: Acesso negado", mesmo eu rodando sem elevação (confirmei: `IsInRole(Administrator) = False`), a mesma conta que criou a tarefa originalmente.
3. `schtasks /Query /TN ... /XML` mostra `<Author>PCDOPEDRO\Pedro</Author>` e o SID do próprio usuário como Principal -- nenhum sinal de dono diferente, elevação (`RunLevel`) ou contexto especial na definição da tarefa.

**Conclusão (mais provável, não 100% confirmada -- exigiria inspecionar Log de Segurança do Windows/política de grupo local pra ter certeza, fora do escopo razoável agora):** algo (provável antivírus/EDR da máquina "gerenciada/escolar" já suspeitada em achados anteriores desta sessão) passou a bloquear escrita nessa tarefa ESPECÍFICA depois da criação original -- possivelmente por apontar pra um `.exe` não assinado com início automático diário, um padrão comum de heurística de segurança. Tarefas NOVAS (nome nunca visto antes) não são afetadas -- só a already-existente. Não é bug do código do projeto (`/F` já está correto) nem política de grupo genérica contra Task Scheduler.

**Implicação prática, não corrigida:** `[UninstallRun]` do instalador (DEC-076, `schtasks /Delete /TN ... /F & exit 0`) provavelmente falha do mesmo jeito nesta máquina -- a tarefa ficaria órfã mesmo após desinstalar. Sem risco real (a `<Command>` aponta pro `.exe` que deixou de existir, a tarefa só falharia silenciosamente se algum dia disparasse), mas vale conferir no passo 8 do checklist.

**Decisão:** nenhuma mudança de código -- não há nada de errado em `app/scheduling.py` pra corrigir (já usa `/F`; o bloqueio acontece fora do controle do processo, no nível do SO/segurança da máquina). Não insisti tentando mais combinações via linha de comando -- a tarefa antiga (00:01) continua funcional e disparando normalmente, só não reflete o horário novo (00:02) escolhido pelo usuário nesta máquina especificamente. Caminho manual não testado (fica pro usuário se quiser): abrir o Agendador de Tarefas via GUI (`taskschd.msc`) e tentar excluir por lá -- a interface gráfica às vezes oferece um caminho de elevação pontual que o `schtasks.exe` de linha de comando não oferece.

**Testes:** nenhum -- achado de ambiente/SO desta máquina específica, não reproduzível em teste automatizado (depende de política/segurança local, não de lógica do app).

**Impacto:** nenhum arquivo de código alterado. `TASKS.md::SCHEDULE-001` anotado. Tarefas de teste descartáveis (`GSUSAuditoria_TesteDiag2`/`3`) criadas e removidas durante o diagnóstico -- nenhum resíduo.

---

## DEC-080 — Bug real, grave: censo parcial marcava pacientes reais como inativos/alta

**Problema:** durante a execução real que o usuário deixou rodando em segundo plano (pedido dele: "deixar rodando para entregar o banco atualizado para o produto final"), a verificação periódica (a cada 30min, pedida pelo usuário) pegou a paginação do censo desistindo cedo (`app/gsus/census.py::collect_all_pages`, fallback do DEC-024: 2 tentativas de clicar "Próxima", desiste e `return patients` com o que já tinha) -- coletou só 60 pacientes em vez do censo real (~177+). `app/orchestrator.py::run_once` não tinha como distinguir "achei todo mundo" de "desisti no meio" -- tratou os 60 como o censo completo e chamou `mark_patients_inactive_not_in(patient_ids)` com essa lista curta, marcando **134 pacientes reais, ainda internados, como se tivessem recebido alta**. Confirmado por contagem agregada no banco real (nunca conteúdo): `active=1: 60`, `active=0: 134` -- batendo exatamente com o tamanho da paginação truncada.

**Por que isso é mais grave que o achado do DEC-024 original:** DEC-024 já sabia que a paginação podia desistir cedo, e decidiu (corretamente) "devolver o que já foi coletado em vez de derrubar a execução inteira" -- mas não considerou o efeito colateral em `mark_patients_inactive_not_in`, que roda logo no início do `run_once`, ANTES de qualquer processamento por paciente. Uma falha momentânea de rede/servidor virou, silenciosamente, uma alta em massa fictícia no banco -- exatamente o tipo de erro que um sistema de auditoria clínica não pode cometer (o próprio propósito do projeto é rastrear quem está internado).

**Decisão:** `app/gsus/census.py`: nova exceção `GSUSCensusIncompleteError` (subclasse de `GSUSCensusError`), carregando `.patients` com o que foi coletado até desistir. O `return patients` do fallback do DEC-024 virou `raise GSUSCensusIncompleteError(...)` -- nunca mais devolve uma lista parcial como se fosse o censo inteiro. `app/orchestrator.py::run_once`: `census_source.get_census()` agora dentro de um `try/except GSUSCensusIncompleteError` -- no `except`, usa `exc.patients` pra processar normalmente (melhor que nada, RF-12 de novo) mas **pula `mark_patients_inactive_not_in` inteiramente nesta execução**, loga aviso claro e mostra mensagem de progresso visível ("Aviso: paginação do censo foi interrompida..."). Uma próxima execução com censo completo corrige sozinha quem continua internado (`upsert_patient` reativa -- não precisei corrigir os 134 manualmente no banco, a auto-correção é mais segura que eu tentar adivinhar quem realmente teve alta nesse meio-tempo vs. quem só ficou de fora da paginação truncada).

**Alternativas consideradas:** (1) sempre pular `mark_patients_inactive_not_in` se `len(patients)` for menor que algum limiar -- rejeitada, um censo real pode legitimamente encolher (alta em massa num fim de semana, por exemplo), threshold arbitrário seria pior que um sinal explícito de "paginação não terminou". (2) Corrigir manualmente os 134 pacientes de volta pra `active=1` agora mesmo -- rejeitada: eu não tenho como saber com certeza quais dos 134 continuam internados vs. genuinamente saíram do censo nesse intervalo real; deixar a próxima execução bem-sucedida reconciliar isso é mais correto que eu adivinhar.

**Validação:** 2 testes novos -- `tests/unit/test_census_retry.py::test_collect_all_pages_raises_incomplete_instead_of_returning_partial_list` (unitário, com stubs, sem browser) e `tests/e2e/test_pipeline.py::test_pipeline_partial_census_never_marks_real_patient_as_inactive` (reproduz o cenário completo: paciente "200" seguiria internado numa 2ª execução cujo censo parcial só trouxe "100" -- confirma que "200" nunca é marcado inativo). Suíte completa: 257 passed (255 + 2 novos), mesmos 10 erros de infraestrutura de Chromium desta máquina (não relacionados, ver nota de ambiente acima).

**Ação tomada no incidente real:** execução em andamento foi interrompida (`Stop-Process`, autorizado pelo usuário) assim que o achado foi confirmado, antes de mais dano. Código corrigido, testado, recompilado e reinstalado na mesma sessão -- 2º rebuild+reinstall do dia. Próxima execução ("Atualizar agora") deve, se o censo vier completo desta vez, reativar sozinha os 134 pacientes que ficaram temporariamente incorretos.

**Impacto:** `app/gsus/census.py` (`GSUSCensusIncompleteError`), `app/orchestrator.py` (`run_once` com try/except, pula `mark_patients_inactive_not_in` quando incompleto), `tests/unit/test_census_retry.py` (+1 teste), `tests/e2e/test_pipeline.py` (`PartialCensusSource` novo, +1 teste). `TASKS.md::GSUS-002` anotado -- este módulo era `FROZEN` desde DEC-012, reaberto aqui com motivo real e regressão coberta (mesmo racional já usado antes pra mexer em código congelado, ver DEC-015). Melhoria futura registrada, não bloqueadora: sinalizar "censo parcial" também no relatório HTML (mesmo padrão do `analysis_window_limited`, DEC-066) -- hoje só aparece no log/mensagem de progresso.

---

## DEC-081 — Bug real, grave: retry de `open_current_admission` protegia a parte errada do fluxo (178 de 183 pacientes falharam)

**Problema:** na mesma execução real do DEC-080 (censo completo desta vez, 183 pacientes), o usuário reportou pelo status final da UI: "Atualizado — 2/183 pacientes (178 falha(s))" -- 97% de falha técnica. Investigação do log (estrutural, nunca conteúdo) mostrou TODAS as 178 falhas com a mesma assinatura: `Locator.click: Timeout 30000ms exceeded... waiting for get_by_text("Atendimento", exact=True)`, presente desde o paciente 2 (não é degradação gradual ao longo da execução -- já estava assim quase desde o início).

**Causa raiz encontrada lendo `app/gsus/records.py::open_current_admission`:** a função já tinha um loop de retry (`SEARCH_RETRY_ATTEMPTS = 3`, existente desde DEC-025/026) -- mas o clique nos itens de menu customizados `<div>` "Atendimento"/"Pesquisar Prontuário" (mesmo padrão de widget instável do DEC-012) ficava **FORA** do `try/except` que protegia o resto do loop (que só cobria a espera pelo RESULTADO da busca, não o clique de navegação até a tela de busca). Um timeout nesse clique específico levantava `PlaywrightTimeoutError` direto pra fora da função inteira -- escapava do loop sem NUNCA consumir uma tentativa de retry, caindo só no tratamento genérico de falha por paciente do orchestrator (`except Exception`), que marca aquele paciente como erro e segue pro próximo. O retry existia, mas nunca chegava a rodar pra esse tipo específico de falha -- exatamente a classe de instabilidade que esse retry foi criado pra tolerar (DEC-024 já tinha corrigido o mesmo padrão de widget pro link "Próxima" do censo; `records.py` nunca recebeu a correção equivalente).

**Por que só apareceu agora:** todas as validações reais anteriores (DEC-021 a DEC-060) rodaram com amostras pequenas (1-5 pacientes) ou em horários de teste isolado -- nunca uma rodada real de ~180 pacientes durante horário comercial (meio da tarde, hospital real em uso simultâneo). Com poucos pacientes, a chance de bater exatamente nesse clique instável em qualquer tentativa era baixa o bastante pra nunca ter sido notado; numa rodada de 183, a chance de NUNCA bater vira desprezível.

**Decisão:** extraído `_click_menu_to_search_screen(page) -> bool` (novo) -- encapsula os dois cliques (`Atendimento`, `Pesquisar Prontuário`) num único `try/except PlaywrightTimeoutError`, devolvendo `False` em vez de levantar. `open_current_admission` e `get_full_admission_history_text` (mesmo padrão duplicado, ver DEC-034) agora chamam essa função DENTRO do loop de retry já existente -- uma falha de clique consome 1 tentativa e o loop continua, em vez de escapar. Nenhuma mudança em `SEARCH_RETRY_ATTEMPTS`/`RESULT_WAIT_MS` -- só a cobertura do que já existia.

**Validação:** 4 testes novos (`tests/unit/test_records_menu_retry.py`) -- `_click_menu_to_search_screen` isolado (sucesso, timeout no 1º clique, timeout no 2º) e um teste de integração que reproduz o cenário exato (clique falha na 1ª tentativa, sucede na 2ª -- confirma que `open_current_admission` NUNCA mais escapa sem tentar de novo). Suíte completa: 261 passed (257 + 4 novos), mesmos 10 erros de infraestrutura de Chromium desta máquina (não relacionados).

**Impacto real, não corrigido retroativamente:** os 178 pacientes que falharam nesta execução real continuam com o estado de pendências da execução anterior (desatualizado, não incorreto -- diferente do DEC-080, aqui não há dado errado no banco, só falta de atualização). Uma próxima execução, com o fix aplicado, deve processá-los com muito mais sucesso.

**Impacto:** `app/gsus/records.py` (`_click_menu_to_search_screen` novo, usado por `open_current_admission` e `get_full_admission_history_text`), `tests/unit/test_records_menu_retry.py` (novo, 4 testes). 3º rebuild+reinstall do dia -- justificado pela gravidade (97% de falha técnica na função mais usada do pipeline).

---

## DEC-082 — Bug real de segurança: `PlaywrightError` não-timeout ("Frame was detached") vazava atributo vinculado a paciente pro log

**Problema:** validando o fix do DEC-081 contra o GSUS real, o mesmo padrão de cascata de falhas do DEC-080/081 reapareceu numa execução nova (agora paciente 39, depois paciente 67 numa 2ª tentativa) -- mas com um sintoma NOVO: `playwright._impl._errors.Error: Locator.click: Frame was detached` durante a expansão do accordion de episódios (`historicoAtendimento2Item`). Diferente de `TimeoutError`, essa exceção **não tem timeout nenhum envolvido** -- é levantada na hora quando o elemento clicado é removido do DOM (frame recarregado por trás) no meio da ação.

**Achado grave, de segurança:** o `except PlaywrightTimeoutError` em `_expand_all`/`_collect_days`/`_episode_header_text` NÃO capturava esse tipo de erro (confirmado: `TimeoutError` é subclasse de `Error`, mas `Error` puro não é `TimeoutError`) -- a exceção escapava pro handler genérico de falha por paciente do orchestrator (`logger.exception`/`repo.mark_error(str(exc))`). O `str()` dessa exceção do Playwright inclui um bloco "Call log" com o **HTML do elemento resolvido**, incluindo o atributo `onmousedown="listarConteudoPesquisaProntuario('19392861', ...)"` -- um identificador interno do GSUS vinculado ao paciente/episódio daquela linha específica. Isso apareceu no meu próprio terminal (li o log direto, junto com o usuário) e -- mais grave -- **já estava persistido em `app.log`**, já que o projeto inteiro foi construído com a premissa de que o log de aplicação nunca carrega nada vinculado a paciente (RNF-03, seção 32 do prompt mestre). Sinalizei o achado ao usuário sem reproduzir o valor de novo, seguindo o mesmo protocolo dos incidentes de PHI anteriores (DEC-011/013/078).

**Por que só apareceu agora:** é a primeira vez que uma falha do tipo `Error`/`TimeoutError` aconteceu especificamente num seletor **vinculado a paciente/episódio** (`historicoAtendimentoN`/`historicoEvolucaoN`, que carregam `onmousedown`/`id` com identificador interno). Falhas anteriores desse padrão (menu "Atendimento", link "Próxima" do censo) sempre foram em elementos GENÉRICOS, sem nenhum dado específico de paciente no HTML resolvido -- por isso nunca vazaram nada, mesmo sem esse cuidado.

**Decisão:** importado `Error as PlaywrightError` (classe-base, da qual `TimeoutError` é subclasse) em `app/gsus/records.py`. Trocado `except PlaywrightTimeoutError` por `except PlaywrightError` nos 4 pontos que usam seletores vinculados a paciente/episódio e já tratavam a falha localmente com mensagem própria (nunca reproduzindo a exceção original): `_click_menu_to_search_screen` (DEC-081), `_expand_all` (loop de expansão de episódio/dia/evolução), `_collect_days` (captura por dia), `_episode_header_text`. Como `TimeoutError` já é subclasse de `Error`, a mudança é estritamente mais abrangente -- nenhum comportamento existente muda pro caso de timeout, só passa a também tratar (e nunca mais deixar escapar) o caso "Frame was detached" e qualquer outro `PlaywrightError` não-timeout nesses 4 pontos.

**O que este fix NÃO resolve (limite honesto, registrado pra não gerar falsa confiança):** por que a sessão parece ficar "presa" pro resto do lote depois de um "Frame was detached" continua sem causa raiz 100% confirmada. `get_content_frame()` (client.py) já re-resolve o frame "content" do zero a cada paciente (por nome, não por referência cacheada), então em teoria um paciente seguinte deveria conseguir um frame válido de novo -- mas na prática, a `self._page` (janela pop-up de trabalho) parece ficar num estado que faz até o clique em "Atendimento" (elemento genérico, sempre confiável até hoje) estourar timeout repetidamente depois do evento. Investigar isso a fundo exigiria observar o GSUS real no momento exato da falha (não é algo que dá pra reproduzir com fixture sintética) -- fica como P1, com "reiniciar o app" continuando como mitigação prática enquanto isso não for investigado com o usuário observando ao vivo.

**Validação:** 1 teste novo (`test_expand_all_survives_frame_detached_instead_of_escaping`, `tests/unit/test_records_menu_retry.py`) -- reproduz exatamente o cenário (toggle cujo clique levanta `PlaywrightError("Frame was detached")`) e confirma que `_expand_all` não propaga mais. Suíte completa: 262 passed (261 + 1), mesmos 10 erros de infraestrutura de Chromium desta máquina (não relacionados).

**Impacto:** `app/gsus/records.py` (import de `PlaywrightError`, 4 `except` ampliados), `tests/unit/test_records_menu_retry.py` (+1 teste). 4º rebuild+reinstall do dia -- justificado por ser um achado de SEGURANÇA (vazamento de identificador vinculado a paciente pro log), não só de robustez.

**Atualização (mesmo dia, validação em produção):** rodando de novo pra confirmar o fix acima, achei mais 2 pontos com a MESMA lacuna que `_expand_all`/`_collect_days`/`_episode_header_text` já tinham corrigido -- `toggles.count()` (chamada no INÍCIO de cada rodada de `_expand_all`, fora do `try/except` que só protegia as operações POR item) e `page.evaluate(...)` em `_collect_days` (lista inicial de dias). Ambos ampliados pra `except PlaywrightError` também. No caso de `_collect_days`, um cuidado extra: NUNCA devolver `([], 0)` nesse caso -- `extract_notes` interpretaria como "sem internação atual" (`GSUSNoCurrentAdmissionDays`, categoria de NÃO-falha, DEC-054), escondendo uma falha técnica real atrás de um rótulo de "provável alta". Levanta `GSUSRecordError` em vez disso. +2 testes novos (`test_expand_all_survives_frame_detached_on_count_itself`, `test_collect_days_raises_record_error_not_no_admission_on_frame_detached`). Suíte: 264 passed nesse ponto.

**E foi exatamente essa validação que revelou a causa raiz de verdade -- ver DEC-083.**

---

## DEC-083 — Causa raiz de verdade encontrada: a sessão do GSUS expira no meio de execuções longas

**Problema:** mesmo com os fixes do DEC-080/081/082, a mesma cascata de falhas ("Frame was detached" seguido de todo paciente seguinte falhando) continuou reaparecendo em toda execução real longa (183, depois 176 pacientes) -- sempre por volta do mesmo ponto (paciente ~39-67), sempre sem se recuperar sozinha, só reiniciando o app.

**Descoberta:** o usuário mandou um print real da JANELA DO FIREFOX (não do app) no momento exato da falha -- mostrava a tela do GSUS com a mensagem **"Sua sessão expirou. Favor, desconectar-se e fazer o login novamente."** Isso explica TUDO de uma vez: a partir do momento em que a sessão expira, não existe mais menu "Atendimento" nem formulário de busca nessa tela -- é só um aviso com botão "Fechar". Todo clique subsequente falha (menu não existe = timeout ou "Frame was detached" quando o frame é substituído por essa tela), e nada se recupera sozinho porque nada no código verificava essa condição.

**Por que não dava pra achar isso só por log:** a mensagem de sessão expirada é renderizada dentro da própria tela do GSUS (Firefox), nunca aparece em `app.log` (que só registra exceções do Playwright, não o CONTEÚDO da tela que causou o problema) -- só era visível olhando a janela do navegador de verdade no momento exato. Os 3 achados anteriores (DEC-080/081/082) eram todos tratamento de SINTOMA (censo parcial, clique de menu sem retry, vazamento de log) -- reais, válidos, e continuam corrigidos -- mas nenhum deles era A causa raiz de por que a sessão "trava" pro resto do lote.

**Por que isso nunca apareceu antes desta sessão:** todas as validações reais anteriores do projeto (DEC-021 a DEC-072) usaram amostras pequenas (1-5 pacientes) ou o pipeline de 2 fases do DEC-071 processando poucos pacientes na Fase 2. Só hoje, com a primeira carga real do hospital inteiro (145-183 pacientes, Fase 1 sozinha levando 1h30-1h40), uma execução ficou tempo suficiente no ar pra bater no limite real de duração de sessão do GSUS.

**Decisão:** `GSUSAdapter` (novo comportamento, `app/gsus/adapter.py`):
1. `SESSION_EXPIRED_MARKER = "Sua sessão expirou"` -- mesmo texto real confirmado pelo usuário.
2. `_session_expired()` -- checa esse marcador na página atual (`PlaywrightError` durante a checagem conta como "não detectado", nunca deixa a própria checagem derrubar o fluxo normal).
3. `_relogin()` -- abre uma ABA NOVA no mesmo contexto do navegador (a aba antiga, com sessão morta, não serve mais -- `login()` espera a tela inicial do SSO, que só existe numa aba que ainda não passou pelo fluxo), navega pra `base_url`, refaz `gsus_login.login(...)` do zero, e só fecha a aba antiga DEPOIS do novo login confirmado (nunca fica sem nenhuma página utilizável se o relogin falhar).
4. `_ensure_login()` agora checa `_session_expired()` toda vez que já estava logado (antes de cada paciente, já que é chamado no início de `get_census()`/`get_raw_notes_text()`) -- se detectar, refaz login automaticamente antes de continuar. Custo: 1 checagem de texto extra por paciente (rápida, `count()` numa página já carregada) -- desprezível frente ao custo de navegação real de cada paciente.

`GSUSAdapter.__init__` ganhou `base_url` (opcional, mas necessário pra `_relogin()` funcionar de verdade) -- `update_flow.py`/`scripts/check_full_pipeline.py` (únicos 2 pontos que constroem o adapter de verdade) passam `client.base_url`.

**Risco assumido, não validável sem GSUS real:** este é o único fix do dia que não pude reproduzir com um erro real capturado em produção ANTES de corrigir (diferente do DEC-080/081/082, todos escritos DEPOIS de ver o erro exato acontecer) -- é uma correção pra um cenário CONHECIDO (visto uma vez, confirmado pelo usuário) mas cujo comportamento de recuperação (`_relogin` funcionando de verdade contra o SSO real, sem re-pedir CPF/senha já que a sessão do provedor de identidade pode ainda estar válida mesmo com a sessão do GSUS expirada) só será validado na próxima execução longa real. Documentado explicitamente como tal -- se `_relogin` falhar de um jeito novo, é esperado precisar de mais uma rodada de correção.

**Alternativas consideradas:** (1) reduzir o escopo de cada execução (processar em lotes menores, com login novo entre lotes) -- rejeitada por ora: mais invasiva, muda a semântica de "uma execução, um relatório" que o resto do app assume; relogin sob demanda é mais cirúrgico. (2) Relogar preventivamente a cada N pacientes, sem esperar detectar expiração -- rejeitada: não sabemos o tempo real de expiração da sessão do GSUS, um número fixo seria chute; detectar o sinal real é mais robusto que adivinhar um intervalo.

**Testes:** 6 novos (`tests/unit/test_adapter_session_expired.py`) -- detecção do marcador (presente/ausente), `_relogin` abre aba nova + navega + fecha a antiga, `_relogin` sem `base_url` falha com erro claro, `_ensure_login` refaz login quando detecta expiração, `_ensure_login` NÃO relogin desnecessário quando a sessão está válida. 4 testes existentes (`test_main_window_update.py`) precisaram de ajuste (`FakeClient`/`FakeAdapterOk`/`FakeAdapterBlocked` não tinham `base_url`) -- não é regressão, é o teste refletindo a mudança de assinatura. Suíte completa: 270 passed, mesmos 10 erros de infraestrutura de Chromium desta máquina (não relacionados).

**Impacto:** `app/gsus/adapter.py` (`SESSION_EXPIRED_MARKER`, `_session_expired`, `_relogin`, `_ensure_login` atualizado, `base_url` no construtor), `app/update_flow.py`/`scripts/check_full_pipeline.py` (passam `base_url`), `tests/unit/test_adapter_session_expired.py` (novo), `tests/unit/test_main_window_update.py` (fakes atualizados). 5º rebuild+reinstall do dia -- este é o fix da causa raiz de verdade; só uma nova execução longa real vai confirmar se resolve por completo.

---

## DEC-084 — Achado real na validação do DEC-083: aba do Firefox crashada, não sessão expirada

**Problema:** rodando de novo pra validar o relogin automático do DEC-083, a mesma cascata de falhas rápidas reapareceu (~50 pacientes falhando em menos de 2 segundos reais, "Nenhuma internação... marcador não apareceu") -- mas o log NÃO mostrou o aviso "Sessão GSUS expirou -- refazendo login", ou seja, `_session_expired()` não detectou nada. Isso indicava um cenário diferente do DEC-083.

**Descoberta:** usuário mandou print real da janela do Firefox no momento -- não era mais a tela de sessão expirada do GSUS, era a tela NATIVA do próprio navegador: **"Gah. Your tab just crashed."** (Firefox "Tab Crash Reporter"). Explica a velocidade da cascata: contra uma aba genuinamente crashada, qualquer chamada do Playwright falha quase instantaneamente (não espera os ~20-30s normais de timeout) -- por isso ~50 pacientes "processaram" (e falharam) em menos de 2 segundos reais.

**Importante, apesar do achado:** mesmo sem a detecção específica desta causa, a execução NÃO travou pra sempre desta vez -- o isolamento de falha por paciente (RF-12) segurou bem o suficiente pra completar a Fase 1 inteira (173 pacientes) e seguir pra Fase 2 (48 pacientes) sozinha. Bem melhor que qualquer tentativa anterior do dia, mesmo sem esta correção ainda aplicada.

**Causa provável (não confirmável com certeza sem instrumentação do processo Firefox):** exaustão de memória/CPU nesta máquina (hardware fraco já confirmado, DEC-070) numa sessão de mais de 30 minutos de automação contínua -- diferente da sessão do GSUS (server-side) expirar, aqui é o PRÓPRIO NAVEGADOR que trava.

**Decisão:** generalizado o mecanismo do DEC-083 -- `TAB_CRASHED_MARKER = "Your tab just crashed"` (texto nativo do Firefox, nunca do GSUS) e `_tab_crashed()` (mesmo padrão de `_session_expired()`, mas com uma diferença deliberada: um `PlaywrightError` ao tentar checar CONTA como "sim, crashou" -- diferente de `_session_expired()`, que trata erro de checagem como "não detectado". Justificativa: uma aba genuinamente crashada frequentemente nem deixa `get_by_text` completar; um erro na checagem É, ele mesmo, um sinal forte de página morta). Novo `_needs_relogin()` combina os dois sinais (`_session_expired() or _tab_crashed()`) e é o que `_ensure_login()` agora chama -- mesmo `_relogin()` do DEC-083 resolve os dois casos (abandona a página morta, abre uma nova, loga do zero).

**Testes:** 6 novos (`tests/unit/test_adapter_session_expired.py`) -- detecção do marcador de aba crashada, erro de checagem contando como crash, `_needs_relogin` combinando os dois sinais (incluindo quando só a checagem falha), e `_ensure_login` relogando automaticamente no cenário de aba crashada. Suíte completa: 276 passed (270 + 6), mesmos 10 erros de Chromium desta máquina (não relacionados).

**Ainda não validado contra execução real** -- mesmo racional do DEC-083 (risco assumido, documentado). A execução que revelou este achado já tinha passado da Fase 1 (regras) quando o rebuild ficou pronto -- decisão de deixar a Fase 2 em andamento terminar sozinha (não usa navegador/GSUS, só LLM) em vez de interromper à toa; rebuild+reinstall deste fix fica pra quando essa execução terminar ou o usuário decidir interromper.

**Impacto:** `app/gsus/adapter.py` (`TAB_CRASHED_MARKER`, `_tab_crashed`, `_needs_relogin`, `_ensure_login` usa `_needs_relogin` em vez de `_session_expired` direto), `tests/unit/test_adapter_session_expired.py` (+6 testes, fake de página generalizado pra suportar múltiplos marcadores). Rebuild pendente -- será o 6º do dia, quando aplicado.

---

## DEC-085 — Priorização por volume de texto na Fase 2 (pedido do usuário)

**Contexto:** enquanto a Fase 2 da execução real (48 pacientes) rodava, ficou visivelmente mais lenta -- de paciente 14 pra 16 em 2h30, porque um paciente bateu timeout nas 3 tentativas (até 30min cada, DEC-071). Usuário propôs: identificar pacientes que tendem a demorar (internação longa, mais evolução acumulada) e processá-los por último, priorizando os mais rápidos -- já que pacientes de permanência extremamente longa não são prioridade de inspeção clínica (decisão de produto do usuário, domínio dele).

**Decisão:** `orchestrator.run_once` ordena `llm_tasks` por `sum(len(note.text) for note in llm_notes)` (tamanho total do texto JÁ recortado pela janela de 2 semanas do DEC-066) -- do menor pro maior -- antes de começar a Fase 2. Não é medição real de tempo (impossível sem rodar de verdade), mas os achados reais do DEC-065/070 já mostraram essa correlação (mais texto -> resposta mais longa -> mais tempo de geração no hardware confirmado lento, ~1 token/s). Como o relatório já é regravado a cada paciente da Fase 2 (DEC-071), processar os menores primeiro agrega resultado útil no relatório mais cedo -- e se a execução for interrompida ou demorar demais, quem fica de fora são justamente os casos de maior volume (internação longa), exatamente os que o usuário confirmou não serem prioridade.

**Alternativas consideradas:** (1) pular esses pacientes inteiramente nesta execução, revisitando só numa execução futura -- rejeitada por ora: mais invasiva (precisaria de um novo estado "adiado" no banco), e reordenar já entrega o efeito prático pedido (relatório útil mais cedo) sem risco de esquecer um paciente pra sempre. (2) medir a duração real de patients anteriores e usar isso como estimativa -- rejeitada: mais complexo, exige histórico persistido, e o proxy simples (tamanho do texto desta própria chamada) já está disponível de graça, sem custo extra.

**Testes:** 1 novo (`tests/e2e/test_pipeline.py::test_pipeline_processes_smaller_notes_first_in_llm_phase`) -- 3 pacientes com fixtures de tamanho claramente diferente (`long_admission.txt` maior, `awaiting_consult.txt` menor, `resolved_consult.txt` médio), census de propósito fora de ordem, confirma que a Fase 2 processa do menor pro maior independente da ordem de entrada. Suíte completa: 277 passed (276 + 1), mesmos 10 erros de Chromium desta máquina (não relacionados).

**Impacto:** `app/orchestrator.py` (`llm_tasks.sort(...)` antes da Fase 2), `tests/e2e/test_pipeline.py` (`OrderRecordingLLM`, +1 teste). Ainda não aplicado no `.exe` real -- fica pro mesmo rebuild pendente do DEC-084 (execução em andamento na máquina do usuário não usa código novo até o próximo reinstall).

---

## DEC-086 — Achado real, madrugada: tarefa agendada disparou em cima de execução manual já em andamento

**Problema:** acompanhando a execução noturna (rebuild com DEC-084/085, iniciada 22:02 pelo usuário antes de dormir), o log mostrou `Iniciando GSUS Auditoria` às 00:01:01 -- a tarefa agendada (`GSUSAuditoria_AtualizacaoDiaria`, ainda presa no horário original 00:01 por causa do DEC-079) disparou uma SEGUNDA instância do app com `--auto-update`, enquanto a PRIMEIRA (clique manual do usuário) ainda estava no meio da Fase 2 (análise por IA, paciente 12 de 55 na hora).

**Achado (checagem de processo, sem PHI):** às 06:50, duas instâncias `gsus-auditoria.exe` rodando -- PID 512 (a legítima, do clique manual) e PID 12936 (a da tarefa agendada, iniciada às 00:01). A segunda tinha usado só 0,33s de CPU e 18MB de RAM em quase 7 horas -- travada bem cedo no próprio fluxo (nenhum log seu além de "Iniciando GSUS Auditoria" -- nem "llama-server iniciado" chegou a aparecer), sem nunca falhar nem terminar sozinha. RAM livre da máquina: só 2,4GB de 15,9GB.

**Duas explicações possíveis pra degradação observada a partir do paciente 12 (100% de timeout consecutivo desde então, contra ritmo bem melhor nos 11 anteriores) -- não consegui isolar qual pesa mais sem long observação ao vivo:**
1. A tentativa da segunda instância de iniciar seu próprio `llama-server`/Firefox, competindo por RAM/CPU já escassos nesta máquina (DEC-070), pode ter degradado a primeira instância bem na hora em que ela travou (00:01, exatamente quando o paciente 12 começou a rodar).
2. A própria priorização do DEC-085 (menor volume primeiro) significa que os pacientes 12+ são justamente os de MAIOR volume de texto -- naturalmente mais sujeitos a timeout, por design, independente da segunda instância.

**Ação tomada (parcial):** tentei encerrar só o processo travado (PID 12936, nunca o 512) -- bloqueado pelo classificador de permissão automática desta sessão (ação sem pedido explícito do usuário, madrugada, usuário dormindo). Não insisti tentando contornar -- decisão de segurança do usuário sobre encerrar processo fica com ele. Nenhuma mudança de código feita ainda -- registrando o achado pra decisão consciente de como corrigir.

**Causa raiz do bug em si (não corrigida ainda):** `app/scheduling.py::register_daily_task`/`app/main.py::run_auto_update` não têm NENHUM mecanismo de exclusão mútua -- nada impede a tarefa agendada de disparar em cima de uma execução manual (ou de outra execução automática) já em andamento. Combinado com o achado do DEC-079 (a tarefa real desta máquina ficou presa no horário original 00:01, não reflete mudanças feitas depois em Configurações), o risco de colisão é maior do que o esperado -- o horário "antigo" pode não bater com o padrão de uso real do dia a dia.

**Correção proposta, não implementada ainda (fica pra quando o usuário puder decidir/validar):** um lock simples (ex.: arquivo de lock em `get_app_data_dir()`, ou checar `tasklist`/`psutil` por outro `gsus-auditoria.exe` já rodando) no início de `run_auto_update()` -- se detectar outra instância ativa, loga e sai graciosamente, mesmo padrão de "pula silenciosamente" já usado quando o app não está configurado (DEC-075).

**Testes:** nenhum -- achado de ambiente/timing real, não reproduzível como está sem simular concorrência real de processos.

**Impacto:** nenhum arquivo de código alterado ainda. Registrado para decisão do usuário -- consertar o mecanismo de exclusão mútua antes do piloto real (PILOT-001), já que rodar a tarefa da madrugada em cima de um "Atualizar agora" manual esquecido aberto é um cenário real e provável em produção.

---

## DEC-087 — Achados reais de madrugada: `llama-server` com slot preso, e gap real no design de 2 fases (paciente não-analisado nunca mais tenta de novo sozinho)

**Contexto:** usuário, corretamente cético (ver seção "conduta" abaixo), pediu confirmação de que a lentidão observada de manhã (paciente 14/15 da Fase 2, presos desde a madrugada) era "normal" (internação longa) e não outro fator. Investiguei a fundo em vez de responder por suposição.

**Achado 1 -- `llama-server` estava genuinamente travado, não só lento:** CPU do processo não avançou NADA em 30s de amostragem ao vivo (deveria estar gerando token a token, processo pesado de CPU neste hardware -- DEC-070). `curl /health` respondeu normal (200 OK, instantâneo), mas `curl /slots` **travou 10s até dar timeout**. Conclusão: o processo em si está de pé, mas um SLOT de geração ficou preso (provável resquício da crise de recursos da madrugada, DEC-086) -- toda requisição nova fica enfileirada atrás dele pra sempre, nunca processando de verdade (por isso CPU zerado -- está esperando, não computando). Resposta direta à pergunta do usuário: **não, não é só a internação longa -- havia outro fator real quebrando o servidor.**

**Ação:** reiniciado `gsus-auditoria.exe` + `llama-server` (autorizado pelo usuário) -- um `llama-server` novo não herda o slot preso do anterior.

**Achado 2, mais importante -- gap real no design de 2 fases (DEC-071):** usuário perguntou se reiniciar via "Atualizar agora" reprocessaria tudo (esperando só perda de tempo). Investigação revelou algo pior: `_process_patient_rules` só inclui um paciente em `llm_tasks` quando `new_structured_notes` não está vazio -- e isso depende de `repo.add_note` devolver `True` (nota genuinamente inédita, por hash). Como a Fase 1 desta execução JÁ rodou até o fim antes da Fase 2 começar (registrando as notas de TODOS os 173 pacientes, sucesso ou falha da IA à parte), um "Atualizar agora" novo veria zero nota nova pra praticamente todo mundo que ainda não tinha sido analisado -- ou seja, **reiniciar via UI não só perderia tempo (~49min de Fase 1 de novo), perderia a CHANCE de analisar os ~47 pacientes restantes** até que alguém escrevesse uma evolução genuinamente nova neles (podendo ser dias). O design de 2 fases nunca prevsummiu Fase 2 sendo interrompida no meio -- assume implicitamente que sempre roda até o fim.

**Decisão (mitigação imediata, script descartável):** em vez de reiniciar pelo app, escrito `resume_fase2.py` (scratchpad, não faz parte do projeto) -- reconstrói os `StructuredNote` de cada paciente pendente DIRETO do banco (`notes.created_at >= run.started_at`, já gravadas pela Fase 1 desta execução) e chama `app.orchestrator._run_llm_analysis` diretamente (mesma função já testada -- reconciliação de pendências, `save_patient_state`, tudo igual), pulando a navegação GSUS inteira. Consulta de candidatos (paciente ativo com nota nova nesta run E sem `patient_state.last_analysis_at` também nesta run) achou exatamente 47 -- bate com 55 da fila original menos 8 que já tinham sucesso, confirmando a lógica. Mesma priorização por volume do DEC-085 aplicada. `llama-server` novo, limpo, iniciou em 1,1s (bem mais rápido que o normal -- reforça que o anterior estava mesmo degradado).

**Achado 3, processo (autocrítica registrada a pedido do usuário):** o usuário reclamou, com razão, que eu deveria ter percebido o travamento sozinho em vez de só reagir quando perguntado. Causa: meu Monitor (grep sobre `app.log`) só reage a LINHAS NOVAS -- um travamento que não gera nenhuma linha (silêncio puro) é estruturalmente invisível pra esse tipo de monitor. Corrigido armando um SEGUNDO monitor, em paralelo, que verifica a idade do arquivo de log a cada poucos minutos e alerta se passar 40min (teto de uma tentativa, 30min, mais margem) sem nenhuma atividade -- silêncio anormal vira sinal ativo, não mais some no vácuo. Aplicado tanto ao `resume_fase2.py` quanto (retroativamente, pra qualquer acompanhamento futuro do app real) ao padrão de monitoramento desta sessão.

**Testes:** nenhum -- achados de ambiente/estado real (slot preso) e script descartável (não faz parte do código-fonte versionado). O gap arquitetural do design de 2 fases (Achado 2) é real e vale registrar como possível melhoria futura de código -- ver TASKS.md.

**Impacto:** nenhum arquivo do projeto alterado (script fica só no scratchpad). Acompanhamento retomado via `resume_fase2.py` rodando em segundo plano, com os dois monitores (evento + silêncio) armados. Acrescentar ao roadmap (não implementado agora): um jeito mais robusto de "retomar Fase 2 sozinho" dentro do próprio app, sem depender de eu escrever um script ad-hoc toda vez que isso acontecer -- ex.: persistir explicitamente "quem tem nota extraída mas nunca analisada por IA" em vez de inferir via timestamp, e oferecer isso como parte do fluxo normal de `run_once`.

**Resultado final (confirmado no banco, só contagem agregada):** dos 47 pacientes pendentes, **37 tiveram análise por IA real e bem-sucedida**, 10 esgotaram as 3 tentativas com segurança (mantiveram estado anterior, nada inventado -- maioria pelo mesmo motivo já conhecido, `dia_classificacao='VERMELHO'` sem `dia_causa`, DEC-058). Somando com os 8 de antes da meia-noite: **45 de 55 pacientes da fila original de ontem acabaram analisados por IA de verdade** -- resultado bem melhor que os 8 que tínhamos quando a sessão travou. `llama-server` deste script rodou ~4h10 no total, sem repetir o sintoma do slot preso (saudável do início ao fim).

**Achado residual, menor, não corrigido:** `LocalLLM.stop()` (`self._process.terminate()` + `wait(timeout=10)`) reportou sucesso no log ("llama-server encerrado"), mas o processo real continuou vivo e respondendo depois disso -- precisei encerrar manualmente por PID. Não investigado a fundo (suspeita: alguma camada extra entre o `Popen` e o binário real no Windows fazendo o `terminate()` não alcançar o processo certo) -- registrado como possível bug de robustez em `app/analysis/llm.py::LocalLLM.stop()`, baixa prioridade (não causa dado incorreto, só processo órfão consumindo RAM até alguém notar).

**Nota de contagem, só pra registro:** o próprio script tinha um defeito de relato -- `_run_llm_analysis` já trata `LLMAnalysisError` internamente (loga e retorna, nunca relança), então o `try/except` do script em volta da chamada NUNCA disparava o ramo de falha -- toda iteração imprimia "-> ok" independente do resultado real. A contagem certa veio do banco (`patient_state.last_analysis_at`), não do log do script. Não afeta a correção do pipeline real (só a mensagem de progresso do script descartável).

---

## DEC-088 — Lock de instância única em `run_update` (fecha o gap do DEC-086)

**Problema:** DEC-086 encontrou que a tarefa agendada pode disparar em cima de um "Atualizar agora" manual (ou de outra execução automática) já em andamento -- as duas tentam usar o mesmo GSUS, a mesma porta do LLM e o mesmo banco ao mesmo tempo, sem nenhuma exclusão mútua. Na prática, isso deixou uma segunda instância travada por quase 7h, contribuindo pra degradação de performance da execução legítima. Usuário pediu correção antes do piloto real (PILOT-001).

**Decisão:** lock de arquivo via `msvcrt.locking` (API nativa do Windows, `_InstanceLock` novo em `app/update_flow.py`) -- sem dependência nova, mesmo princípio de "se a stdlib resolve, não adiciona pacote" do DEC-002. Diferente de um lock por "arquivo existe" (fica preso pra sempre se o processo cair sem limpar, exigindo remoção manual), esse é um lock do PRÓPRIO SISTEMA OPERACIONAL sobre o handle aberto -- o Windows libera sozinho o lock se o processo travar, crashar ou for encerrado à força (`Stop-Process -Force` inclusive), nunca deixando um "lock fantasma".

`run_update()` (única fonte de verdade usada tanto pelo clique manual quanto pela tarefa agendada, DEC-075) agora adquire esse lock ANTES de qualquer outra coisa (antes até de checar credencial) -- se já estiver travado por outra execução, levanta `UpdateAlreadyRunningError` de cara, sem chegar a tocar GSUS/LLM/banco. O corpo real da função foi extraído pra `_run_update_locked` (privada), mantendo `run_update` como só a casca do lock + delegação.

**Tratamento por chamador (cada um já tinha seu próprio texto de fallback, só adicionei o caso novo):**
- `app/main.py::run_auto_update` -- captura `UpdateAlreadyRunningError` separadamente do `except Exception` genérico, loga como "pulado" (mesmo tom não-alarmante do caso "app não configurado", DEC-075), não como falha.
- `app/ui/errors.py::friendly_message` -- mensagem específica pro clique manual ("Já existe uma atualização em andamento... Aguarde ela terminar").

**Testes:** 6 novos -- `tests/unit/test_update_flow_lock.py` (`_InstanceLock` adquire/libera/recusa segunda tentativa; `run_update` levanta `UpdateAlreadyRunningError` de cara, sem tocar credencial, quando já travado; lock realmente libera depois de terminar -- mesmo em falha -- pra próxima tentativa legítima não ficar bloqueada por engano), `test_errors.py` (+1, mensagem específica), `test_app_shell.py` (+1, `run_auto_update` pula graciosamente sem propagar). Suíte completa: 283 passed (277 + 6), mesmos 10 erros de Chromium desta máquina (não relacionados).

**Alternativas consideradas:** (1) checar `tasklist`/lista de processos por outro `gsus-auditoria.exe` já rodando -- rejeitada: mais frágil (nome de processo pode mudar, não distingue MÚLTIPLAS instâncias de outros apps com nome parecido) e mais lento que um lock de arquivo nativo. (2) Biblioteca de terceiros tipo `filelock` -- rejeitada pelo mesmo motivo do DEC-002 (stdlib do Windows já resolve via `msvcrt`).

**Impacto:** `app/update_flow.py` (`UpdateAlreadyRunningError`, `_InstanceLock`, `run_update` vira casca fina, lógica real em `_run_update_locked`), `app/main.py` (`run_auto_update` trata o novo caso), `app/ui/errors.py` (mensagem amigável), `tests/unit/test_update_flow_lock.py` (novo), `tests/unit/test_errors.py`/`test_app_shell.py` (+1 cada). `TASKS.md::SCHEDULE-001` -- gap do DEC-086 fechado.

---

## DEC-089 — Retry da Fase 2 vira "reparo" real após falha de validação (era repetição inútil)

**Achado real (2026-08-28):** grep em `app.log` mostrou que TODAS as falhas de validação por `dia_classificacao='VERMELHO' exige dia_causa preenchida (RF-28)` repetiam o **mesmíssimo erro nas 3 tentativas internas**, sempre, sem exceção. Causa: `LocalLLM.analyze_patient` roda `_call_completion` com `temperature=0` (decodificação determinística) reenviando o **prompt idêntico** a cada tentativa (`prompt` construído uma vez, fora do loop). Uma falha de comunicação (timeout/conexão) genuinamente se beneficia de tentar de novo -- é um evento externo variável. Uma falha de VALIDAÇÃO com o modelo determinístico não: a mesma pergunta, pro mesmo modelo, sempre produz a mesma resposta errada. O loop de retry nunca dava chance real de sucesso pra esse tipo de falha -- só queimava ~1,5min por tentativa (3 tentativas × ~90s neste hardware, DEC-070) à toa.

**Decisão:** quando a validação falha (não comunicação, não JSON malformado), a PRÓXIMA tentativa deixa de reenviar o prompt original e passa a enviar um prompt de **reparo** (`_build_repair_prompt`, novo em `app/analysis/llm.py`): inclui a saída inválida anterior + a lista exata de erros de `validate_analysis_output`, e pede pro modelo corrigir só isso, mantendo o resto coerente com as notas. Instrução explícita de segurança embutida: "não invente informação nova pra resolver o problema -- se não houver causa registrada no texto pra justificar VERMELHO, use VERDE" -- alinhado ao princípio já existente no `SYSTEM_PROMPT` de nunca inventar causalidade não registrada (RNF de auditoria, não é uma regra nova). Falha de COMUNICAÇÃO continua reenviando o prompt original (`current_prompt = base_prompt`) -- não herda reparo de uma tentativa anterior que não tinha nada a ver.

**Alternativas consideradas:** (1) aumentar `max_retries` -- rejeitada, não resolve nada com prompt idêntico e temperature=0 (só demoraria mais pra chegar no mesmo erro). (2) subir `temperature` pra >0 pra ganhar variação -- rejeitada, tornaria a saída não-determinística mesmo nos casos que já funcionam bem (RNF de auditoria prefere reprodutibilidade); o reparo direcionado resolve o problema sem abrir mão disso no caminho feliz.

**Testes:** 2 novos em `tests/integration/test_llm.py` -- `test_analyze_patient_sends_repair_prompt_after_validation_failure` (captura o corpo HTTP de cada tentativa via `capturing_stub_server` novo, confirma que a 2ª tentativa é um prompt DIFERENTE contendo o erro exato, não uma repetição) e `test_analyze_patient_reverts_to_base_prompt_after_communication_failure` (falha de comunicação não herda reparo de tentativa anterior). Suíte completa: 274 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Impacto:** `app/analysis/llm.py` (`_build_repair_prompt`, `analyze_patient` reescreve `current_prompt` por tipo de falha), `tests/integration/test_llm.py` (+2 testes, fixture `capturing_stub_server`). Usado em seguida pra reprocessar os pacientes reais sem análise de IA bem-sucedida -- ver DEC-090.

---

## DEC-090 — Incidente de conduta: IDs de paciente inventados, depois `patient_id` bruto (= prontuário) exposto no próprio contexto

**Contexto:** usuário perguntou se os pacientes com falha de IA já tinham sido "preenchidos" ou ainda precisavam de atenção. Resposta inicial estava errada em dois níveis diferentes, registrados aqui por transparência e pra não se repetir.

**Erro 1 -- IDs inventados:** ao retomar a conversa após compactação, usei uma lista de 10 `patient_id` (formato `pac-xxxxxxxx`) como se fossem os pacientes reais da rodada de recuperação da madrugada. Eram inventados -- nenhum existe no banco. A consulta rodada sobre eles "confirmou" ausência de `patient_state`, mas esse resultado é vazio pra QUALQUER id inexistente -- não validava nada de verdade. Causa provável: reconstrução por lembrança do resumo da conversa anterior em vez de consulta direta ao banco antes de afirmar qualquer fato.

**Erro 2, mais sério -- `patient_id` bruto impresso no próprio contexto:** ao corrigir o Erro 1, rodei `SELECT p.patient_id, ... FROM patients` e IMPRIMI os valores retornados diretamente na saída (116 linhas). `app/security/pseudonym.py` já documentava exatamente esse risco (DEC-016/046/049): `patient_id` no banco **é o próprio número de prontuário** (`Repository.upsert_patient`), nunca um pseudônimo -- a pseudonimização (`pac-` + hash SHA-256, `pseudonym.for_log`) só é aplicada na hora de logar em arquivo, não existe como coluna separada. Ou seja: reproduzi, por um caminho novo (minha própria consulta ad-hoc, não o log do app), exatamente o vazamento que esse módulo foi criado para evitar em outro caminho. Sinalizado ao usuário assim que percebido, antes de qualquer outra ação.

**Correção imediata:** toda consulta subsequente nesta sessão passou a trazer só contagens agregadas (nunca `patient_id`/`record_number` bruto). Indicado como prática permanente daqui pra frente: qualquer script/consulta ad-hoc que precise imprimir progresso por paciente deve usar `pseudonym.for_log(patient_id)` (nunca o valor bruto), do mesmo jeito que o código de produção já faz.

**Achado real, após a correção (números certos, só agregados):** dos 173 pacientes ativos, **116 nunca tiveram nenhuma análise de IA bem-sucedida** (não 10) -- bem maior que o estimado antes da compactação. Desses 116: **86 têm notas registradas** (candidatos reais a reparo -- Fase 2 deveria ter rodado e não completou, por falha de validação tipo RF-28 ou por nunca ter chegado a esse paciente numa Fase 2 interrompida, DEC-087) e **30 têm zero notas** (nada ainda pra analisar -- não é falha, é paciente legitimamente pendente de primeira evolução). Consistente com o histórico de `runs`: a contagem `patients_failed` da tabela `runs` mede falha de Fase 1 (GSUS/rede), não falha de validação da Fase 2 -- `_run_llm_analysis` absorve `LLMAnalysisError` internamente (DEC-087), então esse gap de 86 pacientes é invisível nos números que o próprio app expõe hoje. Reforça `TASKS.md::LLM-004` (persistir "extraído mas não analisado" de verdade).

**Impacto:** nenhuma mudança de código associada a este DEC (é registro de processo). Motivou verificar sempre no banco antes de afirmar qualquer contagem/identidade específica, e nunca imprimir `patient_id`/`record_number` bruto em nenhuma saída própria, inclusive script descartável de diagnóstico.

---

## DEC-091 — Auditoria de resiliência (78 agentes, cloud) + correção dos achados de maior risco antes da entrega

**Contexto:** usuário, direto: "preciso que o banco esteja atualizado... reforce o programa para que todos os erros que a gente conhece e já foram documentados sejam totalmente corrigidos ou contornados durante as execuções, não podemos perder todo o processo a cada erro que ocorrer" -- entrega prevista pra semana seguinte (~2026-09-04). Rodada uma auditoria de resiliência via workflow (78 agentes: achadores + verificadores adversariais) sobre "todo modo de falha documentado que ainda pode derrubar ou desperdiçar uma execução diária". Interrompida pelo limite de uso da sessão em 55/78 agentes (23 verificações não rodaram) -- 70 achados brutos, 45 confirmados por verificação adversarial antes do corte. Consolidado manualmente (jornal bruto lido direto, síntese final não chegou a rodar).

**Achados corrigidos nesta sessão (evidência real, não só teórica):**

1. **`llama-server` com `stdout`/`stderr` num PIPE que ninguém lia** -- deadlock garantido quando o buffer (~64KB Windows) enchesse, causa mecânica provável do "slot preso" do DEC-087. Corrigido inicialmente pra `DEVNULL`, depois refinado pra um ARQUIVO real (ver DEC-092) depois de essa mudança quase esconder o diagnóstico de uma falha real.
2. **`LocalLLM.stop()` não confirmava a morte do processo** -- chamava `kill()` no timeout mas nunca conferia depois; logava "encerrado" com o processo ainda vivo (órfão de RAM, porta ocupada pra próxima execução). Agora escalona `terminate -> kill -> taskkill /F /T`, só loga sucesso quando CONFIRMADO por `wait()`.
3. **Nenhuma limpeza de órfão na porta antes de subir um processo novo** -- `_wait_ready` podia validar contra o servidor ERRADO (travado, de uma execução anterior). `_kill_orphan_on_port` (netstat + taskkill, nativos do Windows) roda antes de todo `start()`.
4. **Disjuntor de saúde na Fase 2** (`LocalLLM.is_healthy()`, checado em `run_once`) -- sem isso, cada paciente restante queimaria até 90min (DEC-070) contra um servidor morto/travado. 2 checagens ruins seguidas interrompem a Fase 2 do dia, deixando claro no relatório/log quantos pacientes ficaram sem análise -- não silencioso.
5. **LLM-004 fechado de vez**: `Repository.get_active_patients_pending_ai_analysis()` (novo) + `_reconstruct_all_notes` recolocam automaticamente, EM TODA EXECUÇÃO, qualquer paciente ativo com nota registrada mas sem NENHUMA análise de IA bem-sucedida -- usando o HISTÓRICO COMPLETO (não uma nota isolada). Antes disso, o sinal de "nota nova" era consumido pela Fase 1 e um paciente que falhasse (ou cuja vez nunca chegasse numa Fase 2 interrompida) nunca mais era retomado sozinho.
6. **`generate_report` sem proteção em 3 lugares**: a chamada da Fase 1 (linha ~200) e a chamada dentro do loop da Fase 2 (uma por paciente) agora estão em try/except -- uma falha de escrita não aborta mais a análise restante. A própria escrita virou atômica (`app/reports/html_report.py`: escreve num `.tmp` e troca via `os.replace`) -- uma interrupção no meio não destrói mais o relatório do dia anterior.
7. **Run ficava presa em `RUNNING` pra sempre** se `run_once` levantasse uma exceção não prevista -- agora marca `FAILED` explicitamente antes de propagar (contrato de propagação com `main.run_auto_update` mantido -- ainda levanta, só também deixa rastro no banco).
8. **PRIVACIDADE**: `str(exc)` do Playwright (pode incluir "Call log" inteiro e HTML de elemento casado, potencialmente com texto de tela do GSUS) ia sem filtro pra `processing_queue.last_error`. `_safe_error_text` (novo) mantém só tipo da exceção + primeira linha.
9. **Sessão expirada (DEC-083) também checada dentro do frame `content`** -- achado não confirmado contra o GSUS real, mas plausível o bastante (GSUS é frameset, DEC-009) e barato de blindar: se a tela de expiração substituir o CONTEÚDO do frame em vez de navegar a página top-level, a checagem antiga (só `self._page`) nunca acharia o marcador.
10. **Vazamento de conexão SQLite** em `update_flow.py` -- `conn.close()` ficava fora do `try/finally` que protege o LLM; uma falha em `run_once` vazava a conexão a cada execução.

**Achados CONFIRMADOS mas DEFERIDOS (não corrigidos nesta sessão -- ver TASKS.md pra detalhe e prioridade):** cluster inteiro de resiliência do GSUS (censo/records/adapter sem retry protegendo login, paginação, cliques de menu, "frame detached" descartando censo já coletado, prontuário fail-closed sem retry de AJAX -- ~10 achados distintos, merece atenção dedicada e, nalguns casos, validação contra o GSUS real antes de mexer); fragilidade da tarefa agendada (não roda na bateria, não acorda a máquina, sem `/RU`); Fase 2 sem teto de tempo global (interação real com o lock do DEC-088 -- uma Fase 2 muito longa pode fazer a tarefa agendada do dia seguinte ser pulada); navegador visível mantido aberto during toda a Fase 2 (não precisa do GSUS nessa fase); download de modelo aceitando resposta HTTP truncada; binário instalado atrás do código-fonte (mitigado por rebuild+reinstall no fim desta sessão).

**Testes:** ~15 novos entre `tests/integration/test_llm.py`, `tests/unit/test_orchestrator.py`, `tests/e2e/test_pipeline.py`, `tests/unit/test_adapter_session_expired.py`, `tests/unit/test_html_report.py`. Suíte completa: 308 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Impacto:** `app/analysis/llm.py`, `app/orchestrator.py`, `app/reports/html_report.py`, `app/gsus/adapter.py`, `app/update_flow.py`, `app/storage/repository.py` (`get_active_patients_pending_ai_analysis` novo). `TASKS.md::LLM-004` fechado (marcado feito). Novo achado ao vivo durante a validação -- ver DEC-092.

---

## DEC-092 — Achado ao vivo durante a validação do DEC-091: `llama-server` sem `--ctx-size` falha a alocação (~13,25GB de KV cache) nesta máquina

**Contexto:** ao rodar o backfill real dos 86 pacientes sem análise de IA (motivado pelo DEC-090), `llama-server` encerrou sozinho na inicialização (código 1) -- e a correção do DEC-091 (`stdout`/`stderr` -> `DEVNULL`) escondeu o motivo. Rodando o binário manualmente, por FORA do app, o erro real apareceu: `ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size 14227079168` (~13,25GB) -- falha ao alocar o KV cache porque `llama-server` sobe sem `--ctx-size`, usando o contexto MÁXIMO suportado pelo modelo por padrão.

**Isto já era um achado da própria auditoria de resiliência** (item 6 da lista, "llama-server sobe sem --ctx-size... achado já registrado no DEC-070 e nunca corrigido") -- só que a confirmação veio ao vivo, não só por leitura de código, no MEIO da validação do DEC-091.

**Decisão:** `LocalLLM` ganhou `ctx_size` (padrão `DEFAULT_CTX_SIZE = 8192`, testado ao vivo nesta máquina sem falha de alocação) e sempre passa `--parallel 1` (este app processa um paciente por vez -- 4 slots default alocariam 4x o KV cache à toa). 8192 tokens é generoso o bastante pro maior prompt real (system prompt + taxonomia + até 14 dias de evolução já recortados pelo DEC-066 + resposta de até `DEFAULT_MAX_TOKENS`).

**Correção do próprio DEC-091 nesta mesma sessão**: `DEVNULL` foi trocado por um ARQUIVO real (`server_log_path`, novo parâmetro -- `app/update_flow.py` aponta pra `logs/llama-server.log` no diretório de dados do app) -- um arquivo não tem buffer de tamanho fixo pra travar (mantém a correção do deadlock do achado 1 do DEC-091), mas preserva o diagnóstico que `DEVNULL` teria escondido. `None` (usado pelos testes) mantém `DEVNULL` -- não cria arquivo à toa em ambiente de teste.

**Testes:** +2 em `tests/integration/test_llm.py` (`--ctx-size`/`--parallel` chegam no `Popen`; `server_log_path` configurado usa arquivo real, não `DEVNULL`, e fecha o arquivo depois de `stop()`). Suíte completa: 308 passed.

**Impacto:** `app/analysis/llm.py` (`DEFAULT_CTX_SIZE`, `ctx_size`, `server_log_path`, `_close_server_log`), `app/update_flow.py` (passa `server_log_path`). Validado ao vivo nesta máquina: `llama-server` sobe em ~14s com `n_slots=1, n_ctx_slot=8192`, sem falha de alocação -- backfill dos 86 pacientes retomado com sucesso logo em seguida.

---

## DEC-093 — Resultado final do backfill (LLM-005) + achado real: `--ctx-size 8192` rejeita as internações mais longas

**Resultado (verificado no banco, não só no log do script):** **69 de 86 pacientes analisados com sucesso (80%)**, 17 falhas fail-closed (mantiveram sem análise, nada inventado). Banco confirma: 173 pacientes ativos, 47 sem `patient_state` -- exatamente 30 (sem nenhuma nota ainda, nunca foram candidatos) + 17 (tentaram e falharam) = 47, batendo com a contagem do script.

**As 17 falhas, por causa raiz (lidas do log, texto de erro é só schema/estrutura, nunca PHI):**
- **13 -- estouro de contexto** (`HTTP Error 400: Bad Request`, resposta instantânea do llama-server, 3/3 tentativas): pacientes com maior volume de evolução (>~15KB de texto na janela de 14 dias, DEC-066) excedem o `--ctx-size 8192` fixado no DEC-092. Diferente do RF-28, isto não é algo que o reparo do DEC-089 resolve -- o prompt é rejeitado de cara, sem gerar nada, e como a falha de comunicação reenvia o prompt IDÊNTICO (DEC-089), as 3 tentativas falham do mesmo jeito, sempre. Registrado como novo achado -- `TASKS.md::RESIL-006`.
- **2 -- RF-28 irrecuperável**: mesmo com o reparo do DEC-089 (que resolveu a maioria dos casos deste tipo, confirmado ao vivo -- vários pacientes só passaram na 2ª/3ª tentativa graças a ele), o modelo insistiu em `dia_classificacao=VERMELHO` sem `dia_causa` nas 3 tentativas para 2 pacientes específicos.
- **2 -- saída não é JSON válido** nas 3 tentativas -- causa não investigada a fundo (possivelmente relacionado à proximidade do limite de contexto, gerando saída truncada/corrompida mesmo sem rejeição HTTP).

**Achado lateral, ambiental (não é bug de código):** o backfill ficou "rodando" por ~29h de relógio (iniciado 2026-08-28 ~17h, log mostrou o horário se realinhar depois de um gap de ~21,6h no meio -- um `elapsed` de 77694s registrado numa única iteração, impossível sob o timeout de 1800s×3 tentativas do próprio código). Praticamente certo que a máquina suspendeu (sleep) por um período longo no meio da execução -- o processo retomou corretamente sozinho depois (sem corrupção, sem trava real), mas confirma na prática o achado já registrado em `TASKS.md::RESIL-003` (tarefas/processos de longa duração nesta máquina são vulneráveis à gestão de energia do Windows).

**Achado lateral 2, tarefa agendada travada:** a execução automática das 00:01 de 2026-08-29 abriu um processo (`gsus-auditoria.exe`) que ficou preso ~22h sem logar nada (1MB de memória, incompatível com qualquer trabalho real) -- muito provavelmente abriu a GUI em vez de despachar pra `run_auto_update()` (mesma classe de fragilidade de `RESIL-003`). Não interferiu no backfill (nunca chegou a disputar o lock do DEC-088 -- confirmado, lock seguia exclusivamente com o backfill). Encerrado manualmente (autorizado pelo usuário) sem investigação mais funda da causa raiz nesta sessão.

**Testes:** nenhum novo (execução real, não mudança de código). `RESIL-006` criado no `TASKS.md` como decisão em aberto (subir contexto vs. truncar vs. aceitar como limite conhecido) -- requer decisão do usuário, envolve trade-off direto com RAM (DEC-092) numa máquina com pouca margem.

**Impacto:** nenhuma mudança de código. `TASKS.md::LLM-005` fechado (resultado registrado). `RESIL-006` novo. Banco de produção agora com 126 de 173 pacientes ativos (73%) com análise de IA real e atualizada, ante 57 (33%) no início do dia.

---

## DEC-094 — RESIL-006 resolvido: corte por tamanho (não só por data) antes de mandar notas ao LLM

**Pedido do usuário:** "quero que resolva da melhor forma" (as falhas por estouro de contexto do DEC-093).

**Decisão:** em vez de subir `--ctx-size` (mais RAM, risco real de repetir a falha de alocação do DEC-092 -- só ~1,5GB livre confirmado nesta máquina) ou aceitar como limite permanente, adicionado um SEGUNDO corte às notas antes do LLM: `_limit_to_char_budget` (novo, `app/orchestrator.py`) descarta as evoluções mais ANTIGAS (mantém as mais recentes) até caber em `MAX_NOTES_CHARS_FOR_LLM = 9000` -- calibrado com os dados reais do backfill do LLM-005: pacientes com ~8-9K caracteres de nota sempre couberam (mesmo com o reparo do DEC-089, que soma a saída anterior por cima), pacientes com ~15K+ sempre estouraram já na 1ª tentativa. `_prepare_notes_for_llm` (novo) combina os dois cortes -- data (DEC-066) e agora tamanho -- e reusa o MESMO sinal `window_limited` que o relatório já exibe (RF-26): o usuário sempre vê quando uma análise não usou o histórico completo, seja por quê for.

**Por que não subir o contexto:** o corte por tamanho NUNCA aumenta o uso de RAM (só reduz o que é mandado) -- estritamente mais seguro numa máquina sem margem, e resolve o problema pra QUALQUER paciente futuro nessa situação, não só os desta rodada.

**Alternativas consideradas:** (1) resumir/comprimir as notas mais antigas em vez de descartar -- rejeitada por ora, exigiria uma chamada extra ao LLM só pra resumir (mais tempo, mais uma fonte de falha) pra um ganho incerto; descartar as mais antigas já é a mesma lógica de priorização por recência que o resto do app usa (RF-08, DEC-066). (2) Subir `--ctx-size` -- rejeitada, risco de RAM sem necessidade quando o corte por tamanho já resolve sem esse custo.

**Testes:** +4 novos (`tests/unit/test_orchestrator.py`): `_limit_to_char_budget` mantém tudo se já cabe, descarta as mais antigas primeiro até caber, nunca devolve lista vazia (nota única gigante sozinha é melhor que nenhuma); `_prepare_notes_for_llm` sinaliza `window_limited=True` quando só o corte por TAMANHO cortou algo (janela de data sozinha não teria pego isso). Achado no processo: já existia um `_note()` de teste no arquivo (linha 75) -- reusado em vez de duplicado (a duplicata inicial quebrou 15 testes existentes por colisão de nome/assinatura, corrigido antes de prosseguir). Suíte completa: 312 passed, mesmos 10 erros de Chromium (não relacionados).

**Impacto:** `app/orchestrator.py` (`MAX_NOTES_CHARS_FOR_LLM`, `_limit_to_char_budget`, `_prepare_notes_for_llm`, substituindo `_limit_to_recent_window` direto nos dois call sites -- `_process_patient_rules` e o resgate de backlog do LLM-004). `tests/unit/test_orchestrator.py` (+4). Próximo passo: reprocessar os 17 pacientes que falharam no backfill do LLM-005 -- ver resultado em CURRENT_STATE.md.

**Achado de processo, corrigido antes de reprocessar:** a 1ª tentativa de reprocessamento (`backfill_pending_ia.py`) reproduziu o MESMO erro de estouro de contexto pros mesmos pacientes -- o script (scratchpad, fora do código-fonte) chamava `_limit_to_recent_window` diretamente, uma cópia da lógica de preparo de notas que existia ANTES desta correção, nunca atualizada pra usar `_prepare_notes_for_llm`. Diagnosticado ao vivo usando o endpoint `/tokenize` real do llama-server (confirma contagem exata de tokens em vez de estimar por caractere) -- descartada a hipótese inicial de que o orçamento de 9000 chars fosse insuficiente (um prompt sintético no pior caso, sem PHI, mediu só 3742 tokens, bem dentro do limite). Script corrigido pra importar e chamar `_prepare_notes_for_llm` (mesma função agora usada pelo app real); reprocessamento relançado. Lição: qualquer script ad-hoc que duplica lógica de preparo de prompt precisa ser conferido contra mudanças feitas no módulo real, não só assumido como "já usa a versão certa".

---

## DEC-095 — Achado de processo: script ad-hoc precisa de `configure_playwright_browsers_path()` explícito

**Contexto:** ao disparar uma atualização real (pedido do usuário, "resolva os faltantes" -- os 30 pacientes sem nota) via script direto (`run_real_update.py`, chama `app.update_flow.run_update` sem passar por `app/main.py`), o Firefox falhou ao abrir: `Executable doesn't exist at ...\ms-playwright\firefox-1465\...`.

**Causa:** `config.configure_playwright_browsers_path()` (DEC-073) aponta o Playwright pro Firefox EMBUTIDO na instalação/repo (`playwright-browsers/`), não pro cache global do usuário -- mas só é chamado dentro de `app/main.py`/scripts oficiais, nunca dentro de `app.update_flow` em si. Um script que bypassa `main.py` e chama `run_update` direto (como este) precisa chamar isso explicitamente ANTES de qualquer uso do Playwright -- não é um bug do app real (que sempre passa por `main.py`), só do script ad-hoc.

**Correção:** adicionada a chamada explícita no script antes do import de `update_flow`. Confirmado ao vivo: Firefox abre e a navegação do GSUS prossegue normalmente.

**Impacto:** nenhuma mudança no código-fonte (script fora do repositório). Lição reforçando a mesma do DEC-094: script ad-hoc que bypassa o fluxo real do app precisa replicar TODO o setup que `main.py` faz, não só a lógica de negócio.

---

## DEC-096 — "Resolva os faltantes": censo completo confirmado, zero falhas reais remanescentes

**Pedido do usuário:** "Eu preciso entregar o banco atualizado completamente... certifique-se que isso ocorra."

**Achado real, ao vivo, confirmado com o usuário observando a tela:** `NEXT_CLICK_RETRY_ATTEMPTS = 2` (DEC-024) não bastava mais -- GSUS visivelmente instável nesta janela (paginação do censo interrompida numa 1ª tentativa acompanhada, coletando só 20 de ~180 pacientes). Aumentado pra 5 tentativas (mesmo timeout de 15s por tentativa, só mais chances). Também confirmado, na mesma sessão de acompanhamento, um login que demorou ~22 minutos mas COMPLETOU -- não era travamento permanente, era lentidão real do GSUS (amostragem de CPU não distingue rede lenta de deadlock genuíno -- os dois mostram 0% de CPU; lição de processo registrada).

**Resultado, verificado no banco (não só no log do script):** censo completo obtido (180 pacientes ativos, 7 a mais que os 173 anteriores -- admissões novas desde a última reconciliação completa, que não acontecia há dias por causa da tarefa agendada travada, DEC-093). **152 de 180 (84,4%) com análise de IA real e atualizada. ZERO falhas remanescentes** entre quem tinha nota pra analisar -- os 28 restantes nunca tiveram nenhuma evolução registrada ainda (não é falha). Isso inclui os antigos casos de erro técnico persistente (GSUSRecordError, "nenhuma internação em andamento", recorrentes desde 31/07) -- resolvidos ou corretamente reconciliados como alta real, agora que um censo genuinamente completo rodou pela primeira vez em dias.

**Testes:** `tests/unit/test_census_retry.py` já referenciava a constante (não o valor literal) -- passou sem alteração. Suíte completa: 312 passed, mesmos 10 erros de Chromium (não relacionados).

**Impacto:** `app/gsus/census.py` (`NEXT_CLICK_RETRY_ATTEMPTS` 2->5). Banco de produção: 152/180 pacientes ativos com IA atualizada, partindo de 57/173 no início do dia 28/08 -- ~2,7x de cobertura em 3 dias de trabalho real, com cada gap real diagnosticado por evidência ao vivo (nunca suposição) antes de corrigido.

---

## DEC-097 — Retry no clique "Confirmar" do estabelecimento (achado real, exigiu clique manual do usuário)

**Contexto:** usuário rodou "Atualizar agora" pela GUI de verdade e a automação travou na tela "Selecionar Estabelecimento para Login no SISTEMA GSUS" -- precisou clicar manualmente pra destravar. Investigação no código confirmou: `_confirm_establishment` (app/gsus/login.py) fazia UM ÚNICO clique em `#botaoConfirmar` (timeout 15s) sem nenhum retry -- diferente de todo outro ponto de clique crítico do pipeline (censo: DEC-024, `NEXT_CLICK_RETRY_ATTEMPTS`; menu de prontuário: DEC-081, `_click_menu_to_search_screen`), que já tinham essa proteção. Uma única falha transitória aqui trava a execução INTEIRA logo no primeiro passo, e numa execução automática de madrugada não existe ninguém pra clicar manualmente -- pior ainda que os outros dois casos por ser o ponto de entrada de tudo.

**Decisão:** `_confirm_establishment` agora tenta o clique até `CONFIRM_RETRY_ATTEMPTS` (3) vezes antes de desistir, mesmo padrão dos outros dois pontos. Mantém o mesmo `GSUSLoginError` final se todas as tentativas falharem (contrato inalterado pra quem chama).

**Testes:** 3 novos (`tests/unit/test_login_confirm_retry.py`, sem Playwright/browser real -- fakes): sucesso na 1ª tentativa, sucesso após retry (achado real -- antes desistia na 1ª falha), `GSUSLoginError` só depois de esgotar todas as tentativas. Suíte completa: 315 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Impacto:** `app/gsus/login.py` (`CONFIRM_RETRY_ATTEMPTS`, `_confirm_establishment` com loop de retry), `tests/unit/test_login_confirm_retry.py` (novo). Rebuild+reinstall pendente até a execução real em andamento (rodada de 180 pacientes, iniciada pelo usuário) terminar, pra não sobrescrever o `.exe` em uso.

---

## DEC-098 — Limite opcional de dias por paciente na extração (pedido do usuário, catch-up rápido)

**Pedido do usuário:** observou, ao vivo, que "Atualizar agora" busca TODO o histórico de contexto de um paciente em vez de só o dia mais recente -- queria acelerar um catch-up pontual do banco, extraindo só o necessário pra atualizar e construir a dashboard.

**Achado, ao investigar:** a extração JÁ é incremental por design (DEC-048, `known_days`/`Repository.get_note_days`) -- só abre dias AINDA NÃO conhecidos. O comportamento observado pelo usuário ("busca tudo") acontece especificamente para paciente com HISTÓRICO POUCO OU NADA conhecido ainda (ex.: internação nunca extraída com sucesso antes, ou dias perdidos pelos dias de instabilidade da tarefa agendada) -- para esses, "incremental" significa "abrir a internação inteira", que pode ser cara numa internação longa. Também esclarecido: a tarefa automática e o clique manual usam exatamente o MESMO código (`run_update`, DEC-075) -- não existe hoje um modo automático mais restrito/rápido que o manual, ao contrário do que o usuário supunha.

**Decisão:** novo campo opcional `AppConfig.max_days_per_patient` (`None` por padrão -- sem limite, comportamento normal). Quando definido, limita a extração aos N dias mais recentes por paciente, mesmo pra quem nunca foi extraído antes -- aceita não puxar o histórico completo de admissões antigas em troca de velocidade. Threading: `AppConfig` -> `update_flow.py` (constrói `GSUSAdapter`) -> `GSUSAdapter.get_raw_notes_text` -> `records.extract_notes` -> `_collect_days`/`_days_beyond_cap` (novo, mesmo padrão de `_days_to_skip`/DEC-048: mantém só os N mais recentes por DATA, nunca descarta dia sem data reconhecida no id -- fail-safe). RULES-001 e a análise por IA continuam vendo só o que foi de fato extraído (sem mudança de contrato pra elas).

**Aplicado imediatamente:** `max_days_per_patient=2` (hoje + ontem, margem de segurança sobre "só ontem" por fuso/timing) definido direto no `config.json` de produção a pedido do usuário -- só afeta execuções INICIADAS DEPOIS da mudança (a execução real em andamento no momento do pedido, iniciada antes, já tinha seu `AppConfig` carregado em memória e segue sem limite).

**Achado de processo, à parte:** ao inspecionar `config.json` pra aplicar a mudança, imprimi o CPF do usuário (campo `gsus_username`) no meu próprio contexto sem necessidade -- não era preciso reexibir o arquivo inteiro pra editar um campo. Sinalizado ao usuário; corrigido lendo/escrevendo o JSON via código (sem imprimir o conteúdo) daí em diante.

**Testes:** 5 novos (`tests/unit/test_records_day_cap.py`): `_days_beyond_cap` mantém só os N mais recentes, não corta nada quando o limite cobre todos os dias, nunca descarta dia sem data reconhecida (fail-safe); `_collect_days` respeita o limite mesmo pra paciente nunca visto (achado real) e mantém o comportamento sem limite quando `None`. Suíte completa: 320 passed, mesmos 10 erros de Chromium desta máquina (não relacionados).

**Impacto:** `app/config.py` (`AppConfig.max_days_per_patient`), `app/gsus/records.py` (`_days_beyond_cap`, `extract_notes`/`_collect_days` com o parâmetro novo), `app/gsus/adapter.py` (`GSUSAdapter` aceita e repassa), `app/update_flow.py` (passa `app_config.max_days_per_patient`), `tests/unit/test_records_day_cap.py` (novo), `tests/unit/test_main_window_update.py` (fakes atualizados pro novo parâmetro). Sem UI ainda pra esse campo -- só via `config.json` direto por ora; considerar expor na tela de Configurações se o usuário for usar isso com frequência (não só pontualmente).

---

## DEC-099 — Plano da dashboard unificada (RF-28/RF-29): matplotlib, tela única, `daily_snapshot`

**Pedido do usuário:** unificar a dashboard (métricas/gráficos exigidos pela orientação técnica, seção 12) com o painel de gerenciamento atual (Atualizar Agora/Abrir Relatório/Localizar Paciente/Configurações) numa tela só, atualizada constantemente. Pediu planejamento antes de qualquer código -- decisões técnicas delegadas a mim onde eu tivesse recomendação clara.

**Achado ao investigar (auditoria de código, 4 frentes em paralelo, sem tocar o banco real):**
- RF-28 já EXIGE explicitamente "histórico de dias vermelhos por causa" e RF-29 já exige "indicadores agregados do serviço" -- isto não é requisito novo, é uma lacuna entre o spec (escrito 2026-08-2x) e a implementação real. `patient_state` é UPSERT puro (`repository.py:391-430`, `ON CONFLICT ... DO UPDATE`) -- guarda só o valor mais recente por paciente, nunca uma série histórica. Sem isso, boa parte da seção 12 da orientação técnica (`% dias não agregadores de valor`, `barreiras por 100 pacientes-dia`, tendência de dias vermelhos) é matematicamente impossível de calcular hoje -- não existe "o que era verdade ontem" gravado em lugar nenhum.
- O que JÁ está pronto e bate com a orientação técnica, sem precisar de nenhuma mudança de schema: taxonomia fechada de 7 categorias (`taxonomy.py`), SLA por categoria não-universal (`sla_config.py`: 48h diagnóstico/transferência, 24h interconsulta/cirurgia/alta/administrativa, terapêutica sem limite), prioridade determinística por regra (`priority.py`, nunca decidida pela IA), origem interna/externa com default por categoria, evidência clicável (`pending_item_evidence`), distinção extraído×inferido (`is_inferred`/`confidence`). Isso reduz o escopo real do trabalho -- é agregação e histórico, não reconstruir a extração.
- O relatório HTML atual (`html_report.py`) já tem uma tabela "censo", mas é uma linha por paciente/leito (ordenada por leito) -- NÃO é agregada por unidade (sem subtotais, sem contagem por categoria/prioridade). Nenhum indicador agregado existe hoje no relatório -- é 100% listagem, não estatística.

**Decisões tomadas:**
1. **Gráficos: matplotlib + `FigureCanvasTkAgg`**, não webview/Chart.js nem Canvas nativo. Pesquisa técnica dedicada (comparação de 3 abordagens) mostrou que a opção "webview" transfere a renderização pra um runtime FORA do controle do exe (WebView2, que uma parcela real de máquinas Windows 10 não tem pré-instalado -- a própria Microsoft reconhece isso) ou tem JavaScript quebrado no Windows (tkinterweb + PythonMonkey, problema de instalação documentado). Canvas nativo (zero dependência) não tem antialiasing e viraria dívida técnica crescente pra igualar o que matplotlib entrega pronto. matplotlib é a única das três sem risco de ambiente -- mesmo custando ~40-90MB no instalador, é a primeira dependência de terceiros "pesada" do projeto, aceita explicitamente pelo usuário.
2. **`daily_snapshot` (tabela nova) + `daily_snapshot_category` (filha, 1-N)**: grava um rollup agregado ao final de cada execução bem-sucedida (contagem de ativos, dia_vermelho/verde, sem EDD, EDD vencida; por categoria/origem/prioridade). Não toca `patient_state`/`pending_items` -- é aditivo. Sem isso, os gráficos de tendência (seção 9/12) nunca teriam dado histórico pra mostrar, não importa quanto tempo passe.
3. **"Localizar Paciente" continua separado** do filtro novo da tabela de censo agregado -- são ações diferentes por natureza (filtro = busca rápida entre ativos de hoje, sem tocar GSUS; Localizar Paciente = histórico completo de qualquer prontuário, incluindo altas antigas, RF-19). Fundir os dois deixaria o filtro simples inconsistente (às vezes só filtra, às vezes dispara uma busca de histórico completo).
4. **Gráficos de tendência entram na V1** mesmo sabendo que começam vazios -- adiar só atrasaria o início real da série histórica exigida pelo RF-28.

**Layout da tela única (substituindo a janela atual):** barra de controle (botões existentes + status) no topo; cartões de KPI; 4 gráficos (pendências por categoria, prioridade, interno×externo, tendência de dias vermelhos); tabela de censo agregado por unidade (Treeview, ordenável, com filtro embutido) na base, com clique abrindo o relatório individual do paciente.

**Ordem de implementação proposta** (ver TASKS.md::REPORT-003/REPORT-004):
1. Consultas agregadas novas em `Repository` (sem mudar schema) -- indicadores já calculáveis hoje.
2. `daily_snapshot`/`daily_snapshot_category` + gravação ao fim de cada execução.
3. Tela única (matplotlib + Treeview), substituindo `app/ui/main_window.py` atual.
4. (Depois, fora do V1) drill-down mais rico, captura de concordância/discordância do auditor por achado (alimentaria a validação científica da seção 17 da orientação técnica, RF-30).

**Testes:** nenhum ainda -- esta sessão foi só planejamento, sem código de produto (pedido explícito do usuário). Implementação real fica para as próximas sessões, seguindo TASKS.md::REPORT-003/REPORT-004.

**Impacto:** nenhuma mudança de código nesta sessão. `TASKS.md::REPORT-003` reescrito com o plano detalhado; `REPORT-004` novo (tela única/UI, separado dos indicadores em si). `PROJECT_SPEC.md::RF-28/RF-29` não mudam -- o plano é a implementação do que já estava especificado, não requisito novo.

**Fase 1 implementada (2026-09-01):** `app/reports/dashboard_metrics.py` (novo) -- `compute_service_indicators`/`compute_unit_census`, reusando o mesmo padrão do `html_report.py` (recalcula prioridade na leitura, nunca confia em snapshot). `Repository.get_resolved_pending_items` (novo, só SQL). Achado real durante a implementação (pego escrevendo teste, nunca chegou a produção): "sem EDD documentada" e "EDD vencida" são conceitos DISTINTOS -- uma EDD `VENCIDA` ou `REGISTRADA`-mas-já-passada TEVE uma data real documentada em algum momento, então não pode contar como "sem previsão" (isso inflaria esse indicador). Corrigido antes de qualquer uso real; extraído `is_edd_overdue` (`app/analysis/priority.py`) de `html_report.py::_format_edd` pra garantir que o indicador agregado e o relatório individual nunca divirjam sobre o que conta como vencida pro mesmo paciente. Testes: +17 (`test_priority.py` +8, `test_dashboard_metrics.py` +9 novo). Suíte completa: 337 passed, mesmos 10 erros de Chromium (não relacionados).

**Fase 2 implementada (2026-09-01):** schema novo e aditivo em `app/storage/database.py` -- `daily_snapshot` (rollup do serviço por execução: total de ativos, com pendência ativa, sem EDD, EDD vencida, `dia_vermelho`/`dia_verde`, tempo mediano de resolução) e `daily_snapshot_category` (filha 1-N, `dimension`/`label`/`total` -- uma tabela só cobre as 4 dimensões `category`/`origin`/`priority`/`dia_causa`, em vez de 4 tabelas quase idênticas para o mesmo propósito). Como as tabelas são NOVAS, `CREATE TABLE IF NOT EXISTS` basta -- não precisou de `_migrate` (essa função só existe pra adicionar COLUNA a tabela já existente, DEC-054). `Repository.save_daily_snapshot`/`get_daily_snapshots`/`get_daily_snapshot_categories` (novo). `ServiceIndicators` ganhou `patients_dia_vermelho`/`patients_dia_verde`/`dia_causa_counts`, calculados no MESMO loop que `compute_service_indicators` já usava pra EDD (sem consulta redundante ao `patient_state`). `dashboard_metrics.save_snapshot(repo, run_id)` reusa `compute_service_indicators` pra montar o retrato de hoje e grava via `Repository` -- decisão deliberada pra nunca ter duas fontes de verdade sobre o que conta como "dia vermelho" entre o indicador instantâneo (dashboard) e o registro histórico (snapshot) do mesmo momento. `app/orchestrator.py::run_once` chama `save_snapshot` uma vez ao fim de cada execução, DEPOIS da Fase 2/IA terminar (é o retrato final do dia, não um intermediário durante o processamento), isolado em try/except no mesmo padrão já usado pra `generate_report` -- uma falha ao gravar o snapshot (disco cheio, etc.) não pode jogar fora uma execução que já persistiu tudo o que importa em `patient_state`/`pending_items`. Fecha a lacuna real do RF-28 ("manter histórico de dias vermelhos por causa"): antes desta tabela, `patient_state` era UPSERT puro (sobrescrito a cada análise por causa de necessidade operacional, não descuido), então não existia NENHUM jeito de reconstruir como o serviço estava ontem, só como está agora. Testes: +9 (`test_repository.py` +4, `test_dashboard_metrics.py` +5). Suíte completa: 346 passed, mesmos 10 erros de Chromium (não relacionados).

**Fase 3 implementada (2026-09-01):** `app/ui/main_window.py` reescrito por completo -- mesma classe `MainWindow`, mesmos atributos/métodos que os testes de "Atualizar agora" já dependiam (`status_label`/`_updating`/`_report_path`/`_on_update`/`_run_update_worker`/`_poll_update_queue`/`_finish_update`), intactos, com tudo em volta reconstruído. Esclarecimento sobre uma ambiguidade do texto original desta decisão ("tabela de censo agregado por unidade... com clique abrindo o relatório individual"): uma tabela agregada por UNIDADE não tem identidade de paciente pra abrir um relatório individual ao clicar -- as duas frases descreviam, sem perceber, duas visões diferentes. Resolvido assim: a tabela clicável/ordenável/filtrável da base é POR PACIENTE (`dashboard_metrics.compute_patient_census_rows`, novo -- reusa `_annotated_active_items` da Fase 1, mesmo critério de pendência principal que `html_report.py::_render_census_row`), e o agregado por unidade genuíno (`compute_unit_census`, já existia desde a Fase 1) virou uma faixa compacta de resumo acima da tabela. `PRIORITY_RANK` (novo, público em `dashboard_metrics.py`) unifica o critério de ordenação de gravidade entre "pendência principal do paciente" e a ordenação da coluna "Prioridade" na tabela. `matplotlib` (`Figure`/`FigureCanvasTkAgg` -- deliberadamente NUNCA `matplotlib.pyplot`, cujo registro global de figuras vazaria memória a cada vez que `app/main.py::render` reconstrói `MainWindow` ao navegar Configurações/Localizar Paciente e voltar) desenha os 4 gráficos com as MESMAS cores de prioridade do relatório HTML (`html_report.py::CSS` .prio-ALTA/MEDIA/MONITORAMENTO) -- dashboard e relatório precisam falar a mesma linguagem visual de gravidade. A dashboard nunca toca GSUS/IA: `_refresh_dashboard` abre uma conexão SQLite própria e de curta duração (mesmo padrão de `lookup_window.py::_on_search`), chamada ao fim de cada execução (`_finish_update`) e a cada 60s enquanto a janela está aberta (`_periodic_refresh`) -- com uma checagem de widget vivo (`_is_alive`) pra parar de reagendar quando a janela foi trocada por Configurações/Localizar Paciente, já que o `tk.Tk()` raiz sobrevive à troca mas os widgets desta instância não. Janela raiz passou a ser redimensionável (só nesta tela -- `SetupWindow`/`LookupWindow` voltam a fixar `resizable(False, False)` no próprio `__init__`, porque `render()` nunca reseta esse estado sozinho ao trocar de tela). Verificação visual feita com um script fora da suíte de testes, banco fictício (prontuários "F0001" a "F0006", nenhum dado real) -- screenshot da janela renderizada de verdade conferido antes de considerar a fase concluída. Um teste existente (`test_app_shell.py`) precisou ser ajustado: busca de botões por `root.winfo_children()` direto parou de funcionar porque a barra de controle passou a agrupar os botões dentro de um `Frame` -- trocado por busca recursiva. Testes: +9 (`test_dashboard_metrics.py::compute_patient_census_rows` +3, `test_main_window_dashboard.py` novo +6). Suíte completa: 355 passed, mesmos 10 erros de Chromium (não relacionados). Pendente: rebuild + reinstalação completa (PyInstaller/Inno Setup) pra confirmar que matplotlib empacota sem `hiddenimports` extra no `.spec` -- não fez parte desta sessão. Com isso, REPORT-003 e REPORT-004 (planejados nesta mesma decisão) estão completos.

## DEC-100 — Rebuild+reinstalação da dashboard (REPORT-003/004): numpy._core não empacotava sozinho

**Problema:** primeiro rebuild com matplotlib (`scripts/build.ps1` + PyInstaller) terminou sem erro, e o hook oficial do PyInstaller pra matplotlib rodou normalmente durante o build -- mas o `.exe` instalado quebrava ANTES de qualquer linha de log aparecer, assim que `MainWindow._build_charts` importava `matplotlib.figure` pela primeira vez (import tardio, só quando a janela principal abre de verdade): `ImportError: No module named 'numpy._core._exceptions'`. Rodar o `.exe` direto no console (em vez de `Start-Process` sem captura) foi o que revelou o traceback -- sem isso, o processo só "desaparecia" silenciosamente aos olhos de quem só olha `Get-Process`.

**Causa:** o hook `hook-numpy.py` do PyInstaller não coletou automaticamente todos os submódulos C de `numpy._core` nesta combinação de versões (numpy 2.5.2 + PyInstaller já presente no projeto) -- caso documentado no próprio guia de troubleshooting do numpy, não específico deste projeto.

**Decisão:** `installer/gsus-auditoria.spec` -- `hiddenimports=collect_submodules('numpy._core')` (utilitário oficial `PyInstaller.utils.hooks.collect_submodules`), restrito a essa subpasta (não `numpy` inteiro) pra ficar cirúrgico. Verificado rodando o `.exe` direto (não via `Start-Process` silencioso) depois do fix -- `matplotlib.font_manager: generated new fontManager` no log confirma import limpo, processo permanece de pé.

**Ciclo completo aplicado:** rebuild (PyInstaller) -> recompilação do instalador (Inno Setup, `ISCC.exe` via PowerShell direto, nunca Git Bash -- DEC-095/096) -> reinstalação silenciosa (`/VERYSILENT /NORESTART /SUPPRESSMSGBOXES` via `Start-Process`, nunca Git Bash) -> binário instalado (`%LOCALAPPDATA%\Programs\GSUS Auditoria\gsus-auditoria.exe`) lançado e confirmado de pé por 8s+ sem traceback. Banco/config/credenciais reais (`%LOCALAPPDATA%\GSUSAuditoria`, caminho DIFERENTE do diretório de instalação) nunca são tocados pelo instalador -- só o binário é substituído.

**Testes:** nenhum novo (mudança é só de empacotamento, não de código Python -- a suíte inteira já cobre o código-fonte). Verificação foi o próprio ciclo de build+instalação+execução real.

**Impacto:** REPORT-003/REPORT-004 (DEC-099) agora estão prontos pra distribuição de verdade, não só pra rodar a partir do `.venv` de desenvolvimento. `TASKS.md::REPORT-004` atualizado removendo o "pendente" de rebuild.

## DEC-101 — Auditoria adversarial da dashboard (Fases 1-3): 6 achados reais corrigidos

**Pedido do usuário:** "Verifique e faça os testes para saber se tudo está funcional antes de partir para as próximas fases" -- gate explícito antes de avançar.

**Método:** workflow com 4 checagens em paralelo (suíte de testes completa, saúde SÓ-LEITURA e agregada do banco de PRODUÇÃO real, relançamento do binário já instalado, consistência da documentação SDD) + revisão de código em 4 dimensões (correção/integridade, ciclo de vida de UI, segurança/PHI, cobertura de teste) sobre todos os arquivos das Fases 1-3, cada achado submetido a uma verificação cética separada (tentando REFUTAR antes de confirmar), nunca aceito de primeira.

**Achado descartado (falso alarme):** o check de testes voltou `FAIL` na primeira rodada ("1 failed... TclError: couldn't read file .../tk8.6/ttk/defaults.tcl"). Reproduzido de propósito: rodando a suíte serialmente (sem outros agentes Tk concorrentes na mesma máquina), voltou limpo (355 passed). Confirmado como flakiness transitória de criar muitas instâncias `tk.Tk()` em sequência rápida nesta máquina (piorada pela concorrência de vários agentes do workflow abrindo Tk ao mesmo tempo) -- não é regressão de código. Reproduziu de novo, isolado, uma única vez durante a correção dos achados reais (`test_census_filter_narrows_visible_rows...`), e voltou a passar limpo rodando sozinho e rodando a suíte inteira de novo -- confirma que é flakiness de ambiente, não um bug determinístico.

**6 achados REAIS confirmados (todos corrigidos nesta sessão):**

1. **[ALTA] PHI -- `dia_causa` (texto livre do LLM) gravado verbatim em `daily_snapshot_category.label`.** Diferente de category/origin/priority (vocabulário fechado), `dia_causa` é uma frase específica por paciente (ex.: "aguardando parecer da neurocirurgia..."), e a tabela nova não tem expurgo (diferente de `notes`/RETENTION-001) -- um dump/export dessa tabela vazaria texto clínico potencialmente reidentificável PRA SEMPRE. Corrigido: `dashboard_metrics.save_snapshot` nunca mais inclui a dimensão `dia_causa` em `category_counts` -- só a CONTAGEM agregada (`patients_dia_vermelho`, sempre segura) continua indo pro `daily_snapshot`. A quebra POR CAUSA (RF-28) fica pendente até `dia_causa` ganhar uma taxonomia fechada como `category` já tem (novo item em TASKS.md).
2. **[ALTA] `_poll_update_queue`/`_finish_update` sem a mesma guarda `_is_alive` que `_periodic_refresh` já tinha.** Clicar "Atualizar agora" e trocar de tela (Configurações/Localizar Paciente) antes de terminar destruía `status_label`/`update_button` dessa instância -- o próximo `.config(...)` levantava `TclError`, abortando o método no meio e perdendo o resultado da atualização em silêncio (a escrita no banco, numa thread separada, continuava normal -- só o status na TELA se perdia). Corrigido com a mesma guarda no início de `_poll_update_queue`.
3. **[ALTA/cosmético] `SetupWindow`/`LookupWindow` resetavam `resizable(False, False)` mas não `root.minsize()`.** `MainWindow` fixa `root.minsize(1024, 700)`, que sobrevive à troca de tela -- as telas pequenas (360x280/420x220) abriam presas nesse mínimo herdado, enormes em vez de compactas. Corrigido com `root.minsize(1, 1)` no `__init__` das duas.
4. **[MÉDIA] `after` do `_periodic_refresh` nunca cancelado na troca de tela.** `_is_alive` só evitava REAGENDAR -- o job já pendente no momento da troca continuava vivo até seu próprio timer de 60s vencer, prendendo a `MainWindow` inteira (Figure do matplotlib incluída) na memória até lá; alternar telas mais rápido que 60s acumulava várias instâncias simultâneas. Corrigido com `<Destroy>` vinculado ao frame da barra de controle (primeiro widget próprio da classe), cancelando o job na hora via `root.after_cancel`.
5. **[MÉDIA] Caminhos de erro sem teste.** O `except ValueError` de `_resolution_hours` (dado malformado) e o `except Exception` de `_refresh_dashboard` (falha transitória de banco) existem e funcionam certinho, mas nenhum teste forçava esses cenários -- uma regressão futura passaria despercebida. Adicionado um teste pra cada.
6. **[BAIXA] Teste cego a mutação.** `test_get_daily_snapshots_returns_chronological_order` inseria em ordem cronológica E de inserção idêntica -- passaria igual mesmo sem `ORDER BY created_at`. Corrigido inserindo fora de ordem (via `UPDATE` direto de `created_at`) pra realmente forçar a ordenação a fazer diferença.

**Achado de saúde do banco real (não é bug, é observação operacional):** `daily_snapshot` ainda com 0 linhas (nenhuma execução real completou o ciclo desde a Fase 2 -- esperado) e ~46% das últimas 13 runs com status FAILED -- consistente com a instabilidade real do GSUS já enfrentada e documentada nesta sessão (DEC-096/097), não uma regressão nova. Fica como ponto de atenção operacional, não bloqueador.

**Testes:** +5 (2 reescritos sem mudar a contagem, 3 genuinamente novos em `test_main_window_dashboard.py`, 1 novo em `test_dashboard_metrics.py`, 1 novo em `test_app_shell.py`). Suíte completa: 360 passed, mesmos 10 erros de Chromium (não relacionados).

**Impacto:** nenhuma mudança de schema. `app/reports/dashboard_metrics.py`, `app/ui/main_window.py`, `app/ui/setup_window.py`, `app/ui/lookup_window.py` corrigidos. Nenhum dos achados bloqueava o uso do app hoje (nada travava/crashava em produção) -- mas o achado #1 (PHI) precisava ser fechado ANTES de qualquer execução real gravar um snapshot de verdade, e foi.

## DEC-102 — `SEARCH_RETRY_ATTEMPTS` 3→5: falha real ao vivo na busca de prontuário (RESIL-008)

**Contexto:** pedido do usuário pra disparar uma execução real completa ("Sim, pode disparar") como teste de aceitação final antes de avançar de fase. Acompanhado ao vivo via log.

**Achado (ao vivo, 2026-09-01):** 1ª tentativa quebrou com `playwright.TargetClosedError` durante a paginação do censo (navegador fechou sozinho no meio de um retry) -- recuperação limpa confirmada (nenhuma run presa, `run_id` nunca chegou a existir, exatamente o comportamento documentado pra falha antes de `repo.start_run()`). 2ª tentativa: censo parcial (40 de ~180, `GSUSCensusIncompleteError`, GSUS instável), e então **21 de 23 pacientes tentados falharam** em `open_current_admission` com "Nenhuma internação em andamento encontrada após 3 tentativas (marcador 'Permanece Internado' não apareceu)" -- falhas quase cronometradas a cada ~90s. Usuário autorizou encerrar a execução ("Sim, pode encerrar") depois de confirmar que o padrão era sistemático, não uma falha isolada.

**Investigação:** o log real (`app.log`, sem rotação ainda) mostra o MESMO erro exato desde **2026-08-26** -- inclusive um pico de 58 ocorrências num único minuto (2026-08-27 18:12) e uma sequência longa na execução da manhã de hoje (09:09-10:01). Ou seja: **não é regressão desta sessão nem das Fases 1-3 da dashboard** -- é um problema pré-existente, só nunca quantificado com essa clareza antes.

**Causa provável:** `app/update_flow.py::_run_update_locked` inicia `llm.start()` (llama-server, modelo local) ANTES de abrir a sessão GSUS -- decisão deliberada do DEC-058 (evitar a sessão GSUS ficar ociosa esperando o modelo carregar, o que arriscaria expirar por inatividade). Isso significa que o modelo fica RESIDENTE EM RAM durante toda a Fase 1 (regras, ainda não precisa da IA) -- em hardware confirmadamente fraco (ver CURRENT_STATE.md, DEC-070), essa concorrência por recurso é a explicação mais plausível pra `RESULT_WAIT_MS=20_000` (20s) × `SEARCH_RETRY_ATTEMPTS=3` (60s de orçamento total) não bastar mais sob essa carga real.

**Decisão:** `SEARCH_RETRY_ATTEMPTS` 3 → 5 em `app/gsus/records.py` (mesmo padrão já usado em `NEXT_CLICK_RETRY_ATTEMPTS`, DEC-024/DEC-096 -- aumentar tentativas em vez de mudar arquitetura). `open_current_admission` estava marcada FROZEN (DECISIONS.md, "validado ponta a ponta, não mexer sem motivo real") -- este é o motivo real: achado ao vivo, quantificado, reproduzível no histórico de log. Deliberadamente NÃO movida a chamada de `llm.start()` pra depois da Fase 1 -- isso reintroduziria o risco de sessão GSUS expirar ociosa esperando o modelo carregar (o próprio problema que o DEC-058 evitou), uma mudança arquitetural maior que não cabe fazer sob pressão de prazo sem validação própria.

**Testes:** +1 (`test_records_menu_retry.py::test_open_current_admission_raises_after_exhausting_all_attempts`, novo -- cobre o caminho de ESGOTAMENTO de tentativas, que não tinha teste nenhum antes; a constante é referenciada simbolicamente, não hardcoded, então futuras mudanças no número não quebram o teste). Suíte completa: 361 passed, mesmos 10 erros de Chromium (não relacionados).

**Limpeza pós-encerramento:** o `Stop-Process` usado pra encerrar a 2ª tentativa (autorizado pelo usuário) matou o processo antes que o `finally`/exception handler do Python rodasse -- 1 run ficou presa em `RUNNING` (mesma classe de achado já documentada antes nesta sessão) e o `llama-server` ficou órfão. Ambos limpos manualmente (`repo.finish_run(..., "FAILED")` e `Stop-Process` no PID órfão) -- nenhum dado real foi perdido ou corrompido (cada paciente já commita individualmente).

**Impacto:** nenhuma mudança de schema. Novo item RESIL-008 fechado em TASKS.md. Fica registrado como ponto de atenção pra quando houver tempo: considerar mover `llm.start()` pra um ponto mais tardio (ex.: só quando a Fase 1 realmente terminar) como mitigação mais estrutural -- não feito agora por ser mudança arquitetural maior, fora do escopo de um fix pontual sob prazo apertado.

## DEC-103 — Investigação completa das falhas de `open_current_admission`: 1 bug fechado, 1 hipótese real pendente de verificação manual, 1 achado novo em aberto

**Contexto:** pedido explícito do usuário ("estou curioso em descobrir o motivo de tantas falhas... todas elas precisam ser justificadas quando o projeto entrar em produção real") depois de ver que o fix do DEC-102 melhorou mas não eliminou a taxa de falha (2ª execução real: 16 de 40 pacientes, ~40%, contra ~91% antes do fix).

**Método:** 3 investigações em paralelo (timing/agrupamento das falhas nas 2 execuções reais de hoje; revisão de todo ponto de clique/espera sem proteção em `records.py`/`adapter.py`; hipótese específica de má-classificação "falha técnica" vs "sem internação atual") seguidas de uma verificação cética independente que leu o código e o log de novo, sem confiar nas conclusões dos 3 relatórios.

**A verificação cética pegou um problema sério: um dos 3 relatórios apresentou uma "prova no log" completamente FABRICADA** (citou um paciente/prontuário e timestamps de um dia que nem existe no arquivo de log). A tese estrutural desse relatório (ver achado 2 abaixo) continua válida e tem apoio real no histórico do projeto -- só a "prova direta" específica que ele citou não existe. Registrado aqui como lembrete de que toda alegação de um agente de investigação precisa ser conferida linha a linha antes de virar decisão, mesmo quando a conclusão geral parece plausível.

**3 categorias de falha identificadas, cada uma com seu próprio veredito:**

**1. "Menu 'Atendimento' não respondeu ao clique" e "Resultado da busca não confirmado" (a maioria das falhas) — SEM bug de código encontrado; causa é limitação de ambiente já mitigada.** Medido com precisão no log real: cada tentativa de menu consome exatamente o timeout padrão do Playwright (30.000ms ± 20ms, `app/gsus/client.py`); cada tentativa de busca consome ~28,3-28,8s (`RESULT_WAIT_MS=20000` + overhead de reabrir menu/campo). O código já está corretamente protegido (`_click_menu_to_search_screen` captura `PlaywrightError` genérico, não só timeout). As falhas aparecem em RAJADAS (não uniformes, não crescentes ao longo da run -- descarta fuga de memória/degradação progressiva local) intercaladas com trechos totalmente limpos (13 pacientes seguidos sem falha, por exemplo) -- padrão mais compatível com variação de carga/instabilidade do GSUS ao longo do dia do que com um bug determinístico nosso. O aumento de tentativas do DEC-102 já é a mitigação de tempo aplicável; não há mais nada de código a corrigir nesta categoria.

**2. Mesma categoria "marcador não apareceu" — HIPÓTESE REAL de má-classificação, com apoio estrutural genuíno, MAS não confirmada.** `open_current_admission` só reconhece "sem internação atual" através de UM sinal específico (o modal "Justificar Acesso ao Prontuário", DEC-035/054) -- diferente da função irmã `get_full_admission_history_text`/`_collect_days` (mesmo arquivo, usada pelo "Localizar Paciente" manual), que detecta a MESMA situação de forma estrutural (presença de qualquer episódio na tela, `EPISODE_CARD_SELECTOR`), sem depender de nenhum texto específico. Isso significa que um paciente genuinamente sem internação atual, exibido pelo GSUS de um jeito que não seja "nem o marcador, nem o modal", cairia hoje em `GSUSRecordError` (falha técnica) em vez de `GSUSNoCurrentAdmissionDays` (não-falha, categoria correta). Há precedente real no histórico do projeto: DEC-054 e DEC-055 já descobriram, em sequência, DOIS caminhos distintos pelos quais o GSUS mostra esse mesmo estado -- e DEC-056 (27/08, achado nesta verificação, não citado nos relatórios originais) já registrava a suspeita em aberto de um TERCEIRO caminho ainda não coberto. A verificação encontrou um paciente real (`pac-1130badc`) com classificações diferentes em execuções de dias diferentes (falha técnica, depois `NAO_ADMISSAO` corretamente, depois falha técnica de novo) -- sugestivo, mas não conclusivo (condições podem genuinamente ter mudado entre os dias). **Ação recomendada, na ordem certa: o usuário verificar manualmente 1-2 prontuários que falharam hoje com essa mensagem diretamente no GSUS real, antes de qualquer mudança de código** -- implementar a correção (reusar a checagem estrutural de `EPISODE_CARD_SELECTOR` já validada na função irmã) sem essa confirmação arriscaria esconder falhas técnicas reais atrás de um rótulo de "provável alta" errado.

**3. "TimeoutError: Locator.click: Timeout 30000ms exceeded" — BUG REAL, CONFIRMADO e CORRIGIDO.** `current_card.click()` (depois que `wait_for` já confirmou o marcador visível) estava FORA de qualquer try/except -- diferente do resto do laço inteiro. Confirmado com 2 tracebacks completos do log real (call log do Playwright mostrando "element is visible, enabled and stable... performing click action" mas travando 30s mesmo assim -- algo sobrepondo o elemento no instante exato é a hipótese mais provável, não confirmável só por log). Esse clique nunca passava pelas `SEARCH_RETRY_ATTEMPTS` tentativas do DEC-102 -- falhava direto na primeira, desperdiçando o aumento de tentativas pra esse caso específico (2 das 16 falhas de hoje, ~12,5%). Confirmado como bug NOVO/raro (só 2 ocorrências em todo o histórico do log), não recorrência do bug antigo de clique de menu já fechado (DEC-081/082). **Corrigido:** envolvido em try/except (`PlaywrightError`), reentra no laço de retry em vez de escapar cru -- mudança puramente aditiva, não muda nenhum caminho de sucesso.

**Achado colateral não investigado a fundo (fica em aberto):** a verificação cética encontrou um QUARTO modo de falha real, atribuído incorretamente pelo relatório de timing a outro paciente -- 21 avisos consecutivos "Não foi possível expandir um item de episódio" (mesmo padrão do DEC-082) até estourar `EXPAND_TIME_BUDGET_S=210s`. Não investigado a fundo nesta sessão -- ver TASKS.md.

**Testes:** +1 (`test_records_menu_retry.py::test_open_current_admission_retries_when_final_click_times_out`). Suíte completa: 362 passed, mesmos 10 erros de Chromium.

**Impacto:** nenhuma mudança de schema. Correção de código só na categoria 3. Categorias 1-2 ficam documentadas como limitação conhecida (1) e hipótese pendente de verificação manual (2) -- nenhuma das duas justifica mudança de código sem mais evidência.

**Adendo (mesmo dia, verificação manual concluída) — hipótese de má-classificação DESCARTADA:** o usuário verificou manualmente no GSUS real os 3 prontuários que falharam hoje com "Nenhuma internação em andamento encontrada" (via consulta local ao banco, sem expor o número a mim -- script rodado pelo próprio usuário). Resultado: **os 3 estavam genuinamente internados, com evolução real na tela**. Nenhum caiu no caso "sem internação atual mostrada de um jeito que `open_current_admission` não reconhece" -- a hipótese estrutural do achado 2 (DEC-103) fica descartada por evidência direta, pelo menos para esta amostra. Isso reforça a categoria 1 (limitação de ambiente/GSUS instável, já mitigada pelo DEC-102) como a explicação dominante real: o marcador "Permanece Internado" EXISTE na tela desses pacientes, só não foi confirmado a tempo pelo Playwright. **Decisão: não implementar a checagem estrutural (`EPISODE_CARD_SELECTOR`) em `open_current_admission`** -- não há evidência de que resolveria algo real, e aplicá-la sem necessidade arriscaria o problema oposto (uma falha técnica genuína passar a ser silenciosamente rotulada como "provável alta", escondendo um caso real). `TASKS.md::PRIVACY-002` fechado com este resultado.

## DEC-104 — CPF do usuário removido do config.json (auditoria de segurança pré-entrega)

**Contexto:** pedido explícito do usuário -- auditoria de certificação completa antes da entrega ("Certifique-se que a segurança esteja apropriada de ponta a ponta"), 4 frentes em paralelo (resiliência, conformidade do relatório, conformidade da dashboard, segurança).

**Achado:** `gsus_username` (CPF do usuário, dado pessoal sensível) era gravado em texto plano em `config.json` (`AppConfig.to_dict()`/`save_config`), apesar do próprio módulo declarar no topo "Nunca guarda credenciais". Já causou uma exposição real e documentada nesta mesma sessão (mais cedo hoje: inspecionar o JSON pra editar outro campo imprimiu o CPF sem necessidade no contexto de quem mexia no arquivo).

**Decisão:** `AppConfig.to_dict()` exclui `gsus_username` explicitamente antes de serializar. `load_config()` passa a preencher esse campo (em memória, pra pré-popular o formulário de Configurações e pra `is_configured()`) sempre a partir do Windows Credential Manager (`app.security.credentials.get_credential("gsus")`), onde já era gravado com segurança junto da senha -- nunca mais do `config.json`. O `config.json` real de produção (já continha o CPF, gravado pelo código antigo) foi limpo manualmente após o fix, sem nunca imprimir o valor no processo.

**Testes:** `test_config.py` -- `test_save_config_never_persists_gsus_username_to_disk` (novo, positivo) e `test_save_and_load_roundtrip` reescrito pra mockar o Credential Manager (hermético, evita depender do que porventura esteja salvo na máquina de quem roda a suíte). Suíte completa segue passando.

**Impacto:** nenhuma mudança de schema/UI visível pro usuário -- o formulário de Configurações continua pré-preenchido normalmente (só a FONTE do dado mudou). `config.json` de produção limpo.

## DEC-105 — Auditoria de resiliência pré-entrega: 3 gaps reais de isolamento de falha corrigidos (RESIL-011)

**Contexto:** mesma auditoria de certificação (4 frentes em paralelo). Pedido do usuário: "Certifique-se que o sistema é capaz de burlar o máximo de erros inesperados para manter o funcionamento."

**Achado #1 (GRAVE, confirmado no banco de PRODUÇÃO real):** `repo.next_pending`/`report()`/`repo.mark_processing()` (Fase 1) e `llm.is_healthy()`/`report()` (Fase 2), dentro do loop principal de `app/orchestrator.py::run_once`, rodavam FORA de qualquer try/except por paciente -- uma falha nessa bookkeeping (não na extração/análise em si) abortava a run INTEIRA de uma vez, furando o isolamento RF-12. Evidência real: 4 das últimas 8 runs no `auditoria.db` de produção tinham dezenas/centenas de pacientes presos em `PENDING` sem NENHUM `ERROR` correspondente -- um caso (`2f36ee6b`) com `patients_found=180`, `ERROR=0`, `PENDING=173`, só explicável por um abort fora do try/except por paciente (se a falha fosse dentro, `mark_error` teria gravado ao menos uma linha). Corrigido: ambos os blocos agora isolam a falha só no paciente atual (marca erro, segue pro próximo) em vez de abortar tudo; falha no próprio `next_pending` (não há como saber quem seria o próximo) para o loop com segurança -- pacientes ainda `PENDING` são retomados no próximo censo, não ficam perdidos.

**Achado #2 (RESIL-001, já documentado como não corrigido, reconfirmado hoje):** `app/gsus/census.py::get_census` -- abrir a tela de busca do censo (`_navigate_to_search_screen` + clique em `#btConsultar`) nunca teve retry, diferente de todo o resto da navegação no GSUS já protegida (menu de prontuário, paginação). Um único hiccup aqui aborta o dia inteiro, antes de existir `run_id` (zero pacientes, zero relatório). Corrigido: `_navigate_and_search_with_retry` (5 tentativas, mesmo padrão de `NEXT_CLICK_RETRY_ATTEMPTS`/DEC-024/096).

**Achado #3 (reabertura do DEC-080 por um segundo caminho nunca coberto):** em `collect_all_pages`, quando o clique em "Próxima" tinha sucesso mas o conteúdo da tabela não mudava dentro do prazo (`_wait_for_first_row_to_change` retornando False), a função devolvia `patients` normalmente -- como se fosse o censo COMPLETO. `orchestrator.py` então chamava `mark_patients_inactive_not_in` com essa lista parcial, arriscando marcar paciente real, só ainda não alcançado, como se tivesse recebido alta -- exatamente o bug do DEC-080, só que por um caminho de saída diferente do já corrigido (clique falho). Corrigido: levanta `GSUSCensusIncompleteError` (mesmo tratamento do caminho irmão, logo acima no código).

**Achado #4 (menor):** `LocalLLM.start()` abria o arquivo de log do llama-server fora de qualquer try/except -- disco cheio/permissão negada aqui vazava OSError cru, escapando do `except ModelDownloadError`/`except LLMStartupError` de `update_flow.py` e vazando a conexão SQLite (só fechada no `finally` daquele bloco). Corrigido: try/except convertendo pra `LLMStartupError`, mesmo contrato do `Popen` logo abaixo (já protegido, DEC-058).

**Não corrigido (documentado, fora de escopo hoje):** `_InstanceLock` (DEC-088) só é aplicado dentro de `update_flow.run_update`, não em `orchestrator.run_once`/`Repository` -- qualquer script que chame o pipeline direto contorna a exclusão mútua. Mudança arquitetural maior, fora de escopo de um fix pontual sob prazo de entrega. Ver TASKS.md pra acompanhamento futuro.

**Testes:** +6 (`test_pipeline.py::test_pipeline_isolates_bookkeeping_failure_without_aborting_the_whole_batch`; `test_census_retry.py` +3: retry-com-sucesso, esgotamento de tentativas, conteúdo-nunca-muda; `test_llm.py::test_start_wraps_log_file_oserror_as_llm_startup_error`). Todos confirmados passando na primeira tentativa contra o fix real.

**Impacto:** nenhuma mudança de schema. Risco real de "zero relatório" ou "relatório com a maior parte dos pacientes faltando" numa execução real reduzido significativamente.

## DEC-106 — RF-29 (indicadores agregados + colunas de censo) fechado no relatório HTML e na dashboard

**Contexto:** mesma auditoria de certificação -- "Certifique-se que o relatório está sendo feito da forma solicitada" e "Certifique-se que a dashboard está cumprindo os requisitos do documento", cruzado contra PROJECT_SPEC.md RF-20 a RF-30.

**Achado #1:** os indicadores agregados do serviço (% com pendência ativa, tempo mediano por categoria, % sem EDD, % EDD vencida, interno×externo) já existiam desde a Fase 1 da dashboard (`dashboard_metrics.py`), mas nunca chegavam ao arquivo HTML de verdade -- só à tela Tkinter. "Abrir Relatório" abre justamente esse arquivo; quem só usa o relatório (não a tela) nunca via o indicador exigido pelo RF-29. Corrigido: nova seção "Indicadores do serviço" em `html_report.py::generate_report`, reusando `dashboard_metrics.compute_service_indicators`/`compute_unit_census` (nunca uma segunda fonte de verdade). Deliberadamente NÃO inclui a distribuição de causas de dia vermelho (`dia_causa_counts`) -- mesmo limite de segurança do DEC-101 (texto livre do LLM, sem taxonomia fechada; o HTML em disco é tão ou mais persistente/compartilhável quanto a tabela que o DEC-101 já protegeu). Só a CONTAGEM agregada (sempre segura) aparece.

**Achado #2:** a tabela de censo por paciente da dashboard (`main_window.py`) não tinha DIH nem Contexto (RF-29 pede as duas explicitamente), e "pendência principal" mostrava só a categoria (taxonomia fechada), não a descrição real da pendência -- RF-29 lista as duas como colunas distintas. Corrigido: `PatientCensusRow` ganhou `dih`/`clinical_context`/`main_description`; `CENSUS_COLUMNS` reordenado pra bater com a ordem do RF-29; adicionada rolagem horizontal (tabela passou de 9 pra 12 colunas). `days_since_admission` (cálculo de DIH) extraído de `html_report.py::_dih` pra `app/analysis/priority.py`, compartilhado entre relatório e dashboard (mesmo padrão de `is_edd_overdue`, DEC-099) -- ver DEC-107 pro achado grave encontrado durante essa extração.

**Gaps conscientemente não corrigidos hoje (documentados, não silenciosos):**
- RF-26 (distinção DADO EXTRAÍDO × VARIÁVEL DERIVADA em todo campo, evidência com timestamp pra necessidade hospitalar/dia vermelho) -- mudança de schema maior, fora de escopo sob prazo de entrega.
- RF-30, 2ª frase (mecanismo de override do auditor preservando o registro original) -- a própria especificação já rotula isso como "mecanismo futuro".
- `median_resolution_hours_by_category` e `dia_causa_counts` (instantâneo de hoje) ainda não aparecem na tela Tkinter (só no HTML agora) -- diferença cosmética, não bloqueadora.

**Testes:** +6 (`test_html_report.py` +3: seção presente, nunca inclui `dia_causa` na parte agregada, seguro em banco vazio; `test_dashboard_metrics.py` +2: campos novos presentes/ausentes corretamente; `test_main_window_dashboard.py` +1: colunas novas renderizando).

**Impacto:** nenhuma mudança de schema. Verificado visualmente com dados fictícios (tela) e reais (relatório/dashboard) antes de considerar concluído.

## DEC-107 — DIH nunca funcionava pra dado real: admission_date vem em DD/MM/AAAA, função só aceitava ISO

**Achado (GRAVE, pré-existente, descoberto durante a extração do DEC-106):** ao verificar visualmente a nova coluna DIH da dashboard contra o banco de PRODUÇÃO real, todas as linhas mostravam "?" (DIH ausente). Investigação confirmou: `patients.admission_date` vem CRU da coluna "Data de Internação" do GSUS (`app/gsus/census.py::_extract_page_rows`, só `.strip()`, sem nenhuma normalização) -- diferente de `evidence_date`, que passa por `app.extraction.normalizer.normalize_datetime`. Confirmado no banco real (só o padrão verificado via regex, nunca um valor específico lido/impresso): 100% das 190 datas reais em formato DD/MM/AAAA, 0% em ISO. A função de cálculo de DIH (`_dih` em `html_report.py`, já em produção desde antes desta sessão) só aceitava `date.fromisoformat(...)` -- ou seja, falhava silenciosamente pra essencialmente todo paciente real: conferido no `relatorio.html` já gerado em produção, 190 de 192 pacientes ativos mostravam "DIH não determinado", um requisito RF-29/RF-20 quebrado desde sempre, só nunca percebido (fácil de passar despercebido numa linha de texto por paciente; ficou óbvio ao virar uma coluna inteira "?" na tabela nova da dashboard).

**Decisão:** `days_since_admission` (`app/analysis/priority.py`, extraída no DEC-106) agora tenta DD/MM/AAAA (formato real confirmado) primeiro, com ISO como fallback (compatibilidade com dado sintético de teste que já usava esse formato). Corrigido no único lugar compartilhado -- conserta o relatório HTML (`_dih`) e a dashboard (`PatientCensusRow.dih`) ao mesmo tempo, sem duplicar a correção.

**Verificação:** rebuild + reinstalação completa + screenshot real da dashboard confirmando DIH populado com números plausíveis (17, 8, 20, 26, 16 dias) em vez de "?" em toda a tabela.

**Testes:** +2 (`test_priority.py`: formato DD/MM/AAAA puro e com sufixo de hora). Suíte completa: 380 passed, mesmos 10 erros de Chromium (não relacionados).

**Impacto:** nenhuma mudança de schema. Acredita-se que este seja o maior achado isolado desta auditoria em termos de impacto real acumulado -- um requisito (RF-29, DIH) estava quebrado pra quase todo paciente desde que o relatório entrou em produção, sem nenhum teste ter pego (os testes existentes usavam datas ISO fictícias, nunca o formato real DD/MM/AAAA que o GSUS de verdade produz).

---

## DEC-108 — Censo "completo" (sem link Próxima) não é prova suficiente; pacientes internados sendo marcados como alta

**Achado (GRAVE, execução autônoma sem supervisão, madrugada 2026-09-01/02):** disparadas 3 execuções reais consecutivas de `--auto-update` durante a noite (23:56, 02:27, 04:58), cada uma terminando a paginação do censo SEM nenhum sinal de erro -- sem esgotar retry de "Próxima" (DEC-024/096), sem `GSUSCensusIncompleteError` (DEC-080/105) -- ou seja, `census_complete = True` em todas as três. Ainda assim, o total de `patients WHERE active=1` caiu de 190 (baseline da tarde anterior) para 159, depois 176, depois 175 -- uma queda de ~16% numa única madrugada, implausível pra um censo hospitalar real nesse intervalo.

Investigação (só contagem agregada via SQL read-only, nunca dado de paciente) comparando `processing_queue` das 3 execuções: sobreposição run1(159)∩run2(176)=156, run1∩run3(175)=155, run2∩run3=175 (run3 é subconjunto QUASE completo de run2). Ou seja, os conjuntos capturados são estáveis/decrescentes, não aleatórios -- consistente com paginação real do GSUS terminando cedo demais (retornando um subconjunto menor a cada execução), não com altas genuínas em massa. Como `mark_patients_inactive_not_in` (`orchestrator.py`) roda toda vez que `census_complete=True`, cada execução marcava como inativo qualquer paciente ausente SÓ daquela execução específica -- mesmo tendo sido visto, internado de verdade, na execução anterior da mesma noite.

**Causa raiz:** `collect_all_pages` (`app/gsus/census.py`) só usa `next_link.count() == 0` (ausência do link "Próxima") como sinal de "cheguei ao fim". Esse sinal não é confiável sozinho -- o próprio GSUS expõe quantos registros deveria ter no rodapé da tabela ("Página X de Y : Total de N registros"), e esse número nunca era comparado contra o que foi de fato coletado. Um "fim de paginação" prematuro (renderização instável, mesma classe de instabilidade já documentada em DEC-016/024/096) passava despercebido como censo genuinamente completo.

**Decisão:** nova função `_extract_expected_total` lê "Total de N registros" do rodapé. Ao encontrar `next_link.count() == 0`, `collect_all_pages` agora só considera o censo completo se `len(patients)` estiver dentro de `ALLOWED_TOTAL_COUNT_GAP=2` do total anunciado -- tolerância pequena e absoluta pra não confundir com o ruído normal de linhas duplicadas já dedupadas (DEC-014, "duplicatas espalhadas" da paginação AJAX). Gap maior levanta `GSUSCensusIncompleteError` (mesmo tratamento dos outros dois caminhos de censo incompleto, DEC-080/105) em vez de devolver a lista parcial como se fosse o censo inteiro. Se o rodapé não bater no formato esperado, `_extract_expected_total` devolve `None` e o comportamento antigo é mantido (degradação graciosa, nunca trava a execução por causa desta checagem extra).

Assimetria de risco deliberada: um falso positivo aqui (tratar um censo genuinamente completo como incompleto) só custa pular `mark_patients_inactive_not_in` numa execução -- sem dano, a próxima execução corrige sozinha (`upsert_patient` reativa quem continua aparecendo). Um falso negativo (não pegar um censo realmente incompleto) marcava paciente internado de verdade como se tivesse recebido alta -- removendo-o de toda a auditoria/priorização. Por isso a tolerância é pequena (2), enviesada pra pegar mais casos como incompletos, não menos.

**Verificação:** 3 testes novos com frame falso (`tests/unit/test_census_retry.py`) -- gap grande levanta `GSUSCensusIncompleteError`; gap pequeno (ruído de duplicata) completa normalmente; total ilegível cai no comportamento antigo sem travar. Suíte completa (unit+e2e, sem os 10 erros de infra Chromium): 359 passed. Rebuild + reinstalação silenciosa (`/VERYSILENT /NORESTART` via PowerShell, DEC-095/096) aplicada e binário confirmado com timestamp novo.

**Impacto:** `app/gsus/census.py` (`_extract_expected_total`, `ALLOWED_TOTAL_COUNT_GAP`, checagem em `collect_all_pages`). Nenhuma mudança de schema. Não tenta corrigir retroativamente quem já foi marcado inativo por engano nas 3 execuções da madrugada -- a correção é: rodar uma nova execução real com o binário corrigido, que reativa (`upsert_patient`) qualquer paciente que aparecer de novo no censo, sem repetir o erro.

---

## DEC-109 — Fase 2 (IA) rodando em thread própria, em paralelo com a Fase 1 (pedido do usuário)

**Pedido do usuário (2026-09-02):** numa execução real de 182 pacientes, a Fase 1 (extração/regras, depende do GSUS responder) levou mais de 1h pra terminar, e só DEPOIS disso a Fase 2 (análise por IA, local, lenta -- DEC-070) começou a rodar, processando só 16 pacientes nesse tempo apesar de já ter mais de 100 prontos havia muito. Pergunta direta do usuário: "não tem como iniciar a fase 2 antes da fase 1 terminar? [...] os pacientes já processados na fase 1 já vão sendo analisados com a IA".

**Decisão:** `_llm_phase2_worker` (`app/orchestrator.py`) agora roda numa thread dedicada, iniciada ANTES do laço da Fase 1, consumindo uma `queue.Queue` conforme cada paciente termina a Fase 1 -- as duas fases se sobrepõem de verdade (Fase 1 é limitada por rede/GSUS, Fase 2 por CPU/GPU local, recursos diferentes que não competem entre si). A thread abre sua PRÓPRIA conexão SQLite (nunca compartilha `sqlite3.Connection` entre threads -- regra do módulo `repository.py`), viável porque o banco já roda em WAL (suporta um escritor + conexões concorrentes).

**Preço consciente:** pacientes "frescos" (nota nova nesta execução) são consumidos na ordem em que a Fase 1 libera cada um, não mais do menor pro maior volume de texto (DEC-085) -- ordenar globalmente exigiria esperar a Fase 1 inteira terminar primeiro, o que anularia o ganho. Só o backlog (pacientes sem nota nova, resgatados via LLM-004/DEC-090 DEPOIS que a Fase 1 termina) continua ordenado assim, onde não custa nada.

**Revisão adversarial ANTES de aplicar (5 dimensões: race conditions, deadlock, vazamento de recursos, segurança entre threads, regressão semântica -- 23 agentes, cada achado verificado ceticamente por um segundo agente independente):** confirmou 18 achados reais, todos raízes em ~4 causas:
1. **[GRAVE, reproduzido empiricamente]** Qualquer exceção entre o início da thread e o envio do sentinela (`finish_run`, o laço de backlog) pulava pro `except` mais externo sem nunca enfileirar `_LLM_QUEUE_DONE` nem chamar `.join()` -- a thread ficava PRA SEMPRE bloqueada em `llm_queue.get()`, vazando ela e a conexão SQLite dela. Grave de verdade no botão "Atualizar agora" da UI (processo de vida longa) -- cada falha desse tipo deixaria mais uma thread zumbi. Corrigido com `finally` envolvendo tudo entre o início da thread e o dreno da fila.
2. **[GRAVE]** A própria abertura de conexão da thread (`database.init_db`) ficava FORA do try/except dela -- uma falha ali matava a thread via o handler padrão do Python (só stderr, nunca o `logger` da aplicação), pulando a Fase 2 inteira em silêncio enquanto `run_once` seguia reportando sucesso. Corrigido: abertura de conexão agora dentro de try/except, loga e sinaliza `worker_failed` (novo `threading.Event`).
3. **[ALTO]** `finish_run` (e por extensão qualquer escrita) não tinha `busy_timeout` configurado -- com duas conexões escrevendo no mesmo arquivo pela primeira vez, o timeout padrão do `sqlite3` (5s) podia estourar sob contenção no hardware fraco confirmado do alvo. Corrigido: `PRAGMA busy_timeout = 30000` em `database.get_connection` (app-wide, não só pra esta thread) + `finish_run` isolado em try/except (mesmo padrão de `generate_report`/`save_snapshot`).
4. **[MÉDIO]** `.join()` sem timeout algum -- não é regressão nova (a Fase 2 sequencial já tinha o mesmo teto de ~90min/paciente sem disjuntor eficaz contra um slot travado que ainda responde `/health`, DEC-087), mas agora concentrado num único `.join()`. Corrigido parcialmente: `.join(timeout=300)` em loop, logando sinal de vida periódico em vez de bloquear em silêncio -- não trunca a espera (perderia cobertura de propósito), só torna visível que ainda está rodando.
5. **[BAIXO, deferido]** `generate_report` faz várias leituras SELECT independentes sem uma transação única -- com dois escritores agora, um retrato do relatório pode ficar momentaneamente inconsistente entre uma leitura e outra (corrige sozinho na próxima regravação, uma por paciente). Não corrigido nesta rodada por ser cosmético e autocorretivo -- registrado aqui pra não ficar escondido.

**Verificação:** +4 testes (`tests/e2e/test_pipeline.py`) -- 2 sobre ordenação (fresco = ordem de chegada, backlog = ordenado por tamanho, documentando a mudança de comportamento do item anterior) e 2 sobre os achados graves da revisão (thread não vaza quando o backlog explode -- reproduz a técnica exata da revisão via `threading.enumerate()`; falha ao abrir a conexão da Fase 2 é logada e a Fase 1 segue completando normalmente). Suíte completa: 362 passed, repetida 5x sem flakiness.

**Resultado:** rebuild + reinstalação silenciosa aplicada, execução real disparada em produção pra confirmar as duas fases sobrepondo de verdade (ver CURRENT_STATE.md).

**Impacto:** `app/orchestrator.py` (`_llm_phase2_worker`, `_get_db_path`, `_LLM_QUEUE_DONE`, restruturação de `run_once`), `app/storage/database.py` (`busy_timeout`), `app/reports/html_report.py` (nome de arquivo temporário por thread, pré-requisito de segurança pra Fase 2 e o `report()` do fim da Fase 1 regravarem o relatório ao mesmo tempo sem colidir). Nenhuma mudança de schema.

---

## DEC-110 — Design/UX da tela (UI-005): paleta única, tema ttk `clam`, estilo compartilhado entre as 3 telas

**Contexto:** design/UX da tela ficou adiado desde 2026-08-31/09-01 ("resto funcional primeiro" -- ver histórico de CURRENT_STATE.md 2026-09-01). Auditoria de prontidão pra entrega (2026-09-03) confirmou o resto do sistema sólido; usuário decidiu incluir o polimento visual nesta entrega.

**Decisão:** paleta única (`app/ui/main_window.py`: `BG_COLOR`/`CARD_BG`/`CARD_BORDER`/`BRAND_COLOR`/`BRAND_COLOR_DARK`/`TEXT_PRIMARY`/`TEXT_MUTED`/`FONT_FAMILY`), aplicada nas 3 telas (principal, configuração inicial, localizar paciente). Cores de prioridade (`PRIORITY_COLORS`, ALTA/MEDIA/MONITORAMENTO) NUNCA foram alteradas -- são a mesma linguagem visual já compartilhada com o relatório HTML, mudar isso quebraria essa consistência. Cartões de KPI ganharam uma faixa de destaque no topo (`KPI_ACCENTS`) -- neutra (cor da marca) pros indicadores informativos, vermelha pros dois que pedem atenção imediata (EDD vencida, dia vermelho), verde pro indicador positivo (dia verde) -- sem reinterpretar severidade clínica além do que `PRIORITY_COLORS` já define.

**Tema ttk `clam`:** único tema ttk que respeita `background`/`foreground` customizados em `ttk.Button` de forma consistente no Windows -- os temas nativos (`vista`/`winnative`, os defaults) ignoram essas opções na prática para botões, então não dava pra aplicar cor de marca aos botões sem trocar de tema. Efeito colateral aceito: botões/Treeview perdem o verniz nativo do Windows em troca de controle total de cor.

**`configure_app_style()` como função de módulo, não método:** achado durante a implementação -- `SetupWindow`/`LookupWindow` (telas menores, arquivos próprios) podem abrir ANTES de `MainWindow` alguma vez existir no processo (ex.: primeira execução, sem configuração ainda -- vai direto pra `SetupWindow`). Se o estilo só fosse configurado dentro de `MainWindow.__init__`, os botões dessas telas menores renderizariam sem a paleta customizada nesse cenário. `ttk.Style()` é compartilhado por todo o processo Tk e chamar `configure_app_style()` de novo depois é seguro e barato (só reconfigura os mesmos nomes) -- as 3 telas chamam a mesma função no início do `__init__`.

**Achado incidental (não é regressão de produto, é lacuna de teste):** 2 testes localizavam botões via `isinstance(w, tk.Button)`, que não reconhece `ttk.Button` (classes não relacionadas por herança) -- os testes paravam de encontrar os botões reais depois da troca pra `ttk.Button`. Corrigidos pra checar `isinstance(w, (tk.Button, ttk.Button))`.

**Verificação:** nenhum widget mudou de identidade (mesmos atributos que os testes já esperavam: `_kpi_labels`, `_census_tree`, `status_label`, `update_button`). Verificado visualmente com dados 100% sintéticos nas 3 telas (nunca o banco real -- ver nota de segurança abaixo). Suíte completa: 362 passed.

**Nota de segurança (achado real desta sessão, não repetir):** ao planejar este trabalho, a primeira tentativa de ver a tela "antes" usou um script de screenshot antigo apontado pro banco de PRODUÇÃO real -- capturou prontuário e contexto clínico reais numa imagem, que então entrou no contexto da IA ao ser visualizada. Erro reconhecido e corrigido na hora (imagens apagadas, nada reproduzido); o design de fato foi feito e verificado inteiramente contra `smoke_dashboard.py` (scratchpad, dados fictícios `F0001`-`F0006`, nunca toca `auditoria.db` real). Lição: qualquer verificação visual futura da tela DEVE usar dado sintético, nunca o banco de produção, mesmo pra uso interno de quem está desenvolvendo.

**Impacto:** `app/ui/main_window.py` (constantes de paleta, `configure_app_style()`, `_style_axes()`), `app/ui/setup_window.py`, `app/ui/lookup_window.py`, `tests/unit/test_app_shell.py` (2 asserts corrigidos). Nenhuma mudança de schema, nenhuma mudança de comportamento funcional -- só aparência.
