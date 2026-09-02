"""Testa app/analysis/model_downloader.py (BUILD-001) -- nunca bate na rede
de verdade, sempre mocka urllib.request.urlopen."""
import urllib.error

import pytest

from app.analysis.model_downloader import ModelDownloadError, ensure_model_downloaded


class FakeResponse:
    def __init__(self, chunks: list[bytes], total_bytes: int | None = None):
        self._chunks = list(chunks)
        total = total_bytes if total_bytes is not None else sum(len(c) for c in chunks)
        self.headers = {"Content-Length": str(total)}

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def test_skips_download_when_file_already_exists(tmp_path, monkeypatch):
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"ja existe")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("não deveria acessar a rede -- arquivo já existe")

    monkeypatch.setattr("urllib.request.urlopen", fail_if_called)

    ensure_model_downloaded(model_path)

    assert model_path.read_bytes() == b"ja existe"


def test_downloads_and_writes_file_content(tmp_path, monkeypatch):
    model_path = tmp_path / "subpasta" / "model.gguf"
    chunks = [b"parte-um-", b"parte-dois"]
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse(chunks))

    ensure_model_downloaded(model_path)

    assert model_path.exists()
    assert model_path.read_bytes() == b"parte-um-parte-dois"
    assert not model_path.with_name(model_path.name + ".part").exists()


def test_reports_progress_percentage(tmp_path, monkeypatch):
    model_path = tmp_path / "model.gguf"
    chunks = [b"a" * 25, b"b" * 25, b"c" * 25, b"d" * 25]
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse(chunks, total_bytes=100))

    messages = []
    ensure_model_downloaded(model_path, progress=messages.append)

    assert len(messages) >= 1
    assert "100%" in messages[-1]
    assert all("Baixando modelo" in m for m in messages)


def test_raises_model_download_error_and_cleans_up_on_network_failure(tmp_path, monkeypatch):
    model_path = tmp_path / "model.gguf"

    def raise_url_error(*args, **kwargs):
        raise urllib.error.URLError("sem conexão")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)

    with pytest.raises(ModelDownloadError):
        ensure_model_downloaded(model_path)

    assert not model_path.exists()
    assert not model_path.with_name(model_path.name + ".part").exists()


def test_does_not_leave_partial_file_when_write_fails_midway(tmp_path, monkeypatch):
    model_path = tmp_path / "model.gguf"

    class BrokenResponse(FakeResponse):
        def read(self, _size: int) -> bytes:
            raise OSError("disco cheio")

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: BrokenResponse([b"x"]))

    with pytest.raises(ModelDownloadError):
        ensure_model_downloaded(model_path)

    assert not model_path.exists()
    assert not model_path.with_name(model_path.name + ".part").exists()
