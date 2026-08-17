"""Tests for exact two-ratio local cover typography."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from scripts.build_covers import build_variant


def test_cover_builder_outputs_exact_ratios(tmp_path: Path) -> None:
    source = tmp_path / "art.png"
    Image.new("RGB", (1920, 1080), "#20384A").save(source)
    horizontal = tmp_path / "horizontal.png"
    vertical = tmp_path / "vertical.png"

    build_variant(source, horizontal, (1600, 1200))
    build_variant(source, vertical, (1200, 1600))

    with Image.open(horizontal) as image:
        assert image.size == (1600, 1200)
    with Image.open(vertical) as image:
        assert image.size == (1200, 1600)
