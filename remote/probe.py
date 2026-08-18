"""Read-only probe for the AMD execution container."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from runtime_common import discover_mounts, public_mounts


def command(arguments: list[str], timeout: float = 20) -> dict[str, object]:
    """Run a bounded read-only command and capture a short public result."""

    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
        return {"code": result.returncode, "stdout": result.stdout[-12000:], "stderr": result.stderr[-4000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": f"{type(error).__name__}: {error}"}


def main() -> dict[str, object]:
    """Collect storage, device-node, ROCm, and existing-runtime evidence."""

    mounts = discover_mounts()
    device_nodes = sorted(
        str(path)
        for pattern in ("/dev/dri/renderD*", "/dev/dri/card*", "/dev/kfd")
        for path in Path("/").glob(pattern.lstrip("/"))
    )
    data_root = Path("/ai-comic-series")
    model_progress = []
    if data_root.is_dir():
        for path in sorted((data_root / "models").rglob("*")):
            if path.is_file():
                try:
                    model_progress.append(
                        {
                            "path": str(path.relative_to(data_root)),
                            "bytes": path.stat().st_size,
                        }
                    )
                except OSError:
                    continue
    result: dict[str, object] = {
        "platform": platform.platform(),
        "python": sys.version,
        "uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "cpu_count": os.cpu_count(),
        "memory": command(["bash", "-lc", "free -b | sed -n '1,2p'"]),
        "mounts": public_mounts(mounts),
        "device_nodes": device_nodes,
        "rocm_smi": command(
            [
                "bash",
                "-lc",
                "command -v rocm-smi >/dev/null && rocm-smi --showproductname --showmeminfo vram --showuse || true",
            ],
            30,
        ),
        "rocminfo_agents": command(
            [
                "bash",
                "-lc",
                "command -v rocminfo >/dev/null && rocminfo | grep -E '^[[:space:]]*Name:|Marketing Name' | head -80 || true",
            ],
            30,
        ),
        "workspace": {
            "exists": Path("/workspace").is_dir(),
            "free": shutil.disk_usage("/workspace").free if Path("/workspace").is_dir() else None,
        },
        "local_rocm_wheels": sorted(str(path) for path in Path("/").glob("*rocm*.whl")),
        "data_root": {
            "path": str(data_root),
            "exists": data_root.is_dir(),
            "free": shutil.disk_usage(data_root).free if data_root.is_dir() else None,
            "model_files": model_progress[-40:],
        },
    }
    print("AI_COMIC_PROBE_JSON=" + json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
