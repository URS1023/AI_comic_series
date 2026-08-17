"""Typed, secret-safe project configuration."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ai_comic_series.exceptions import ConfigurationError

__all__ = ["ProjectSettings", "RemoteCredentials", "RemoteSettings", "load_project_settings"]


def _normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError("remote.base_url must be an absolute HTTPS URL")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("remote.base_url must not contain a query string or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _cookie_value(cookie: str, name: str) -> str | None:
    for part in cookie.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value.strip().strip('"')
    return None


@dataclass(frozen=True, slots=True)
class RemoteCredentials:
    """Process-local Jupyter credentials whose representation never reveals secrets."""

    cookie: str = field(repr=False)
    token: str = field(default="amd-oneclick", repr=False)
    xsrf_token: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls) -> RemoteCredentials:
        """Load credentials from the child process environment.

        Raises:
            ConfigurationError: If no cookie is present or it contains a newline.
        """

        cookie = os.environ.get("AI_COMIC_JUPYTER_COOKIE", "").strip()
        if not cookie:
            raise ConfigurationError(
                "AI_COMIC_JUPYTER_COOKIE is missing. Use scripts/remote.ps1 so the cookie is read with hidden input."
            )
        if "\n" in cookie or "\r" in cookie:
            raise ConfigurationError("AI_COMIC_JUPYTER_COOKIE must be a single HTTP Cookie header value")
        token = os.environ.get("AI_COMIC_JUPYTER_TOKEN", "amd-oneclick").strip()
        if not token:
            raise ConfigurationError("AI_COMIC_JUPYTER_TOKEN must not be empty")
        xsrf = os.environ.get("AI_COMIC_JUPYTER_XSRF", "").strip() or _cookie_value(cookie, "_xsrf") or ""
        return cls(cookie=cookie, token=token, xsrf_token=xsrf)


@dataclass(frozen=True, slots=True)
class RemoteSettings:
    """Non-secret settings for the AMD Jupyter execution target."""

    base_url: str
    remote_root: str
    data_root: str
    kernel_name: str
    request_timeout_seconds: float
    execution_timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _normalize_base_url(self.base_url))
        if not self.remote_root or self.remote_root.startswith(("/", "\\")) or ".." in Path(self.remote_root).parts:
            raise ConfigurationError("remote.remote_root must be a safe Jupyter-relative path")
        if self.data_root and not self.data_root.startswith("/"):
            raise ConfigurationError("remote.data_root must be blank or an absolute Linux path")
        if self.request_timeout_seconds <= 0 or self.execution_timeout_seconds <= 0:
            raise ConfigurationError("remote timeout values must be positive")

    @property
    def origin(self) -> str:
        """Return the HTTPS origin used by authenticated WebSocket requests."""

        parsed = urlsplit(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    """Complete local project settings used by the control CLI."""

    root: Path
    project_id: str
    title: str
    episode: str
    remote: RemoteSettings
    raw: dict[str, object]


def load_project_settings(path: Path) -> ProjectSettings:
    """Parse and validate ``config/project.toml``."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise ConfigurationError(f"Project configuration does not exist: {resolved}")
    with resolved.open("rb") as handle:
        raw = tomllib.load(handle)
    try:
        project = raw["project"]
        remote = raw["remote"]
        assert isinstance(project, dict)
        assert isinstance(remote, dict)
        remote_settings = RemoteSettings(
            base_url=str(remote["base_url"]),
            remote_root=str(remote.get("remote_root", "ai-comic-series")),
            data_root=str(remote.get("data_root", "")),
            kernel_name=str(remote.get("kernel_name", "python3")),
            request_timeout_seconds=float(remote.get("request_timeout_seconds", 30)),
            execution_timeout_seconds=float(remote.get("execution_timeout_seconds", 120)),
        )
        return ProjectSettings(
            root=resolved.parent.parent,
            project_id=str(project["id"]),
            title=str(project["title"]),
            episode=str(project["episode"]),
            remote=remote_settings,
            raw=raw,
        )
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        raise ConfigurationError(f"Invalid project configuration {resolved}: {error}") from error
