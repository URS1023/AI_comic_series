"""Resume, verify, and materialize locked Hugging Face model files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from runtime_common import atomic_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--inside-venv", action="store_true")
    return parser.parse_args()


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_status(path: Path, state: str, phase: str, **details: object) -> None:
    atomic_json(path, {"job": "models", "state": state, "phase": phase, "updated": time.time(), **details})


def main() -> int:
    args = parse_args()
    project_root = Path.cwd()
    state_path = project_root / "state.json"
    if not state_path.is_file():
        raise RuntimeError("Remote bootstrap state.json is missing; run bootstrap first")
    runtime_state = json.loads(state_path.read_text(encoding="utf-8"))
    venv_python = Path(runtime_state["python"])
    if not args.inside_venv and Path(sys.executable).resolve() != venv_python.resolve():
        command = [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:], "--inside-venv"]
        return subprocess.run(command, cwd=project_root, check=False).returncode

    from huggingface_hub import hf_hub_download  # Imported only inside the verified remote venv.

    status_path = (project_root / args.status).resolve()
    manifest = json.loads((project_root / args.manifest).read_text(encoding="utf-8"))
    profile = manifest.get("profiles", {}).get(args.profile)
    if not isinstance(profile, dict):
        raise RuntimeError(f"Unknown model profile: {args.profile}")
    files = profile.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError(f"Model profile has no files: {args.profile}")
    data_root = Path(runtime_state["data_root"])
    cache_dir = data_root / "cache" / "huggingface"
    models_root = data_root / "models"
    completed: list[dict[str, object]] = []
    try:
        for index, item in enumerate(files, start=1):
            target = (models_root / str(item["target"])).resolve()
            expected_size = int(item["bytes"])
            expected_sha = str(item["sha256"]).lower()
            write_status(
                status_path,
                "running",
                "download",
                profile=args.profile,
                current=index,
                total=len(files),
                target=str(target),
                completed=completed,
            )
            if target.is_file() and target.stat().st_size == expected_size and digest(target) == expected_sha:
                completed.append(
                    {"target": str(target), "bytes": expected_size, "sha256": expected_sha, "reused": True}
                )
                continue
            cached = Path(
                hf_hub_download(
                    repo_id=str(item["repo"]),
                    filename=str(item["filename"]),
                    revision=str(item["revision"]),
                    cache_dir=cache_dir,
                )
            )
            if cached.stat().st_size != expected_size:
                raise RuntimeError(f"Size mismatch for {item['filename']}: {cached.stat().st_size} != {expected_size}")
            actual_sha = digest(cached)
            if actual_sha != expected_sha:
                raise RuntimeError(f"SHA-256 mismatch for {item['filename']}: {actual_sha} != {expected_sha}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".new")
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
            try:
                os.link(cached, temporary)
            except OSError:
                temporary.symlink_to(cached)
            os.replace(temporary, target)
            completed.append({"target": str(target), "bytes": expected_size, "sha256": expected_sha, "reused": False})
        write_status(status_path, "complete", "verified", profile=args.profile, completed=completed)
        return 0
    except Exception as error:
        write_status(
            status_path,
            "failed",
            "error",
            profile=args.profile,
            completed=completed,
            error=f"{type(error).__name__}: {error}",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
