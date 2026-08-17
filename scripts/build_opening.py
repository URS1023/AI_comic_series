"""Build the text-verified HBG opening from real generated video clips."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(arguments: list[str], *, cwd: Path) -> None:
    """Run one media command and fail with its exact command on error."""

    subprocess.run(arguments, cwd=cwd, check=True)


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def write_opening_ass(
    target: Path,
    *,
    audio: dict[str, object],
    style: dict[str, object],
    labels: list[str],
) -> None:
    """Write exact Chinese lead, flash labels, and selected-life title."""

    canvas = style["canvas"]
    layout = style["opening"]["layout"]
    opening = audio["opening"]
    flash = opening["flash"]
    flash_piece = float(flash["duration"]) / len(labels)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {int(canvas['width'])}",
        f"PlayResY: {int(canvas['height'])}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Lead,Noto Sans SC,{int(layout['leadFontSize'])},&H00EFF2F5,&H00EFF2F5,&H00101010,&H780F1014,-1,0,0,0,100,100,2,0,3,10,0,5,140,140,0,1",
        f"Style: Flash,Noto Sans SC,{int(layout['flashLabelFontSize'])},&H00EFF2F5,&H00EFF2F5,&H00101010,&H780F1014,-1,0,0,0,100,100,2,0,3,9,0,5,160,160,0,1",
        f"Style: Title,Noto Sans SC,{int(layout['primaryTitleFontSize'])},&H00EFF2F5,&H00EFF2F5,&H00101010,&H900F1014,-1,0,0,0,100,100,1,0,3,11,0,5,180,180,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,{ass_timestamp(float(opening['lead']['start']))},{ass_timestamp(float(opening['lead']['end']))},Lead,,0,0,0,,今天体验的人生副本是……",
    ]
    for index, label in enumerate(labels):
        start = float(flash["start"]) + index * flash_piece
        end = start + flash_piece
        lines.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Flash,,0,0,0,,{label}")
    lines.append(
        f"Dialogue: 0,{ass_timestamp(float(flash['end']))},{ass_timestamp(float(audio['openingDuration']))},Title,,0,0,0,,高考回档\\N雨夜白月光"
    )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def build(project_root: Path = PROJECT_ROOT) -> Path:
    """Render a revisioned opening preview; approval remains a separate step."""

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required")
    audio = json.loads((project_root / "audio_meta.json").read_text(encoding="utf-8"))
    style = json.loads((project_root / "HBG_STYLE.json").read_text(encoding="utf-8"))
    spec = json.loads((project_root / "PROJECT_SPEC.json").read_text(encoding="utf-8"))
    plan = json.loads((project_root / "production" / "opening-plan.json").read_text(encoding="utf-8"))
    storyboard = json.loads((project_root / "STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))
    scenes = {scene["id"]: scene for scene in storyboard}
    work = project_root / "qa" / "opening-work"
    work.mkdir(parents=True, exist_ok=True)
    required_ids = [plan["leadScene"], *plan["flashScenes"], plan["revealScene"]]
    for scene_id in required_ids:
        path = project_root / scenes[scene_id]["asset"]
        if not path.is_file():
            raise FileNotFoundError(f"Opening source video is missing: {path}")

    opening = audio["opening"]
    flash_duration = float(opening["flash"]["duration"])
    piece_duration = flash_duration / len(plan["flashScenes"])
    segments: list[Path] = []

    def segment(scene_id: str, duration: float, name: str) -> Path:
        source = project_root / scenes[scene_id]["asset"]
        output = work / f"{name}.mp4"
        run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-t",
                f"{duration:.6f}",
                "-an",
                "-vf",
                "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30,setsar=1",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "16",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ],
            cwd=project_root,
        )
        return output

    segments.append(segment(plan["leadScene"], float(opening["flash"]["start"]), "lead"))
    for index, scene_id in enumerate(plan["flashScenes"], start=1):
        segments.append(segment(scene_id, piece_duration, f"flash-{index:02d}"))
    reveal_duration = float(audio["openingDuration"]) - float(opening["flash"]["end"])
    segments.append(segment(plan["revealScene"], reveal_duration, "reveal"))

    concat = work / "concat.txt"
    concat.write_text("\n".join(f"file '{item.as_posix()}'" for item in segments) + "\n", encoding="utf-8")
    picture = work / "picture.mp4"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c",
            "copy",
            str(picture),
        ],
        cwd=project_root,
    )

    opening_ass = work / "opening.ass"
    write_opening_ass(
        opening_ass, audio=audio, style=style, labels=[item["label"] for item in spec["opening"]["flashLives"]]
    )
    ratchet = work / "ratchet.m4a"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(project_root / spec["audio"]["ratchetClick"]),
            "-t",
            f"{flash_duration:.6f}",
            "-af",
            "atempo=1.8,volume=0.72",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(ratchet),
        ],
        cwd=project_root,
    )

    preview = project_root / plan["previewPath"]
    preview.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = (
        f"[0:v]subtitles='{opening_ass.as_posix()}':fontsdir='{(project_root / 'assets' / 'fonts').as_posix()}'[v];"
        f"[1:a]adelay={round(float(opening['lead']['start']) * 1000)}|{round(float(opening['lead']['start']) * 1000)}[lead];"
        f"[2:a]adelay={round(float(opening['reveal']['start']) * 1000)}|{round(float(opening['reveal']['start']) * 1000)}[reveal];"
        f"[3:a]adelay={round(float(opening['flash']['start']) * 1000)}|{round(float(opening['flash']['start']) * 1000)}[ratchet];"
        f"[4:a]adelay={round(float(opening['flash']['start']) * 1000)}|{round(float(opening['flash']['start']) * 1000)},volume=0.7[whoosh];"
        f"[lead][reveal][ratchet][whoosh]amix=inputs=4:duration=longest:normalize=0,atrim=0:{float(audio['openingDuration']):.6f},aresample=48000[a]"
    )
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(picture),
            "-i",
            str(project_root / opening["lead"]["path"]),
            "-i",
            str(project_root / opening["reveal"]["path"]),
            "-i",
            str(ratchet),
            "-i",
            str(project_root / spec["audio"]["rewindWhoosh"]),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
            str(preview),
        ],
        cwd=project_root,
    )
    return preview


if __name__ == "__main__":
    print(build())
