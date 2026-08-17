"""Tests for the all-video queue and locked model manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path


def load_json(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_generation_queue_has_unique_outputs_and_all_video_final_stage() -> None:
    queue = load_json("production/generation-queue.json")
    jobs = queue["jobs"]
    assert isinstance(jobs, list)

    outputs = [job["output"] for job in jobs]
    assert len(outputs) == len(set(outputs)) == 62
    video_jobs = [job for job in jobs if job["stage"] == "videos"]
    assert len(video_jobs) == 29
    assert all(job["kind"] == "wan-i2v" for job in video_jobs)
    assert all(str(job["output"]).endswith(".mp4") for job in video_jobs)
    assert all(str(job["references"][0]).endswith(".png") for job in video_jobs)


def test_storyboard_final_assets_are_video_only() -> None:
    storyboard = json.loads(Path("STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))

    assert len(storyboard) == 29
    assert all(scene["motion"] == "source-video" for scene in storyboard)
    assert all(scene["asset"].endswith(".mp4") for scene in storyboard)
    assert len({scene["asset"] for scene in storyboard}) == len(storyboard)


def test_locked_models_have_valid_sha256_and_positive_sizes() -> None:
    manifest = load_json("config/models.json")
    profiles = manifest["profiles"]
    assert isinstance(profiles, dict)
    assert {"qwen-image-production", "wan22-i2v-14b-quality"}.issubset(profiles)
    for profile in profiles.values():
        assert isinstance(profile, dict)
        for item in profile["files"]:
            assert int(item["bytes"]) > 1_000_000
            assert re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            assert re.fullmatch(r"[0-9a-f]{40}", item["revision"])


def test_api_workflows_have_real_output_nodes() -> None:
    workflows = {
        "qwen_image_2512_t2i.json": "SaveImage",
        "qwen_image_edit_2511.json": "SaveImage",
        "wan22_i2v_14b.json": "SaveVideo",
    }
    for filename, output_type in workflows.items():
        document = load_json(f"workflows/comfyui/api/{filename}")
        prompt = document["prompt"]
        assert isinstance(prompt, dict)
        assert any(node["class_type"] == output_type for node in prompt.values())
