"""Verify that every visual approval is current and bound to exact media hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Review gate is not a JSON object: {path}")
    return value


def _verify_gate(
    project_root: Path,
    filename: str,
    expected: dict[str, str],
    *,
    require_zero_rejections: bool = True,
) -> Path:
    path = project_root / "production" / filename
    if not path.is_file():
        raise FileNotFoundError(f"Required visual review gate is missing: {path}")
    document = _load(path)
    if document.get("state") != "approved" or document.get("visualReviewConfirmed") is not True:
        raise RuntimeError(f"Visual review gate is not approved: {filename}")
    if require_zero_rejections and (document.get("rejections") or document.get("rejectedSceneIds")):
        raise RuntimeError(f"Visual review gate still contains rejected assets: {filename}")
    assets = document.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError(f"Visual review gate has no assets array: {filename}")
    by_id = {
        str(asset.get("id")): asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("id") is not None
    }
    if set(by_id) != set(expected):
        raise RuntimeError(f"Visual review gate does not cover the exact expected ids: {filename}")
    for asset_id, relative in expected.items():
        asset = by_id[asset_id]
        if str(asset.get("path")) != relative:
            raise RuntimeError(f"Approved path differs for {asset_id}: {filename}")
        media = project_root / relative
        metadata = media.with_suffix(media.suffix + ".meta.json")
        if not media.is_file() or not metadata.is_file():
            raise FileNotFoundError(f"Approved media or metadata is missing: {relative}")
        if asset.get("sha256") != sha256(media) or asset.get("metadataSha256") != sha256(metadata):
            raise RuntimeError(f"Approved media changed after visual review: {relative}")
    return path


def verify_review_gates(project_root: Path = PROJECT_ROOT) -> list[Path]:
    queue = _load(project_root / "production" / "generation-queue.json")
    jobs = [job for job in queue.get("jobs", []) if isinstance(job, dict)]
    storyboard_value: object = json.loads((project_root / "STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))
    if not isinstance(storyboard_value, list):
        raise RuntimeError("STORYBOARD_VIDEO.json must be an array")
    storyboard = [scene for scene in storyboard_value if isinstance(scene, dict)]
    package = _load(project_root / "publishing" / "package.json")
    covers = [cover for cover in package.get("covers", []) if isinstance(cover, dict)]

    anchors = {
        str(job["id"]).removeprefix("anchor-"): str(job["output"])
        for job in jobs
        if job.get("stage") == "anchors"
    }
    keyframes = {str(scene["id"]): str(scene["sourceImage"]) for scene in storyboard}
    motion_endframes = {
        str(job["motionSceneId"]): str(job["output"])
        for job in jobs
        if job.get("stage") == "motion-keyframes"
    }
    cover_art = {str(cover["id"]): str(cover["artwork"]) for cover in covers}
    full_videos = {str(scene["id"]): str(scene["asset"]) for scene in storyboard}

    return [
        _verify_gate(project_root, "anchor-approval.json", anchors),
        _verify_gate(project_root, "keyframe-approval.json", keyframes),
        _verify_gate(project_root, "motion-endframe-approval.json", motion_endframes),
        _verify_gate(project_root, "cover-approval.json", cover_art),
        _verify_gate(project_root, "full-video-approval.json", full_videos),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    paths = verify_review_gates(args.project.resolve())
    print(json.dumps({"status": "passed", "gates": [str(path) for path in paths]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
