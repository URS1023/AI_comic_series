"""Tests for in-memory cookie rotation helpers."""

from __future__ import annotations

import httpx
import pytest

from ai_comic_series.auth import merge_cookie_header, parse_cookie_header, refresh_amd_credentials
from ai_comic_series.config import RemoteCredentials, RemoteSettings


def test_cookie_parser_preserves_quoted_signed_values() -> None:
    header = 'session=abc.def; username="2|signed|value"; _xsrf=token'

    cookies = parse_cookie_header(header)

    assert cookies["session"] == "abc.def"
    assert cookies["username"] == '"2|signed|value"'
    assert cookies["_xsrf"] == "token"


def test_cookie_merge_rotates_tokens_without_dropping_instance_cookie() -> None:
    original = "username=signed; sso_access_token_prod=old; sso_refresh_token_prod=refresh; session=value"

    merged = merge_cookie_header(
        original,
        {"sso_access_token_prod": "new", "sso_refresh_token_prod": "new-refresh"},
    )

    assert merged == ("username=signed; sso_access_token_prod=new; sso_refresh_token_prod=new-refresh; session=value")


def test_refresh_preserves_imported_browser_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = RemoteSettings("https://developer.amd.com.cn/radeon/instances/u-1-demo", "project", "", "python3", 10, 10)
    credentials = RemoteCredentials(
        cookie="session=value; _xsrf=old-xsrf; sso_refresh_token_prod=refresh-value",
        token="oneclick-test",
        xsrf_token="old-xsrf",
        user_agent="Browser Test/1.0",
        referer="https://developer.amd.com.cn/radeon/instances/u-1-demo/lab/tree/README.md",
        accept_language="zh-CN",
        accept="*/*",
    )
    response = httpx.Response(
        200,
        json={"Status": 1},
        request=httpx.Request("POST", "https://developer.amd.com.cn/api/api/Auth/Refresh"),
        headers=[
            ("set-cookie", "_xsrf=new-xsrf; Path=/"),
            ("set-cookie", "sso_refresh_token_prod=new-refresh; Path=/"),
        ],
    )
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: response)

    refreshed = refresh_amd_credentials(settings, credentials, required=True)

    assert refreshed.xsrf_token == "new-xsrf"
    assert refreshed.user_agent == credentials.user_agent
    assert refreshed.referer == credentials.referer
    assert refreshed.accept_language == credentials.accept_language
    assert refreshed.accept == credentials.accept
