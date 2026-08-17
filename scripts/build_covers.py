"""Add exact local Chinese typography to two approved no-text cover artworks."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def font(path: Path, size: int, variation: str | None = None) -> ImageFont.FreeTypeFont:
    selected = ImageFont.truetype(str(path), size)
    if variation:
        with suppress(AttributeError, OSError):
            selected.set_variation_by_name(variation)
    return selected


def build_variant(source: Path, target: Path, size: tuple[int, int]) -> None:
    """Crop approved artwork and add exact, high-contrast local title text."""

    width, height = size
    with Image.open(source) as image:
        canvas = ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    alpha = Image.new("L", (1, height))
    for y in range(height):
        normalized = y / max(1, height - 1)
        value = int(205 * max(0, min(1, (normalized - 0.46) / 0.54)))
        alpha.putpixel((0, y), value)
    alpha = alpha.resize(size)
    color = Image.new("RGBA", size, (15, 16, 20, 255))
    color.putalpha(alpha)
    overlay.alpha_composite(color)
    composed = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(composed)
    font_path = PROJECT_ROOT / "assets" / "fonts" / "NotoSansSC-Variable.ttf"
    horizontal = width > height
    title_font = font(font_path, 132 if horizontal else 114, "Black")
    subtitle_font = font(font_path, 70 if horizontal else 62, "Bold")
    hook_font = font(font_path, 39 if horizontal else 34, "Medium")
    left = int(width * (0.065 if horizontal else 0.08))
    baseline = int(height * (0.63 if horizontal else 0.69))
    draw.rectangle((left, baseline - 22, left + int(width * 0.18), baseline - 10), fill="#D8000F")
    draw.text((left, baseline), "高考回档", font=title_font, fill="#F5F2EF", stroke_width=2, stroke_fill="#0F1014")
    title_box = draw.textbbox((left, baseline), "高考回档", font=title_font, stroke_width=2)
    subtitle_y = title_box[3] + 10
    draw.text(
        (left, subtitle_y), "雨夜白月光", font=subtitle_font, fill="#CFEAFF", stroke_width=2, stroke_fill="#0F1014"
    )
    hook_y = subtitle_y + subtitle_font.size + 34
    hook = "426分后，我回到了三天前"
    hook_box = draw.textbbox((left, hook_y), hook, font=hook_font)
    padding_x, padding_y = 20, 12
    draw.rounded_rectangle(
        (hook_box[0] - padding_x, hook_box[1] - padding_y, hook_box[2] + padding_x, hook_box[3] + padding_y),
        radius=12,
        fill=(20, 15, 13, 215),
    )
    draw.text((left, hook_y), hook, font=hook_font, fill="#F5F2EF")
    target.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(target, format="PNG", optimize=True)


def build(project_root: Path = PROJECT_ROOT) -> list[Path]:
    package = json.loads((project_root / "publishing" / "package.json").read_text(encoding="utf-8"))
    approval_path = project_root / "production" / "cover-approval.json"
    if not approval_path.is_file():
        raise RuntimeError("production/cover-approval.json is required before adding final typography")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    if approval.get("state") != "approved":
        raise RuntimeError("Cover artwork review is not approved")
    approved = {entry["path"]: entry["sha256"] for entry in approval.get("assets", [])}
    outputs: list[Path] = []
    for variant in package["covers"]:
        source = project_root / variant["artwork"]
        if not source.is_file():
            raise FileNotFoundError(f"Approved no-text cover artwork is missing: {source}")
        relative = str(source.relative_to(project_root)).replace("\\", "/")
        if approved.get(relative) != sha256(source):
            raise RuntimeError(f"Cover artwork changed after visual approval: {relative}")
        target = project_root / variant["output"]
        size = (int(variant["width"]), int(variant["height"]))
        build_variant(source, target, size)
        with Image.open(target) as image:
            if image.size != size:
                raise RuntimeError(f"Cover size mismatch: {target} is {image.size}, expected {size}")
        variant["state"] = "complete"
        variant["sha256"] = sha256(target)
        outputs.append(target)
    (project_root / "publishing" / "package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return outputs


if __name__ == "__main__":
    for output in build():
        print(output)
