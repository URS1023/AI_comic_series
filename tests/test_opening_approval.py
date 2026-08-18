"""Tests for promotion of a visually reviewed real-video opening."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.approve_opening import approve_opening, sha256


def test_opening_approval_copies_preview_and_binds_hash(tmp_path: Path) -> None:
    plan = {
        "previewPath": "qa/opening-preview-r001.mp4",
        "approvedPath": "qa/opening-approved.mp4",
    }
    plan_path = tmp_path / "production" / "opening-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    preview = tmp_path / plan["previewPath"]
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"real-video-opening")

    gate_path = approve_opening(tmp_path, "test reviewer")

    approved = tmp_path / plan["approvedPath"]
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert approved.read_bytes() == preview.read_bytes()
    assert gate["state"] == "approved"
    assert gate["visualReviewConfirmed"] is True
    assert gate["sha256"] == sha256(approved)
    assert gate["bytes"] == approved.stat().st_size
