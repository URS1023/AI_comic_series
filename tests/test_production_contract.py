"""Tests for the all-video queue and locked model manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path


def load_json(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_generation_queue_has_unique_outputs_and_all_video_final_stage() -> None:
    queue = load_json("production/generation-queue.json")
    quality_policy = load_json("production/quality-policy.json")
    jobs = queue["jobs"]
    assert isinstance(jobs, list)

    outputs = [job["output"] for job in jobs]
    assert len(outputs) == len(set(outputs)) == 81
    anchor_jobs = [job for job in jobs if job["stage"] == "anchors"]
    assert len(anchor_jobs) == 11
    assert sum(job.get("anchorType") == "location" for job in anchor_jobs) == 7
    motion_keyframe_jobs = [job for job in jobs if job["stage"] == "motion-keyframes"]
    assert len(motion_keyframe_jobs) == 8
    assert all(job["kind"] == "qwen-edit" for job in motion_keyframe_jobs)
    assert all(str(job["output"]).startswith("assets/generated/endframes/") for job in motion_keyframe_jobs)
    assert all(str(job["references"][0]).startswith("assets/generated/keyframes/") for job in motion_keyframe_jobs)
    motion_outputs = {job["output"] for job in motion_keyframe_jobs}

    video_jobs = [job for job in jobs if job["stage"] == "videos"]
    assert len(video_jobs) == 29
    assert sum(job["representativeSample"] is True for job in video_jobs) == 10
    flf_jobs = [job for job in video_jobs if job["kind"] == "wan-flf2v"]
    i2v_jobs = [job for job in video_jobs if job["kind"] == "wan-i2v"]
    assert len(flf_jobs) == 8
    assert len(i2v_jobs) == 21
    assert all(len(job["references"]) == 2 and job["references"][1] in motion_outputs for job in flf_jobs)
    assert all(len(job["references"]) == 1 for job in i2v_jobs)
    assert all(str(job["output"]).endswith(".mp4") for job in video_jobs)
    assert all(str(job["references"][0]).endswith(".png") for job in video_jobs)
    assert all(float(job["duration"]) >= float(quality_policy["minimumClipSeconds"]) for job in video_jobs)
    assert all(float(job["duration"]) <= float(quality_policy["maximumClipSeconds"]) for job in video_jobs)
    assert len([job for job in jobs if job["stage"] == "cover-drafts"]) == 2
    assert len([job for job in jobs if job["stage"] == "covers"]) == 2


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
        "wan22_flf2v_14b.json": "SaveVideo",
    }
    for filename, output_type in workflows.items():
        document = load_json(f"workflows/comfyui/api/{filename}")
        prompt = document["prompt"]
        assert isinstance(prompt, dict)
        assert any(node["class_type"] == output_type for node in prompt.values())

    qwen_edit = load_json("workflows/comfyui/api/qwen_image_edit_2511.json")["prompt"]
    assert qwen_edit["12"]["inputs"]["reference_latents_method"] == "index_timestep_zero"
    assert qwen_edit["13"]["inputs"]["reference_latents_method"] == "index_timestep_zero"
    wan = load_json("workflows/comfyui/api/wan22_i2v_14b.json")["prompt"]
    assert wan["15"]["inputs"]["codec"] == {"codec": "auto"}
    wan_flf = load_json("workflows/comfyui/api/wan22_flf2v_14b.json")["prompt"]
    assert wan_flf["10"]["class_type"] == "WanFirstLastFrameToVideo"
    assert wan_flf["15"]["inputs"]["codec"] == {"codec": "auto"}


def test_sheet_map_covers_every_keyframe_once() -> None:
    sheet_map = load_json("assets/generated/prompts/SHEET_MAP.json")
    jobs = sheet_map["jobs"]
    assert isinstance(jobs, list)
    scene_ids = [scene_id for job in jobs for scene_id in job["sceneIds"]]
    assert len(scene_ids) == len(set(scene_ids)) == 29
    assert sheet_map["coverage"] == {
        "expectedScenes": 29,
        "mappedScenes": 29,
        "duplicates": 0,
        "missing": 0,
    }
