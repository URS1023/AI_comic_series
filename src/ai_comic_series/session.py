"""Long-lived, stdin-controlled AMD session that keeps rotated cookies in memory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ai_comic_series.config import RemoteCredentials, load_project_settings
from ai_comic_series.curl_import import apply_imported_target, parse_copy_as_curl, read_framed_curl_stdin
from ai_comic_series.exceptions import ComicSeriesError
from ai_comic_series.jupyter import JupyterClient
from ai_comic_series.remote_manager import RemoteManager

__all__ = ["main"]


def dispatch(manager: RemoteManager, command: dict[str, Any]) -> object:
    """Execute one bounded control-plane action from a JSON-line request."""

    action = str(command.get("action", ""))
    if action == "probe":
        return manager.run_probe()
    if action == "sync":
        return {"uploaded": manager.sync_bundle()}
    if action == "bootstrap":
        data_root = str(command.get("dataRoot", ""))
        started = manager.start_job(
            "bootstrap",
            "bootstrap.py",
            ["--status", "status/bootstrap.json", "--data-root", data_root],
        )
        return {"job": started.job, "pid": started.pid, "status": started.status_path, "log": started.log_path}
    if action == "install-models":
        profile = str(command["profile"])
        started = manager.start_job(
            "models",
            "install_models.py",
            [
                "--status",
                "status/models.json",
                "--manifest",
                "config/models.json",
                "--profile",
                profile,
            ],
        )
        return {"job": started.job, "pid": started.pid, "profile": profile}
    if action == "generate":
        stage = str(command["stage"])
        allowed = {
            "anchors",
            "cover-drafts",
            "covers",
            "keyframes",
            "motion-keyframes",
            "video-sample",
            "videos",
        }
        if stage not in allowed:
            raise ValueError(f"Unknown generation stage: {stage}")
        job = f"generate-{stage}"
        started = manager.start_job(
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
                str(int(command.get("maxWorkers", 8))),
            ],
        )
        return {"job": started.job, "pid": started.pid, "stage": stage}
    if action == "status":
        job = str(command["job"])
        return manager.read_status(job) or {"state": "not-started", "job": job}
    if action == "logs":
        return {"job": str(command["job"]), "tail": manager.read_log_tail(str(command["job"]))}
    if action == "wait":
        return manager.wait(
            str(command["job"]),
            timeout_seconds=float(command.get("timeout", 28_800)),
            poll_seconds=float(command.get("poll", 10)),
        )
    if action == "fetch":
        return {
            "fetched": manager.fetch_artifacts(
                str(command["job"]),
                cleanup_remote_mirror=not bool(command.get("keepRemoteMirror", False)),
            )
        }
    if action == "stop":
        return {"stopSignal": manager.stop_generation()}
    if action == "resume":
        return {"removedStopSignal": manager.resume_generation()}
    if action == "quit":
        return {"closing": True}
    raise ValueError(f"Unknown session action: {action}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Long-lived, stdin-controlled AMD session")
    parser.add_argument("config", nargs="?", type=Path, default=Path("config/project.toml"))
    parser.add_argument(
        "--curl-stdin",
        action="store_true",
        help="Read a multiline Copy-as-cURL prelude through stdin before JSON commands",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read JSON commands from stdin and emit one JSON response per command."""

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
    except ComicSeriesError as error:
        print(json.dumps({"session": "error", "error": f"{type(error).__name__}: {error}"}), flush=True)
        return 2

    try:
        with JupyterClient(settings.remote, credentials) as client:
            preflight = client.preflight_exact_get(preflight_url, preflight_headers) if preflight_url else None
            manager = RemoteManager(settings, client)
            ready: dict[str, object] = {"session": "ready", "remote": settings.remote.base_url}
            if preflight is not None:
                ready["preflight"] = preflight
            print(json.dumps(ready), flush=True)
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    command = json.loads(line)
                    if not isinstance(command, dict):
                        raise ValueError("Session command must be a JSON object")
                    result = dispatch(manager, command)
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
    except ComicSeriesError as error:
        print(json.dumps({"session": "error", "error": f"{type(error).__name__}: {error}"}), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
