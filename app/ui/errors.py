"""Mapeamento de erros técnicos para mensagens amigáveis ao usuário (UI-003).

O detalhe técnico (traceback, nome da exceção) SEMPRE fica só no log --
nunca é mostrado na tela (prompt mestre seção 9).
"""
from __future__ import annotations

from app.analysis.llm import LLMAnalysisError, LLMStartupError
from app.gsus.login import GSUSLoginError
from app.gsus.records import GSUSAccessJustificationRequired, GSUSRecordError
from app.update_flow import UpdateAlreadyRunningError


def friendly_message(exc: BaseException) -> str:
    if isinstance(exc, UpdateAlreadyRunningError):
        return (
            "Já existe uma atualização em andamento nesta máquina (pode ser a tarefa "
            "agendada da madrugada). Aguarde ela terminar antes de tentar de novo."
        )
    if isinstance(exc, GSUSLoginError):
        return "Não foi possível acessar o GSUS. Verifique o usuário e a senha em Configurações."
    if isinstance(exc, GSUSAccessJustificationRequired):
        return (
            "Esse prontuário exige justificativa de acesso no próprio GSUS (paciente não "
            "internado nesta unidade no momento). Abra manualmente no GSUS se precisar "
            "consultar esse histórico."
        )
    if isinstance(exc, GSUSRecordError):
        return "Não foi possível localizar esse prontuário. Confira o número e tente de novo."
    if isinstance(exc, NotImplementedError):
        return (
            "Esta função ainda está em desenvolvimento (aguardando finalizar a integração "
            "com o GSUS). Nenhum dado foi alterado."
        )
    if isinstance(exc, (LLMStartupError, LLMAnalysisError)):
        return "O módulo de análise local não pôde ser usado. A atualização continuou sem ele."
    if isinstance(exc, TimeoutError):
        return "O GSUS demorou demais para responder. O sistema pode tentar novamente."
    # DIAG-002: mesma leitura do diagnóstico da execução
    # (`run_diagnosis.classify_top_level_exception`) -- a barra de status não
    # pode dizer "tente novamente" enquanto a faixa diz "reinstale".
    message = str(exc)
    if "Executable doesn't exist" in message or "playwright install" in message:
        return (
            "O navegador interno do programa não foi encontrado nesta máquina. Reinstale o "
            "programa ou acione o suporte técnico."
        )
    return "Não foi possível concluir a operação. O sistema pode tentar novamente."
