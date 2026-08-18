"""In-memory AMD session refresh without secret persistence."""

from __future__ import annotations

import json
from collections import OrderedDict

import httpx

from ai_comic_series.config import RemoteCredentials, RemoteSettings
from ai_comic_series.exceptions import AuthenticationError, RemoteProtocolError

__all__ = ["merge_cookie_header", "parse_cookie_header", "refresh_amd_credentials"]


def parse_cookie_header(header: str) -> OrderedDict[str, str]:
    """Parse a browser Cookie header while preserving order and quoted values."""

    cookies: OrderedDict[str, str] = OrderedDict()
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            cookies[name] = value
    return cookies


def merge_cookie_header(original: str, updates: dict[str, str]) -> str:
    """Merge rotated response cookies into an existing browser Cookie header."""

    cookies = parse_cookie_header(original)
    for name, value in updates.items():
        if value:
            cookies[name] = value
        else:
            cookies.pop(name, None)
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def refresh_amd_credentials(
    settings: RemoteSettings,
    credentials: RemoteCredentials,
    *,
    required: bool,
) -> RemoteCredentials:
    """Refresh AMD SSO cookies in memory using the site's own refresh endpoint.

    Args:
        settings: Non-secret Jupyter target settings.
        credentials: Current process-local browser cookies.
        required: Raise when the refresh token is invalid instead of retaining a
            still-valid access token.

    Returns:
        A new secret-safe credential value. Nothing is written to disk.

    Raises:
        AuthenticationError: If refresh is required and the login is no longer valid.
        RemoteProtocolError: If the refresh endpoint returns malformed data.
    """

    if "sso_refresh_token_prod=" not in credentials.cookie:
        if required:
            raise AuthenticationError("The Cookie header has no AMD SSO refresh token; sign in again and recopy it.")
        return credentials
    url = f"{settings.origin}/api/api/Auth/Refresh"
    headers = credentials.http_headers()
    headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": settings.origin,
            "Referer": credentials.referer or f"{settings.origin}/",
        }
    )
    try:
        response = httpx.post(url, headers=headers, content=b"{}", timeout=settings.request_timeout_seconds)
    except httpx.HTTPError as error:
        if required:
            raise AuthenticationError(f"AMD SSO refresh request failed: {error}") from error
        return credentials
    if response.status_code != 200:
        if required:
            raise AuthenticationError(f"AMD SSO refresh returned HTTP {response.status_code}; sign in again.")
        return credentials
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise RemoteProtocolError("AMD SSO refresh returned invalid JSON") from error
    if not isinstance(payload, dict) or payload.get("Status") != 1:
        if required:
            message = (
                payload.get("Message", "refresh token is invalid") if isinstance(payload, dict) else "invalid response"
            )
            raise AuthenticationError(f"AMD SSO refresh failed: {message}. Sign in again and recopy the Cookie header.")
        return credentials
    updates = dict(response.cookies.items())
    if not updates:
        raise RemoteProtocolError("AMD SSO refresh succeeded without returning rotated cookies")
    cookie = merge_cookie_header(credentials.cookie, updates)
    xsrf = updates.get("_xsrf", credentials.xsrf_token)
    return RemoteCredentials(
        cookie=cookie,
        token=credentials.token,
        xsrf_token=xsrf,
        user_agent=credentials.user_agent,
        referer=credentials.referer,
        accept_language=credentials.accept_language,
        accept=credentials.accept,
    )
