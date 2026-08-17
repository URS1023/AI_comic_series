"""Tests for typed configuration and secret handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_comic_series.config import RemoteCredentials, RemoteSettings, load_project_settings
from ai_comic_series.exceptions import ConfigurationError


def test_project_configuration_loads_repository_defaults() -> None:
    settings = load_project_settings(Path("config/project.toml"))

    assert settings.project_id == "gaokao-rewind-rainy-night"
    assert settings.remote.base_url.endswith("/u-4389-46ab5765")
    assert settings.remote.origin == "https://developer.amd.com.cn"


def test_remote_settings_reject_insecure_url() -> None:
    with pytest.raises(ConfigurationError, match="HTTPS"):
        RemoteSettings("http://example.test/instance", "project", "", "python3", 10, 10)


def test_credentials_parse_xsrf_without_revealing_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    cookie = "session=secret-session; _xsrf=xsrf-value; another=value"
    monkeypatch.setenv("AI_COMIC_JUPYTER_COOKIE", cookie)

    credentials = RemoteCredentials.from_environment()

    assert credentials.xsrf_token == "xsrf-value"
    assert cookie not in repr(credentials)
    assert "secret-session" not in repr(credentials)


def test_credentials_require_process_local_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_COMIC_JUPYTER_COOKIE", raising=False)

    with pytest.raises(ConfigurationError, match="scripts/remote.ps1"):
        RemoteCredentials.from_environment()
