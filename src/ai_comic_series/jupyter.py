"""Minimal authenticated Jupyter REST and kernel WebSocket client."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from websockets.sync.client import connect

from ai_comic_series.auth import refresh_amd_credentials
from ai_comic_series.config import RemoteCredentials, RemoteSettings
from ai_comic_series.exceptions import (
    AuthenticationError,
    RemoteExecutionError,
    RemoteProtocolError,
    RemoteTimeoutError,
)

__all__ = ["ExecutionResult", "JupyterClient"]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Captured output from one remote kernel execution request."""

    stdout: str
    stderr: str
    execute_count: int | None


class JupyterClient:
    """Authenticated Jupyter client that never persists or logs credentials."""

    def __init__(self, settings: RemoteSettings, credentials: RemoteCredentials) -> None:
        self._settings = settings
        self._credentials = credentials
        headers = {
            "Accept": "application/json",
            "Authorization": f"token {credentials.token}",
            "Cookie": credentials.cookie,
            "User-Agent": "ai-comic-series/0.1",
        }
        if credentials.xsrf_token:
            headers["X-XSRFToken"] = credentials.xsrf_token
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers=headers,
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
        )

    def _refresh_credentials(self) -> None:
        refreshed = refresh_amd_credentials(self._settings, self._credentials, required=True)
        self._credentials = refreshed
        self._http.headers["Authorization"] = f"token {refreshed.token}"
        self._http.headers["Cookie"] = refreshed.cookie
        if refreshed.xsrf_token:
            self._http.headers["X-XSRFToken"] = refreshed.xsrf_token

    def __enter__(self) -> JupyterClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the local HTTP connection pool."""

        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._send(method, path, **kwargs)
        if response.status_code in {401, 403}:
            self._refresh_credentials()
            response = self._send(method, path, **kwargs)
        if response.status_code in {401, 403}:
            raise AuthenticationError(
                "AMD/Jupyter still returned an authorization failure after refreshing. Sign in again and recopy Cookie."
            )
        return response

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            raise RemoteTimeoutError(f"Jupyter {method} {path} timed out") from error
        except httpx.HTTPError as error:
            raise RemoteProtocolError(f"Jupyter {method} {path} failed: {error}") from error

    def _json(self, response: httpx.Response, operation: str) -> Any:
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPStatusError, json.JSONDecodeError) as error:
            body = response.text[:500].replace("\n", " ")
            raise RemoteProtocolError(f"{operation} failed with HTTP {response.status_code}: {body}") from error

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return active Jupyter sessions."""

        data = self._json(self._request("GET", "/api/sessions"), "List sessions")
        if not isinstance(data, list):
            raise RemoteProtocolError("Jupyter sessions response is not a list")
        return data

    def ensure_kernel(self) -> str:
        """Reuse an active kernel or create one with the configured kernelspec."""

        for session in self.list_sessions():
            kernel = session.get("kernel", {})
            if isinstance(kernel, dict) and kernel.get("id"):
                return str(kernel["id"])
        payload = {"name": self._settings.kernel_name}
        data = self._json(self._request("POST", "/api/kernels", json=payload), "Create kernel")
        if not isinstance(data, dict) or not data.get("id"):
            raise RemoteProtocolError("Create-kernel response does not contain an id")
        return str(data["id"])

    @staticmethod
    def build_execute_message(code: str, session_id: str, message_id: str) -> dict[str, Any]:
        """Build a Jupyter messaging-protocol execute request."""

        return {
            "header": {
                "msg_id": message_id,
                "username": "ai-comic-series",
                "session": session_id,
                "date": datetime.now(UTC).isoformat(),
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": False,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }

    def execute_python(self, code: str, timeout_seconds: float | None = None) -> ExecutionResult:
        """Execute Python in a remote kernel and capture streams deterministically."""

        kernel_id = self.ensure_kernel()
        session_id = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        message = self.build_execute_message(code, session_id, message_id)
        websocket_base = self._settings.base_url.replace("https://", "wss://", 1)
        websocket_url = f"{websocket_base}/api/kernels/{quote(kernel_id)}/channels?session_id={session_id}"
        headers = {
            "Authorization": f"token {self._credentials.token}",
            "Cookie": self._credentials.cookie,
            "Origin": self._settings.origin,
            "User-Agent": "ai-comic-series/0.1",
        }
        if self._credentials.xsrf_token:
            headers["X-XSRFToken"] = self._credentials.xsrf_token
        deadline = time.monotonic() + (timeout_seconds or self._settings.execution_timeout_seconds)
        stdout: list[str] = []
        stderr: list[str] = []
        execute_count: int | None = None
        reply_received = False
        idle_received = False
        try:
            with connect(websocket_url, additional_headers=headers, open_timeout=20, close_timeout=5) as websocket:
                websocket.send(json.dumps(message))
                while not (reply_received and idle_received):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RemoteTimeoutError("Remote kernel execution timed out")
                    raw = websocket.recv(timeout=min(remaining, 30))
                    data = json.loads(raw)
                    if data.get("parent_header", {}).get("msg_id") != message_id:
                        continue
                    message_type = data.get("msg_type") or data.get("header", {}).get("msg_type")
                    content = data.get("content", {})
                    if message_type == "stream":
                        target = stderr if content.get("name") == "stderr" else stdout
                        target.append(str(content.get("text", "")))
                    elif message_type == "error":
                        traceback = "\n".join(content.get("traceback", []))
                        raise RemoteExecutionError(traceback or str(content.get("evalue", "Remote execution failed")))
                    elif message_type == "execute_reply":
                        reply_received = True
                        count = content.get("execution_count")
                        execute_count = int(count) if isinstance(count, int) else None
                    elif message_type == "status" and content.get("execution_state") == "idle":
                        idle_received = True
        except TimeoutError as error:
            raise RemoteTimeoutError("Remote kernel WebSocket timed out") from error
        except json.JSONDecodeError as error:
            raise RemoteProtocolError("Remote kernel returned an invalid JSON message") from error
        return ExecutionResult(stdout="".join(stdout), stderr="".join(stderr), execute_count=execute_count)

    @staticmethod
    def _contents_path(remote_path: str) -> str:
        clean = str(PurePosixPath(remote_path.strip("/")))
        return f"/api/contents/{quote(clean, safe='/')}"

    def ensure_directory(self, remote_path: str) -> None:
        """Create a Jupyter-relative directory and all parents if absent."""

        current = PurePosixPath()
        for part in PurePosixPath(remote_path).parts:
            current /= part
            path = self._contents_path(str(current))
            response = self._request("GET", path, params={"content": 0})
            if response.status_code == 404:
                self._json(self._request("PUT", path, json={"type": "directory"}), f"Create directory {current}")
                continue
            data = self._json(response, f"Inspect directory {current}")
            if not isinstance(data, dict) or data.get("type") != "directory":
                raise RemoteProtocolError(f"Remote path exists but is not a directory: {current}")

    def upload_bytes(self, remote_path: str, content: bytes) -> None:
        """Upload a small control-plane file through the Jupyter Contents API."""

        parent = str(PurePosixPath(remote_path).parent)
        if parent not in {"", "."}:
            self.ensure_directory(parent)
        payload = {"type": "file", "format": "base64", "content": base64.b64encode(content).decode("ascii")}
        self._json(self._request("PUT", self._contents_path(remote_path), json=payload), f"Upload {remote_path}")

    def download_bytes(self, remote_path: str) -> bytes:
        """Download a file through the Jupyter Contents API."""

        response = self._request("GET", self._contents_path(remote_path), params={"content": 1})
        data = self._json(response, f"Download {remote_path}")
        if not isinstance(data, dict) or data.get("type") != "file":
            raise RemoteProtocolError(f"Remote path is not a file: {remote_path}")
        content = data.get("content", "")
        if data.get("format") == "base64":
            try:
                return base64.b64decode(content, validate=True)
            except (TypeError, ValueError) as error:
                raise RemoteProtocolError(f"Remote file contains invalid base64: {remote_path}") from error
        if data.get("format") == "text" and isinstance(content, str):
            return content.encode("utf-8")
        raise RemoteProtocolError(f"Unsupported Jupyter file format for {remote_path}: {data.get('format')}")

    def exists(self, remote_path: str) -> bool:
        """Return whether a Jupyter-relative path exists."""

        response = self._request("GET", self._contents_path(remote_path), params={"content": 0})
        if response.status_code == 404:
            return False
        self._json(response, f"Inspect {remote_path}")
        return True

    def delete(self, remote_path: str) -> None:
        """Delete one explicit Jupyter-relative file after it has been verified locally."""

        response = self._request("DELETE", self._contents_path(remote_path))
        if response.status_code not in {200, 204}:
            body = response.text[:500].replace("\n", " ")
            raise RemoteProtocolError(f"Delete {remote_path} failed with HTTP {response.status_code}: {body}")
