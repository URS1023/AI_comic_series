"""Pure helper tests for media QA code."""

from __future__ import annotations

from PIL import Image, ImageDraw

from remote.video_motion import analyze_frame_sequence, motion_errors
from scripts.qa_generated_media import frame_rate


def test_frame_rate_parses_ffprobe_fraction() -> None:
    assert frame_rate("30/1") == 30
    assert round(frame_rate("24000/1001"), 3) == 23.976
    assert frame_rate("0/0") == 0


def detailed_still() -> Image.Image:
    image = Image.new("L", (256, 144), 24)
    draw = ImageDraw.Draw(image)
    for index in range(12):
        shade = 45 + index * 14
        draw.rectangle((index * 21, 0, index * 21 + 12, 143), fill=shade)
    draw.ellipse((72, 25, 165, 118), fill=210, outline=80, width=5)
    draw.line((0, 130, 255, 18), fill=128, width=4)
    return image


def test_motion_gate_rejects_repeated_and_exposure_only_frames() -> None:
    source = detailed_still()
    repeated = analyze_frame_sequence([source.copy() for _ in range(9)])
    exposure_only = analyze_frame_sequence(
        [source.point(lambda value, offset=offset: min(255, value + offset)) for offset in range(0, 45, 5)]
    )

    assert motion_errors(repeated)
    assert motion_errors(exposure_only)
    assert repeated["dynamicPairs"] == 0
    assert exposure_only["p90GlobalCompensatedChange"] < 0.006


def test_motion_gate_rejects_ken_burns_only_sequence() -> None:
    source = detailed_still()
    frames = []
    for index in range(9):
        scale = 1.0 + index * 0.004
        width = round(source.width / scale)
        height = round(source.height / scale)
        left = min(source.width - width, index)
        top = min(source.height - height, index // 2)
        crop = source.crop((left, top, left + width, top + height))
        frames.append(crop.resize(source.size, Image.Resampling.BILINEAR))

    analysis = analyze_frame_sequence(frames)

    assert motion_errors(analysis)
    assert analysis["dynamicPairs"] < analysis["requiredDynamicPairs"]


def test_motion_gate_accepts_local_articulated_change() -> None:
    source = detailed_still()
    frames = []
    for index in range(9):
        frame = source.copy()
        draw = ImageDraw.Draw(frame)
        x = 18 + index * 13
        draw.rectangle((x, 52, x + 30, 92), fill=245, outline=5, width=3)
        draw.ellipse((x + 6, 34 + index % 3, x + 24, 54 + index % 3), fill=185)
        frames.append(frame)

    analysis = analyze_frame_sequence(frames)

    assert motion_errors(analysis) == []
    assert analysis["dynamicPairs"] >= analysis["requiredDynamicPairs"]
    assert analysis["p90GlobalCompensatedChange"] >= 0.006
