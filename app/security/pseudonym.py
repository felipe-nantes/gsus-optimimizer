"""Pseudonimização de identificadores de paciente para LOG (RNF-03).

`patient_id` no banco É o número do prontuário (ver
`Repository.upsert_patient`), então logá-lo em texto puro coloca um
identificador direto de paciente em arquivo de log. Isso já tinha
acontecido uma vez por outro caminho (DECISIONS.md DEC-016) e voltou a
acontecer quando o log passou a ser copiado para dentro do repositório
(DEC-046/049).

O pseudônimo é determinístico: a mesma pessoa gera sempre o mesmo rótulo,
então continua sendo possível correlacionar linhas de log entre si e
entre execuções -- que é a única coisa para a qual o identificador serve
em depuração. Não é reversível sem o prontuário original.

NÃO usar para nada além de log: persistência, relatório e telas seguem
usando o identificador real (o usuário precisa saber de qual paciente se
trata).
"""
from __future__ import annotations

import hashlib

_LABEL_LENGTH = 8


def for_log(identifier: str | None) -> str:
    """Rótulo curto e estável para o identificador. Vazio/None vira
    "(sem-id)" -- nunca levanta exceção, porque falhar ao LOGAR não pode
    derrubar o processamento."""
    if not identifier:
        return "(sem-id)"
    digest = hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()
    return f"pac-{digest[:_LABEL_LENGTH]}"
