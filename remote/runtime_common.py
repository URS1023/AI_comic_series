"""Shared, dependency-free helpers used by remote bootstrap jobs."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

GIB = 1024**3
PSEUDO_FILESYSTEMS = {
    "autofs",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "proc",
    "pstore",
    "securityfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


@dataclass(frozen=True, slots=True)
class MountCandidate:
    """Writable physical mount with measured capacity."""

    path: str
    filesystem: str
    source: str
    total: int
    used: int
    free: int


def _decode_mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[tuple[str, str, str]]:
    """Parse Linux ``/proc/self/mountinfo`` into path/filesystem/source tuples."""

    mounts: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        before, separator, after = raw.partition(" - ")
        if not separator:
            continue
        left = before.split()
        right = after.split()
        if len(left) < 5 or len(right) < 2:
            continue
        mounts.append((_decode_mount_path(left[4]), right[0], _decode_mount_path(right[1])))
    return mounts


def discover_mounts(mountinfo: str | None = None) -> list[MountCandidate]:
    """Return unique writable non-pseudo mounts ordered by free space."""

    text = mountinfo if mountinfo is not None else Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    candidates: dict[str, MountCandidate] = {}
    for path_text, filesystem, source in parse_mountinfo(text):
        path = Path(path_text)
        if filesystem in PSEUDO_FILESYSTEMS or not path.is_dir() or not os.access(path, os.W_OK):
            continue
        try:
            stats = os.statvfs(path)
        except OSError:
            continue
        total = stats.f_blocks * stats.f_frsize
        free = stats.f_bavail * stats.f_frsize
        used = max(0, total - free)
        previous = candidates.get(str(path))
        candidate = MountCandidate(str(path), filesystem, source, total, used, free)
        if previous is None or candidate.free > previous.free:
            candidates[str(path)] = candidate
    return sorted(candidates.values(), key=lambda item: (item.free, item.total), reverse=True)


def select_data_root(explicit: str, minimum_free_bytes: int = 100 * GIB) -> tuple[Path, list[MountCandidate]]:
    """Select or validate the remote data root without touching unrelated files."""

    mounts = discover_mounts()
    if explicit:
        root = Path(explicit).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        stats = os.statvfs(root)
        free = stats.f_bavail * stats.f_frsize
        if free < minimum_free_bytes:
            raise RuntimeError(
                f"Configured data root {root} has only {free / GIB:.1f} GiB free; "
                f"at least {minimum_free_bytes / GIB:.0f} GiB is required"
            )
        return root, mounts
    for mount in mounts:
        if mount.path != "/" and mount.free >= minimum_free_bytes:
            root = (Path(mount.path) / "ai-comic-series").resolve()
            root.mkdir(parents=True, exist_ok=True)
            return root, mounts
    summary = ", ".join(f"{item.path}={item.free / GIB:.1f}GiB" for item in mounts[:10])
    raise RuntimeError(f"No writable non-system mount has {minimum_free_bytes / GIB:.0f} GiB free: {summary}")


def atomic_json(path: Path, value: object) -> None:
    """Atomically write a UTF-8 JSON status document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def public_mounts(mounts: Iterable[MountCandidate]) -> list[dict[str, object]]:
    """Convert mount records to JSON-compatible public data."""

    return [asdict(item) for item in mounts]
