"""Bearer-authenticated direct transport and RemoteManager-compatible adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from ai_comic_series.config import ProjectSettings
from ai_comic_series.exceptions import (
    AuthenticationError,
    ConfigurationError,
    IntegrityError,
    RemoteExecutionError,
    RemoteProtocolError,
    RemoteTimeoutError,
)
from ai_comic_series.remote_manager import JobStart

__all__ = ["DirectCredentials", "DirectManager", "DirectTransport"]

TOKEN_ENV = "AI_COMIC_DIRECT_TOKEN"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GENERATION_STAGES = frozenset(
    {"anchors", "cover-drafts", "covers", "keyframes", "motion-keyframes", "video-sample", "videos"}
)


def _base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not loopback_http:
        raise ConfigurationError("Direct gateway URL must use HTTPS (HTTP is allowed only for loopback tests)")
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "Direct gateway URL must be an absolute origin without credentials, query, or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise ConfigurationError("Direct gateway URL must not contain a path")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


@dataclass(frozen=True, slots=True)
class DirectCredentials:
    """Process-local bearer credential whose representation is always redacted."""

    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.token.encode("utf-8")) < 32:
            raise ConfigurationError(f"{TOKEN_ENV} must contain at least 32 UTF-8 bytes")
        if any(character in self.token for character in "\r\n\0"):
            raise ConfigurationError(f"{TOKEN_ENV} must be one safe HTTP header value")

    @classmethod
    def from_environment(cls) -> DirectCredentials:
        """Read the direct bearer token only from the current process environment."""

        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            raise ConfigurationError(f"{TOKEN_ENV} is missing")
        return cls(token=token)


class DirectTransport:
    """Small HTTP client for the seven direct-gateway operations."""

    def __init__(
        self,
        base_url: str,
        credentials: DirectCredentials,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = _base_url(base_url)
        self._credentials = credentials
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {credentials.token}", "Accept": "application/json"},
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return "DirectTransport(endpoint=<redacted>, credentials=<redacted>)"

    def __enter__(self) -> DirectTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the direct HTTP connection pool."""

        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise RemoteExecutionError("Direct gateway request failed") from None

    @staticmethod
    def _raise_response(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise AuthenticationError("Direct gateway rejected the bearer credential")
        if 300 <= response.status_code < 400:
            raise RemoteProtocolError("Direct gateway returned a redirect; refusing to forward credentials")
        if not 200 <= response.status_code < 300:
            if not response.is_stream_consumed:
                response.read()
            message = f"HTTP {response.status_code}"
            try:
                value = response.json()
                if isinstance(value, dict) and isinstance(value.get("error"), str):
                    message += f": {value['error'][:500]}"
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise RemoteExecutionError(f"Direct gateway operation failed: {message}")

    def _json_response(self, response: httpx.Response) -> dict[str, Any]:
        self._raise_response(response)
        try:
            value: object = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RemoteProtocolError("Direct gateway returned invalid JSON") from error
        if not isinstance(value, dict) or value.get("ok") is not True or not isinstance(value.get("result"), dict):
            raise RemoteProtocolError("Direct gateway returned an invalid response envelope")
        return cast(dict[str, Any], value["result"])

    def read_status(self, job: str) -> dict[str, Any]:
        """Read one remote status object."""

        result = self._json_response(self._request("GET", "/v1/status", params={"job": job}))
        if job == "direct-gateway":
            result.pop("publicUrl", None)
        return result

    def read_log_tail(self, job: str, max_bytes: int = 16_000) -> str:
        """Read one bounded remote log tail."""

        response = self._request("GET", "/v1/log", params={"job": job, "max_bytes": str(max_bytes)})
        self._raise_response(response)
        return response.content.decode("utf-8", errors="replace")

    def start_job(self, request: dict[str, object]) -> dict[str, Any]:
        """Start one whitelisted remote job template."""

        return self._json_response(self._request("POST", "/v1/start-job", json=request))

    def sync_file(self, relative: str, content: bytes) -> dict[str, Any]:
        """Upload one hash-bound project source/config file."""

        digest = hashlib.sha256(content).hexdigest()
        response = self._request(
            "PUT",
            "/v1/sync",
            params={"path": relative},
            headers={"Content-Type": "application/octet-stream", "X-Content-SHA256": digest},
            content=content,
        )
        result = self._json_response(response)
        if result.get("sha256") != digest or int(result.get("bytes", -1)) != len(content):
            raise IntegrityError(f"Direct sync acknowledgement differs for {relative}")
        return result

    def artifact_info(self, relative: str) -> dict[str, object] | None:
        """Return size and digest headers without downloading an artifact."""

        response = self._request("HEAD", "/v1/fetch", params={"path": relative})
        if response.status_code == 404:
            return None
        self._raise_response(response)
        try:
            size = int(response.headers["Content-Length"])
            digest = response.headers["X-Content-SHA256"]
        except (KeyError, ValueError) as error:
            raise RemoteProtocolError("Direct artifact HEAD lacks valid size/digest headers") from error
        if size < 0 or not SHA256_PATTERN.fullmatch(digest):
            raise RemoteProtocolError("Direct artifact HEAD returned invalid size/digest values")
        return {"bytes": size, "sha256": digest}

    def download_to(
        self,
        relative: str,
        target: Path,
        *,
        expected_sha256: str = "",
        expected_bytes: int | None = None,
        max_bytes: int = 16 * 1024 * 1024 * 1024,
    ) -> dict[str, object]:
        """Stream one artifact to disk and atomically publish it after SHA verification."""

        if max_bytes <= 0 or expected_bytes is not None and expected_bytes < 0:
            raise ValueError("Download byte limits must be positive")
        if expected_sha256 and not SHA256_PATTERN.fullmatch(expected_sha256):
            raise ValueError("expected_sha256 must be one lowercase SHA-256 digest")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".download")
        temporary.unlink(missing_ok=True)
        hasher = hashlib.sha256()
        total = 0
        try:
            try:
                with self._http.stream("GET", "/v1/fetch", params={"path": relative}) as response:
                    self._raise_response(response)
                    try:
                        declared = int(response.headers["Content-Length"])
                        header_digest = response.headers["X-Content-SHA256"]
                    except (KeyError, ValueError) as error:
                        raise RemoteProtocolError("Direct artifact response lacks valid size/digest headers") from error
                    if declared < 0 or declared > max_bytes or not SHA256_PATTERN.fullmatch(header_digest):
                        raise RemoteProtocolError("Direct artifact response exceeds limits or has an invalid digest")
                    if expected_bytes is not None and declared != expected_bytes:
                        raise IntegrityError(f"Artifact size mismatch for {relative}: {declared} != {expected_bytes}")
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            total += len(chunk)
                            if total > max_bytes or total > declared:
                                raise IntegrityError(f"Artifact stream exceeded its declared size for {relative}")
                            handle.write(chunk)
                            hasher.update(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
            except httpx.HTTPError:
                raise RemoteExecutionError("Direct gateway media request failed") from None
            digest = hasher.hexdigest()
            if total != declared:
                raise IntegrityError(f"Artifact stream ended early for {relative}: {total} != {declared}")
            if not hmac.compare_digest(digest, header_digest):
                raise IntegrityError(f"Artifact response SHA-256 mismatch for {relative}")
            if expected_sha256 and not hmac.compare_digest(digest, expected_sha256):
                raise IntegrityError(f"Artifact expected SHA-256 mismatch for {relative}")
            temporary.replace(target)
            return {"path": relative, "bytes": total, "sha256": digest}
        finally:
            temporary.unlink(missing_ok=True)

    def delete_artifact(self, relative: str) -> dict[str, Any]:
        """Delete one project mirror only after a verified local download."""

        return self._json_response(self._request("DELETE", "/v1/fetch", params={"path": relative}))

    def stop(self) -> dict[str, Any]:
        """Request a recoverable generation stop."""

        return self._json_response(self._request("POST", "/v1/stop", content=b""))

    def resume(self) -> dict[str, Any]:
        """Remove the recoverable generation stop."""

        return self._json_response(self._request("POST", "/v1/resume", content=b""))


class DirectManager:
    """RemoteManager-compatible orchestration over :class:`DirectTransport`."""

    def __init__(self, settings: ProjectSettings, transport: DirectTransport) -> None:
        self._settings = settings
        self._transport = transport

    def __repr__(self) -> str:
        return f"DirectManager(project={self._settings.project_id!r}, transport={self._transport!r})"

    @property
    def remote_root(self) -> PurePosixPath:
        """Return the gateway project root marker."""

        return PurePosixPath(".")

    def run_probe(self) -> dict[str, Any]:
        """Return recorded public runtime evidence without remote shell execution."""

        return {
            "transport": "direct-https",
            "gateway": self.read_status("direct-gateway"),
            "bootstrap": self.read_status("bootstrap"),
        }

    def sync_bundle(self) -> list[str]:
        """Upload the same credential-free source bundle as RemoteManager."""

        uploaded: list[str] = []
        for directory in ("remote", "config", "workflows", "production"):
            local_root = self._settings.root / directory
            for path in sorted(local_root.rglob("*")):
                if not path.is_file() or path.name.startswith(".") or "__pycache__" in path.parts:
                    continue
                if path.name in {"STOP", "status.json"}:
                    continue
                relative = path.relative_to(self._settings.root).as_posix()
                self._transport.sync_file(relative, path.read_bytes())
                uploaded.append(relative)
        return uploaded

    @staticmethod
    def _argument_map(arguments: list[str]) -> dict[str, str]:
        if len(arguments) % 2:
            raise RemoteProtocolError("Direct start_job arguments must be flag/value pairs")
        values: dict[str, str] = {}
        for index in range(0, len(arguments), 2):
            name, value = arguments[index : index + 2]
            if not name.startswith("--") or name in values:
                raise RemoteProtocolError("Direct start_job arguments contain an unsafe or duplicate flag")
            values[name] = value
        return values

    def _start_request(self, job: str, script_name: str, arguments: list[str]) -> dict[str, object]:
        values = self._argument_map(arguments)
        if job == "bootstrap" and script_name == "bootstrap.py":
            if values.get("--status") != "status/bootstrap.json" or set(values) != {"--status", "--data-root"}:
                raise RemoteProtocolError("Bootstrap arguments differ from the direct gateway contract")
            return {"kind": "bootstrap", "dataRoot": values["--data-root"]}
        if job == "models" and script_name == "install_models.py":
            expected = {"--status", "--manifest", "--profile"}
            if (
                set(values) != expected
                or values["--status"] != "status/models.json"
                or values["--manifest"] != "config/models.json"
            ):
                raise RemoteProtocolError("Model-install arguments differ from the direct gateway contract")
            return {"kind": "install-models", "profile": values["--profile"]}
        if job.startswith("generate-") and script_name == "generate.py":
            stage = job.removeprefix("generate-")
            expected = {"--status", "--queue", "--stage", "--max-workers"}
            if (
                stage not in GENERATION_STAGES
                or set(values) != expected
                or values["--status"] != f"status/{job}.json"
                or values["--queue"] != "production/generation-queue.json"
                or values["--stage"] != stage
            ):
                raise RemoteProtocolError("Generation arguments differ from the direct gateway contract")
            try:
                workers = int(values["--max-workers"])
            except ValueError as error:
                raise RemoteProtocolError("Generation max-workers is not an integer") from error
            return {"kind": "generate", "stage": stage, "maxWorkers": workers}
        raise RemoteProtocolError("Direct start_job requested a non-whitelisted script")

    def start_job(self, job: str, script_name: str, arguments: list[str]) -> JobStart:
        """Start one RemoteManager-compatible whitelisted job."""

        self.sync_bundle()
        result = self._transport.start_job(self._start_request(job, script_name, arguments))
        try:
            return JobStart(
                job=str(result["job"]),
                pid=int(result["pid"]),
                status_path=str(result["status"]),
                log_path=str(result["log"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RemoteProtocolError("Direct start-job response is malformed") from error

    def read_status(self, job: str) -> dict[str, Any] | None:
        """Return a job status or None when it has not started."""

        result = self._transport.read_status(job)
        return None if result.get("state") == "not-started" else result

    def read_log_tail(self, job: str, max_bytes: int = 16_000) -> str:
        """Return the bounded remote log tail."""

        return self._transport.read_log_tail(job, max_bytes=max_bytes)

    def fetch_artifacts(self, job: str, *, cleanup_remote_mirror: bool = True) -> list[dict[str, object]]:
        """Stream, verify, and atomically publish all completed job artifacts."""

        status = self.read_status(job)
        if status is None:
            raise RemoteProtocolError(f"Remote job has no status document: {job}")
        completed = status.get("completed", [])
        if not isinstance(completed, list):
            raise RemoteProtocolError(f"Remote job has an invalid completed list: {job}")
        fetched: list[dict[str, object]] = []
        for item in completed:
            if not isinstance(item, dict) or not item.get("mirror") or not item.get("target"):
                raise RemoteProtocolError(f"Remote job contains an invalid artifact record: {item}")
            mirror = str(item["mirror"])
            target_relative = str(item["target"])
            expected_sha = str(item.get("sha256", ""))
            expected_bytes = int(item["bytes"]) if item.get("bytes") is not None else None
            target = (self._settings.root / target_relative).resolve()
            if self._settings.root.resolve() not in target.parents:
                raise RemoteProtocolError(f"Artifact target escapes the local project: {target_relative}")
            stage = target.with_suffix(target.suffix + ".direct-stage")
            metadata_relative = mirror + ".meta.json"
            metadata_target = target.with_suffix(target.suffix + ".meta.json")
            metadata_stage = metadata_target.with_suffix(metadata_target.suffix + ".direct-stage")
            stage.unlink(missing_ok=True)
            metadata_stage.unlink(missing_ok=True)
            metadata_present = self._transport.artifact_info(metadata_relative) is not None
            try:
                result = self._transport.download_to(
                    mirror,
                    stage,
                    expected_sha256=expected_sha,
                    expected_bytes=expected_bytes,
                )
                if metadata_present:
                    self._transport.download_to(metadata_relative, metadata_stage, max_bytes=4 * 1024 * 1024)
                target.parent.mkdir(parents=True, exist_ok=True)
                stage.replace(target)
                if metadata_present:
                    metadata_stage.replace(metadata_target)
            finally:
                stage.unlink(missing_ok=True)
                metadata_stage.unlink(missing_ok=True)
            if cleanup_remote_mirror:
                if metadata_present:
                    self._transport.delete_artifact(metadata_relative)
                self._transport.delete_artifact(mirror)
            fetched.append({"target": target_relative, "sha256": result["sha256"], "bytes": result["bytes"]})
        return fetched

    def stop_generation(self) -> str:
        """Request a recoverable stop before the next queued generation item."""

        result = self._transport.stop()
        return str(result["stopSignal"])

    def resume_generation(self) -> str:
        """Remove only the project-local stop signal."""

        result = self._transport.resume()
        return str(result["removedStopSignal"])

    def wait(self, job: str, timeout_seconds: float, poll_seconds: float = 10) -> dict[str, Any]:
        """Wait for a detached direct job without restarting it."""

        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("wait timeout and poll interval must be positive")
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.read_status(job)
            if last and last.get("state") == "complete":
                return last
            if last and last.get("state") == "failed":
                message = str(last.get("error", "Remote job failed"))
                raise RemoteExecutionError(f"{message}\n{self.read_log_tail(job)}")
            time.sleep(poll_seconds)
        detail = json.dumps(last, ensure_ascii=False) if last else "status not created"
        raise RemoteTimeoutError(f"Remote job {job!r} did not finish within {timeout_seconds}s: {detail}")
