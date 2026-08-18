"""One-shot Jupyter bootstrap followed by a bearer-authenticated direct session."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from ai_comic_series.config import RemoteCredentials, load_project_settings
from ai_comic_series.curl_import import apply_imported_target, parse_copy_as_curl, read_framed_curl_stdin
from ai_comic_series.direct_transport import DirectCredentials, DirectManager, DirectTransport
from ai_comic_series.exceptions import ComicSeriesError, RemoteExecutionError, RemoteProtocolError
from ai_comic_series.jupyter import JupyterClient
from ai_comic_series.remote_manager import RemoteManager
from ai_comic_series.session import dispatch

__all__ = ["bootstrap_direct_manager", "main"]

DIRECT_REMOTE_FILES = (
    "remote/direct_gateway.py",
    "remote/start_direct_gateway.py",
)
DIRECT_MARKER = "AI_COMIC_DIRECT_JSON="
TUNNEL_HOST_PATTERN = re.compile(r"^[a-z0-9-]+\.trycloudflare\.com$")


def _validated_tunnel_url(value: object) -> str:
    """Accept only the exact HTTPS origin shape emitted by Quick Tunnel."""

    parsed = urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError as error:
        raise RemoteProtocolError("Direct gateway readiness contains an invalid Quick Tunnel endpoint") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not TUNNEL_HOST_PATTERN.fullmatch(parsed.hostname)
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RemoteProtocolError("Direct gateway readiness contains an invalid Quick Tunnel endpoint")
    return urlunsplit(("https", parsed.hostname, "", "", ""))


def _launcher_code(remote_root: str, token: str) -> str:
    """Build one history-free kernel cell; the token never enters argv or a file."""

    return f"""
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path.cwd() / {remote_root!r}
status = root / 'status' / 'direct-gateway.json'
log_path = root / 'logs' / 'direct-gateway-bootstrap.log'
status.parent.mkdir(parents=True, exist_ok=True)
log_path.parent.mkdir(parents=True, exist_ok=True)
status.unlink(missing_ok=True)
environment = os.environ.copy()
environment['AI_COMIC_DIRECT_TOKEN'] = {token!r}
for name in list(environment):
    if name.startswith('AI_COMIC_JUPYTER_'):
        environment.pop(name, None)
script = root / 'remote' / 'start_direct_gateway.py'
with log_path.open('ab', buffering=0) as log:
    process = subprocess.Popen(
        [sys.executable, str(script), '--root', str(root), '--status', 'status/direct-gateway.json'],
        cwd=str(root),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
environment.pop('AI_COMIC_DIRECT_TOKEN', None)
deadline = time.monotonic() + 330
last = None
while time.monotonic() < deadline:
    if status.is_file():
        try:
            last = json.loads(status.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            last = None
        if isinstance(last, dict) and last.get('state') == 'running' and last.get('publicUrl'):
            print({DIRECT_MARKER!r} + json.dumps({{'pid': process.pid, 'publicUrl': last['publicUrl']}}))
            break
        if isinstance(last, dict) and last.get('state') == 'failed':
            raise RuntimeError('Direct gateway launcher reported failure; inspect its public log path')
    if process.poll() is not None:
        raise RuntimeError('Direct gateway launcher exited before publishing readiness')
    time.sleep(0.25)
else:
    raise TimeoutError('Direct gateway did not publish readiness before the bounded deadline')
"""


def bootstrap_direct_manager(
    settings: Any,
    client: JupyterClient,
) -> tuple[DirectManager, DirectTransport]:
    """Upload only the gateway bootstrap, start it once, then verify direct HTTPS."""

    for relative in DIRECT_REMOTE_FILES:
        source = settings.root / relative
        if not source.is_file():
            raise FileNotFoundError(f"Direct bootstrap source is missing: {relative}")
        client.upload_bytes(f"{settings.remote.remote_root}/{relative}", source.read_bytes())
    token = secrets.token_urlsafe(48)
    execution = client.execute_python(
        _launcher_code(settings.remote.remote_root, token),
        timeout_seconds=360,
    )
    position = execution.stdout.rfind(DIRECT_MARKER)
    if position < 0:
        raise RemoteExecutionError("Direct gateway bootstrap did not return its public readiness marker")
    try:
        value: object = json.loads(execution.stdout[position + len(DIRECT_MARKER) :].strip())
    except json.JSONDecodeError as error:
        raise RemoteProtocolError("Direct gateway bootstrap returned malformed readiness JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("publicUrl"), str):
        raise RemoteProtocolError("Direct gateway readiness did not contain a public URL")
    transport = DirectTransport(
        _validated_tunnel_url(value["publicUrl"]),
        DirectCredentials(token=token),
        timeout_seconds=settings.remote.request_timeout_seconds,
    )
    deadline = time.monotonic() + 45
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            gateway = transport.read_status("direct-gateway")
            if gateway.get("state") == "running":
                return DirectManager(settings, transport), transport
        except ComicSeriesError as error:
            last_error = error
        time.sleep(1)
    transport.close()
    detail = f": {type(last_error).__name__}" if last_error is not None else ""
    raise RemoteExecutionError(f"Direct HTTPS tunnel did not become reachable{detail}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap once, then control AMD directly over bearer HTTPS")
    parser.add_argument("config", nargs="?", type=Path, default=Path("config/project.toml"))
    parser.add_argument("--curl-stdin", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read Copy-as-cURL once, close Jupyter, then serve direct JSON-line commands."""

    args = _parser().parse_args(argv)
    try:
        settings = load_project_settings(args.config)
        preflight_url: str | None = None
        preflight_headers: tuple[tuple[str, str], ...] = ()
        if args.curl_stdin:
            imported = parse_copy_as_curl(read_framed_curl_stdin(sys.stdin))
            settings = apply_imported_target(settings, imported)
            credentials = imported.credentials
            preflight_url = imported.request_url
            preflight_headers = imported.extra_headers
        else:
            credentials = RemoteCredentials.from_environment()
        with JupyterClient(settings.remote, credentials) as client:
            if preflight_url:
                client.preflight_exact_get(preflight_url, preflight_headers)
            manager, transport = bootstrap_direct_manager(settings, client)
    except (ComicSeriesError, FileNotFoundError) as error:
        print(json.dumps({"session": "error", "error": f"{type(error).__name__}: {error}"}), flush=True)
        return 2

    print(json.dumps({"session": "ready", "transport": "direct-https", "jupyterClosed": True}), flush=True)
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError("Session command must be a JSON object")
                result = dispatch(cast(RemoteManager, manager), command)
                print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
                if command.get("action") == "quit":
                    return 0
            except (ComicSeriesError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                print(
                    json.dumps(
                        {"ok": False, "error": f"{type(error).__name__}: {error}"},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
