"""Secret-safe parser for browser Copy-as-cURL AMD Jupyter requests."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field, replace
from typing import TextIO
from urllib.parse import urlsplit, urlunsplit

from ai_comic_series.auth import parse_cookie_header
from ai_comic_series.config import ProjectSettings, RemoteCredentials
from ai_comic_series.exceptions import ConfigurationError

__all__ = [
    "CURL_END_MARKER",
    "ImportedCurl",
    "apply_imported_target",
    "parse_copy_as_curl",
    "read_curl_stdin",
    "read_framed_curl_stdin",
]

CURL_END_MARKER = "__AI_COMIC_CURL_END__"
_MAX_CURL_CHARACTERS = 1_048_576
_AMD_HOST = "developer.amd.com.cn"
_INSTANCE_PATH = re.compile(r"^/radeon/instances/(u-[1-9][0-9]*-[A-Za-z0-9][A-Za-z0-9-]*)(/.*)?$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


@dataclass(frozen=True, slots=True)
class ImportedCurl:
    """Validated GET preflight plus process-local headers from Copy-as-cURL."""

    base_url: str
    request_url: str = field(repr=False)
    credentials: RemoteCredentials = field(repr=False)
    extra_headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)


def _read_bounded(value: str) -> str:
    if not value.strip():
        raise ConfigurationError("Copy-as-cURL input is empty")
    if len(value) > _MAX_CURL_CHARACTERS:
        raise ConfigurationError("Copy-as-cURL input exceeds the one MiB safety limit")
    if "\0" in value:
        raise ConfigurationError("Copy-as-cURL input contains a forbidden NUL character")
    return value


def read_curl_stdin(stream: TextIO) -> str:
    """Read one bounded cURL command from stdin without echoing or persisting it."""

    return _read_bounded(stream.read(_MAX_CURL_CHARACTERS + 1))


def read_framed_curl_stdin(stream: TextIO) -> str:
    """Read a multiline cURL prelude terminated by the public framing marker."""

    lines: list[str] = []
    characters = 0
    for line in stream:
        if line.rstrip("\r\n") == CURL_END_MARKER:
            return _read_bounded("".join(lines))
        characters += len(line)
        if characters > _MAX_CURL_CHARACTERS:
            raise ConfigurationError("Copy-as-cURL input exceeds the one MiB safety limit")
        lines.append(line)
    raise ConfigurationError("Copy-as-cURL stdin ended before the framing marker")


def _shell_tokens(command: str) -> list[str]:
    normalized = re.sub(r"\\\r?\n", " ", _read_bounded(command))
    normalized = re.sub(r"`\r?\n", " ", normalized)
    normalized = re.sub(r"\^\r?\n", " ", normalized)
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as error:
        raise ConfigurationError("Copy-as-cURL has invalid shell quoting") from error
    if not tokens or tokens[0].lower() not in {"curl", "curl.exe"}:
        raise ConfigurationError("Input must begin with curl or curl.exe")
    return tokens


def _consume_value(tokens: list[str], index: int, option: str) -> tuple[str, int]:
    if index + 1 >= len(tokens):
        raise ConfigurationError(f"Copy-as-cURL option {option} is missing its value")
    return tokens[index + 1], index + 2


def _parse_arguments(tokens: list[str]) -> tuple[str, list[str], str | None]:
    urls: list[str] = []
    raw_headers: list[str] = []
    cookie_option: str | None = None
    method = "GET"
    harmless_flags = {"--compressed", "--silent", "-s", "--show-error", "-S", "--fail", "-f", "--globoff", "-g"}
    forbidden_prefixes = ("--data", "--form", "--upload-file", "--json", "-d", "-F", "-T")
    index = 1
    while index < len(tokens):
        argument = tokens[index]
        if argument == "--url":
            value, index = _consume_value(tokens, index, "--url")
            urls.append(value)
        elif argument.startswith("--url="):
            urls.append(argument.partition("=")[2])
            index += 1
        elif argument in {"-H", "--header"}:
            value, index = _consume_value(tokens, index, argument)
            raw_headers.append(value)
        elif argument.startswith("--header="):
            raw_headers.append(argument.partition("=")[2])
            index += 1
        elif argument.startswith("-H") and len(argument) > 2:
            raw_headers.append(argument[2:])
            index += 1
        elif argument in {"-b", "--cookie"}:
            value, index = _consume_value(tokens, index, argument)
            if cookie_option is not None:
                raise ConfigurationError("Copy-as-cURL contains more than one Cookie option")
            cookie_option = value
        elif argument.startswith("--cookie="):
            if cookie_option is not None:
                raise ConfigurationError("Copy-as-cURL contains more than one Cookie option")
            cookie_option = argument.partition("=")[2]
            index += 1
        elif argument.startswith("-b") and len(argument) > 2:
            if cookie_option is not None:
                raise ConfigurationError("Copy-as-cURL contains more than one Cookie option")
            cookie_option = argument[2:]
            index += 1
        elif argument in {"-X", "--request"}:
            method, index = _consume_value(tokens, index, argument)
        elif argument.startswith("--request="):
            method = argument.partition("=")[2]
            index += 1
        elif argument in harmless_flags:
            index += 1
        elif argument in {"-k", "--insecure", "-L", "--location", "--location-trusted"}:
            raise ConfigurationError("Copy-as-cURL may not disable TLS checks or follow redirects")
        elif argument.startswith(forbidden_prefixes):
            raise ConfigurationError("Copy-as-cURL must describe a body-free GET request")
        elif argument.startswith("-"):
            option = argument.partition("=")[0]
            raise ConfigurationError(f"Copy-as-cURL contains unsupported option {option}")
        else:
            urls.append(argument)
            index += 1
    if method.upper() != "GET":
        raise ConfigurationError("Copy-as-cURL must use GET")
    if len(urls) != 1:
        raise ConfigurationError("Copy-as-cURL must contain exactly one URL")
    return urls[0], raw_headers, cookie_option


def _headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers:
        name, separator, value = raw.partition(":")
        normalized = name.strip().lower()
        if not separator or not _HEADER_NAME.fullmatch(name.strip()):
            raise ConfigurationError("Copy-as-cURL contains a malformed header")
        if normalized in headers:
            raise ConfigurationError(f"Copy-as-cURL repeats the {name.strip()} header")
        if normalized in {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}:
            raise ConfigurationError(f"Copy-as-cURL may not override the {name.strip()} header")
        header_value = value.strip()
        if any(character in header_value for character in ("\r", "\n", "\0")):
            raise ConfigurationError(f"Copy-as-cURL {name.strip()} header is not a safe single value")
        headers[normalized] = header_value
    return headers


def _validate_target(request_url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(request_url)
    except ValueError as error:
        raise ConfigurationError("Copy-as-cURL URL is malformed") from error
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigurationError("Copy-as-cURL URL contains an invalid port") from error
    if parsed.scheme.lower() != "https":
        raise ConfigurationError("Copy-as-cURL URL must use HTTPS")
    if parsed.username or parsed.password or parsed.hostname != _AMD_HOST or port not in {None, 443}:
        raise ConfigurationError("Copy-as-cURL URL must target the official AMD developer host")
    if parsed.fragment:
        raise ConfigurationError("Copy-as-cURL URL must not contain a fragment")
    match = _INSTANCE_PATH.fullmatch(parsed.path)
    if not match:
        raise ConfigurationError("Copy-as-cURL URL must contain a valid /radeon/instances/u-… instance path")
    suffix = match.group(2) or ""
    if suffix != "/api/contents" and not suffix.startswith("/api/contents/"):
        raise ConfigurationError("Copy-as-cURL preflight must target the read-only Jupyter Contents API")
    base_path = f"/radeon/instances/{match.group(1)}"
    base_url = urlunsplit(("https", _AMD_HOST, base_path, "", ""))
    return base_url, base_path


def _validate_referer(value: str, base_path: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ConfigurationError("Copy-as-cURL Referer is malformed") from error
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigurationError("Copy-as-cURL Referer contains an invalid port") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != _AMD_HOST
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or (parsed.path != base_path and not parsed.path.startswith(f"{base_path}/"))
    ):
        raise ConfigurationError("Copy-as-cURL Referer must belong to the same AMD instance")


def parse_copy_as_curl(command: str) -> ImportedCurl:
    """Parse and strictly validate one browser Copy-as-cURL GET request."""

    request_url, raw_headers, cookie_option = _parse_arguments(_shell_tokens(command))
    headers = _headers(raw_headers)
    base_url, base_path = _validate_target(request_url)

    cookie_header = headers.get("cookie")
    if cookie_option and cookie_header and cookie_option != cookie_header:
        raise ConfigurationError("Copy-as-cURL Cookie option conflicts with its Cookie header")
    cookie = cookie_option or cookie_header or ""
    required = {
        "authorization": "Authorization",
        "user-agent": "User-Agent",
        "referer": "Referer",
        "accept-language": "Accept-Language",
        "accept": "Accept",
        "x-xsrftoken": "X-XSRFToken",
    }
    missing = [display for key, display in required.items() if not headers.get(key)]
    if not cookie:
        missing.append("Cookie")
    if missing:
        raise ConfigurationError(f"Copy-as-cURL is missing required browser header(s): {', '.join(sorted(missing))}")

    authorization = headers["authorization"].split(None, 1)
    if len(authorization) != 2 or authorization[0].lower() != "token" or not authorization[1].strip():
        raise ConfigurationError("Copy-as-cURL Authorization must use the Jupyter token scheme")
    _validate_referer(headers["referer"], base_path)
    cookies = parse_cookie_header(cookie)
    if not {"session", "_xsrf", "sso_refresh_token_prod"}.issubset(cookies):
        raise ConfigurationError("Copy-as-cURL Cookie lacks the required AMD session, XSRF, or refresh cookie")
    xsrf = headers["x-xsrftoken"]
    if cookies["_xsrf"].strip('"') != xsrf:
        raise ConfigurationError("Copy-as-cURL X-XSRFToken does not match the Cookie value")

    credentials = RemoteCredentials(
        cookie=cookie,
        token=authorization[1].strip(),
        xsrf_token=xsrf,
        user_agent=headers["user-agent"],
        referer=headers["referer"],
        accept_language=headers["accept-language"],
        accept=headers["accept"],
    )
    credential_headers = {
        "authorization",
        "cookie",
        "user-agent",
        "referer",
        "accept-language",
        "accept",
        "x-xsrftoken",
    }
    extra_headers = tuple((name, value) for name, value in headers.items() if name not in credential_headers)
    return ImportedCurl(
        base_url=base_url,
        request_url=request_url,
        credentials=credentials,
        extra_headers=extra_headers,
    )


def apply_imported_target(settings: ProjectSettings, imported: ImportedCurl) -> ProjectSettings:
    """Use the validated instance URL while retaining local project settings."""

    return replace(settings, remote=replace(settings.remote, base_url=imported.base_url))
