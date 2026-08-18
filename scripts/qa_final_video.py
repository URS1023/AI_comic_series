"""Technical and visual-evidence verifier for the final encoded MP4."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from remote.video_motion import analyze_video_motion  # noqa: E402


def command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, check=False)


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def probe(path: Path) -> dict[str, Any]:
    data: object = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            text=True,
        )
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"FFprobe returned a non-object for {path}")
    return data


def extract(path: Path, time_seconds: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{time_seconds:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(output),
        ],
        check=True,
    )


def contact_sheet(rows: list[tuple[str, Path]], target: Path, columns: int = 5) -> None:
    width, height, label = 360, 203, 34
    count_rows = (len(rows) + columns - 1) // columns
    output = Image.new("RGB", (columns * width, count_rows * (height + label)), "#0F1014")
    draw = ImageDraw.Draw(output)
    font = ImageFont.truetype(str(PROJECT_ROOT / "assets" / "fonts" / "NotoSansSC-Variable.ttf"), 22)
    for index, (name, path) in enumerate(rows):
        with Image.open(path) as source:
            image = ImageOps.fit(source.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
        x = index % columns * width
        y = index // columns * (height + label)
        output.paste(image, (x, y))
        draw.rectangle((x, y + height, x + width, y + height + label), fill="#1C1410")
        draw.text((x + 10, y + height + 3), name, font=font, fill="#F5F2EF")
    target.parent.mkdir(parents=True, exist_ok=True)
    output.save(target, format="PNG", optimize=True)


def verify(video: Path, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    audio_meta = json.loads((project_root / "audio_meta.json").read_text(encoding="utf-8"))
    storyboard = json.loads((project_root / "STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))
    generated_report_path = project_root / "qa" / "generated-media-report.json"
    media = probe(video)
    errors: list[str] = []
    generated_report: dict[str, Any] = {}
    if not generated_report_path.is_file():
        errors.append("generated-media QA report is missing")
    else:
        generated_report = json.loads(generated_report_path.read_text(encoding="utf-8"))
        if generated_report.get("status") != "passed":
            errors.append("generated-media QA report is not passed")
    generated_by_id = {
        str(item.get("id")): item
        for item in generated_report.get("scenes", [])
        if isinstance(item, dict) and item.get("id")
    }
    source_graph: list[dict[str, str]] = []
    for scene in storyboard:
        source = project_root / scene["asset"]
        item = generated_by_id.get(str(scene["id"]))
        if not source.is_file():
            errors.append(f"{scene['id']}: source video is missing during final QA")
            continue
        digest = sha256(source)
        source_graph.append({"id": str(scene["id"]), "path": str(scene["asset"]), "sha256": digest})
        if not isinstance(item, dict) or item.get("sha256") != digest or item.get("errors"):
            errors.append(f"{scene['id']}: source video differs from its passed generated-media evidence")
    source_graph_sha = hashlib.sha256(
        json.dumps(source_graph, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    video_streams = [stream for stream in media.get("streams", []) if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in media.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        errors.append(f"expected one video stream, found {len(video_streams)}")
    if len(audio_streams) < 1:
        errors.append("missing audio stream")
    stream = video_streams[0] if video_streams else {}
    duration = float(media.get("format", {}).get("duration", 0))
    fps = float(Fraction(str(stream.get("avg_frame_rate", "0/1"))))
    expected_duration = float(audio_meta["totalDuration"])
    if stream.get("codec_name") != "h264":
        errors.append(f"video codec is {stream.get('codec_name')}, expected h264")
    if int(stream.get("width", 0)) != 1920 or int(stream.get("height", 0)) != 1080:
        errors.append(f"resolution is {stream.get('width')}x{stream.get('height')}, expected 1920x1080")
    if abs(fps - 30) > 0.01:
        errors.append(f"fps is {fps}, expected 30")
    if abs(duration - expected_duration) > 1 / 30 + 0.02:
        errors.append(f"duration differs from timeline: {duration:.3f} vs {expected_duration:.3f}")
    if audio_streams and audio_streams[0].get("codec_name") != "aac":
        errors.append(f"audio codec is {audio_streams[0].get('codec_name')}, expected aac")

    black = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-vf",
            "blackdetect=d=0.15:pix_th=0.02",
            "-an",
            "-f",
            "null",
            os.devnull,
        ]
    )
    black_events = [
        {"start": float(start), "end": float(end), "duration": float(length)}
        for start, end, length in re.findall(
            r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)", black.stderr
        )
    ]
    unexplained_black = [event for event in black_events if event["start"] < duration - 0.9]
    if unexplained_black:
        errors.append(f"unexplained black intervals: {unexplained_black}")

    silence = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-af",
            "silencedetect=n=-50dB:d=1.5",
            "-vn",
            "-f",
            "null",
            os.devnull,
        ]
    )
    silence_durations = [float(value) for value in re.findall(r"silence_duration:\s*([0-9.]+)", silence.stderr)]
    if any(value > 2.0 for value in silence_durations):
        errors.append(f"audio contains silence longer than 2 seconds: {silence_durations}")

    loudness = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            os.devnull,
        ]
    )
    integrated_values = re.findall(r"I:\s*(-?[0-9.]+) LUFS", loudness.stderr)
    peak_values = re.findall(r"Peak:\s*(-?[0-9.]+) dBFS", loudness.stderr)
    integrated = float(integrated_values[-1]) if integrated_values else None
    true_peak = float(peak_values[-1]) if peak_values else None
    if true_peak is None or true_peak > -3.0:
        errors.append(f"true peak lacks 3 dB headroom: {true_peak}")

    timeline_motion: list[dict[str, Any]] = []
    for scene in storyboard:
        try:
            evidence = analyze_video_motion(
                video,
                start_seconds=float(scene["start"]),
                duration_seconds=float(scene["duration"]),
                sample_count=9,
                trim_fraction=0.18,
                crop_bottom_fraction=0.28,
            )
        except (OSError, RuntimeError, ValueError) as error:
            evidence = {"status": "failed", "errors": [f"temporal analysis failed: {error}"]}
        evidence["id"] = scene["id"]
        timeline_motion.append(evidence)
        errors.extend(
            f"{scene['id']}: final timeline temporal motion gate: {message}" for message in evidence.get("errors", [])
        )

    frame_dir = project_root / "qa" / "frames" / "final"
    proof_times = [("opening-lead", 0.5), ("opening-flash", 3.6), ("opening-title", 6.2)]
    proof_times.extend((scene["id"], (float(scene["start"]) + float(scene["end"])) / 2) for scene in storyboard)
    proof_times.append(("ending", max(0, duration - 0.8)))
    rows: list[tuple[str, Path]] = []
    for name, time_seconds in proof_times:
        target = frame_dir / f"{name}.png"
        extract(video, time_seconds, target)
        rows.append((name, target))
    contact = project_root / "qa" / "contact-sheets" / "final-all-scenes.png"
    contact_sheet(rows, contact)
    report = {
        "status": "passed" if not errors else "failed",
        "file": str(video),
        "sha256": sha256(video),
        "bytes": video.stat().st_size,
        "duration": duration,
        "expectedDuration": expected_duration,
        "fps": fps,
        "resolution": [stream.get("width"), stream.get("height")],
        "videoCodec": stream.get("codec_name"),
        "audioCodec": audio_streams[0].get("codec_name") if audio_streams else None,
        "integratedLufs": integrated,
        "truePeakDbfs": true_peak,
        "blackEvents": black_events,
        "silenceDurations": silence_durations,
        "sourceGraphSha256": source_graph_sha,
        "sourceGraph": source_graph,
        "timelineMotion": timeline_motion,
        "contactSheet": str(contact.relative_to(project_root)).replace("\\", "/"),
        "errors": errors,
    }
    output = project_root / "qa" / "final-video-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    report = verify(args.video.resolve(), args.project.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
