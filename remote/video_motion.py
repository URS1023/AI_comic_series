"""Dependency-light temporal-motion proof for generated video clips.

The gate deliberately compensates for global translation, zoom, and exposure changes
before measuring motion.  A still image wrapped in an MP4 with a Ken Burns move must
therefore not count as an animated comic shot.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

POLICY_VERSION = 1
DEFAULT_SAMPLE_COUNT = 9
ANALYSIS_SIZE = (128, 72)
SCALE_FACTORS = tuple(round(0.96 + index * 0.002, 3) for index in range(41))
SEARCH_RADIUS = 6
PAIR_RESIDUAL_THRESHOLD = 0.004
MIN_P90_RESIDUAL = 0.006
MIN_ACTIVE_TILE_FRACTION = 0.04
MAX_LOCALIZED_ACTIVE_TILE_FRACTION = 0.5
MIN_NON_GLOBAL_TILE_FRACTION = 0.5
MAX_CAMERA_ONLY_FRACTION = 0.75


def _normalized_frame(image: Image.Image) -> Image.Image:
    normalized = ImageOps.fit(image.convert("L"), ANALYSIS_SIZE, method=Image.Resampling.LANCZOS)
    return normalized.filter(ImageFilter.GaussianBlur(radius=1.0))


def _normalized_mae(first: Image.Image, second: Image.Image) -> float:
    return float(ImageStat.Stat(ImageChops.difference(first, second)).mean[0]) / 255.0


def _photometric_match(image: Image.Image, reference: Image.Image) -> Image.Image:
    """Remove whole-frame exposure/contrast changes before motion measurement."""

    source_stats = ImageStat.Stat(image)
    reference_stats = ImageStat.Stat(reference)
    source_mean = float(source_stats.mean[0])
    reference_mean = float(reference_stats.mean[0])
    source_std = max(float(source_stats.stddev[0]), 1.0)
    reference_std = max(float(reference_stats.stddev[0]), 1.0)
    gain = min(2.0, max(0.5, reference_std / source_std))
    lookup = [max(0, min(255, round((value - source_mean) * gain + reference_mean))) for value in range(256)]
    return image.point(lookup)


def _scaled_about_center(image: Image.Image, scale: float) -> Image.Image:
    width, height = image.size
    center_x = width / 2
    center_y = height / 2
    inverse = 1.0 / scale
    return image.transform(
        image.size,
        Image.Transform.AFFINE,
        (
            inverse,
            0.0,
            center_x - center_x * inverse,
            0.0,
            inverse,
            center_y - center_y * inverse,
        ),
        resample=Image.Resampling.BILINEAR,
        fillcolor=0,
    )


def _active_tile_fraction(first: Image.Image, second: Image.Image, threshold: float = 0.008) -> float:
    columns, rows = 8, 4
    active = 0
    for row in range(rows):
        top = round(row * first.height / rows)
        bottom = round((row + 1) * first.height / rows)
        for column in range(columns):
            left = round(column * first.width / columns)
            right = round((column + 1) * first.width / columns)
            box = (left, top, right, bottom)
            if _normalized_mae(first.crop(box), second.crop(box)) >= threshold:
                active += 1
    return active / (columns * rows)


def _solve_three_by_three(matrix: list[list[float]], vector: list[float]) -> tuple[float, float, float] | None:
    augmented = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-9:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return tuple(augmented[row][3] for row in range(3))  # type: ignore[return-value]


def _fit_affine_component(samples: Sequence[tuple[float, float, float]]) -> tuple[float, float, float] | None:
    matrix = [[0.0, 0.0, 0.0] for _ in range(3)]
    vector = [0.0, 0.0, 0.0]
    for x, y, value in samples:
        basis = (x, y, 1.0)
        for row in range(3):
            vector[row] += basis[row] * value
            for column in range(3):
                matrix[row][column] += basis[row] * basis[column]
    return _solve_three_by_three(matrix, vector)


def _local_motion_field(first: Image.Image, second: Image.Image) -> dict[str, float]:
    """Measure tiles whose motion cannot belong to one affine camera move."""

    columns, rows = 8, 4
    radius = 4
    matched_second = _photometric_match(second, first)
    samples: list[tuple[float, float, float, float, float]] = []
    for row in range(rows):
        top = max(radius, round(row * first.height / rows))
        bottom = min(first.height - radius, round((row + 1) * first.height / rows))
        for column in range(columns):
            left = max(radius, round(column * first.width / columns))
            right = min(first.width - radius, round((column + 1) * first.width / columns))
            if right - left < 6 or bottom - top < 6:
                continue
            tile = first.crop((left, top, right, bottom))
            if float(ImageStat.Stat(tile).stddev[0]) < 3.0:
                continue
            best = (float("inf"), 0, 0)
            for offset_y in range(-radius, radius + 1):
                for offset_x in range(-radius, radius + 1):
                    candidate = matched_second.crop(
                        (left + offset_x, top + offset_y, right + offset_x, bottom + offset_y)
                    )
                    residual = _normalized_mae(tile, candidate)
                    if residual < best[0]:
                        best = (residual, offset_x, offset_y)
            center_x = ((left + right) / first.width) - 1.0
            center_y = ((top + bottom) / first.height) - 1.0
            samples.append((center_x, center_y, float(best[1]), float(best[2]), float(best[0])))
    if len(samples) < 6:
        return {"trackedTiles": float(len(samples)), "nonGlobalTileFraction": 0.0, "medianTileResidual": 0.0}

    def fit(values: Sequence[tuple[float, float, float, float, float]]) -> tuple[tuple[float, ...], tuple[float, ...]]:
        x_fit = _fit_affine_component([(x, y, dx) for x, y, dx, _, _ in values])
        y_fit = _fit_affine_component([(x, y, dy) for x, y, _, dy, _ in values])
        if x_fit is None or y_fit is None:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        return x_fit, y_fit

    x_fit, y_fit = fit(samples)

    def vector_error(sample: tuple[float, float, float, float, float]) -> float:
        x, y, dx, dy, _ = sample
        predicted_x = x_fit[0] * x + x_fit[1] * y + x_fit[2]
        predicted_y = y_fit[0] * x + y_fit[1] * y + y_fit[2]
        return math.hypot(dx - predicted_x, dy - predicted_y)

    retained_count = max(6, math.ceil(len(samples) * 0.75))
    retained = sorted(samples, key=vector_error)[:retained_count]
    x_fit, y_fit = fit(retained)
    non_global = 0
    residuals: list[float] = []
    for x, y, dx, dy, residual in samples:
        predicted_x = x_fit[0] * x + x_fit[1] * y + x_fit[2]
        predicted_y = y_fit[0] * x + y_fit[1] * y + y_fit[2]
        deviation = math.hypot(dx - predicted_x, dy - predicted_y)
        residuals.append(residual)
        if deviation >= 1.25 or residual >= 0.018:
            non_global += 1
    return {
        "trackedTiles": float(len(samples)),
        "nonGlobalTileFraction": round(non_global / len(samples), 7),
        "medianTileResidual": round(float(median(residuals)), 7),
    }


def compensated_pair_change(first: Image.Image, second: Image.Image) -> dict[str, float]:
    """Measure non-rigid change after the best small global camera transform."""

    left = _normalized_frame(first)
    right = _normalized_frame(second)
    margin = SEARCH_RADIUS + 2
    core_box = (margin, margin, left.width - margin, left.height - margin)
    left_core = left.crop(core_box)
    raw = _normalized_mae(left_core, _photometric_match(right, left).crop(core_box))

    scaled_images = [_photometric_match(_scaled_about_center(right, scale), left) for scale in SCALE_FACTORS]
    coarse_offsets = range(-SEARCH_RADIUS, SEARCH_RADIUS + 1, 2)
    best = (float("inf"), 0, 0, 0)
    for scale_index, scaled in enumerate(scaled_images):
        for offset_y in coarse_offsets:
            for offset_x in coarse_offsets:
                shifted_box = (
                    margin + offset_x,
                    margin + offset_y,
                    left.width - margin + offset_x,
                    left.height - margin + offset_y,
                )
                residual = _normalized_mae(left_core, scaled.crop(shifted_box))
                if residual < best[0]:
                    best = (residual, scale_index, offset_x, offset_y)

    _, scale_index, offset_x, offset_y = best
    for refined_scale_index in range(max(0, scale_index - 1), min(len(SCALE_FACTORS), scale_index + 2)):
        scaled = scaled_images[refined_scale_index]
        for refined_y in range(max(-SEARCH_RADIUS, offset_y - 1), min(SEARCH_RADIUS, offset_y + 1) + 1):
            for refined_x in range(max(-SEARCH_RADIUS, offset_x - 1), min(SEARCH_RADIUS, offset_x + 1) + 1):
                shifted_box = (
                    margin + refined_x,
                    margin + refined_y,
                    left.width - margin + refined_x,
                    left.height - margin + refined_y,
                )
                candidate = scaled.crop(shifted_box)
                residual = _normalized_mae(left_core, candidate)
                if residual < best[0]:
                    best = (residual, refined_scale_index, refined_x, refined_y)

    residual, scale_index, offset_x, offset_y = best
    aligned_box = (
        margin + offset_x,
        margin + offset_y,
        left.width - margin + offset_x,
        left.height - margin + offset_y,
    )
    aligned = scaled_images[scale_index].crop(aligned_box)
    active_tiles = _active_tile_fraction(left_core, aligned)
    local_motion = _local_motion_field(left, right)
    explained = max(0.0, min(1.0, 1.0 - residual / raw)) if raw > 1e-6 else 1.0
    return {
        "rawChange": round(raw, 7),
        "globalCompensatedChange": round(residual, 7),
        "activeTileFraction": round(active_tiles, 7),
        **local_motion,
        "globalMotionExplainedFraction": round(explained, 7),
        "bestScale": SCALE_FACTORS[scale_index],
        "bestOffsetX": float(offset_x),
        "bestOffsetY": float(offset_y),
    }


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return float(ordered[index])


def analyze_frame_sequence(frames: Sequence[Image.Image]) -> dict[str, Any]:
    """Return evidence that sampled frames contain non-global temporal change."""

    normalized = [_normalized_frame(frame) for frame in frames]
    hashes = [hashlib.sha256(frame.tobytes()).hexdigest() for frame in normalized]
    pairs = [compensated_pair_change(first, second) for first, second in zip(normalized, normalized[1:], strict=False)]
    residuals = [float(item["globalCompensatedChange"]) for item in pairs]
    dynamic_pairs = [
        item
        for item in pairs
        if float(item["globalCompensatedChange"]) >= PAIR_RESIDUAL_THRESHOLD
        and float(item["activeTileFraction"]) >= MIN_ACTIVE_TILE_FRACTION
        and (
            float(item["activeTileFraction"]) <= MAX_LOCALIZED_ACTIVE_TILE_FRACTION
            or float(item["nonGlobalTileFraction"]) >= MIN_NON_GLOBAL_TILE_FRACTION
        )
    ]
    camera_only_pairs = [
        item
        for item in pairs
        if float(item["rawChange"]) >= PAIR_RESIDUAL_THRESHOLD
        and float(item["globalCompensatedChange"]) < PAIR_RESIDUAL_THRESHOLD
        and float(item["globalMotionExplainedFraction"]) >= 0.85
    ]
    pair_count = len(pairs)
    return {
        "policyVersion": POLICY_VERSION,
        "sampleFrames": len(normalized),
        "samplePairs": pair_count,
        "exactUniqueFrames": len(set(hashes)),
        "exactUniqueFrameRatio": round(len(set(hashes)) / len(hashes), 7) if hashes else 0.0,
        "dynamicPairs": len(dynamic_pairs),
        "requiredDynamicPairs": max(2, math.ceil(pair_count * 0.35)) if pair_count else 2,
        "cameraOnlyPairs": len(camera_only_pairs),
        "cameraOnlyPairFraction": round(len(camera_only_pairs) / pair_count, 7) if pair_count else 1.0,
        "medianGlobalCompensatedChange": round(float(median(residuals)), 7) if residuals else 0.0,
        "p90GlobalCompensatedChange": round(_nearest_rank(residuals, 0.9), 7),
        "maxGlobalCompensatedChange": round(max(residuals), 7) if residuals else 0.0,
        "pairs": pairs,
    }


def motion_errors(analysis: dict[str, Any]) -> list[str]:
    """Convert temporal evidence into hard rejection reasons."""

    errors: list[str] = []
    if int(analysis.get("sampleFrames", 0)) < 5:
        errors.append("fewer than five frames could be decoded for temporal analysis")
    if int(analysis.get("exactUniqueFrames", 0)) < 3:
        errors.append("decoded samples contain fewer than three unique frames")
    if int(analysis.get("dynamicPairs", 0)) < int(analysis.get("requiredDynamicPairs", 2)):
        errors.append(
            "insufficient non-global temporal changes: "
            f"{analysis.get('dynamicPairs', 0)}/{analysis.get('requiredDynamicPairs', 2)} sampled pairs"
        )
    if float(analysis.get("p90GlobalCompensatedChange", 0.0)) < MIN_P90_RESIDUAL:
        errors.append(
            "global-motion-compensated temporal change is too small: "
            f"p90={analysis.get('p90GlobalCompensatedChange', 0.0)}"
        )
    if float(analysis.get("cameraOnlyPairFraction", 1.0)) >= MAX_CAMERA_ONLY_FRACTION:
        errors.append(
            "most sampled change is explainable by camera pan/zoom alone: "
            f"fraction={analysis.get('cameraOnlyPairFraction', 1.0)}"
        )
    return errors


def analyze_video_motion(
    video: Path,
    *,
    ffmpeg: str = "ffmpeg",
    start_seconds: float = 0.0,
    duration_seconds: float,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    trim_fraction: float = 0.08,
    crop_bottom_fraction: float = 0.0,
) -> dict[str, Any]:
    """Decode evenly spaced frames and measure genuine within-shot motion."""

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if sample_count < 5:
        raise ValueError("sample_count must be at least five")
    trim = min(duration_seconds * trim_fraction, max(0.0, duration_seconds / 2 - 0.05))
    analysis_start = max(0.0, start_seconds + trim)
    analysis_duration = max(0.1, duration_seconds - trim * 2)
    frame_rate = sample_count / analysis_duration
    with tempfile.TemporaryDirectory(prefix="comic-motion-") as temporary:
        target_pattern = Path(temporary) / "frame-%03d.png"
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{analysis_start:.6f}",
                "-i",
                str(video),
                "-t",
                f"{analysis_duration:.6f}",
                "-vf",
                f"fps={frame_rate:.9f},scale={ANALYSIS_SIZE[0]}:{ANALYSIS_SIZE[1]}:flags=area,format=gray",
                "-frames:v",
                str(sample_count),
                str(target_pattern),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg temporal frame extraction failed: {result.stderr[-2000:]}")
        paths = sorted(Path(temporary).glob("frame-*.png"))
        frames: list[Image.Image] = []
        for path in paths:
            with Image.open(path) as source:
                frame = source.convert("L")
                if crop_bottom_fraction > 0:
                    keep_height = max(1, round(frame.height * (1.0 - crop_bottom_fraction)))
                    frame = frame.crop((0, 0, frame.width, keep_height))
                frames.append(frame.copy())
    analysis = analyze_frame_sequence(frames)
    analysis.update(
        {
            "source": str(video),
            "startSeconds": round(analysis_start, 6),
            "durationSeconds": round(analysis_duration, 6),
            "trimFraction": trim_fraction,
            "cropBottomFraction": crop_bottom_fraction,
        }
    )
    analysis["errors"] = motion_errors(analysis)
    analysis["status"] = "passed" if not analysis["errors"] else "failed"
    return analysis
