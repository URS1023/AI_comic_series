"""Loopback-only HTTP control gateway for the AMD production runtime.

Cloudflared may expose this server, but the server itself never listens on a
non-loopback interface and every control/media route requires one bearer token.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

__all__ = ["GatewayApplication", "GatewayLimits", "create_server", "main"]

TOKEN_ENV = "AI_COMIC_DIRECT_TOKEN"
JOB_PATTERN = re.compile(
    r"^(?:direct-gateway|bootstrap|models|generate-(?:anchors|cover-drafts|covers|keyframes|motion-keyframes|video-sample|videos))$"
)
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GENERATION_STAGES = frozenset(
    {"anchors", "cover-drafts", "covers", "keyframes", "motion-keyframes", "video-sample", "videos"}
)
SYNC_PREFIXES = frozenset({"remote", "config", "workflows", "production"})
FETCH_PREFIX = "artifacts"


class GatewayRequestError(RuntimeError):
    """One safe client-visible gateway failure."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    """Hard resource limits applied before request data reaches the filesystem."""

    max_json_bytes: int = 64 * 1024
    max_sync_bytes: int = 8 * 1024 * 1024
    max_status_bytes: int = 1024 * 1024
    max_log_bytes: int = 256 * 1024
    max_artifact_bytes: int = 16 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            min(
                self.max_json_bytes,
                self.max_sync_bytes,
                self.max_status_bytes,
                self.max_log_bytes,
                self.max_artifact_bytes,
            )
            <= 0
        ):
            raise ValueError("Gateway size limits must all be positive")


@dataclass(slots=True)
class GatewayApplication:
    """Filesystem and process operations behind the authenticated HTTP boundary."""

    root: Path
    token: str = field(repr=False)
    limits: GatewayLimits = field(default_factory=GatewayLimits)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Gateway project root does not exist: {self.root}")
        if len(self.token.encode("utf-8")) < 32:
            raise ValueError(f"{TOKEN_ENV} must contain at least 32 UTF-8 bytes")
        if any(character in self.token for character in "\r\n\0"):
            raise ValueError(f"{TOKEN_ENV} must be one safe HTTP header value")

    def authorized(self, authorization: str) -> bool:
        """Compare the complete Authorization value in constant time."""

        supplied = authorization.encode("utf-8", errors="surrogatepass")
        expected = f"Bearer {self.token}".encode()
        return hmac.compare_digest(supplied, expected)

    @staticmethod
    def _job_name(value: str) -> str:
        if not JOB_PATTERN.fullmatch(value):
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Unknown or unsafe job name")
        return value

    def _safe_path(self, relative: str, *, prefixes: frozenset[str]) -> Path:
        if not relative or len(relative) > 512 or "\\" in relative or "\0" in relative:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Unsafe relative path")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Unsafe relative path")
        if not pure.parts or pure.parts[0] not in prefixes:
            raise GatewayRequestError(HTTPStatus.FORBIDDEN, "Path is outside the allowed gateway area")
        candidate = (self.root / Path(*pure.parts)).resolve()
        if self.root != candidate and self.root not in candidate.parents:
            raise GatewayRequestError(HTTPStatus.FORBIDDEN, "Path escapes the gateway project root")
        return candidate

    @staticmethod
    def _read_limited(path: Path, maximum: int) -> bytes:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            raise GatewayRequestError(HTTPStatus.NOT_FOUND, "Requested resource does not exist") from None
        if not path.is_file():
            raise GatewayRequestError(HTTPStatus.NOT_FOUND, "Requested resource is not a file")
        if size > maximum:
            raise GatewayRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Requested resource exceeds its size limit")
        return path.read_bytes()

    def status(self, job: str) -> dict[str, Any] | None:
        """Read one bounded job status document."""

        safe_job = self._job_name(job)
        path = self.root / "status" / f"{safe_job}.json"
        if not path.is_file():
            return None
        content = self._read_limited(path, self.limits.max_status_bytes)
        try:
            value: object = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayRequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "Job status is invalid JSON") from error
        if not isinstance(value, dict):
            raise GatewayRequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "Job status is not a JSON object")
        return cast(dict[str, Any], value)

    def log_tail(self, job: str, maximum: int) -> bytes:
        """Read at most the configured tail bytes from one fixed job log."""

        safe_job = self._job_name(job)
        if maximum <= 0:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "max_bytes must be positive")
        maximum = min(maximum, self.limits.max_log_bytes)
        path = self.root / "logs" / f"{safe_job}.log"
        if not path.is_file():
            return b""
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - maximum))
            return handle.read(maximum)

    def sync_file(self, relative: str, content: bytes, expected_sha256: str) -> dict[str, object]:
        """Atomically write one small, hash-bound source/config file."""

        if len(content) > self.limits.max_sync_bytes:
            raise GatewayRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Sync file exceeds the size limit")
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Sync SHA-256 is missing or malformed")
        actual = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual, expected_sha256):
            raise GatewayRequestError(HTTPStatus.UNPROCESSABLE_ENTITY, "Sync SHA-256 mismatch")
        target = self._safe_path(relative, prefixes=SYNC_PREFIXES)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.gateway-{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": relative, "bytes": len(content), "sha256": actual}

    def artifact(self, relative: str) -> tuple[Path, int, str]:
        """Resolve and hash one bounded project artifact."""

        path = self._safe_path(relative, prefixes=frozenset({FETCH_PREFIX}))
        if not path.is_file():
            raise GatewayRequestError(HTTPStatus.NOT_FOUND, "Artifact does not exist")
        size = path.stat().st_size
        if size > self.limits.max_artifact_bytes:
            raise GatewayRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Artifact exceeds the download limit")
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                hasher.update(chunk)
        return path, size, hasher.hexdigest()

    def delete_artifact(self, relative: str) -> dict[str, object]:
        """Delete only one already mirrored artifact file."""

        path = self._safe_path(relative, prefixes=frozenset({FETCH_PREFIX}))
        existed = path.is_file()
        if existed:
            path.unlink()
        return {"path": relative, "deleted": existed}

    def _validate_data_root(self, value: object) -> str:
        text = str(value or "")
        if not text:
            return ""
        if len(text) > 256 or "\0" in text or "\n" in text or "\r" in text:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "dataRoot is unsafe")
        path = PurePosixPath(text)
        if not path.is_absolute() or ".." in path.parts or str(path) == "/":
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "dataRoot must be a non-root absolute Linux path")
        protected = {"proc", "sys", "dev", "etc", "usr", "bin", "sbin", "boot"}
        if len(path.parts) < 2 or path.parts[1] in protected:
            raise GatewayRequestError(HTTPStatus.FORBIDDEN, "dataRoot points into a protected system area")
        return str(path)

    def _profile(self, value: object) -> str:
        profile = str(value or "")
        if not SAFE_NAME_PATTERN.fullmatch(profile):
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Model profile is unsafe")
        manifest_path = self.root / "config" / "models.json"
        try:
            manifest = json.loads(self._read_limited(manifest_path, self.limits.max_status_bytes))
        except json.JSONDecodeError as error:
            raise GatewayRequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "Model manifest is invalid") from error
        profiles = manifest.get("profiles", {}) if isinstance(manifest, dict) else {}
        if not isinstance(profiles, dict) or profile not in profiles:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Unknown model profile")
        return profile

    def _job_spec(self, payload: dict[str, Any]) -> tuple[str, str, list[str]]:
        kind = str(payload.get("kind", ""))
        if kind == "bootstrap":
            return (
                "bootstrap",
                "bootstrap.py",
                ["--status", "status/bootstrap.json", "--data-root", self._validate_data_root(payload.get("dataRoot"))],
            )
        if kind == "install-models":
            return (
                "models",
                "install_models.py",
                [
                    "--status",
                    "status/models.json",
                    "--manifest",
                    "config/models.json",
                    "--profile",
                    self._profile(payload.get("profile")),
                ],
            )
        if kind == "generate":
            stage = str(payload.get("stage", ""))
            if stage not in GENERATION_STAGES:
                raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Unknown generation stage")
            try:
                workers = int(payload.get("maxWorkers", 8))
            except (TypeError, ValueError) as error:
                raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "maxWorkers must be an integer") from error
            if not 1 <= workers <= 32:
                raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "maxWorkers must be between 1 and 32")
            job = f"generate-{stage}"
            return (
                job,
                "generate.py",
                [
                    "--status",
                    f"status/{job}.json",
                    "--queue",
                    "production/generation-queue.json",
                    "--stage",
                    stage,
                    "--max-workers",
                    str(workers),
                ],
            )
        raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Unknown start-job kind")

    def start_job(self, payload: dict[str, Any]) -> dict[str, object]:
        """Launch one of three fixed remote job templates."""

        job, script_name, arguments = self._job_spec(payload)
        existing = self.status(job)
        if existing is not None and existing.get("state") == "running":
            raise GatewayRequestError(HTTPStatus.CONFLICT, "Job is already running")
        script = (self.root / "remote" / script_name).resolve()
        remote_dir = (self.root / "remote").resolve()
        if remote_dir not in script.parents or not script.is_file():
            raise GatewayRequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "Whitelisted remote script is missing")
        status_path = f"status/{job}.json"
        log_path = f"logs/{job}.log"
        (self.root / "status").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        child_environment = {
            name: value
            for name, value in os.environ.items()
            if name != TOKEN_ENV and not name.startswith("AI_COMIC_JUPYTER_")
        }
        with (self.root / log_path).open("ab", buffering=0) as log:
            process = subprocess.Popen(
                [sys.executable, str(script), *arguments],
                cwd=self.root,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        (self.root / "status" / f"{job}.pid").write_text(f"{process.pid}\n", encoding="utf-8")
        return {"job": job, "pid": process.pid, "status": status_path, "log": log_path}

    def stop(self) -> dict[str, object]:
        """Create the recoverable project-local generation stop signal."""

        target = self.root / "production" / "STOP"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("stop requested by authenticated direct gateway\n", encoding="utf-8")
        return {"stopSignal": "production/STOP"}

    def resume(self) -> dict[str, object]:
        """Remove only the project-local generation stop signal."""

        (self.root / "production" / "STOP").unlink(missing_ok=True)
        return {"removedStopSignal": "production/STOP"}


class DirectGatewayHandler(BaseHTTPRequestHandler):
    """Authenticated HTTP/1.1 adapter around :class:`GatewayApplication`."""

    server_version = "AIComicDirectGateway/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def application(self) -> GatewayApplication:
        return cast("GatewayHTTPServer", self.server).application

    def log_message(self, _format: str, *_arguments: object) -> None:
        """Disable request logging so Authorization headers can never be echoed."""

    def _send_headers(self, status: HTTPStatus, content_type: str, length: int, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()

    def _json(self, status: HTTPStatus, value: object) -> None:
        body = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, error: GatewayRequestError) -> None:
        self.close_connection = True
        self._json(error.status, {"ok": False, "error": str(error)})

    def _authenticate(self) -> bool:
        if self.application.authorized(self.headers.get("Authorization", "")):
            return True
        self.close_connection = True
        body = b'{"ok":false,"error":"Unauthorized"}\n'
        self._send_headers(
            HTTPStatus.UNAUTHORIZED,
            "application/json; charset=utf-8",
            len(body),
            WWW_Authenticate='Bearer realm="ai-comic-direct"',
        )
        if self.command != "HEAD":
            self.wfile.write(body)
        return False

    def _content_length(self, maximum: int) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None or self.headers.get("Transfer-Encoding"):
            raise GatewayRequestError(HTTPStatus.LENGTH_REQUIRED, "A bounded Content-Length is required")
        try:
            length = int(raw)
        except ValueError as error:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Content-Length is invalid") from error
        if length < 0 or length > maximum:
            raise GatewayRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body exceeds the size limit")
        return length

    def _body(self, maximum: int) -> bytes:
        length = self._content_length(maximum)
        content = self.rfile.read(length)
        if len(content) != length:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Request body ended early")
        return content

    def _json_body(self) -> dict[str, Any]:
        try:
            value: object = json.loads(self._body(self.application.limits.max_json_bytes))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Request body is invalid JSON") from error
        if not isinstance(value, dict):
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")
        return cast(dict[str, Any], value)

    def _query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)

    @staticmethod
    def _one(query: dict[str, list[str]], name: str) -> str:
        values = query.get(name, [])
        if len(values) != 1:
            raise GatewayRequestError(HTTPStatus.BAD_REQUEST, f"Exactly one {name} query value is required")
        return values[0]

    def _dispatch_get(self, *, head_only: bool = False) -> None:
        path, query = self._query()
        if path == "/v1/status":
            job = self._one(query, "job")
            status = self.application.status(job) or {"state": "not-started", "job": job}
            self._json(HTTPStatus.OK, {"ok": True, "result": status})
            return
        if path == "/v1/log":
            job = self._one(query, "job")
            raw_maximum = query.get("max_bytes", ["16000"])
            if len(raw_maximum) != 1:
                raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "max_bytes may appear only once")
            try:
                maximum = int(raw_maximum[0])
            except ValueError as error:
                raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "max_bytes must be an integer") from error
            content = self.application.log_tail(job, maximum)
            self._send_headers(HTTPStatus.OK, "text/plain; charset=utf-8", len(content))
            if not head_only:
                self.wfile.write(content)
            return
        if path == "/v1/fetch":
            relative = self._one(query, "path")
            artifact, size, digest = self.application.artifact(relative)
            self._send_headers(
                HTTPStatus.OK,
                "application/octet-stream",
                size,
                X_Content_SHA256=digest,
            )
            if not head_only:
                try:
                    with artifact.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
            return
        raise GatewayRequestError(HTTPStatus.NOT_FOUND, "Unknown gateway route")

    def do_GET(self) -> None:  # noqa: N802
        self._handle(lambda: self._dispatch_get(head_only=False))

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(lambda: self._dispatch_get(head_only=True))

    def do_PUT(self) -> None:  # noqa: N802
        def dispatch() -> None:
            path, query = self._query()
            if path != "/v1/sync":
                raise GatewayRequestError(HTTPStatus.NOT_FOUND, "Unknown gateway route")
            relative = self._one(query, "path")
            digest = self.headers.get("X-Content-SHA256", "")
            content = self._body(self.application.limits.max_sync_bytes)
            self._json(HTTPStatus.OK, {"ok": True, "result": self.application.sync_file(relative, content, digest)})

        self._handle(dispatch)

    def do_POST(self) -> None:  # noqa: N802
        def dispatch() -> None:
            path, _ = self._query()
            if path == "/v1/start-job":
                result = self.application.start_job(self._json_body())
            elif path == "/v1/stop":
                if self._content_length(self.application.limits.max_json_bytes) != 0:
                    raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "stop accepts an empty body")
                result = self.application.stop()
            elif path == "/v1/resume":
                if self._content_length(self.application.limits.max_json_bytes) != 0:
                    raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "resume accepts an empty body")
                result = self.application.resume()
            else:
                raise GatewayRequestError(HTTPStatus.NOT_FOUND, "Unknown gateway route")
            self._json(HTTPStatus.OK, {"ok": True, "result": result})

        self._handle(dispatch)

    def do_DELETE(self) -> None:  # noqa: N802
        def dispatch() -> None:
            path, query = self._query()
            if path != "/v1/fetch":
                raise GatewayRequestError(HTTPStatus.NOT_FOUND, "Unknown gateway route")
            result = self.application.delete_artifact(self._one(query, "path"))
            self._json(HTTPStatus.OK, {"ok": True, "result": result})

        self._handle(dispatch)

    def _handle(self, dispatch: Any) -> None:
        if not self._authenticate():
            return
        try:
            dispatch()
        except GatewayRequestError as error:
            self._error(error)
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError):
            self.close_connection = True
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Gateway operation failed"})


class GatewayHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying one immutable application boundary."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], application: GatewayApplication) -> None:
        self.application = application
        super().__init__(address, DirectGatewayHandler)


def create_server(
    root: Path,
    token: str,
    *,
    port: int = 8765,
    limits: GatewayLimits | None = None,
) -> GatewayHTTPServer:
    """Create a gateway that is unconditionally bound to IPv4 loopback."""

    if not 0 <= port <= 65535:
        raise ValueError("Gateway port must be between 0 and 65535")
    application = GatewayApplication(root=root, token=token, limits=limits or GatewayLimits())
    return GatewayHTTPServer(("127.0.0.1", port), application)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Loopback-only AI comic direct gateway")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the authenticated loopback server until its supervisor stops it."""

    args = _parser().parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    server = create_server(args.root, token, port=args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
