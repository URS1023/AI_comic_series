"""Tests for hash-bound human review gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from remote.generate import GenerationWorker
from scripts.approve_assets import parse_rejections


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_approval(project: Path, filename: str, relative: str, content: bytes) -> None:
    approval = {
        "state": "approved",
        "assets": [{"path": relative, "sha256": digest(content)}],
    }
    path = project / "production" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(approval), encoding="utf-8")


def worker(tmp_path: Path, stage: str) -> GenerationWorker:
    data_root = tmp_path / "data"
    state = {
        "data_root": str(data_root),
        "ffmpeg": "ffmpeg",
        "workers": [{"port": 8188}],
    }
    return GenerationWorker(tmp_path, state, tmp_path / "status.json", stage)


def test_rejection_parser_requires_reason() -> None:
    assert parse_rejections(["v2-s022:extra hand"]) == {"v2-s022": "extra hand"}
    with pytest.raises(ValueError, match="expected scene-id:reason"):
        parse_rejections(["v2-s022"])


def test_keyframe_gate_is_bound_to_exact_anchor_hash(tmp_path: Path) -> None:
    relative = "assets/generated/anchors/chen-yan.png"
    content = b"approved-anchor"
    asset = tmp_path / "data" / "project-assets" / relative
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(content)
    write_approval(tmp_path, "anchor-approval.json", relative, content)
    generation = worker(tmp_path, "keyframes")
    jobs = [{"references": [relative]}]

    generation.enforce_review_gates(jobs)
    asset.write_bytes(b"changed-after-review")

    with pytest.raises(RuntimeError, match="changed after review"):
        generation.enforce_review_gates(jobs)


def test_full_video_gate_requires_ninety_percent_sample_pass(tmp_path: Path) -> None:
    relative = "assets/generated/keyframes/v2-s001.png"
    content = b"approved-keyframe"
    asset = tmp_path / "data" / "project-assets" / relative
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(content)
    write_approval(tmp_path, "keyframe-approval.json", relative, content)
    sample_ids = [f"v2-s{index:03d}" for index in range(1, 11)]
    sample = {
        "state": "approved",
        "passRate": 0.8,
        "assets": [
            {"id": scene_id, "path": f"assets/generated/video/{scene_id}.mp4", "sha256": "unused"}
            for scene_id in sample_ids
        ],
        "rejectedSceneIds": ["v2-s001", "v2-s002"],
    }
    sample_path = tmp_path / "production" / "video-sample-approval.json"
    sample_path.write_text(json.dumps(sample), encoding="utf-8")
    generation = worker(tmp_path, "videos")
    jobs = [
        {
            "id": f"video-{scene_id}",
            "references": [relative],
            "representativeSample": True,
        }
        for scene_id in sample_ids
    ]

    with pytest.raises(RuntimeError, match="below 90%"):
        generation.enforce_review_gates(jobs)
