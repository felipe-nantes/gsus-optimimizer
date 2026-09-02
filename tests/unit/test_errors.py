from app.gsus.login import GSUSLoginError
from app.gsus.records import GSUSAccessJustificationRequired, GSUSRecordError
from app.ui.errors import friendly_message
from app.update_flow import UpdateAlreadyRunningError


def test_update_already_running_has_specific_message():
    """Achado real (DEC-086/088): clique manual colidindo com a tarefa
    agendada (ou vice-versa) precisa de mensagem clara, não um erro genérico."""
    msg = friendly_message(UpdateAlreadyRunningError("detalhe técnico qualquer"))
    assert "andamento" in msg.lower()
    assert "detalhe técnico qualquer" not in msg


def test_gsus_login_error_has_specific_message():
    msg = friendly_message(GSUSLoginError("detalhe técnico qualquer"))
    assert "GSUS" in msg
    assert "detalhe técnico qualquer" not in msg  # nunca vaza detalhe técnico


def test_gsus_record_error_has_specific_message():
    # DEC-051: exceções de records.py não embutem mais o prontuário no
    # texto -- a identificação vem só do pseudônimo logado pelo chamador.
    msg = friendly_message(GSUSRecordError("Nenhuma internação encontrada."))
    assert "prontuário" in msg.lower()
    assert "123456" not in msg  # nunca vaza identificador de paciente


def test_gsus_access_justification_required_has_specific_message_not_generic_record_error():
    msg = friendly_message(GSUSAccessJustificationRequired("GSUS exige justificativa de acesso auditada"))
    assert "justificativa" in msg.lower()
    # não pode cair na mensagem genérica de "não encontrado" -- é um caso diferente
    assert "Confira o número" not in msg


def test_not_implemented_error_has_specific_message():
    msg = friendly_message(NotImplementedError("BLOCKED_GSUS: seletor pendente"))
    assert "desenvolvimento" in msg.lower()
    assert "BLOCKED_GSUS" not in msg


def test_unknown_error_falls_back_to_generic_message():
    msg = friendly_message(ValueError("algo interno qualquer"))
    assert "algo interno qualquer" not in msg
    assert msg.strip() != ""


def test_pseudonym_is_stable_and_hides_identifier():
    from app.security import pseudonym

    label = pseudonym.for_log("262531")
    assert "262531" not in label
    assert label == pseudonym.for_log("262531")  # determinístico
    assert label != pseudonym.for_log("262532")  # distingue pacientes
    assert pseudonym.for_log(None) == "(sem-id)"
    assert pseudonym.for_log("") == "(sem-id)"
