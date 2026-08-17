"""Build an ASS caption rail from the same HBG style used by HTML preview."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def timestamp(seconds: float) -> str:
    """Format seconds as ASS ``H:MM:SS.cc`` time."""

    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def ass_text(value: str) -> str:
    """Escape user text for an ASS dialogue field."""

    return value.replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def build(project_root: Path = PROJECT_ROOT) -> Path:
    """Write ``assets/captions/captions.ass`` from audio and style truth."""

    audio = json.loads((project_root / "audio_meta.json").read_text(encoding="utf-8"))
    style = json.loads((project_root / "HBG_STYLE.json").read_text(encoding="utf-8"))
    canvas = style["canvas"]
    captions = style["captions"]
    body_start = float(audio["opening"]["bodyStart"])
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {int(canvas["width"])}
PlayResY: {int(canvas["height"])}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Rail,{captions["fontFamily"]},{int(captions["fontSize"])},&H00EFF2F5,&H00EFF2F5,&H000F0F0F,{captions["assBoxColor"]},-1,0,0,0,100,100,{float(captions["letterSpacingEm"]) * 10:.2f},0,3,{int(captions["boxPadding"])},0,2,120,120,{int(captions["bottom"])},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for cue in audio["captions"]:
        start = body_start + float(cue["start"])
        end = body_start + float(cue["end"])
        events.append(f"Dialogue: 0,{timestamp(start)},{timestamp(end)},Rail,,0,0,0,,{ass_text(str(cue['text']))}")
    output = project_root / "assets" / "captions" / "captions.ass"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(build())
