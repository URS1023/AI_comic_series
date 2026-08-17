"""Tests for in-memory cookie rotation helpers."""

from __future__ import annotations

from ai_comic_series.auth import merge_cookie_header, parse_cookie_header


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
