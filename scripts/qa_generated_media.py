"""Verify every generated keyframe/video and build complete contact sheets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def probe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    return json.loads(subprocess.check_output(command, text=True))


def frame_rate(value: str) -> float:
    return float(Fraction(value)) if value and value != "0/0" else 0.0


def extract_frame(video: Path, time_seconds: float, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0, time_seconds):.6f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(target),
        ],
        check=True,
    )


def image_similarity(first: Path, second: Path) -> float:
    with Image.open(first) as first_image, Image.open(second) as second_image:
        left = ImageOps.fit(first_image.convert("RGB"), (640, 360), method=Image.Resampling.LANCZOS)
        right = ImageOps.fit(second_image.convert("RGB"), (640, 360), method=Image.Resampling.LANCZOS)
        difference = ImageChops.difference(left, right)
        means = ImageStat.Stat(difference).mean
        return max(0.0, 1.0 - sum(means) / (len(means) * 255.0))


def freeze_events(video: Path) -> list[float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-vf",
            "freezedetect=n=-50dB:d=1.0",
            "-an",
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return [float(value) for value in re.findall(r"freeze_duration:\s*([0-9.]+)", result.stderr)]


def contact_sheet(rows: list[tuple[str, Path]], target: Path, *, columns: int = 5) -> None:
    thumb_width, thumb_height, label_height = 360, 203, 34
    lines = (len(rows) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * thumb_width, lines * (thumb_height + label_height)), "#0F1014")
    draw = ImageDraw.Draw(sheet)
    font_path = PROJECT_ROOT / "assets" / "fonts" / "NotoSansSC-Variable.ttf"
    font = ImageFont.truetype(str(font_path), 22)
    for index, (label, path) in enumerate(rows):
        with Image.open(path) as image:
            thumb = ImageOps.fit(image.convert("RGB"), (thumb_width, thumb_height), method=Image.Resampling.LANCZOS)
        x = (index % columns) * thumb_width
        y = (index // columns) * (thumb_height + label_height)
        sheet.paste(thumb, (x, y))
        draw.rectangle((x, y + thumb_height, x + thumb_width, y + thumb_height + label_height), fill="#1C1410")
        draw.text((x + 10, y + thumb_height + 3), label, font=font, fill="#F5F2EF")
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, format="PNG", optimize=True)


def verify(project_root: Path = PROJECT_ROOT, *, sample_only: bool = False) -> dict[str, Any]:
    storyboard = json.loads((project_root / "STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))
    if sample_only:
        profile = json.loads((project_root / "production" / "comfy-model-profile.json").read_text(encoding="utf-8"))
        sample_ids = set(profile["gates"]["videoSampleIds"])
        storyboard = [scene for scene in storyboard if scene["id"] in sample_ids]
        if len(storyboard) != len(sample_ids):
            raise RuntimeError("Representative video sample ids do not match the active storyboard")
    frame_dir = project_root / "qa" / "frames" / "generated"
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    midframes: list[tuple[str, Path]] = []
    high_risk: list[tuple[str, Path]] = []
    for scene in storyboard:
        scene_id = scene["id"]
        video = project_root / scene["asset"]
        keyframe = project_root / scene["sourceImage"]
        metadata_path = video.with_suffix(video.suffix + ".meta.json")
        scene_errors: list[str] = []
        if not video.is_file():
            errors.append(f"{scene_id}: missing video {video}")
            continue
        if not keyframe.is_file():
            errors.append(f"{scene_id}: missing keyframe {keyframe}")
            continue
        if not metadata_path.is_file():
            errors.append(f"{scene_id}: missing generation metadata {metadata_path}")
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("kind") != "wan-i2v":
            scene_errors.append("metadata kind is not wan-i2v")
        if not metadata.get("promptId") or not metadata.get("workflowSha256"):
            scene_errors.append("metadata lacks promptId/workflowSha256")
        actual_sha = sha256(video)
        if metadata.get("sha256") != actual_sha:
            scene_errors.append("video SHA-256 differs from generation metadata")
        media = probe(video)
        streams = [stream for stream in media.get("streams", []) if stream.get("codec_type") == "video"]
        if not streams:
            scene_errors.append("no video stream")
            continue
        stream = streams[0]
        duration = float(media.get("format", {}).get("duration", 0))
        fps = frame_rate(str(stream.get("avg_frame_rate", "0/0")))
        if int(stream.get("width", 0)) < 1200 or int(stream.get("height", 0)) < 675:
            scene_errors.append(f"generation resolution too small: {stream.get('width')}x{stream.get('height')}")
        if fps < 15:
            scene_errors.append(f"generation fps too small: {fps}")
        if duration + 0.15 < float(scene["duration"]):
            scene_errors.append(f"video duration {duration:.3f}s is shorter than scene {scene['duration']:.3f}s")
        first = frame_dir / f"{scene_id}-first.png"
        middle = frame_dir / f"{scene_id}-middle.png"
        last = frame_dir / f"{scene_id}-last.png"
        extract_frame(video, 0.0, first)
        extract_frame(video, max(0, duration / 2), middle)
        extract_frame(video, max(0, duration - 0.08), last)
        first_match = image_similarity(keyframe, first)
        first_mid = image_similarity(first, middle)
        first_last = image_similarity(first, last)
        if first_match < 0.72:
            scene_errors.append(f"first frame diverges from keyframe: similarity={first_match:.4f}")
        if first_mid > 0.997 and first_last > 0.997:
            scene_errors.append("clip is visually frozen across first/middle/last samples")
        freezes = freeze_events(video)
        if any(value > 1.25 for value in freezes):
            scene_errors.append(f"encoded frozen interval exceeds 1.25s: {freezes}")
        result = {
            "id": scene_id,
            "sha256": actual_sha,
            "duration": duration,
            "fps": fps,
            "width": stream.get("width"),
            "height": stream.get("height"),
            "firstFrameSimilarity": round(first_match, 5),
            "firstMiddleSimilarity": round(first_mid, 5),
            "firstLastSimilarity": round(first_last, 5),
            "freezeDurations": freezes,
            "highRisk": scene["highRisk"],
            "errors": scene_errors,
        }
        results.append(result)
        errors.extend(f"{scene_id}: {message}" for message in scene_errors)
        midframes.append((scene_id, middle))
        if scene["highRisk"]:
            high_risk.append((scene_id, middle))
    suffix = "video-sample" if sample_only else "all-scenes"
    complete_sheet = project_root / "qa" / "contact-sheets" / f"generated-{suffix}.png"
    risk_sheet = project_root / "qa" / "contact-sheets" / f"generated-{suffix}-high-risk.png"
    if midframes:
        contact_sheet(midframes, complete_sheet)
    if high_risk:
        contact_sheet(high_risk, risk_sheet)
    report = {
        "status": "passed" if not errors and len(results) == len(storyboard) else "failed",
        "scenesExpected": len(storyboard),
        "scenesChecked": len(results),
        "errors": errors,
        "scenes": results,
        "contactSheet": str(complete_sheet.relative_to(project_root)).replace("\\", "/"),
        "highRiskSheet": str(risk_sheet.relative_to(project_root)).replace("\\", "/"),
    }
    output_name = "generated-video-sample-report.json" if sample_only else "generated-media-report.json"
    output = project_root / "qa" / output_name
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--sample-only", action="store_true")
    args = parser.parse_args()
    report = verify(args.project.resolve(), sample_only=args.sample_only)
    print(json.dumps({key: value for key, value in report.items() if key != "scenes"}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
