"""Structural proofs that the final comic cannot fall back to still-image motion."""

from __future__ import annotations

import json
import re
from pathlib import Path


def load_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_wan_graph_contains_temporal_latent_and_video_encode_chain() -> None:
    document = load_json("workflows/comfyui/api/wan22_i2v_14b.json")
    assert isinstance(document, dict)
    prompt = document["prompt"]
    assert isinstance(prompt, dict)

    assert prompt["1"]["class_type"] == "LoadImage"
    assert prompt["10"]["class_type"] == "WanImageToVideo"
    assert prompt["10"]["inputs"]["start_image"] == ["1", 0]
    assert prompt["11"]["class_type"] == "KSamplerAdvanced"
    assert prompt["11"]["inputs"]["latent_image"] == ["10", 2]
    assert prompt["12"]["class_type"] == "KSamplerAdvanced"
    assert prompt["12"]["inputs"]["latent_image"] == ["11", 0]
    assert prompt["13"] == {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["12", 0], "vae": ["9", 0]},
    }
    assert prompt["14"]["class_type"] == "CreateVideo"
    assert prompt["14"]["inputs"]["images"] == ["13", 0]
    assert prompt["15"]["class_type"] == "SaveVideo"
    assert prompt["15"]["inputs"]["video"] == ["14", 0]
    assert prompt["2"]["inputs"]["unet_name"] != prompt["4"]["inputs"]["unet_name"]


def test_master_composition_mounts_every_story_scene_as_video() -> None:
    storyboard = load_json("STORYBOARD_VIDEO.json")
    assert isinstance(storyboard, list)
    html = Path("index.html").read_text(encoding="utf-8")

    for scene in storyboard:
        assert isinstance(scene, dict)
        scene_id = re.escape(str(scene["id"]))
        asset = re.escape(str(scene["asset"]))
        assert re.search(
            rf'<video id="video-{scene_id}" class="clip scene-video" src="{asset}"[^>]*></video>',
            html,
        )
    assert '<img id="video-' not in html
    assert "background-image:" not in html


def test_final_render_runs_source_and_timeline_motion_gates() -> None:
    render_script = Path("scripts/render-final.ps1").read_text(encoding="utf-8")
    generated_gate = render_script.index("scripts\\qa_generated_media.py")
    render = render_script.index("npm run render")
    final_gate = render_script.index("scripts\\qa_final_video.py")

    assert generated_gate < render < final_gate
