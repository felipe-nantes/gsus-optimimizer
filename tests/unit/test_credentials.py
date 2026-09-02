import sys

import pytest

from app.security import credentials

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager só existe no Windows")

TEST_TARGET = "test-scaffold"


@pytest.fixture(autouse=True)
def cleanup_test_credential():
    yield
    try:
        credentials.delete_credential(TEST_TARGET)
    except credentials.CredentialError:
        pass


def test_save_and_get_credential_roundtrip():
    credentials.save_credential(TEST_TARGET, "usuario.teste", "senha-super-secreta")

    result = credentials.get_credential(TEST_TARGET)

    assert result is not None
    username, password = result
    assert username == "usuario.teste"
    assert password == "senha-super-secreta"


def test_get_credential_missing_returns_none():
    credentials.delete_credential(TEST_TARGET)
    assert credentials.get_credential(TEST_TARGET) is None


def test_save_credential_overwrites_previous_value():
    credentials.save_credential(TEST_TARGET, "usuario.um", "senha-um")
    credentials.save_credential(TEST_TARGET, "usuario.dois", "senha-dois")

    username, password = credentials.get_credential(TEST_TARGET)
    assert username == "usuario.dois"
    assert password == "senha-dois"


def test_delete_credential_is_idempotent():
    credentials.delete_credential(TEST_TARGET)
    credentials.delete_credential(TEST_TARGET)  # segunda chamada não deve levantar erro
