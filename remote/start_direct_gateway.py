"""Supervise the loopback gateway and one Cloudflare Quick Tunnel."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

TOKEN_ENV = "AI_COMIC_DIRECT_TOKEN"
PUBLIC_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
SECRET_ENV_PREFIXES = ("AI_COMIC_JUPYTER_",)
SECRET_ENV_NAMES = frozenset({TOKEN_ENV})
CLOUDFLARED_VERSION = "2026.8.2"
CLOUDFLARED_URL = (
    f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
)
CLOUDFLARED_SIZE = 39_799_316
CLOUDFLARED_SHA256 = "fcfb02b575a52ca1af2e3267af4e1517bcdeb30ac48c834c69abaed3c0576ad2"

__all__ = [
    "child_environments",
    "cloudflared_command",
    "gateway_command",
    "install_cloudflared",
    "main",
]


def gateway_command(python: Path, root: Path, port: int) -> list[str]:
    """Build a secret-free gateway command line."""

    return [
        str(python.resolve()),
        str((root / "remote" / "direct_gateway.py").resolve()),
        "--root",
        str(root.resolve()),
        "--port",
        str(port),
    ]


def cloudflared_command(cloudflared: Path, port: int) -> list[str]:
    """Build a token-free Cloudflare Quick Tunnel command line."""

    return [
        str(cloudflared.resolve()),
        "--no-autoupdate",
        "tunnel",
        "--url",
        f"http://127.0.0.1:{port}",
    ]


def child_environments(environment: Mapping[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Keep the bearer token only in the gateway child, never cloudflared."""

    source = dict(environment if environment is not None else os.environ)
    token = source.get(TOKEN_ENV, "")
    if len(token.encode("utf-8")) < 32 or any(character in token for character in "\r\n\0"):
        raise ValueError(f"{TOKEN_ENV} must contain at least 32 safe UTF-8 bytes")
    non_jupyter_environment = {
        name: value
        for name, value in source.items()
        if not any(name.startswith(prefix) for prefix in SECRET_ENV_PREFIXES)
    }
    gateway_environment = non_jupyter_environment.copy()
    gateway_environment[TOKEN_ENV] = token
    cloudflared_environment = {
        name: value for name, value in non_jupyter_environment.items() if name not in SECRET_ENV_NAMES
    }
    return gateway_environment, cloudflared_environment


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _status_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or pure.parts[0] != "status" or ".." in pure.parts:
        raise ValueError("--status must be a safe path below status/")
    target = (root / Path(*pure.parts)).resolve()
    status_root = (root / "status").resolve()
    if status_root != target and status_root not in target.parents:
        raise ValueError("--status escapes the project status directory")
    return target


def _file_digest(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            hasher.update(chunk)
    return size, hasher.hexdigest()


def install_cloudflared(data_root: Path) -> Path:
    """Install the exact verified cloudflared build on the large data volume."""

    binary = (data_root / "bin" / "cloudflared").resolve()
    binary.parent.mkdir(parents=True, exist_ok=True)
    if binary.is_file() and _file_digest(binary) == (CLOUDFLARED_SIZE, CLOUDFLARED_SHA256):
        binary.chmod(0o755)
        return binary
    descriptor, temporary_name = tempfile.mkstemp(prefix=".cloudflared-", suffix=".download", dir=binary.parent)
    temporary = Path(temporary_name)
    try:
        hasher = hashlib.sha256()
        size = 0
        request = urllib.request.Request(CLOUDFLARED_URL, headers={"User-Agent": "ai-comic-series/0.1"})
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=120) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > CLOUDFLARED_SIZE:
                    raise RuntimeError("Cloudflared download exceeds its pinned size")
                output.write(chunk)
                hasher.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        digest = hasher.hexdigest()
        if size != CLOUDFLARED_SIZE or digest != CLOUDFLARED_SHA256:
            raise RuntimeError(
                f"Cloudflared integrity mismatch: bytes={size}/{CLOUDFLARED_SIZE}, sha256={digest}/{CLOUDFLARED_SHA256}"
            )
        temporary.chmod(0o755)
        temporary.replace(binary)
        return binary
    finally:
        temporary.unlink(missing_ok=True)


def _data_root(root: Path) -> Path:
    state_path = root / "state.json"
    if not state_path.is_file() or state_path.stat().st_size > 1024 * 1024:
        raise RuntimeError("state.json is required before installing cloudflared on data_root")
    try:
        value: object = json.loads(state_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("state.json is invalid") from error
    data_root = Path(str(value.get("data_root", ""))) if isinstance(value, dict) else Path("")
    if not data_root.is_absolute() or data_root == Path("/"):
        raise RuntimeError("state.json does not contain a safe absolute data_root")
    return data_root


def _cloudflared(value: str, root: Path) -> Path:
    found = shutil.which(value)
    candidate = Path(found or value).resolve()
    if candidate.is_file():
        return candidate
    if value != "cloudflared":
        raise FileNotFoundError(f"Explicit cloudflared binary does not exist: {candidate}")
    return install_cloudflared(_data_root(root))


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _terminate(process: subprocess.Popen[bytes] | subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the AI comic direct gateway and Quick Tunnel")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--status", default="status/direct-gateway.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cloudflared", default=os.environ.get("AI_COMIC_CLOUDFLARED", "cloudflared"))
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Start both children, publish only URL/PIDs, and supervise their lifetime."""

    args = _parser().parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir() or not 1 <= args.port <= 65535 or args.startup_timeout <= 0:
        raise ValueError("Gateway root, port, or startup timeout is invalid")
    status_path = _status_path(root, args.status)
    gateway_environment, cloudflared_environment = child_environments()
    cloudflared = _cloudflared(args.cloudflared, root)
    gateway_script = root / "remote" / "direct_gateway.py"
    if not gateway_script.is_file():
        raise FileNotFoundError(f"Direct gateway script is missing: {gateway_script}")

    gateway: subprocess.Popen[bytes] | None = None
    tunnel: subprocess.Popen[str] | None = None
    public_url = ""
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        log_path = root / "logs" / "direct-gateway.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as log:
            gateway = subprocess.Popen(
                gateway_command(Path(sys.executable), root, args.port),
                cwd=root,
                env=gateway_environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        deadline = time.monotonic() + args.startup_timeout
        while not _port_open(args.port):
            if gateway.poll() is not None:
                raise RuntimeError("Direct gateway exited before opening its loopback port")
            if time.monotonic() >= deadline:
                raise TimeoutError("Direct gateway did not open its loopback port in time")
            time.sleep(0.1)

        tunnel = subprocess.Popen(
            cloudflared_command(cloudflared, args.port),
            cwd=root,
            env=cloudflared_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
            close_fds=True,
        )
        lines: queue.Queue[str | None] = queue.Queue(maxsize=128)
        url_collected = threading.Event()

        def read_tunnel_output() -> None:
            assert tunnel is not None and tunnel.stderr is not None
            try:
                for line in tunnel.stderr:
                    if url_collected.is_set():
                        continue
                    try:
                        lines.put(line, timeout=0.25)
                    except queue.Full:
                        continue
            finally:
                if not url_collected.is_set():
                    with contextlib.suppress(queue.Full):
                        lines.put(None, timeout=0.25)

        reader = threading.Thread(target=read_tunnel_output, name="cloudflared-url-reader", daemon=True)
        reader.start()
        while time.monotonic() < deadline and not public_url:
            if tunnel.poll() is not None:
                raise RuntimeError("Cloudflared exited before publishing a Quick Tunnel URL")
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                continue
            match = PUBLIC_URL_PATTERN.search(line)
            if match:
                public_url = match.group(0).lower()
                url_collected.set()
        if not public_url:
            raise TimeoutError("Cloudflared did not publish a Quick Tunnel URL in time")

        public_status = {
            "state": "running",
            "publicUrl": public_url,
            "launcherPid": os.getpid(),
            "gatewayPid": gateway.pid,
            "cloudflaredPid": tunnel.pid,
            "updated": time.time(),
        }
        _atomic_json(status_path, public_status)
        while not stop_event.wait(1.0):
            if gateway.poll() is not None or tunnel.poll() is not None:
                break
        public_status["state"] = "stopped"
        public_status["updated"] = time.time()
        _atomic_json(status_path, public_status)
        return 0 if stop_event.is_set() else 1
    except Exception:
        _atomic_json(
            status_path,
            {
                "state": "failed",
                "publicUrl": public_url,
                "launcherPid": os.getpid(),
                "gatewayPid": gateway.pid if gateway is not None else None,
                "cloudflaredPid": tunnel.pid if tunnel is not None else None,
                "updated": time.time(),
            },
        )
        raise
    finally:
        _terminate(tunnel)
        _terminate(gateway)


if __name__ == "__main__":
    raise SystemExit(main())
