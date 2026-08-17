"""High-level orchestration for the project-local remote bundle."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast

from ai_comic_series.config import ProjectSettings
from ai_comic_series.exceptions import RemoteExecutionError, RemoteProtocolError, RemoteTimeoutError
from ai_comic_series.jupyter import JupyterClient

__all__ = ["JobStart", "RemoteManager"]


@dataclass(frozen=True, slots=True)
class JobStart:
    """Identity returned after starting a detached remote job."""

    job: str
    pid: int
    status_path: str
    log_path: str


class RemoteManager:
    """Synchronize and run the credential-free remote execution bundle."""

    def __init__(self, settings: ProjectSettings, client: JupyterClient) -> None:
        self._settings = settings
        self._client = client

    @property
    def remote_root(self) -> PurePosixPath:
        """Return the configured Jupyter-relative project root."""

        return PurePosixPath(self._settings.remote.remote_root)

    def sync_bundle(self) -> list[str]:
        """Upload only small source/config files; models and media download remotely."""

        uploaded: list[str] = []
        roots = [
            self._settings.root / "remote",
            self._settings.root / "config",
            self._settings.root / "workflows",
            self._settings.root / "production",
        ]
        for local_root in roots:
            remote_prefix = self.remote_root / local_root.name
            for path in sorted(local_root.rglob("*")):
                if not path.is_file() or path.name.startswith(".") or "__pycache__" in path.parts:
                    continue
                if path.name in {"STOP", "status.json"}:
                    continue
                relative = path.relative_to(local_root)
                remote_path = str(remote_prefix / PurePosixPath(*relative.parts))
                self._client.upload_bytes(remote_path, path.read_bytes())
                uploaded.append(remote_path)
        return uploaded

    def run_probe(self) -> dict[str, Any]:
        """Run the read-only runtime probe and parse its JSON output."""

        self.sync_bundle()
        script = str(self.remote_root / "remote" / "probe.py")
        code = (
            "import pathlib, runpy, sys\n"
            f"script = pathlib.Path.cwd() / {script!r}\n"
            "sys.path.insert(0, str(script.parent))\n"
            "runpy.run_path(str(script), run_name='__main__')\n"
        )
        execution = self._client.execute_python(code, timeout_seconds=60)
        marker = "AI_COMIC_PROBE_JSON="
        position = execution.stdout.rfind(marker)
        if position < 0:
            raise RemoteExecutionError(f"Remote probe did not return its marker: {execution.stdout[-1000:]}")
        try:
            data: object = json.loads(execution.stdout[position + len(marker) :].strip())
        except json.JSONDecodeError as error:
            raise RemoteProtocolError("Remote probe returned invalid JSON") from error
        if not isinstance(data, dict):
            raise RemoteProtocolError("Remote probe result is not a JSON object")
        return cast(dict[str, Any], data)

    def start_job(self, job: str, script_name: str, arguments: list[str]) -> JobStart:
        """Start a detached remote script and return its public status paths."""

        self.sync_bundle()
        remote_root = str(self.remote_root)
        status_path = str(self.remote_root / "status" / f"{job}.json")
        log_path = str(self.remote_root / "logs" / f"{job}.log")
        script_path = str(self.remote_root / "remote" / script_name)
        payload = json.dumps(arguments)
        code = f"""
import json, os, pathlib, subprocess, sys
root = pathlib.Path.cwd() / {remote_root!r}
(root / 'status').mkdir(parents=True, exist_ok=True)
(root / 'logs').mkdir(parents=True, exist_ok=True)
status_path = pathlib.Path.cwd() / {status_path!r}
log_path = pathlib.Path.cwd() / {log_path!r}
script_path = pathlib.Path.cwd() / {script_path!r}
args = json.loads({payload!r})
with log_path.open('ab', buffering=0) as log:
    process = subprocess.Popen(
        [sys.executable, str(script_path), *args],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
print('AI_COMIC_JOB_JSON=' + json.dumps({{'job': {job!r}, 'pid': process.pid}}))
"""
        execution = self._client.execute_python(code, timeout_seconds=30)
        marker = "AI_COMIC_JOB_JSON="
        position = execution.stdout.rfind(marker)
        if position < 0:
            raise RemoteExecutionError(f"Remote job did not start cleanly: {execution.stdout[-1000:]}")
        data = json.loads(execution.stdout[position + len(marker) :].strip())
        return JobStart(job=job, pid=int(data["pid"]), status_path=status_path, log_path=log_path)

    def read_status(self, job: str) -> dict[str, Any] | None:
        """Return a credential-free remote job status document when available."""

        remote_path = str(self.remote_root / "status" / f"{job}.json")
        if not self._client.exists(remote_path):
            return None
        try:
            data: object = json.loads(self._client.download_bytes(remote_path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RemoteProtocolError(f"Remote status is invalid: {remote_path}") from error
        if not isinstance(data, dict):
            raise RemoteProtocolError(f"Remote status is not a JSON object: {remote_path}")
        return cast(dict[str, Any], data)

    def read_log_tail(self, job: str, max_bytes: int = 16_000) -> str:
        """Return the tail of a credential-free remote job log."""

        remote_path = str(self.remote_root / "logs" / f"{job}.log")
        if not self._client.exists(remote_path):
            return ""
        return self._client.download_bytes(remote_path)[-max_bytes:].decode("utf-8", errors="replace")

    def fetch_artifacts(self, job: str, *, cleanup_remote_mirror: bool = True) -> list[dict[str, object]]:
        """Download, verify, and optionally remove explicit Jupyter mirror files."""

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
            remote_path = str(self.remote_root / PurePosixPath(mirror))
            content = self._client.download_bytes(remote_path)
            digest = hashlib.sha256(content).hexdigest()
            expected = str(item.get("sha256", ""))
            if expected and digest != expected:
                raise RemoteProtocolError(f"Artifact SHA-256 mismatch for {target_relative}: {digest} != {expected}")
            target = (self._settings.root / target_relative).resolve()
            if self._settings.root.resolve() not in target.parents:
                raise RemoteProtocolError(f"Artifact target escapes the local project: {target_relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".download")
            temporary.write_bytes(content)
            temporary.replace(target)
            metadata_remote = remote_path + ".meta.json"
            if self._client.exists(metadata_remote):
                metadata_target = target.with_suffix(target.suffix + ".meta.json")
                metadata_target.write_bytes(self._client.download_bytes(metadata_remote))
                if cleanup_remote_mirror:
                    self._client.delete(metadata_remote)
            if cleanup_remote_mirror:
                self._client.delete(remote_path)
            fetched.append({"target": target_relative, "sha256": digest, "bytes": len(content)})
        return fetched

    def stop_generation(self) -> str:
        """Request a recoverable stop before the next queued remote job begins."""

        remote_path = str(self.remote_root / "production" / "STOP")
        self._client.upload_bytes(remote_path, b"stop requested by local control plane\n")
        return remote_path

    def resume_generation(self) -> str:
        """Remove only the project-local stop signal; accepted outputs remain intact."""

        remote_path = str(self.remote_root / "production" / "STOP")
        if self._client.exists(remote_path):
            self._client.delete(remote_path)
        return remote_path

    def wait(self, job: str, timeout_seconds: float, poll_seconds: float = 10) -> dict[str, Any]:
        """Wait for a detached job without restarting it on transient local disconnects."""

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
