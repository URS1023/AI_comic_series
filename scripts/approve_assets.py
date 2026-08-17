"""Write hash-bound review gates only after explicit visual inspection."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.qa_generated_media import contact_sheet, verify

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def parse_rejections(values: list[str]) -> dict[str, str]:
    rejections: dict[str, str] = {}
    for value in values:
        scene_id, separator, reason = value.partition(":")
        if not separator or not scene_id.strip() or not reason.strip():
            raise ValueError(f"Invalid --reject value {value!r}; expected scene-id:reason")
        rejections[scene_id.strip()] = reason.strip()
    return rejections


def image_assets(project_root: Path, stage: str) -> tuple[list[tuple[str, Path]], Path]:
    if stage == "anchors":
        characters = json.loads((project_root / "production" / "characters.json").read_text(encoding="utf-8"))
        rows = [
            (character_id, project_root / profile["output"])
            for character_id, profile in characters["characters"].items()
        ]
        return rows, project_root / "qa" / "contact-sheets" / "anchors.png"
    if stage == "covers":
        package = json.loads((project_root / "publishing" / "package.json").read_text(encoding="utf-8"))
        rows = [(variant["id"], project_root / variant["artwork"]) for variant in package["covers"]]
        return rows, project_root / "qa" / "contact-sheets" / "cover-art.png"
    storyboard = json.loads((project_root / "STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))
    rows = [(scene["id"], project_root / scene["sourceImage"]) for scene in storyboard]
    return rows, project_root / "qa" / "contact-sheets" / "keyframes.png"


def bind_assets(project_root: Path, rows: list[tuple[str, Path]]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for asset_id, path in rows:
        if not path.is_file():
            raise FileNotFoundError(f"Review asset is missing: {path}")
        metadata = path.with_suffix(path.suffix + ".meta.json")
        if not metadata.is_file():
            raise FileNotFoundError(f"Generation metadata is missing: {metadata}")
        relative = str(path.relative_to(project_root)).replace("\\", "/")
        assets.append(
            {
                "id": asset_id,
                "path": relative,
                "sha256": sha256(path),
                "metadataSha256": sha256(metadata),
            }
        )
    return assets


def approve(
    project_root: Path,
    stage: str,
    reviewer: str,
    rejections: dict[str, str],
) -> tuple[Path, dict[str, Any]]:
    if stage in {"anchors", "keyframes", "covers"}:
        rows, sheet = image_assets(project_root, stage)
        contact_sheet(rows, sheet, columns=4 if stage == "anchors" else 5)
        assets = bind_assets(project_root, rows)
        unknown = sorted(set(rejections) - {name for name, _ in rows})
        if unknown:
            raise ValueError(f"Rejected ids are not in {stage}: {unknown}")
        state = "approved" if not rejections else "rejected"
        document = {
            "version": 1,
            "stage": stage,
            "state": state,
            "reviewer": reviewer,
            "reviewedAt": datetime.now(UTC).isoformat(),
            "visualReviewConfirmed": True,
            "contactSheet": str(sheet.relative_to(project_root)).replace("\\", "/"),
            "assets": assets,
            "rejections": rejections,
        }
        filename = {
            "anchors": "anchor-approval.json",
            "keyframes": "keyframe-approval.json",
            "covers": "cover-approval.json",
        }[stage]
    else:
        report = verify(project_root, sample_only=True)
        if report["status"] != "passed":
            raise RuntimeError("Representative sample fails technical video QA; it cannot be visually approved")
        storyboard = json.loads((project_root / "STORYBOARD_VIDEO.json").read_text(encoding="utf-8"))
        profile = json.loads((project_root / "production" / "comfy-model-profile.json").read_text(encoding="utf-8"))
        sample_ids = set(profile["gates"]["videoSampleIds"])
        rows = [(scene["id"], project_root / scene["asset"]) for scene in storyboard if scene["id"] in sample_ids]
        assets = bind_assets(project_root, rows)
        unknown = sorted(set(rejections) - sample_ids)
        if unknown:
            raise ValueError(f"Rejected ids are not representative sample scenes: {unknown}")
        pass_rate = (len(rows) - len(rejections)) / len(rows)
        state = "approved" if pass_rate >= float(profile["gates"]["minimumVideoSamplePassRate"]) else "rejected"
        document = {
            "version": 1,
            "stage": stage,
            "state": state,
            "reviewer": reviewer,
            "reviewedAt": datetime.now(UTC).isoformat(),
            "visualReviewConfirmed": True,
            "technicalReport": "qa/generated-video-sample-report.json",
            "contactSheet": report["contactSheet"],
            "passRate": pass_rate,
            "minimumPassRate": profile["gates"]["minimumVideoSamplePassRate"],
            "assets": assets,
            "rejectedSceneIds": sorted(rejections),
            "rejections": rejections,
        }
        filename = "video-sample-approval.json"
    output = project_root / "production" / filename
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output, document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["anchors", "covers", "keyframes", "video-sample"])
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--confirm-visual-review", action="store_true")
    parser.add_argument("--reject", action="append", default=[])
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    if not args.confirm_visual_review:
        parser.error("--confirm-visual-review is required; generation success is not visual approval")
    output, document = approve(
        args.project.resolve(),
        args.stage,
        args.reviewer,
        parse_rejections(args.reject),
    )
    print(json.dumps({"output": str(output), "state": document["state"]}, ensure_ascii=False, indent=2))
    return 0 if document["state"] == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
