"""Readable command-line interface for local control of the AMD execution node."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ai_comic_series.auth import refresh_amd_credentials
from ai_comic_series.config import RemoteCredentials, load_project_settings
from ai_comic_series.exceptions import ComicSeriesError
from ai_comic_series.jupyter import JupyterClient
from ai_comic_series.remote_manager import RemoteManager

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comicctl", description="AI comic-series production control plane")
    parser.add_argument("--config", type=Path, default=Path("config/project.toml"))
    subcommands = parser.add_subparsers(dest="area", required=True)
    remote = subcommands.add_parser("remote", help="Control the AMD Jupyter/ComfyUI execution node")
    actions = remote.add_subparsers(dest="action", required=True)
    actions.add_parser("sync", help="Upload the credential-free remote bundle")
    actions.add_parser("probe", help="Read mounts, GPU visibility, ROCm, runtime, and disk state")

    bootstrap = actions.add_parser("bootstrap", help="Install ROCm PyTorch, ComfyUI, FFmpeg, and start workers")
    bootstrap.add_argument("--data-root", default=None, help="Absolute remote data root; blank auto-detects")
    bootstrap.add_argument("--wait", action="store_true")
    bootstrap.add_argument("--timeout", type=float, default=3600)

    models = actions.add_parser("install-models", help="Download and SHA-256 verify a locked model profile")
    models.add_argument("--profile", default="wan22-i2v-14b-quality")
    models.add_argument("--wait", action="store_true")
    models.add_argument("--timeout", type=float, default=14400)

    generate = actions.add_parser("generate", help="Run one resumable anchors/keyframes/videos stage")
    generate.add_argument("stage", choices=["anchors", "keyframes", "videos"])
    generate.add_argument("--max-workers", type=int, default=8)
    generate.add_argument("--wait", action="store_true")
    generate.add_argument("--timeout", type=float, default=28800)

    status = actions.add_parser("status", help="Read a detached remote job status")
    status.add_argument("job")
    logs = actions.add_parser("logs", help="Read the tail of a detached remote job log")
    logs.add_argument("job")
    fetch = actions.add_parser("fetch", help="Download and SHA-256 verify completed artifacts")
    fetch.add_argument("job")
    fetch.add_argument("--keep-remote-mirror", action="store_true")
    actions.add_parser("stop", help="Stop before the next queued generation job; preserve completed outputs")
    actions.add_parser("resume", help="Remove the stop signal and allow the next generation run")
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _run_remote(args: argparse.Namespace) -> int:
    settings = load_project_settings(args.config)
    credentials = RemoteCredentials.from_environment()
    credentials = refresh_amd_credentials(settings.remote, credentials, required=False)
    with JupyterClient(settings.remote, credentials) as client:
        manager = RemoteManager(settings, client)
        if args.action == "sync":
            _print({"uploaded": manager.sync_bundle()})
        elif args.action == "probe":
            _print(manager.run_probe())
        elif args.action == "bootstrap":
            data_root = args.data_root if args.data_root is not None else settings.remote.data_root
            start = manager.start_job(
                "bootstrap",
                "bootstrap.py",
                ["--status", "status/bootstrap.json", "--data-root", data_root],
            )
            _print({"job": start.job, "pid": start.pid, "status": start.status_path, "log": start.log_path})
            if args.wait:
                _print(manager.wait("bootstrap", timeout_seconds=args.timeout))
        elif args.action == "install-models":
            start = manager.start_job(
                "models",
                "install_models.py",
                [
                    "--status",
                    "status/models.json",
                    "--manifest",
                    "config/models.json",
                    "--profile",
                    args.profile,
                ],
            )
            _print({"job": start.job, "pid": start.pid, "status": start.status_path, "log": start.log_path})
            if args.wait:
                _print(manager.wait("models", timeout_seconds=args.timeout))
        elif args.action == "generate":
            job = f"generate-{args.stage}"
            start = manager.start_job(
                job,
                "generate.py",
                [
                    "--status",
                    f"status/{job}.json",
                    "--queue",
                    "production/generation-queue.json",
                    "--stage",
                    args.stage,
                    "--max-workers",
                    str(args.max_workers),
                ],
            )
            _print({"job": start.job, "pid": start.pid, "status": start.status_path, "log": start.log_path})
            if args.wait:
                _print(manager.wait(job, timeout_seconds=args.timeout))
        elif args.action == "status":
            _print(manager.read_status(args.job) or {"state": "not-started", "job": args.job})
        elif args.action == "logs":
            print(manager.read_log_tail(args.job))
        elif args.action == "fetch":
            _print({"fetched": manager.fetch_artifacts(args.job, cleanup_remote_mirror=not args.keep_remote_mirror)})
        elif args.action == "stop":
            _print({"stopSignal": manager.stop_generation()})
        elif args.action == "resume":
            _print({"removedStopSignal": manager.resume_generation()})
        else:  # pragma: no cover - argparse enforces the choices
            raise AssertionError(f"Unhandled action: {args.action}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and convert domain failures into concise user-facing errors."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.area == "remote":
            return _run_remote(args)
        parser.error(f"Unknown command area: {args.area}")
    except ComicSeriesError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 2
