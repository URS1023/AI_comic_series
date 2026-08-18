"""Release finalization tests for evidence gates and failure atomicity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import finalize_release


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_reviewed_media(tmp_path: Path, relative: str, content: bytes) -> dict[str, str]:
    media = tmp_path / relative
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(content)
    metadata = media.with_suffix(media.suffix + ".meta.json")
    write_json(metadata, {"sha256": digest(content)})
    return {
        "path": relative,
        "sha256": digest(content),
        "metadataSha256": digest(metadata.read_bytes()),
    }


def write_approval(tmp_path: Path, filename: str, stage: str, assets: list[dict[str, str]]) -> None:
    write_json(
        tmp_path / "production" / filename,
        {
            "version": 1,
            "stage": stage,
            "state": "approved",
            "reviewer": "test-reviewer",
            "visualReviewConfirmed": True,
            "assets": assets,
            "rejections": {},
            "rejectedSceneIds": [],
        },
    )


def release_fixture(tmp_path: Path, *, generated_status: str = "passed") -> tuple[Path, Path]:
    video = tmp_path / "renders" / "episode.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"final-video")
    cover_record = write_reviewed_media(tmp_path, "publishing/cover.png", b"cover")
    package_path = tmp_path / "publishing" / "package.json"
    write_json(
        package_path,
        {
            "episode": {"state": "pending-generation"},
            "covers": [
                {
                    "id": "horizontal",
                    "output": "publishing/cover.png",
                    "artwork": "publishing/cover.png",
                    "state": "complete",
                    "sha256": digest(b"cover"),
                }
            ],
        },
    )
    anchor_record = write_reviewed_media(tmp_path, "assets/generated/anchors/hero.png", b"anchor")
    keyframe_record = write_reviewed_media(tmp_path, "assets/generated/keyframes/s001.png", b"keyframe")
    source_record = write_reviewed_media(
        tmp_path,
        "assets/generated/video/s001.mp4",
        b"generated-wan-video",
    )
    storyboard = [
        {
            "id": "s001",
            "asset": "assets/generated/video/s001.mp4",
            "sourceImage": "assets/generated/keyframes/s001.png",
        }
    ]
    write_json(tmp_path / "STORYBOARD_VIDEO.json", storyboard)
    write_json(
        tmp_path / "production" / "generation-queue.json",
        {
            "jobs": [
                {
                    "id": "anchor-hero",
                    "stage": "anchors",
                    "output": "assets/generated/anchors/hero.png",
                },
                {
                    "id": "keyframe-s001",
                    "stage": "keyframes",
                    "output": "assets/generated/keyframes/s001.png",
                },
                {
                    "id": "video-s001",
                    "stage": "videos",
                    "output": "assets/generated/video/s001.mp4",
                },
            ]
        },
    )
    write_approval(
        tmp_path,
        "anchor-approval.json",
        "anchors",
        [{"id": "hero", **anchor_record}],
    )
    write_approval(
        tmp_path,
        "keyframe-approval.json",
        "keyframes",
        [{"id": "s001", **keyframe_record}],
    )
    write_approval(tmp_path, "motion-endframe-approval.json", "motion-keyframes", [])
    write_approval(
        tmp_path,
        "cover-approval.json",
        "covers",
        [{"id": "horizontal", **cover_record}],
    )
    write_approval(
        tmp_path,
        "full-video-approval.json",
        "full-videos",
        [{"id": "s001", **source_record}],
    )
    source_graph = [
        {
            "id": "s001",
            "path": "assets/generated/video/s001.mp4",
            "sha256": digest(b"generated-wan-video"),
        }
    ]
    source_graph_sha = hashlib.sha256(
        json.dumps(source_graph, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    write_json(
        tmp_path / "qa" / "final-video-report.json",
        {
            "status": "passed",
            "sha256": digest(b"final-video"),
            "duration": 12.5,
            "sourceGraphSha256": source_graph_sha,
        },
    )
    write_json(tmp_path / "qa" / "generated-media-report.json", {"status": generated_status})
    write_json(tmp_path / "qa" / "video" / "voice-alignment.json", {"passed": True})
    write_json(tmp_path / "SOURCE_MANIFEST.json", {"sha256": "source"})
    for relative in [
        "assets/captions/captions.ass",
        "assets/audio/narration.m4a",
        "assets/audio/bgm/looped.m4a",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    return video, package_path


def test_finalize_requires_passed_generated_media_before_mutating_package(tmp_path: Path) -> None:
    video, package_path = release_fixture(tmp_path, generated_status="failed")
    original = package_path.read_bytes()

    with pytest.raises(RuntimeError, match="generated-media-report.json is not passed"):
        finalize_release.finalize(video, tmp_path)

    assert package_path.read_bytes() == original


def test_finalize_rejects_source_video_changed_after_final_qa(tmp_path: Path) -> None:
    video, package_path = release_fixture(tmp_path)
    original = package_path.read_bytes()
    (tmp_path / "assets" / "generated" / "video" / "s001.mp4").write_bytes(b"tampered-video")

    with pytest.raises(RuntimeError, match="Approved media changed after visual review"):
        finalize_release.finalize(video, tmp_path)

    assert package_path.read_bytes() == original


def test_finalize_includes_all_quality_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video, package_path = release_fixture(tmp_path)
    monkeypatch.setattr(finalize_release.subprocess, "check_output", lambda *args, **kwargs: "deadbeef\n")

    manifest_path = finalize_release.finalize(video, tmp_path)

    package = json.loads(package_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert package["episode"]["state"] == "complete"
    assert {item["path"] for item in manifest["artifacts"]} >= {
        "qa/video/voice-alignment.json",
        "qa/generated-media-report.json",
        "qa/final-video-report.json",
    }
