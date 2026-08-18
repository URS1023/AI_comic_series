"""Secret-isolation tests for the Quick Tunnel supervisor."""

from __future__ import annotations

import hashlib
import io
import os
import ssl
import urllib.error
from pathlib import Path

import pytest

from remote import start_direct_gateway
from remote.start_direct_gateway import (
    child_environments,
    cloudflared_command,
    gateway_command,
    serveo_command,
    tunnelmole_command,
)


def test_supervisor_keeps_bearer_out_of_both_command_lines_and_cloudflared_environment(tmp_path: Path) -> None:
    token = "supervisor-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    python = tmp_path / "python"
    cloudflared = tmp_path / "cloudflared"
    root = tmp_path / "project"
    (root / "remote").mkdir(parents=True)
    gateway_environment, cloudflared_environment = child_environments(
        {
            "PATH": "/usr/bin",
            "AI_COMIC_DIRECT_TOKEN": token,
            "AI_COMIC_JUPYTER_COOKIE": "secret-cookie",
            "AI_COMIC_JUPYTER_TOKEN": "secret-jupyter-token",
        }
    )
    gateway = gateway_command(python, root, 8765)
    tunnel = cloudflared_command(cloudflared, 8765)

    assert gateway_environment["AI_COMIC_DIRECT_TOKEN"] == token
    assert all(not name.startswith("AI_COMIC_JUPYTER_") for name in gateway_environment)
    assert "AI_COMIC_DIRECT_TOKEN" not in cloudflared_environment
    assert all(not name.startswith("AI_COMIC_JUPYTER_") for name in cloudflared_environment)
    assert token not in " ".join(gateway)
    assert token not in " ".join(tunnel)
    assert "--token" not in tunnel
    assert tunnel[-1] == "http://127.0.0.1:8765"
    serveo = serveo_command(tmp_path / "ssh", 8765)
    assert serveo[-3:] == ["-R", "80:127.0.0.1:8765", "serveo.net"]
    assert token not in " ".join(serveo)
    assert tunnelmole_command(tmp_path / "tmole", 8765)[-1] == "8765"


def test_supervisor_rejects_short_or_header_unsafe_token() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        child_environments({"AI_COMIC_DIRECT_TOKEN": "short"})
    with pytest.raises(ValueError, match="safe UTF-8"):
        child_environments({"AI_COMIC_DIRECT_TOKEN": "x" * 40 + "\n"})


def test_pinned_cloudflared_download_is_verified_and_atomically_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"pinned-cloudflared-test-binary"
    monkeypatch.setattr(start_direct_gateway, "CLOUDFLARED_SIZE", len(content))
    monkeypatch.setattr(start_direct_gateway, "CLOUDFLARED_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(
        start_direct_gateway.urllib.request,
        "urlopen",
        lambda _request, timeout: io.BytesIO(content),
    )

    binary = start_direct_gateway.install_cloudflared(tmp_path)

    assert binary == (tmp_path / "bin" / "cloudflared").resolve()
    assert binary.read_bytes() == content
    if os.name == "posix":
        assert binary.stat().st_mode & 0o111
    assert list(binary.parent.glob("*.download")) == []


def test_pinned_download_allows_only_certificate_failure_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def fake_urlopen(_request, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs.get("context"))
        if len(calls) == 1:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("self-signed proxy"))
        return io.BytesIO(b"verified-later-by-pinned-sha")

    monkeypatch.setattr(start_direct_gateway.urllib.request, "urlopen", fake_urlopen)

    response = start_direct_gateway._open_pinned_download(  # noqa: SLF001
        start_direct_gateway.urllib.request.Request(start_direct_gateway.CLOUDFLARED_URL)
    )

    assert response.read() == b"verified-later-by-pinned-sha"
    assert calls[0] is None
    assert isinstance(calls[1], ssl.SSLContext)
