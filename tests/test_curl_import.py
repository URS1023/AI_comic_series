"""Tests for secret-safe browser Copy-as-cURL import and preflight."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_comic_series import session
from ai_comic_series.config import RemoteSettings
from ai_comic_series.curl_import import CURL_END_MARKER, parse_copy_as_curl, read_framed_curl_stdin
from ai_comic_series.exceptions import ConfigurationError, RemoteProtocolError
from ai_comic_series.jupyter import JupyterClient


def copied_curl(*, url: str | None = None, referer_instance: str = "u-4389-46ab5765") -> str:
    target = url or (
        "https://developer.amd.com.cn/radeon/instances/u-4389-46ab5765/"
        "api/contents/README.md?type=file&content=1"
    )
    return f"""curl --url '{target}' \\
  -H 'accept: */*' \\
  -H 'cache-control: no-cache' \\
  --header 'accept-language: zh-CN,zh;q=0.9,en;q=0.8' \\
  -H 'authorization: token oneclick-test' \\
  -b 'session=session-value; _xsrf=xsrf-value; sso_refresh_token_prod=refresh-value' \\
  -H 'referer: https://developer.amd.com.cn/radeon/instances/{referer_instance}/lab/tree/README.md' \\
  -H 'user-agent: Browser Test/1.0' \\
  -H 'x-xsrftoken: xsrf-value'"""


def test_copy_as_curl_import_extracts_full_browser_context_without_repr_leak() -> None:
    imported = parse_copy_as_curl(copied_curl())

    assert imported.base_url == "https://developer.amd.com.cn/radeon/instances/u-4389-46ab5765"
    assert imported.request_url.endswith("README.md?type=file&content=1")
    headers = imported.credentials.http_headers()
    assert headers["Authorization"] == "token oneclick-test"
    assert headers["Cookie"].startswith("session=session-value")
    assert headers["X-XSRFToken"] == "xsrf-value"
    assert headers["User-Agent"] == "Browser Test/1.0"
    assert headers["Referer"].endswith("/lab/tree/README.md")
    assert headers["Accept-Language"] == "zh-CN,zh;q=0.9,en;q=0.8"
    assert headers["Accept"] == "*/*"
    assert imported.extra_headers == (("cache-control", "no-cache"),)
    representation = f"{imported!r} {imported.credentials!r}"
    for secret in ("oneclick-test", "session-value", "xsrf-value", "refresh-value", "Browser Test/1.0"):
        assert secret not in representation


def test_copy_as_curl_accepts_long_cookie_option() -> None:
    imported = parse_copy_as_curl(copied_curl().replace("  -b '", "  --cookie '", 1))

    assert imported.credentials.http_headers()["Cookie"].startswith("session=session-value")


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (copied_curl(url="http://developer.amd.com.cn/radeon/instances/u-1-demo/api/contents"), "HTTPS"),
        (copied_curl(url="https://example.test/radeon/instances/u-1-demo/api/contents"), "official AMD"),
        (copied_curl(url="https://developer.amd.com.cn/radeon/not-an-instance/api/contents"), "instance path"),
        (copied_curl(url="https://developer.amd.com.cn/radeon/instances/u-1-demo/api/kernels"), "Contents API"),
        (copied_curl(referer_instance="u-9999-other"), "same AMD instance"),
        (copied_curl().replace("xsrf-value'", "different-value'", 1), "does not match"),
        (copied_curl() + " --data 'not-allowed'", "body-free GET"),
        (copied_curl() + " --request POST", "must use GET"),
    ],
)
def test_copy_as_curl_strictly_rejects_unsafe_or_ambiguous_requests(command: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_copy_as_curl(command)


def test_framed_stdin_reader_leaves_json_commands_for_long_lived_session() -> None:
    stream = io.StringIO(f"{copied_curl()}\n{CURL_END_MARKER}\n{{\"action\":\"probe\"}}\n")

    command = read_framed_curl_stdin(stream)

    assert parse_copy_as_curl(command).base_url.endswith("/u-4389-46ab5765")
    assert stream.readline().strip() == '{"action":"probe"}'


def test_exact_preflight_is_a_non_redirecting_get(monkeypatch: pytest.MonkeyPatch) -> None:
    imported = parse_copy_as_curl(copied_curl())
    settings = RemoteSettings(imported.base_url, "project", "/data", "python3", 10, 10)
    client = JupyterClient(settings, imported.credentials)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
        calls.append((method, path, kwargs))
        return httpx.Response(200, headers={"content-type": "application/json; charset=utf-8"})

    monkeypatch.setattr(client, "_request", request)
    try:
        result = client.preflight_exact_get(imported.request_url, imported.extra_headers)
    finally:
        client.close()

    assert result == {"status": 200, "contentType": "application/json"}
    assert calls == [
        (
            "GET",
            imported.request_url,
            {"headers": {"cache-control": "no-cache"}, "follow_redirects": False},
        )
    ]


def test_preflight_transport_error_does_not_echo_curl_url(monkeypatch: pytest.MonkeyPatch) -> None:
    imported = parse_copy_as_curl(copied_curl())
    settings = RemoteSettings(imported.base_url, "project", "/data", "python3", 10, 10)
    client = JupyterClient(settings, imported.credentials)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteProtocolError(f"failed {imported.request_url}")),
    )

    try:
        with pytest.raises(RemoteProtocolError) as caught:
            client.preflight_exact_get(imported.request_url, imported.extra_headers)
    finally:
        client.close()

    assert imported.request_url not in str(caught.value)
    assert "type=file" not in str(caught.value)


def test_session_preflights_before_constructing_long_lived_manager(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []

    class FakeClient:
        def __init__(self, *_args: object) -> None:
            events.append("client")

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def preflight_exact_get(
            self,
            _url: str,
            _extra_headers: tuple[tuple[str, str], ...],
        ) -> dict[str, object]:
            events.append("preflight")
            return {"status": 200, "contentType": "application/json"}

    class FakeManager:
        def __init__(self, *_args: object) -> None:
            events.append("manager")

    stdin = io.StringIO(f"{copied_curl()}\n{CURL_END_MARKER}\n{{\"action\":\"quit\"}}\n")
    monkeypatch.setattr(session, "JupyterClient", FakeClient)
    monkeypatch.setattr(session, "RemoteManager", FakeManager)
    monkeypatch.setattr(session.sys, "stdin", stdin)

    exit_code = session.main(["--curl-stdin", str(Path("config/project.toml"))])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert events == ["client", "preflight", "manager"]
    assert json.loads(output.splitlines()[0])["preflight"]["status"] == 200
    assert "session-value" not in output
    assert "oneclick-test" not in output


def test_powershell_launchers_pass_complete_curl_only_through_stdin() -> None:
    one_shot = Path("scripts/remote.ps1").read_text(encoding="utf-8")
    long_lived = Path("scripts/remote-session.ps1").read_text(encoding="utf-8")

    assert "--curl-stdin" in one_shot
    assert "$curlCommand | & $python" in one_shot
    assert "AI_COMIC_JUPYTER_COOKIE" not in one_shot
    assert "$start.RedirectStandardInput = $true" in long_lived
    assert "__AI_COMIC_CURL_END__" in long_lived
    assert "$start.ArgumentList.Add('--curl-stdin')" in long_lived
    assert "AI_COMIC_JUPYTER_COOKIE" not in long_lived
