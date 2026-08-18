"""Finalize publishing metadata only after video and both cover gates pass."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.verify_review_gates import verify_review_gates
except ModuleNotFoundError:  # Direct ``python scripts/finalize_release.py`` execution.
    from verify_review_gates import verify_review_gates

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def artifact(project_root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(project_root)).replace("\\", "/"),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def finalize(video: Path, project_root: Path = PROJECT_ROOT) -> Path:
    """Write the release manifest and mark the publishing package complete."""

    review_gate_paths = verify_review_gates(project_root)
    package_path = project_root / "publishing" / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    final_report_path = project_root / "qa" / "final-video-report.json"
    generated_report_path = project_root / "qa" / "generated-media-report.json"
    alignment_report_path = project_root / "qa" / "video" / "voice-alignment.json"
    fixed_artifacts = [
        project_root / "assets" / "captions" / "captions.ass",
        project_root / "assets" / "audio" / "narration.m4a",
        project_root / "assets" / "audio" / "bgm" / "looped.m4a",
        alignment_report_path,
        generated_report_path,
        final_report_path,
    ]
    for path in fixed_artifacts:
        if not path.is_file():
            raise FileNotFoundError(f"Release evidence is missing: {path}")

    report = json.loads(final_report_path.read_text(encoding="utf-8"))
    if report.get("status") != "passed":
        raise RuntimeError("qa/final-video-report.json is not passed")
    if not video.is_file() or report.get("sha256") != sha256(video):
        raise RuntimeError("Final video is missing or differs from the passed QA report")
    generated_report = json.loads(generated_report_path.read_text(encoding="utf-8"))
    if generated_report.get("status") != "passed":
        raise RuntimeError("qa/generated-media-report.json is not passed")
    storyboard = json.loads((project_root / "STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))
    source_graph: list[dict[str, str]] = []
    for scene in storyboard:
        source = project_root / scene["asset"]
        if not source.is_file():
            raise FileNotFoundError(f"Generated source video is missing during finalization: {source}")
        source_graph.append({"id": str(scene["id"]), "path": str(scene["asset"]), "sha256": sha256(source)})
    source_graph_sha = hashlib.sha256(
        json.dumps(source_graph, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if report.get("sourceGraphSha256") != source_graph_sha:
        raise RuntimeError("Final-video QA is not bound to the current generated-video source graph")
    alignment_report = json.loads(alignment_report_path.read_text(encoding="utf-8"))
    if alignment_report.get("passed") is not True:
        raise RuntimeError("qa/video/voice-alignment.json is not passed")

    cover_paths: list[Path] = []
    for cover in package["covers"]:
        path = project_root / cover["output"]
        if cover.get("state") != "complete" or not path.is_file():
            raise RuntimeError(f"Cover is not complete: {cover['id']}")
        digest = sha256(path)
        if cover.get("sha256") != digest:
            raise RuntimeError(f"Cover changed after packaging: {cover['id']}")
        cover_paths.append(path)
    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project_root, text=True).strip()
    package["episode"].update(
        {
            "finalVideo": str(video.relative_to(project_root)).replace("\\", "/"),
            "sha256": report["sha256"],
            "bytes": video.stat().st_size,
            "durationSeconds": report["duration"],
            "state": "complete",
        }
    )
    package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "createdAt": datetime.now(UTC).isoformat(),
        "gitHead": git_head,
        "sourceManifest": json.loads((project_root / "SOURCE_MANIFEST.json").read_text(encoding="utf-8")),
        "artifacts": [
            artifact(project_root, path)
            for path in [video, *cover_paths, *fixed_artifacts, *review_gate_paths]
        ],
        "qualityReports": [
            "qa/video/voice-alignment.json",
            "qa/generated-media-report.json",
            "qa/final-video-report.json",
        ],
        "visualReviewGates": [
            str(path.relative_to(project_root)).replace("\\", "/") for path in review_gate_paths
        ],
    }
    output = project_root / "publishing" / "release-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    print(finalize(args.video.resolve(), args.project.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
