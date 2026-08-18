"""Shared probe and MP4 contract helpers for remote and local video QA."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any


def frame_rate(value: object) -> float:
    """Parse an FFprobe rational frame-rate value without raising."""

    text = str(value or "")
    if not text or text == "0/0":
        return 0.0
    numerator, separator, denominator = text.partition("/")
    try:
        if not separator:
            return float(numerator)
        divisor = float(denominator)
        return float(numerator) / divisor if divisor else 0.0
    except ValueError:
        return 0.0


def mp4_has_faststart(path: Path) -> bool:
    """Return whether an MP4 has its top-level ``moov`` atom before ``mdat``."""

    try:
        file_size = path.stat().st_size
        offset = 0
        moov_offset: int | None = None
        mdat_offset: int | None = None
        with path.open("rb") as handle:
            while offset + 8 <= file_size:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) != 8:
                    return False
                box_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if box_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        return False
                    box_size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset
                if box_size < header_size or offset + box_size > file_size:
                    return False
                if box_type == b"moov" and moov_offset is None:
                    moov_offset = offset
                elif box_type == b"mdat" and mdat_offset is None:
                    mdat_offset = offset
                if moov_offset is not None and mdat_offset is not None:
                    return moov_offset < mdat_offset
                offset += box_size
    except OSError:
        return False
    return False


def video_contract_errors(
    data: dict[str, Any],
    *,
    expected_fps: float | None = None,
    faststart: bool | None = None,
) -> list[str]:
    """Return violations of the normalized generated-video contract."""

    errors: list[str] = []
    streams = data.get("streams", [])
    video_streams = [
        stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type", "video") == "video"
    ]
    if not video_streams:
        return ["no video stream"]
    stream = video_streams[0]
    codec = str(stream.get("codec_name", ""))
    pixel_format = str(stream.get("pix_fmt", ""))
    average_fps = frame_rate(stream.get("avg_frame_rate"))
    nominal_fps = frame_rate(stream.get("r_frame_rate"))
    format_info = data.get("format", {})
    if not isinstance(format_info, dict):
        format_info = {}
    try:
        duration = float(format_info.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    format_name = str(format_info.get("format_name", ""))
    if duration <= 0:
        errors.append("duration is not positive")
    if codec != "h264":
        errors.append(f"codec is {codec or 'missing'}, expected h264")
    if pixel_format != "yuv420p":
        errors.append(f"pixel format is {pixel_format or 'missing'}, expected yuv420p")
    if "mp4" not in format_name.split(","):
        errors.append(f"container is {format_name or 'missing'}, expected mp4")
    if average_fps <= 0 or nominal_fps <= 0:
        errors.append("frame rate is missing or invalid")
    elif abs(average_fps - nominal_fps) > 0.001:
        errors.append(f"frame rate is not CFR: avg={average_fps:.6f}, nominal={nominal_fps:.6f}")
    if expected_fps is not None and average_fps > 0 and abs(average_fps - expected_fps) > 0.01:
        errors.append(f"frame rate is {average_fps:.6f}, expected {expected_fps:.6f}")
    if faststart is False:
        errors.append("MP4 moov atom is not before mdat (+faststart missing)")
    return errors
