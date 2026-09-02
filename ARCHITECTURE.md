# ARCHITECTURE.md — GSUS Auditoria

Registra apenas arquitetura efetivamente adotada. Nada hipotético.

## 1. Visão geral

Aplicativo desktop único (`gsus-auditoria.exe`), processo único orquestrando sub-processos locais (Firefox via Playwright, `llama-server` via llama.cpp). Sem componente cliente-servidor próprio, sem rede além do acesso ao GSUS (intranet hospitalar) e ao `127.0.0.1` do LLM local.

```
Tkinter UI  ──►  Orchestrator (app/main.py)
                     │
                     ├─► GSUS client (Playwright/Firefox)  — login, censo, prontuário, evoluções
                     ├─► Extraction (parser + normalizer)   — DOM → texto estruturado
                     ├─► Storage (SQLite: auditoria.db)      — runs, patients, notes, queue, state, pending_items
                     ├─► Analysis
                     │     ├─ rules.py   (determinístico, sempre roda primeiro)
                     │     └─ llm.py     (llama.cpp local, só quando necessário)
                     ├─► Reports (html_report.py)            — HTML estático por setor/leito
                     └─► Security (credentials.py)           — Windows Credential Manager (ctypes)
```

## 2. Componentes e responsabilidades

### `app/main.py`
Ponto de entrada. Inicializa logging, config, banco (cria schema se ausente), abre UI. Orquestra o fluxo de uma execução (`run`): censo → fila → extração por paciente → regras → LLM → persistência → relatório.

### `app/config.py`
Config não sensível (setor, horário de agendamento, caminho do modelo GGUF, parâmetros de retry/timeout). Armazenada em JSON local (`config.json`, sem segredos). Não contém credenciais.

### `app/ui/`
- `setup_window.py`: tela de configuração inicial (usuário GSUS, senha, setor, horário). Senha vai direto para `security/credentials.py`, nunca para `config.py`.
- `main_window.py`: tela principal (status, progresso, "Atualizar agora", "Abrir relatório", link para configurações, botão "Localizar Paciente"). Mensagens de erro sempre amigáveis (mapa erro técnico → mensagem de usuário); detalhe técnico só em log.
- `lookup_window.py`: busca manual de paciente por prontuário (RF-19) -- REFORMULADO 2026-08-25 (DEC-067): gera o relatório individual a partir do que já está no banco local (`html_report.py::generate_patient_report`), só para paciente ainda internado (alta/desconhecido viram mensagem direta). Busca síncrona, nunca navega no GSUS ao vivo (diferente do desenho original, DEC-031) -- só leitura (nunca chama `save_patient_state`/`add_pending_item`).

### `app/gsus/`
- `client.py`: wrapper do browser Playwright (Firefox -- ver DECISIONS.md DEC-010), ciclo de vida da sessão, waits baseados em estado (nunca `time.sleep` arbitrário). Também expõe `get_content_frame()`: GSUS é frameset clássico, todo conteúdo pós-login vive no frame `content` (DEC-009).
- `login.py`: autenticação GSUS.
- `census.py`: extração da lista de internados (censo) → `list[Patient]`.
- `records.py`: navegação até a internação atual de um paciente e extração das evoluções (`open_current_admission` + `extract_notes`), seguindo o fluxo por *pesquisa de prontuário* (seção 13 do prompt mestre) — não por posição visual na lista. Também expõe `get_full_admission_history_text()`, usada só pelo recurso manual "Localizar" (RF-19) -- percorre TODOS os episódios de internação, não só o atual.
- `adapter.py`: ponte entre `records.py`/`census.py`/`login.py` e o resto do app -- `GSUSAdapter` implementa os Protocols `CensusSource`/`RecordSource` do orchestrator, e também `lookup_full_history()` pro recurso manual.

Seletores Playwright seguem a hierarquia da seção 15 do prompt mestre (role/label/text estável > atributo semântico > ID estável > CSS específico; nunca coordenada/posição/imagem/sleep). Todo seletor real do GSUS que ainda não foi confirmado fica marcado `BLOCKED_GSUS` no código (`NotImplementedError` com mensagem explicando o que falta) até validação com HTML/seletor real fornecido por humano.

### `app/extraction/`
- `parser.py`: DOM → estrutura intermediária (linhas de evolução, campos brutos). Só DOM (`inner_text`/estrutura); sem OCR salvo necessidade comprovada.
- `normalizer.py`: normalização de texto (espaços, quebras de linha, datas) para hashing e leitura pelo LLM.

### `app/storage/`
- `database.py`: schema SQLite (seção 17 do prompt mestre) e migrações simples (criar-se-não-existir).
- `repository.py`: operações CRUD por tabela, incluindo fila (`processing_queue`) com estados `PENDING/PROCESSING/DONE/ERROR` e lógica de resume (reclassificar `PROCESSING` órfão ao reiniciar).

### `app/analysis/`
Modelo de análise alinhado à orientação técnica de auditoria concorrente fornecida pelo usuário 2026-08-24 (ver DECISIONS.md DEC-057, PROJECT_SPEC.md RF-20 a RF-30) -- núcleo é a barreira à progressão da internação, não um resumo livre.
- `taxonomy.py`: taxonomia FECHADA de pendências (7 categorias + subtipos, cada uma com escape "OUTRO"). Fonte única de verdade -- `schemas.py` valida contra ela, `llm.py` gera o prompt a partir dela.
- `sla_config.py`: limites de tempo (SLA) default por categoria -- ponto de partida a validar institucionalmente (mesmo princípio de RULES-001/RF-07), nunca usado para declarar "atraso" automaticamente, só alimenta `priority.py`.
- `priority.py`: prioridade (ALTA/MÉDIA/MONITORAMENTO) calculada por REGRA determinística e transparente -- nunca pelo LLM (RF-24). Também expõe `hours_elapsed_since()` (tempo decorrido desde uma evidência, sempre em horário local ingênuo -- nunca misturar com UTC, ver DEC-057).
- `rules.py`: regras determinísticas Python puro (ex.: exame solicitado sem resultado posterior), categorias vindas de `taxonomy.py`. Cada regra é uma função pura testável isoladamente.
- `llm.py`: classe `LocalLLM`, inicia/consulta `llama-server` local (`127.0.0.1`, porta fixa). Constrói prompt a partir do incremento (estado anterior + pendências ainda ativas + notas novas -- sem as pendências ativas o LLM não saberia dizer se uma foi resolvida), valida saída contra `schemas.py`, aplica retry limitado, mapeia falha final para `LLM_ANALYSIS_ERROR`.
- `schemas.py`: validação manual (stdlib, RNF-07) do contrato de saída do LLM -- necessidade hospitalar (5 categorias fechadas, nunca "internação desnecessária"), taxonomia de pendências, EDD, dia verde/vermelho, inferência com confiança obrigatória.

### `app/reports/html_report.py`
Gera HTML estático (template + CSS local, sem framework JS) a partir do estado persistido. Duas visões: censo de pendências por unidade (uma linha por paciente, pendência de maior prioridade -- para abrir no início do dia) e relatório individual completo por leito (contexto, necessidade hospitalar, objetivo, pendência principal com evidência, próximo passo, EDD, classificação do dia). Prioridade e tempo decorrido são recalculados na leitura, nunca lidos como valor fixo (ficariam desatualizados). Sempre expõe contagem encontrados/processados/falhas/sem-internação-atual e lista de prontuários não processados. Aviso visível quando a análise por IA foi limitada às últimas 2 semanas por backlog grande (`analysis_window_limited`, DEC-066). `generate_patient_report(repo, record_number)` (novo, DEC-067) gera o relatório de UM paciente isolado a partir do mesmo dado já persistido -- usado por "Localizar Paciente" (`lookup_window.py`), nunca toca GSUS.

### `app/security/credentials.py`
Lê/escreve credencial GSUS via Windows Credential Manager, usando `ctypes` contra `advapi32.dll` (`CredWriteW`/`CredReadW`/`CredDeleteW`) — API nativa do Windows, sem dependência de terceiros (ver DEC-002).

## 3. Dados

SQLite único (`%LOCALAPPDATA%\GSUSAuditoria\auditoria.db` em produção; `./auditoria.db` em desenvolvimento). Tabelas conforme seção 17 do prompt mestre: `runs`, `patients`, `notes`, `processing_queue`, `patient_state`, `pending_items`, `pending_item_evidence`. Texto bruto de evolução (`notes.text`) segue política de retenção configurável (RETENTION-001, DEC-068): a cada execução, `Repository.purge_old_notes_for_discharged_patients` apaga `notes` de paciente com alta (`active=0`) há mais de `AppConfig.raw_notes_retention_days` (default 90 dias). Nunca apaga `patient_state`/`pending_items`/`pending_item_evidence` -- o resumo estruturado com evidência é retido indefinidamente, é o que a validação auditor×IA (seção 17 da orientação técnica) precisa.

`patient_state` e `pending_items` carregam os campos do modelo de auditoria concorrente (DEC-057): necessidade hospitalar, objetivo terapêutico, próximo passo, EDD, classificação de dia (verde/vermelho), e, por pendência, subcategoria/origem/prioridade/confiança/inferência/status de fluxo. `pending_item_evidence` guarda evidências ADICIONAIS de uma pendência já registrada (reiterações) -- a primeira evidência sempre fica em `pending_items.evidence`/`evidence_date`. Migração de schema (`ALTER TABLE`, `app/storage/database.py::_migrate`) mantém bancos já existentes compatíveis sem perder dado.

## 4. Interfaces internas

- `Patient` (dataclass): `record_number`, `bed`, `unit`, `admission_date`, `active`.
- `Note` (dataclass): `patient_id`, `source_type`, `specialty`, `timestamp`, `text`, `text_hash`.
- `LocalLLM.analyze_patient(previous_state, new_notes, active_pending_items) -> AnalysisOutput`: única fronteira entre regras determinísticas e IA. `AnalysisOutput` valida contra o contrato de `schemas.py` antes de qualquer persistência; `priority` nunca vem daqui -- é calculada por `app/analysis/priority.py` depois da validação (RF-24).
- `Repository`: única fronteira entre lógica de aplicação e SQLite (nenhum outro módulo abre conexão diretamente).

## 5. Decisões de segurança (resumo — detalhe em `DECISIONS.md`)

- Browser Playwright roda em modo não-headless opcional só para depuração manual; em produção roda controlado, sem input do usuário, sem ações de escrita (nenhum `click` em botão de salvar/confirmar/excluir é implementado).
- `llama-server` bind exclusivo `127.0.0.1`.
- Sem telemetria, sem chamada de rede além do host do GSUS configurado e `127.0.0.1`.
- Credenciais nunca em `config.json`, nunca em log.

## 6. Runtime e empacotamento

- Runtime Python alvo: **3.13** (não 3.14 — recém-lançado, risco de wheels ausentes para Playwright/llama-cpp-python; ver DEC-001).
- Empacotamento: PyInstaller `--onedir`.
- Estrutura de distribuição final:
```
GSUS Auditoria/
├── gsus-auditoria.exe
├── runtime/llama-server.exe
├── firefox/          (Playwright browser bundle -- ver DEC-010)
├── models/model.gguf
├── assets/
└── auditoria.db (criado no primeiro uso, em %LOCALAPPDATA%)
```
- Instalador Windows (NSIS ou Inno Setup — decisão adiada para task BUILD-001/INSTALL-001; ambos leves, sem dependência de runtime extra) oculta a complexidade acima do usuário e registra a tarefa agendada no Windows Task Scheduler.

## 7. Não implementado (deliberadamente, por escopo)

Servidor web, API própria, dashboard, multi-usuário, RAG/embeddings, agentes autônomos, OCR — ver seção "Não-escopo" em `PROJECT_SPEC.md`.
