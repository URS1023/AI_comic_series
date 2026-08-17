"""Unit tests for deterministic ASS time and text formatting."""

from __future__ import annotations

from scripts.build_ass_captions import ass_text, timestamp
from scripts.build_opening import ass_timestamp


def test_ass_timestamp_rounds_to_centiseconds() -> None:
    assert timestamp(65.678) == "0:01:05.68"
    assert ass_timestamp(4.757) == "0:00:04.76"


def test_ass_text_escapes_override_braces_and_newlines() -> None:
    assert ass_text("一{二}\n三") == "一\\{二\\}\\N三"
