# PROJECT_SPEC.md — GSUS Auditoria

Fonte de verdade do produto. Qualquer conflito entre código e este documento deve ser resolvido em favor deste documento (ou este documento deve ser atualizado deliberadamente, com decisão registrada em `DECISIONS.md`).

## 1. Missão

Aplicativo local para Windows que audita automaticamente os pacientes internados no sistema hospitalar GSUS, extraindo evoluções e informações relevantes de cada prontuário, identificando o que é novo desde a última execução, detectando pendências clínicas/administrativas por regras determinísticas (e por um único LLM local quando interpretação textual for necessária), e produzindo um relatório HTML organizado por setor/leito — funcionando 100% offline após instalado, sem exigir conhecimento técnico do usuário.

## 2. Usuário

Profissional de auditoria hospitalar/administrativo do setor, sem conhecimento técnico (não sabe o que é Python, terminal, JSON, banco de dados). Usa um único computador Windows, credencial GSUS própria com permissão somente leitura.

## 3. Problema

Hoje a auditoria concorrente de leitos exige abrir manualmente cada prontuário no GSUS, ler evoluções e identificar pendências (exames sem resultado, interconsultas sem parecer, etc.). É lento, repetitivo e sujeito a itens esquecidos entre um dia e outro. O produto automatiza a coleta e a triagem de pendências, mantendo o julgamento clínico final com o humano — o software aponta evidência, não prescreve conduta.

## 4. Escopo (V1 / MVP de produção)

- Login no GSUS via Playwright (Firefox empacotado -- ver DECISIONS.md DEC-010; Chromium não completa o login real do GSUS), read-only.
- Extração do censo de internados (lista completa).
- Fila persistente de prontuários a processar.
- Para cada prontuário: localizar internação atual, abrir evoluções, extrair texto.
- Normalização do texto extraído.
- Hash determinístico por evolução para detectar o que é novo (SHA-256).
- Regras determinísticas para pendências objetivas (configuráveis, aprovadas antes de uso produtivo).
- Um único LLM local (llama.cpp + GGUF) para: contextualização clínica, situação atual, pendências não capturadas deterministicamente — sempre com evidência textual obrigatória e saída validada por JSON Schema.
- Persistência em SQLite (`auditoria.db`).
- Relatório HTML estático, por setor/leito, abrindo no navegador padrão.
- Execução manual (botão "Atualizar agora") e agendada (Windows Task Scheduler).
- Interface Tkinter simples: configuração inicial + tela principal com status/progresso.
- Armazenamento de credenciais via Windows Credential Manager (nunca texto puro).
- Empacotamento via PyInstaller (onedir) + instalador Windows simples.
- Funciona sem Internet, sem APIs externas, sem tokens, após instalado.

## 5. Não-escopo (explicitamente fora da V1)

- Qualquer operação de escrita no GSUS (prescrever, evoluir, confirmar, excluir, alterar status).
- Múltiplos usuários/máquinas simultâneos, multi-tenant.
- Dashboard web, SPA, servidor web, API própria.
- Suporte Linux/macOS.
- RAG, vector database, embeddings, múltiplos LLMs, agentes autônomos, browser agents, computer-use.
- Cloud, Docker, Kubernetes, bancos remotos.
- OCR (salvo se comprovado que o DOM não expõe o conteúdo necessário — ver regra 16 do prompt mestre).
- Regras clínicas de tempo arbitrárias não validadas por humano.
- Qualquer envio de dado hospitalar para fora da máquina/instituição.

## 6. Requisitos funcionais

RF-01. O sistema deve autenticar no GSUS com credencial read-only configurada pelo usuário.
RF-02. O sistema deve obter a lista completa de pacientes internados do setor configurado (censo).
RF-03. O sistema deve criar/persistir uma fila local de prontuários a processar, sobrevivendo a interrupções.
RF-04. Para cada paciente da fila, o sistema deve localizar a internação atual e extrair as evoluções (texto).
RF-05. O sistema deve normalizar o texto extraído para processamento consistente.
RF-06. O sistema deve calcular hash determinístico por evolução e pular (`SKIP`) evoluções já processadas (`NEW_NOTE` caso contrário).
RF-07. O sistema deve aplicar regras determinísticas configuráveis para detectar pendências objetivas antes de considerar o LLM.
RF-08. O sistema deve invocar o LLM local apenas quando houver texto livre a interpretar, enviando apenas o incremento necessário (estado anterior + notas novas), nunca reenviando o histórico completo desnecessariamente.
RF-09. A saída do LLM deve validar contra um JSON Schema fixo; saída inválida aciona retry limitado e, em falha persistente, `LLM_ANALYSIS_ERROR` (nunca inventar resultado).
RF-10. Toda pendência gerada pelo LLM deve conter evidência textual (`evidence`) e, quando disponível, `evidence_date`; sem evidência suficiente, o LLM deve retornar `insufficient_information: true`.
RF-11. O sistema deve persistir pacientes, evoluções, execuções (`runs`), fila de processamento, estado do paciente e pendências em SQLite (`auditoria.db`), conforme modelo da seção 17 do prompt mestre.
RF-12. Falha em um prontuário não deve interromper o lote; o paciente falho deve ser marcado `ERROR` e reportado no relatório.
RF-13. Execução interrompida deve ser retomável: reclassificar `PROCESSING` órfão e continuar sem repetir `DONE`.
RF-14. O sistema deve gerar relatório HTML estático por setor/leito, incluindo contagem de encontrados/processados/falhos e a lista de prontuários não processados.
RF-15. O sistema deve permitir execução manual (botão) e execução agendada (Windows Task Scheduler configurado pelo próprio instalador/app).
RF-16. A interface deve mostrar progresso em linguagem simples ("Processando paciente 17 de 42...") e nunca expor stack traces/erros técnicos brutos ao usuário.
RF-17. Credenciais GSUS devem ser salvas exclusivamente via mecanismo seguro do Windows (Windows Credential Manager), nunca em texto puro, JSON, YAML, `.env` ou SQLite sem proteção.
RF-18. O sistema deve funcionar integralmente offline após a instalação (exceto o próprio acesso à rede interna do GSUS).
RF-19. O sistema deve permitir localizar manualmente um paciente pelo número do prontuário ("Localizar Paciente"), exibindo o relatório individual JÁ PROCESSADO desse paciente (mesmo formato da rotina automática, RF-20 a RF-30) -- só se o paciente ainda estiver internado. Paciente com alta: mensagem "Paciente recebeu alta". Prontuário não conhecido pela rotina automática: mensagem informando que ainda não foi processado. Busca é SEMPRE no banco local (nunca navega ao vivo no GSUS) -- REFORMULADO 2026-08-25 (decisão do usuário, ver DECISIONS.md DEC-067; substitui o desenho original de 2026-08-20/DEC-031, que buscava o histórico bruto completo -- atual e antigas internações -- ao vivo no GSUS a cada consulta, sem processar). Continua nunca escrevendo no GSUS.

RF-20. A análise de cada paciente deve responder a três perguntas fixas, não um resumo livre: (a) por que ainda precisa estar internado hoje; (b) o que impede a progressão (alta/transferência/próxima etapa); (c) existe ação pendente que poderia ter ocorrido e não ocorreu. Baseado em orientação técnica formal fornecida pelo usuário 2026-08-24 (documento de mestrado aplicado, ver DECISIONS.md DEC-057).

RF-21. "Necessidade de internação hospitalar hoje" é campo obrigatório por paciente, com categoria fechada (SIM / PROVAVELMENTE SIM / INCERTO -- documentação insuficiente / POSSIVELMENTE NÃO -- só pendências / NÃO IDENTIFICADA) + justificativa textual com evidência. O sistema NUNCA formula como "internação desnecessária" -- a formulação segura é "necessidade de nível hospitalar agudo não identificada nos registros disponíveis"; a decisão permanece do auditor humano.

RF-22. Toda pendência deve ser classificada numa taxonomia FECHADA de 7 categorias com subtipos controlados (Diagnóstico, Parecer/interconsulta, Procedimento/cirurgia, Terapêutica, Transferência, Alta/barreira pós-alta, Administrativa/logística) -- nunca categoria livre. Categorias/subtipos definidos em `app/analysis/taxonomy.py`.

RF-23. Toda pendência deve registrar temporalidade rastreável: primeira evidência documental (nunca horário inventado), tempo acumulado até o momento do processamento, e status (resolvida/não resolvida). O sistema reporta TEMPO DECORRIDO, nunca declara "atraso" por conta própria -- limite/SLA é configuração institucional parametrizável (`app/analysis/sla_config.py`), validada localmente antes de uso em produção (mesmo princípio de RF-07).

RF-24. Toda pendência deve ser classificada quanto à origem (interna ao hospital / externa) e quanto à prioridade (Alta / Média / Monitoramento), esta última calculada por REGRA determinística e parametrizável (nunca pelo LLM livremente) a partir de categoria, necessidade hospitalar atual e tempo decorrido vs. SLA configurado.

RF-25. O sistema deve distinguir pendência EXPLÍCITA (frase literal no texto) de INFERIDA (combinação de eventos documentais ao longo do tempo, ex.: solicitação + "mantém investigação" + resultado não encontrado). Pendência inferida exige linguagem de incerteza, escopo temporal e indicação de confiança (Alta/Média/Baixa); nunca afirma causalidade não documentada (ex.: nunca "o hospital atrasou o exame").

RF-26. Cada achado (pendência, necessidade hospitalar, classificação do dia) deve manter uma ou mais evidências associadas, cada uma com timestamp e trecho textual de origem, consultável pelo auditor ("de onde veio?"). Distinção explícita entre DADO EXTRAÍDO (fato recuperado diretamente do prontuário) e VARIÁVEL DERIVADA (inferência da IA) em todo campo da saída estruturada.

RF-27. O sistema deve calcular, quando aplicável, o próximo marco necessário à progressão do paciente ("próximo passo para a alta") e, quando presente no prontuário, extrair a Estimated Date of Discharge (EDD)/previsão de alta, classificando-a como registrada, não registrada ou vencida -- nunca inventada se ausente.

RF-28. Cada dia de processamento de cada paciente deve ser classificado como DIA VERDE (ação necessária ocorreu, paciente progrediu) ou DIA VERMELHO (ação necessária não ocorreu ou pouco cuidado agregador de valor), com causa registrada quando vermelho. O sistema deve manter histórico de dias vermelhos por causa (para indicador agregado, RF-29).

RF-29. O relatório deve oferecer, além da visão individual por paciente, uma visão de censo por unidade (leito, DIH, contexto, pendência principal, tempo, categoria, prioridade -- uma linha por paciente, legível em segundos) e indicadores agregados do serviço (ex.: % pacientes com barreira ativa, tempo mediano de resolução por categoria, % sem EDD documentada, distribuição de causas de dia vermelho, interno×externo).

RF-30. Toda saída do LLM deve registrar metadados de auditabilidade: versão/identificação do modelo usado e horário de processamento. Correção/override de um achado pelo auditor (via RF-19-like mecanismo futuro) deve ser possível sem perder o registro original.

## 7. Requisitos não funcionais

RNF-01. Instalável em Windows 10/11 x64 limpo, sem exigir Python/Node/Docker/Ollama/Playwright/dev tools pré-instalados.
RNF-02. Toda operação crítica (rede, extração, LLM) deve ter timeout controlado e retry limitado (nunca loop infinito).
RNF-03. Logs locais estruturados em `logs/`, sem senha, minimizando PHI (dados de saúde) armazenados em log.
RNF-04. Idempotência: reprocessar uma execução não deve duplicar dados nem reprocessar evoluções já hasheadas.
RNF-05. O LLM roda em processo local (`llama-server`) vinculado a `127.0.0.1`, nunca `0.0.0.0`; carregado uma única vez por execução.
RNF-06. Modelo GGUF deve ser substituível sem alterar código de regra de negócio (interface `LocalLLM`).
RNF-07. Nenhuma dependência nova deve ser introduzida sem justificativa documentada em `DECISIONS.md` (problema, por que a stack atual não resolve, impacto de implantação, alternativa mais simples considerada).
RNF-08. Testes automatizados cobrindo unit/integration/e2e com fixtures sintéticas (sem dados reais de pacientes).

## 8. Segurança

SEC-01. Conta GSUS usada pela automação deve ter privilégios somente leitura/consulta — responsabilidade de provisionamento é do usuário/instituição; o app deve operar assumindo e reforçando uso read-only (nunca chamar ações de escrita).
SEC-02. Em qualquer situação ambígua durante navegação/extração: fail-closed — não executar a ação, registrar erro, seguir para o próximo paciente.
SEC-03. Dados hospitalares (nome, prontuário, texto de evolução) nunca saem da máquina/instituição: proibido enviar a APIs externas, telemetria, analytics ou LLM remoto.
SEC-04. Dados sintéticos apenas em desenvolvimento e testes — nunca dado real identificável em prompt de desenvolvimento, fixture versionada ou exemplo de documentação.
SEC-05. Credenciais GSUS: Windows Credential Manager (via `ctypes`/API nativa do Windows — sem biblioteca de terceiros). Nunca logadas.

## 9. Critérios de produção (resumo — detalhado no prompt mestre, seção 60)

O produto está pronto para produção controlada quando: instala em máquina limpa sem dependências manuais; acessa o GSUS; extrai censo completo; processa todos os prontuários sem que uma falha interrompa o lote; retoma execução interrompida; não reprocessa duplicatas; LLM roda localmente sem saída de dados; saída do LLM obedece schema com evidência; relatório expõe falhas; UI tem "Atualizar agora" e "Abrir relatório"; agendamento funciona; logs locais funcionam; conta GSUS é read-only; testes críticos passam; teste ponta a ponta passa em máquina diferente da de desenvolvimento.

## 10. Definição de pronto (por feature)

Implementada + testada + erro tratado (mensagem amigável ao usuário, detalhe técnico só em log) + log adequado + `CURRENT_STATE.md` atualizado + nenhuma regressão detectada nos testes existentes.
