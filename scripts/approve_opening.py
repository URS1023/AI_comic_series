"""Promote a visually reviewed opening preview and bind its exact hash."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def approve_opening(project_root: Path, reviewer: str) -> Path:
    plan = json.loads((project_root / "production" / "opening-plan.json").read_text(encoding="utf-8"))
    preview = project_root / str(plan["previewPath"])
    approved = project_root / str(plan["approvedPath"])
    if not preview.is_file() or preview.stat().st_size == 0:
        raise FileNotFoundError(f"Opening preview is missing: {preview}")
    approved.parent.mkdir(parents=True, exist_ok=True)
    temporary = approved.with_suffix(approved.suffix + ".approved")
    shutil.copy2(preview, temporary)
    temporary.replace(approved)
    document = {
        "version": 1,
        "stage": "opening",
        "state": "approved",
        "reviewer": reviewer,
        "reviewedAt": datetime.now(UTC).isoformat(),
        "visualReviewConfirmed": True,
        "sourcePreview": str(preview.relative_to(project_root)).replace("\\", "/"),
        "path": str(approved.relative_to(project_root)).replace("\\", "/"),
        "sha256": sha256(approved),
        "bytes": approved.stat().st_size,
    }
    output = project_root / "production" / "opening-approval.json"
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--confirm-visual-review", action="store_true")
    parser.add_argument("--project", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    if not args.confirm_visual_review:
        parser.error("--confirm-visual-review is required after watching the complete opening preview")
    print(approve_opening(args.project.resolve(), args.reviewer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
