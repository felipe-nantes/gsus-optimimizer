# E2E-001 — Checklist de teste em máquina limpa

Procedimento pra validar `installer/output/GSUSAuditoria-Setup.exe` numa
máquina Windows que NUNCA rodou este projeto -- sem Python, sem Playwright,
sem llama.cpp instalados. Se possível, use uma máquina com hardware
parecido com os PCs reais do hospital (fraca, sem GPU) -- já confirmamos
que o app funciona nessas condições (DEC-070/071), então isso não deveria
ser motivo de reprovar o teste, só de demorar mais.

**Importante:** nenhuma credencial ou dado de paciente deve ser copiado de
volta pra mim. Se algo der errado, descreva a estrutura do erro (mensagem,
tela, log) -- nunca prontuário, nome ou texto clínico.

## O que você precisa antes de começar

- [ ] `GSUSAuditoria-Setup.exe` (`installer/output/`, ~107MB) copiado pra
      máquina limpa via pendrive, rede ou serviço de arquivos -- ele não
      está no controle de versão do projeto.
- [ ] Uma credencial GSUS real e válida (CPF + senha) de alguém autorizado
      a usar o sistema.
- [ ] Internet estável na máquina -- a 1ª execução baixa o modelo de IA
      (~4,92GB). Numa conexão doméstica típica isso pode levar de minutos
      a dezenas de minutos.
- [ ] Tempo livre: a primeira atualização completa (download do modelo +
      carga do LLM + análise por IA) pode levar bem mais que uma
      atualização do dia a dia -- reserve pelo menos 1h pra não interromper
      no meio achando que travou.

## 1. Instalação

1. Rode `GSUSAuditoria-Setup.exe` (duplo clique).
2. Se o Windows mostrar "O Windows protegeu o computador" (SmartScreen) --
   **normal** pra um instalador sem assinatura digital, não é bug. Clique
   em "Mais informações" → "Executar assim mesmo".
3. **Não deve pedir permissão de administrador (UAC)** -- a instalação é
   por usuário, de propósito (o app precisa gravar o modelo de IA na
   própria pasta depois, e muita gente no hospital não tem admin na
   máquina de trabalho).
4. Siga o assistente (idioma Português). Pasta padrão está OK. Marque
   "Criar atalho na área de trabalho" se quiser.
5. Ao final, deixe marcado "Abrir GSUS Auditoria agora" (ou abra depois
   pelo atalho/Menu Iniciar).

**Esperado:** instalação termina sem erro, sem pedir reinicialização.

## 2. Primeira execução — configuração

6. O app deve abrir mostrando **CONFIGURAÇÃO INICIAL** (é a primeira vez
   nesta máquina, não tem nada salvo ainda).
7. Preencha CPF, senha (credencial GSUS real), setor, horário de
   atualização (ex.: `06:00`).
8. Clique **CONCLUIR**.

**Esperado:** mensagem "Configuração salva com sucesso"; a MESMA janela
troca pra tela principal ("AUDITORIA GSUS") sem fechar e reabrir.

## 3. Confirmar a tarefa agendada

9. Abra o Agendador de Tarefas do Windows (`taskschd.msc`) ou rode no
   PowerShell/Prompt de Comando:
   ```
   schtasks /Query /TN GSUSAuditoria_AtualizacaoDiaria
   ```

**Esperado:** a tarefa aparece, agendada pro horário escolhido no passo 7,
disparando `gsus-auditoria.exe --auto-update`. Isso confirma que a
atualização automática de madrugada vai funcionar sozinha, sem precisar
clicar em nada.

## 4. Atualizar agora (primeira execução real)

10. Clique **ATUALIZAR AGORA**.
11. **Uma janela do Firefox vai aparecer visível na tela durante o acesso ao GSUS -- é esperado** (DEC-077: o GSUS bloqueia o mesmo acesso quando roda invisível/headless, então a automação usa uma janela visível de propósito). Não precisa mexer nela, ela fecha sozinha.
12. Acompanhe o texto de status -- a ordem exata pode variar um pouco, mas
    espere ver, aproximadamente:
    - "Preparando...", "Baixando modelo de IA (primeira execução)... X%"
      (só na 1ª vez -- pode demorar bastante)
    - "Modelo local pronto. Acessando GSUS..." (carregar o modelo em CPU
      fraca pode levar de 1 a 5 minutos -- não travou, só é lento)
    - "Processando paciente X de Y (regras)..."
    - "Relatório disponível. Iniciando análise por IA..."
    - "Analisando paciente X de Y com IA..." (pode ser BEM lento -- minutos
      por paciente é esperado em hardware fraco, ver DECISIONS.md DEC-070;
      às vezes um paciente específico pode falhar só a parte de IA -- o
      sistema é seguro por design, nunca inventa dado, só sinaliza)
    - "Análise por IA concluída.", "Concluído."
13. Ao final: "Atualizado — X/Y pacientes (Z falha(s))".

**Esperado:** processo termina sem travar a tela (mesmo que a IA demore ou
falhe pra algum paciente).

## 5. Conferir o relatório

14. Clique **ABRIR RELATÓRIO** (pode clicar mesmo enquanto a IA ainda
    processa em segundo plano -- o relatório com regras já fica disponível
    antes da IA terminar).

**Esperado:** abre no navegador padrão, mostra os pacientes reais do setor
configurado, com leito/pendências/prioridade.

## 6. Localizar Paciente

15. Clique **LOCALIZAR PACIENTE**, digite o prontuário de um paciente que
    você sabe que está internado.
16. Repita com um prontuário de paciente já com alta, e com um número que
    não existe.

**Esperado:** (15) mostra o relatório individual desse paciente; (16)
"Paciente recebeu alta." / "Prontuário não encontrado nos registros locais
-- pode não ter sido processado ainda pela rotina automática." -- nunca
tenta buscar ao vivo no GSUS.

## 7. Configurações

17. Clique em "Configurações", mude o horário de atualização, clique
    CONCLUIR.

**Esperado:** volta pra tela principal mostrando o novo horário; repetindo
o passo 3, a tarefa agendada deve refletir o novo horário (se a política da
máquina permitir agendar -- ver observação no fim do log da 1ª tentativa:
"Acesso negado" pode acontecer em conta gerenciada/escolar, e o app já
segue funcionando mesmo assim, só sem a automação de madrugada).

## 8. Desinstalação (recomendado)

18. Desinstale pelo Painel de Controle / Configurações → Aplicativos, ou
    pelo atalho no Menu Iniciar.

**Esperado:** remove os arquivos do app E a tarefa agendada (rode o
comando do passo 3 de novo -- deve dizer que a tarefa não existe mais).
Sem isso, o Windows ficaria tentando abrir um `.exe` que não existe mais,
todo santo dia, pra sempre.

## O que reportar de volta

- Quanto tempo levou o passo 4 (download do modelo + carga do LLM +
  análise por IA) -- ajuda a calibrar expectativa de hardware real.
- Qualquer tela de erro: descreva a mensagem/estrutura, nunca cole dado de
  paciente.
- Se algo travou de vez (zero progresso por muito tempo) vs. só lento.

## Se algo der errado

- **App não abre / fecha sozinho:** veja o log em
  `%LOCALAPPDATA%\GSUSAuditoria\logs\app.log` (nunca tem dado de paciente).
- **Download do modelo falha (sem internet, queda no meio):** não corrompe
  nada -- baixa pra um arquivo temporário e só assume como pronto no
  sucesso (DEC-072). Tenta nova na próxima vez que clicar "Atualizar
  agora".
- **Login GSUS falha (mensagem "verifique usuário e senha"):** raramente é a
  senha de verdade -- confirme fazendo login manual, se possível no próprio
  Firefox que o app usa (`%LOCALAPPDATA%\Programs\GSUS
  Auditoria\playwright-browsers\firefox-1465\firefox\firefox.exe`). Se o
  login manual funcionar mas o app continuar travando com o mesmo erro,
  me chame -- pode ser um achado novo (já corrigimos um caso assim, DEC-077).
- **Windows Defender/antivírus bloqueia algo:** os binários (`llama-
  server.exe`, o próprio `.exe` do app) não são assinados digitalmente --
  pode gerar alerta heurístico numa máquina que nunca os viu. Adicionar
  exceção na pasta de instalação (`%LOCALAPPDATA%\Programs\GSUS
  Auditoria`) resolve, se necessário.
