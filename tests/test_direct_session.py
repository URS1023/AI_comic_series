"""Silent one-shot Jupyter bootstrap tests for the direct session handoff."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_comic_series.config import ProjectSettings, RemoteSettings
from ai_comic_series.direct_session import (
    DIRECT_MARKER,
    _launcher_code,
    _validated_tunnel_url,
    bootstrap_direct_manager,
)
from ai_comic_series.direct_transport import DirectCredentials, DirectTransport
from ai_comic_series.exceptions import RemoteExecutionError, RemoteProtocolError

TOKEN = "handoff-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TUNNEL = "https://silent-handoff-test.trycloudflare.com"


def settings(root: Path) -> ProjectSettings:
    return ProjectSettings(
        root=root,
        project_id="handoff-test",
        title="Handoff",
        episode="ep01",
        remote=RemoteSettings(
            base_url="https://developer.amd.com.cn/radeon/instances/test",
            remote_root="ai-comic-series",
            data_root="/ai-comic-series",
            kernel_name="python3",
            request_timeout_seconds=5,
            execution_timeout_seconds=5,
        ),
        raw={},
    )


@dataclass
class FakeExecution:
    stdout: str
    stderr: str = ""
    execute_count: int | None = None


class FakeJupyter:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.codes: list[str] = []

    def upload_bytes(self, path: str, content: bytes) -> None:
        self.uploads.append((path, content))

    def execute_python(self, code: str, timeout_seconds: float) -> FakeExecution:
        assert timeout_seconds == 360
        self.codes.append(code)
        return FakeExecution(DIRECT_MARKER + json.dumps({"pid": 1234, "publicUrl": TUNNEL}) + "\n")


class FakeDirectTransport:
    instances: list[FakeDirectTransport] = []

    def __init__(self, base_url: str, credentials: DirectCredentials, *, timeout_seconds: float) -> None:
        self.base_url = base_url
        self.token = credentials.token
        self.timeout_seconds = timeout_seconds
        self.closed = False
        self.instances.append(self)

    def read_status(self, job: str) -> dict[str, object]:
        assert job == "direct-gateway"
        return {"state": "running"}

    def close(self) -> None:
        self.closed = True


def test_launcher_cell_keeps_token_out_of_argv_and_files() -> None:
    code = _launcher_code("ai-comic-series", TOKEN)

    assert code.count(TOKEN) == 1
    assert "environment['AI_COMIC_DIRECT_TOKEN']" in code
    assert "environment.pop('AI_COMIC_DIRECT_TOKEN', None)" in code
    assert "--token" not in code
    assert "status/direct-gateway.json" in code


def test_bootstrap_uploads_only_gateway_files_and_never_prints_url_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ai_comic_series import direct_session

    for relative in direct_session.DIRECT_REMOTE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    fake_jupyter = FakeJupyter()
    FakeDirectTransport.instances.clear()
    monkeypatch.setattr(direct_session.secrets, "token_urlsafe", lambda _size: TOKEN)
    monkeypatch.setattr(direct_session, "DirectTransport", FakeDirectTransport)

    manager, owned_transport = bootstrap_direct_manager(
        settings(tmp_path),
        fake_jupyter,  # type: ignore[arg-type]
    )

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert [path for path, _ in fake_jupyter.uploads] == [
        "ai-comic-series/remote/direct_gateway.py",
        "ai-comic-series/remote/start_direct_gateway.py",
    ]
    assert len(fake_jupyter.codes) == 1
    assert FakeDirectTransport.instances[0].base_url == TUNNEL
    assert FakeDirectTransport.instances[0].token == TOKEN
    assert TUNNEL not in repr(manager)
    assert TOKEN not in repr(manager)
    owned_transport.close()
    assert FakeDirectTransport.instances[0].closed is True


@pytest.mark.parametrize(
    "value",
    [
        "http://bad.trycloudflare.com",
        "https://trycloudflare.com.evil.example",
        "https://user@bad.trycloudflare.com",
        "https://bad.trycloudflare.com/path",
        "https://bad.trycloudflare.com?token=leak",
        "https://bad.trycloudflare.com:443",
    ],
)
def test_tunnel_endpoint_validation_rejects_exfiltration_targets(value: str) -> None:
    with pytest.raises(RemoteProtocolError, match="invalid Quick Tunnel endpoint"):
        _validated_tunnel_url(value)


def test_direct_transport_network_error_and_repr_do_not_reveal_endpoint_or_token() -> None:
    endpoint = "http://127.0.0.1:1"
    client = DirectTransport(endpoint, DirectCredentials(TOKEN), timeout_seconds=0.1)
    try:
        with pytest.raises(RemoteExecutionError) as caught:
            client.read_status("bootstrap")
    finally:
        client.close()
    assert endpoint not in str(caught.value)
    assert endpoint not in repr(client)
    assert TOKEN not in repr(client)
