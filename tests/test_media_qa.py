"""Pure helper tests for media QA code."""

from __future__ import annotations

from scripts.qa_generated_media import frame_rate


def test_frame_rate_parses_ffprobe_fraction() -> None:
    assert frame_rate("30/1") == 30
    assert round(frame_rate("24000/1001"), 3) == 23.976
    assert frame_rate("0/0") == 0
