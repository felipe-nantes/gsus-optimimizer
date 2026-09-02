"""Armazenamento de credenciais via Windows Credential Manager.

Usa a API nativa do Windows (advapi32.dll: CredWriteW/CredReadW/CredDeleteW)
atraves de ctypes -- sem dependencia de terceiros (ver DECISIONS.md DEC-002).

A senha NUNCA e' escrita em config.json, SQLite ou log. Este modulo e' a
unica fronteira de leitura/escrita de credenciais da aplicacao.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
DEFAULT_TARGET_PREFIX = "GSUSAuditoria"


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class CredentialError(Exception):
    """Erro ao acessar o Windows Credential Manager. Detalhe fica em log, nao na UI."""


def _advapi32():
    if not hasattr(ctypes, "WinDLL"):
        raise CredentialError("Windows Credential Manager só está disponível no Windows.")
    # use_last_error=True é necessário para que ctypes.get_last_error() reflita
    # o GetLastError() real da chamada Win32 (ctypes.windll não rastreia isso).
    return ctypes.WinDLL("advapi32", use_last_error=True)


def _target_name(name: str) -> str:
    return f"{DEFAULT_TARGET_PREFIX}:{name}"


def save_credential(name: str, username: str, password: str) -> None:
    """Salva/atualiza uma credencial (ex.: name='gsus')."""
    advapi32 = _advapi32()
    blob = password.encode("utf-16-le")
    blob_buffer = ctypes.create_string_buffer(blob, len(blob))

    cred = _CREDENTIAL()
    cred.Flags = 0
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = _target_name(name)
    cred.Comment = None
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_char))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.AttributeCount = 0
    cred.Attributes = None
    cred.TargetAlias = None
    cred.UserName = username

    ok = advapi32.CredWriteW(ctypes.byref(cred), 0)
    if not ok:
        raise CredentialError(f"CredWriteW falhou (erro {ctypes.get_last_error()})")


def get_credential(name: str) -> tuple[str, str] | None:
    """Retorna (username, password) ou None se não existir."""
    advapi32 = _advapi32()
    cred_ptr = ctypes.POINTER(_CREDENTIAL)()
    ok = advapi32.CredReadW(_target_name(name), CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr))
    if not ok:
        return None
    try:
        cred = cred_ptr.contents
        blob_size = cred.CredentialBlobSize
        blob = ctypes.string_at(cred.CredentialBlob, blob_size)
        password = blob.decode("utf-16-le")
        username = cred.UserName or ""
        return username, password
    finally:
        advapi32.CredFree(cred_ptr)


def delete_credential(name: str) -> None:
    advapi32 = _advapi32()
    ok = advapi32.CredDeleteW(_target_name(name), CRED_TYPE_GENERIC, 0)
    if not ok:
        error = ctypes.get_last_error()
        if error != 1168:  # ERROR_NOT_FOUND -- idempotente, não é erro de negócio
            raise CredentialError(f"CredDeleteW falhou (erro {error})")
