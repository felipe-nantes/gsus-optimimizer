# GSUS Auditoria

Um assistente de auditoria hospitalar que abre prontuário por prontuário no GSUS, lê as evoluções e aponta o que está travando a alta de cada paciente. Roda inteiro dentro do hospital: não escreve nada no GSUS, não manda dado nenhum pra fora, e depois de instalado nem precisa de internet.

## O problema que isso resolve

Auditoria concorrente de leito é abrir o prontuário 1, ler a evolução, checar se saiu o resultado do exame, anotar, fechar. Abrir o prontuário 2. Repetir. Num hospital com 150 a 200 pacientes internados em urgência e emergência, isso é o dia inteiro de um auditor - e no dia seguinte, quase tudo se repete, porque a maioria dos pacientes continua internada e a pendência de ontem pode não ter mudado.

O GSUS não tem exportação nativa de dados. Não existe um botão "baixar tudo" ou um relatório consolidado do estado dos leitos. Cada informação vive dentro do prontuário individual, e a única forma de tirá-la de lá é clicando e lendo, um paciente de cada vez. Isso não é um detalhe de usabilidade. É o motivo pelo qual um exame pedido há quatro dias sem resultado, ou uma interconsulta sem retorno, só aparece quando alguém tem tempo de abrir aquele prontuário específico e reparar. E "ter tempo" é o recurso mais escasso numa equipe de auditoria.

Esse projeto nasceu de um mestrado profissional no Hospital Universitário Regional de Maringá justamente para atacar esse gargalo: automatizar a parte mecânica (abrir, navegar, ler) e usar um modelo de linguagem local para interpretar o texto livre das evoluções, deixando pro auditor humano só a parte que exige julgamento clínico.

## O que o sistema faz

- Loga no GSUS com uma credencial só de leitura e lista todo mundo internado no setor configurado.
- Abre o prontuário de cada paciente, identifica a internação atual e extrai as evoluções em texto.
- Compara com o que já foi lido antes (hash por evolução) para processar só o que é novo - não relê o histórico inteiro a cada execução.
- Aplica regras determinísticas para pendências objetivas (exame sem resultado, interconsulta sem parecer) e manda pro modelo de linguagem local só o que precisa de interpretação de texto livre.
- Gera um relatório HTML por setor e leito, com o que está pendente, a evidência textual de cada item e há quanto tempo está parado.
- Roda de novo sozinho, em horário agendado, e também sob demanda com um clique.

Toda pendência que o sistema aponta vem junto com o trecho da evolução que a sustenta. Nunca é "o paciente parece estar aguardando algo" sem mostrar onde no texto está escrito isso - e a decisão sobre o que fazer com aquela pendência continua sendo do auditor.

## Como funciona por dentro

O aplicativo é um único executável Windows (`gsus-auditoria.exe`) que orquestra dois processos locais: um Firefox controlado via Playwright para navegar no GSUS, e um `llama-server` (llama.cpp) rodando um Llama 3.1 de 8B na própria máquina. Não há servidor, não há nuvem, não há chamada de API externa em nenhum ponto do fluxo.

```
Tkinter UI  →  Orquestrador
                  ├─ GSUS (Playwright/Firefox) — login, censo, prontuário, evoluções
                  ├─ Extração (parser + normalizador) — DOM → texto estruturado
                  ├─ Regras determinísticas — sempre rodam primeiro
                  ├─ LLM local (llama.cpp) — só quando há texto livre pra interpretar
                  ├─ SQLite — pacientes, evoluções, fila, estado, pendências
                  └─ Relatório HTML — por setor e leito
```

A navegação usa Firefox e não Chromium. Não é capricho: o Chromium simplesmente não completa o fluxo de login real do GSUS, e isso só se descobre testando contra o sistema de verdade.

Extração de texto e aplicação de pendências por regra são rápidas e ficam limitadas pela velocidade de resposta do próprio GSUS. A análise por IA é lenta, porque roda num modelo local sem GPU dedicada. As duas fases rodam em paralelo: assim que um prontuário termina de ser lido, ele já entra na fila de análise, enquanto o robô segue lendo o próximo. Antes disso, a fase de IA só começava depois que todo o censo já tinha sido processado, o que numa execução de mais de cem pacientes deixava o modelo ocioso por mais de uma hora esperando a extração terminar.

## Princípios que não são negociáveis

A automação só lê. O usuário GSUS configurado tem permissão exclusivamente de consulta, e em nenhum ponto do código o sistema escreve, prescreve, confirma ou altera algo no prontuário. O risco de uma automação bagunçar dado clínico real é grande demais pra correr.

O modelo de linguagem roda dentro da máquina do hospital. Nenhuma evolução, nenhum texto de prontuário, sai da instituição, nem para um provedor de IA nem para qualquer outro lugar.

O sistema nunca guarda nome de paciente, só número de prontuário, leito e unidade, e mesmo isso é pseudonimizado onde o dado bruto não é necessário.

O relatório final aponta evidência, não conduta. Toda pendência vem com o trecho de texto que a sustenta, mas quem decide o que fazer com aquilo é sempre um auditor humano. O sistema não decide alta, nem prioridade final, nem nada que afete o cuidado do paciente.

E se um prontuário específico der erro na extração, ele fica marcado como erro e aparece no relatório, mas o resto do lote segue normalmente. Um paciente com problema técnico não pode travar a auditoria de mais duzentos outros.

## Validação

A pesquisa que originou o projeto propõe medir a acurácia do sistema contra o julgamento de auditores especialistas do próprio hospital, usando o Índice de Concordância de Kappa, com meta de concordância igual ou superior a 0,80. A hipótese de trabalho é que a extração automatizada chega a mais de 90% de acurácia em relação à coleta manual - e essa validação, feita por profissionais humanos revisando caso a caso, é o que decide se a ferramenta está pronta para uso rotineiro, não uma métrica que o próprio sistema reporta sobre si mesmo.

## Rodando localmente

O sistema foi desenhado para uma pessoa sem conhecimento técnico instalar e usar: baixa o instalador, configura usuário e setor uma vez, e a partir daí só clica em "Atualizar agora" ou deixa rodar no horário agendado.

Para desenvolvimento:

```bash
pip install -r requirements.txt
playwright install firefox
pytest
```

O modelo Llama 3.1 8B (formato GGUF) e o binário do `llama-server` não fazem parte deste repositório por tamanho - são baixados/empacotados separadamente no build de instalação.
