"""Idempotent AMD ROCm, ComfyUI, and worker bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .media_contract import mp4_has_faststart, video_contract_errors
    from .runtime_common import atomic_json, public_mounts, select_data_root
except ImportError:  # Script execution on the remote node.
    from media_contract import mp4_has_faststart, video_contract_errors
    from runtime_common import atomic_json, public_mounts, select_data_root


WORKER_PORT_BASE = 18888
WORKER_MARKER_VERSION = 1
REQUIRED_COMFY_CLASSES = frozenset({"SaveImage", "SaveVideo"})
ROCM_SMOKE_CODE = r"""
import json
import torch

report = {
    "torch": torch.__version__,
    "hip": torch.version.hip,
    "available": torch.cuda.is_available(),
    "count": torch.cuda.device_count(),
    "devices": [],
}
for index in range(torch.cuda.device_count()):
    torch.cuda.set_device(index)
    device = {
        "index": index,
        "name": torch.cuda.get_device_name(index),
        "bytes": torch.cuda.get_device_properties(index).total_memory,
        "compute": {},
    }
    for label, dtype in (("fp16", torch.float16), ("bf16", torch.bfloat16)):
        try:
            left = torch.randn((1024, 1024), device=f"cuda:{index}", dtype=dtype)
            right = torch.randn((1024, 1024), device=f"cuda:{index}", dtype=dtype)
            result = left @ right
            torch.cuda.synchronize(index)
            finite = bool(torch.isfinite(result).all().item())
            mean_abs = float(result.float().abs().mean().item())
            device["compute"][label] = {
                "passed": finite and mean_abs > 0.0,
                "finite": finite,
                "mean_abs": mean_abs,
            }
            del left, right, result
            torch.cuda.empty_cache()
        except Exception as error:
            device["compute"][label] = {
                "passed": False,
                "error": f"{type(error).__name__}: {error}",
            }
    report["devices"].append(device)
print(json.dumps(report))
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--data-root", default="")
    return parser.parse_args()


def run(arguments: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=cwd, env=env, check=True)


def status(path: Path, state: str, phase: str, **details: object) -> None:
    atomic_json(path, {"job": "bootstrap", "state": state, "phase": phase, "updated": time.time(), **details})


def verify_media_tools(ffmpeg: str, ffprobe: str) -> None:
    """Exercise H.264 encoding, FFprobe, yuv420p, CFR, and faststart."""

    with tempfile.TemporaryDirectory(prefix="ai-comic-media-smoke-") as temporary_directory:
        target = Path(temporary_directory) / "smoke.mp4"
        encode = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:r=16:d=0.25",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "16",
                "-fps_mode",
                "cfr",
                "-movflags",
                "+faststart",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if encode.returncode != 0 or not target.is_file():
            detail = (encode.stderr or encode.stdout)[-2000:]
            raise RuntimeError(f"FFmpeg H.264 smoke encode failed: {detail}")
        inspect = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,codec_name,pix_fmt,r_frame_rate,avg_frame_rate:format=format_name,duration",
                "-of",
                "json",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode != 0:
            detail = (inspect.stderr or inspect.stdout)[-2000:]
            raise RuntimeError(f"FFprobe smoke inspection failed: {detail}")
        try:
            probe = json.loads(inspect.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("FFprobe smoke inspection returned invalid JSON") from error
        errors = video_contract_errors(probe, expected_fps=16.0, faststart=mp4_has_faststart(target))
        if errors:
            raise RuntimeError(f"FFmpeg/FFprobe media contract failed: {errors}")


def install_ffmpeg(venv_python: Path, venv_bin: Path) -> tuple[str, str]:
    """Install or locate a complete FFmpeg pair and verify the media contract."""

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if (not ffmpeg or not ffprobe) and hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("apt-get"):
        run(["apt-get", "update", "-qq"])
        run(["apt-get", "install", "-y", "-qq", "ffmpeg", "git"])
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        run([str(venv_python), "-m", "pip", "install", "imageio-ffmpeg"])
        code = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
        source = subprocess.check_output([str(venv_python), "-c", code], text=True).strip()
        target = venv_bin / "ffmpeg"
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(source)
        ffmpeg = str(target)
    if not ffprobe:
        raise RuntimeError(
            "A complete FFmpeg installation with ffprobe is required; imageio-ffmpeg alone is insufficient"
        )
    verify_media_tools(ffmpeg, ffprobe)
    return ffmpeg, ffprobe


def validate_rocm_report(report: dict[str, Any]) -> None:
    """Reject a ROCm report unless every visible GPU passed FP16 and BF16 compute."""

    if not report.get("hip") or report.get("available") is not True:
        raise RuntimeError(f"ROCm PyTorch is unavailable: {report}")
    count = int(report.get("count", 0))
    devices = report.get("devices", [])
    if count < 1 or not isinstance(devices, list) or len(devices) != count:
        raise RuntimeError(f"ROCm device enumeration is inconsistent: {report}")
    failures: list[str] = []
    for expected_index, device in enumerate(devices):
        if not isinstance(device, dict) or device.get("index") != expected_index:
            failures.append(f"gpu {expected_index}: malformed device record")
            continue
        compute = device.get("compute", {})
        for precision in ("fp16", "bf16"):
            result = compute.get(precision, {}) if isinstance(compute, dict) else {}
            if not isinstance(result, dict) or result.get("passed") is not True:
                detail = result.get("error", result) if isinstance(result, dict) else result
                failures.append(f"gpu {expected_index} {precision}: {detail}")
    if failures:
        raise RuntimeError("ROCm compute smoke failed: " + "; ".join(failures))


def verify_rocm_compute(python: Path) -> dict[str, Any]:
    """Run a real FP16 and BF16 matrix multiplication on every visible GPU."""

    result = subprocess.run(
        [str(python), "-c", ROCM_SMOKE_CODE],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-4000:]
        raise RuntimeError(f"ROCm PyTorch smoke process failed: {detail}")
    try:
        report: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("ROCm PyTorch smoke returned invalid JSON") from error
    validate_rocm_report(report)
    return report


def install_runtime(data_root: Path, comfy_commit: str) -> tuple[Path, Path, dict[str, object]]:
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    comfy = runtime / "ComfyUI"
    if not comfy.exists():
        run(["git", "clone", "https://github.com/Comfy-Org/ComfyUI.git", str(comfy)])
    run(["git", "fetch", "--depth", "1", "origin", comfy_commit], cwd=comfy)
    run(["git", "checkout", "--detach", comfy_commit], cwd=comfy)

    venv = runtime / "venv"
    if not (venv / "bin" / "python").exists():
        run([sys.executable, "-m", "venv", str(venv)])
    python = venv / "bin" / "python"
    pip = [str(python), "-m", "pip"]
    run([*pip, "install", "--upgrade", "pip", "setuptools", "wheel", "packaging"])

    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheels = sorted(path for path in Path("/").glob("*rocm*.whl") if tag in path.name)
    if wheels:
        run([*pip, "install", "--no-deps", "--force-reinstall", *map(str, wheels)])
    else:
        run(
            [
                *pip,
                "install",
                "torch",
                "torchvision",
                "torchaudio",
                "--index-url",
                "https://download.pytorch.org/whl/rocm7.2",
            ]
        )
    run([*pip, "install", "-r", str(comfy / "requirements.txt")])
    run(
        [
            *pip,
            "install",
            "huggingface_hub[hf_xet]==1.27.0",
            "imageio-ffmpeg",
            "requests==2.34.2",
        ]
    )

    torch_info = verify_rocm_compute(python)
    return comfy, python, torch_info


def write_model_paths(comfy: Path, data_root: Path) -> Path:
    model_root = data_root / "models"
    for folder in (
        "checkpoints",
        "clip_vision",
        "controlnet",
        "diffusion_models",
        "loras",
        "text_encoders",
        "upscale_models",
        "vae",
    ):
        (model_root / folder).mkdir(parents=True, exist_ok=True)
    config = comfy / "extra_model_paths.yaml"
    config.write_text(
        "ai_comic_series:\n"
        f"  base_path: {data_root}\n"
        "  checkpoints: models/checkpoints\n"
        "  clip_vision: models/clip_vision\n"
        "  controlnet: models/controlnet\n"
        "  diffusion_models: models/diffusion_models\n"
        "  loras: models/loras\n"
        "  text_encoders: models/text_encoders\n"
        "  upscale_models: models/upscale_models\n"
        "  vae: models/vae\n",
        encoding="utf-8",
    )
    return config


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def worker_command(
    comfy: Path,
    python: Path,
    model_paths: Path,
    input_root: Path,
    output_root: Path,
    port: int,
    gpu: int,
) -> list[str]:
    """Build the exact project-owned ComfyUI worker command."""

    return [
        str(python.resolve()),
        str((comfy / "main.py").resolve()),
        "--listen",
        "127.0.0.1",
        "--port",
        str(port),
        "--extra-model-paths-config",
        str(model_paths.resolve()),
        "--input-directory",
        str(input_root.resolve()),
        "--output-directory",
        str((output_root / f"gpu{gpu}").resolve()),
        "--disable-auto-launch",
        "--use-pytorch-cross-attention",
    ]


def worker_environment(data_root: Path, gpu: int) -> dict[str, str]:
    """Return the GPU-isolation values that identify a project worker."""

    return {
        "HIP_VISIBLE_DEVICES": str(gpu),
        "ROCR_VISIBLE_DEVICES": str(gpu),
        "PYTORCH_ROCM_ARCH": "gfx1100",
        "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
        "HF_HOME": str((data_root / "cache" / "huggingface").resolve()),
    }


def _read_process_command(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _read_process_environment(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    environment: dict[str, str] = {}
    for part in raw.split(b"\0"):
        name, separator, value = part.partition(b"=")
        if separator:
            environment[name.decode("utf-8", errors="replace")] = value.decode("utf-8", errors="replace")
    return environment


def _marker_paths(process_root: Path, gpu: int) -> tuple[Path, Path]:
    return process_root / f"comfyui-gpu{gpu}.pid", process_root / f"comfyui-gpu{gpu}.json"


def validate_existing_worker(
    process_root: Path,
    data_root: Path,
    comfy: Path,
    python: Path,
    model_paths: Path,
    input_root: Path,
    output_root: Path,
    comfy_commit: str,
    gpu: int,
    port: int,
) -> dict[str, object] | None:
    """Reuse only a live process whose marker, command, and GPU isolation match exactly."""

    pid_path, marker_path = _marker_paths(process_root, gpu)
    if not port_open(port):
        if marker_path.is_file():
            try:
                stale = json.loads(marker_path.read_text(encoding="utf-8"))
                stale_pid = int(stale.get("pid", -1)) if isinstance(stale, dict) else -1
            except (OSError, ValueError, json.JSONDecodeError):
                stale_pid = -1
            if stale_pid > 0 and Path(f"/proc/{stale_pid}").exists():
                raise RuntimeError(
                    f"Project ComfyUI marker for GPU {gpu} points to live PID {stale_pid}, but port {port} is closed"
                )
        return None
    if not pid_path.is_file() or not marker_path.is_file():
        raise RuntimeError(
            f"Exclusive project port {port} is occupied by an unknown process; refusing unsafe ComfyUI reuse"
        )
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Project worker metadata is invalid for GPU {gpu} on port {port}") from error
    command = worker_command(comfy, python, model_paths, input_root, output_root, port, gpu)
    isolation = worker_environment(data_root, gpu)
    expected_marker = {
        "version": WORKER_MARKER_VERSION,
        "project": "ai-comic-series",
        "pid": pid,
        "gpu": gpu,
        "port": port,
        "comfy_commit": comfy_commit,
        "command": command,
        "isolation": isolation,
    }
    if marker != expected_marker:
        raise RuntimeError(f"Project worker marker does not match the requested runtime for GPU {gpu} on port {port}")
    if _read_process_command(pid) != command:
        raise RuntimeError(f"PID {pid} command line does not match the project worker contract for port {port}")
    actual_environment = _read_process_environment(pid)
    mismatched = {name: value for name, value in isolation.items() if actual_environment.get(name) != value}
    if mismatched:
        raise RuntimeError(f"PID {pid} GPU isolation does not match the project worker contract: {mismatched}")
    return {
        "gpu": gpu,
        "port": port,
        "pid": pid,
        "state": "reused",
        "marker": str(marker_path),
    }


def worker_runtime_info(port: int) -> dict[str, Any]:
    """Validate that one isolated ComfyUI worker exposes its core runtime API."""

    def read_json(path: str) -> dict[str, Any]:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            if response.status != 200:
                raise RuntimeError(f"ComfyUI {path} returned HTTP {response.status}")
            value = json.load(response)
        if not isinstance(value, dict):
            raise RuntimeError(f"ComfyUI {path} did not return a JSON object")
        return value

    system_stats = read_json("/system_stats")
    devices = system_stats.get("devices", [])
    if not isinstance(devices, list) or len(devices) != 1:
        raise RuntimeError(f"ComfyUI worker on port {port} must expose exactly one isolated GPU, got {devices}")
    object_info = read_json("/object_info")
    missing = sorted(REQUIRED_COMFY_CLASSES - set(object_info))
    if missing:
        raise RuntimeError(f"ComfyUI worker on port {port} lacks required core nodes: {missing}")
    return system_stats


def start_workers(
    data_root: Path,
    comfy: Path,
    python: Path,
    model_paths: Path,
    gpu_count: int,
    comfy_commit: str,
) -> list[dict[str, object]]:
    """Start or strictly validate one project-owned ComfyUI process per GPU."""

    if gpu_count < 1:
        raise ValueError(f"gpu_count must be positive, got {gpu_count}")
    process_root = data_root / "processes"
    log_root = data_root / "logs"
    output_root = data_root / "comfy-output"
    input_root = data_root / "comfy-input"
    for folder in (process_root, log_root, output_root, input_root):
        folder.mkdir(parents=True, exist_ok=True)
    workers_by_gpu: dict[int, dict[str, object]] = {}
    pending_launches: list[tuple[int, int, list[str], dict[str, str], Path, Path, Path]] = []
    for gpu in range(gpu_count):
        port = WORKER_PORT_BASE + gpu
        existing = validate_existing_worker(
            process_root,
            data_root,
            comfy,
            python,
            model_paths,
            input_root,
            output_root,
            comfy_commit,
            gpu,
            port,
        )
        if existing is not None:
            workers_by_gpu[gpu] = existing
            continue
        isolation = worker_environment(data_root, gpu)
        environment = os.environ.copy()
        environment.update(isolation)
        log_path = log_root / f"comfyui-gpu{gpu}.log"
        command = worker_command(comfy, python, model_paths, input_root, output_root, port, gpu)
        pid_path, marker_path = _marker_paths(process_root, gpu)
        pending_launches.append((gpu, port, command, environment, log_path, pid_path, marker_path))

    launched: list[tuple[subprocess.Popen[bytes], Path, Path]] = []
    try:
        for gpu, port, command, environment, log_path, pid_path, marker_path in pending_launches:
            with log_path.open("ab", buffering=0) as log:
                process = subprocess.Popen(
                    command,
                    cwd=comfy,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            pid_path.write_text(str(process.pid) + "\n", encoding="utf-8")
            isolation = worker_environment(data_root, gpu)
            atomic_json(
                marker_path,
                {
                    "version": WORKER_MARKER_VERSION,
                    "project": "ai-comic-series",
                    "pid": process.pid,
                    "gpu": gpu,
                    "port": port,
                    "comfy_commit": comfy_commit,
                    "command": command,
                    "isolation": isolation,
                },
            )
            workers_by_gpu[gpu] = {
                "gpu": gpu,
                "port": port,
                "pid": process.pid,
                "state": "started",
                "log": str(log_path),
                "marker": str(marker_path),
            }
            launched.append((process, pid_path, marker_path))

        deadline = time.monotonic() + 180
        pending = {WORKER_PORT_BASE + gpu for gpu in range(gpu_count)}
        last_errors: dict[int, str] = {}
        while pending and time.monotonic() < deadline:
            for port in list(pending):
                try:
                    worker_runtime_info(port)
                    pending.remove(port)
                    last_errors.pop(port, None)
                except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError, TypeError) as error:
                    last_errors[port] = f"{type(error).__name__}: {error}"
            if pending:
                time.sleep(2)
        if pending:
            detail = {port: last_errors.get(port, "not reachable") for port in sorted(pending)}
            raise RuntimeError(f"ComfyUI workers failed strict readiness on ports: {detail}")
    except Exception:
        for process, pid_path, marker_path in launched:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            pid_path.unlink(missing_ok=True)
            marker_path.unlink(missing_ok=True)
        raise
    return [workers_by_gpu[gpu] for gpu in range(gpu_count)]


def main() -> int:
    args = parse_args()
    project_root = Path.cwd()
    status_path = (project_root / args.status).resolve()
    try:
        status(status_path, "running", "select-storage")
        data_root, mounts = select_data_root(args.data_root)
        for folder in ("cache", "logs", "models", "outputs", "production", "work"):
            (data_root / folder).mkdir(parents=True, exist_ok=True)
        manifest = json.loads((project_root / "config" / "models.json").read_text(encoding="utf-8"))
        comfy_commit = str(manifest["comfyui"]["commit"])
        status(status_path, "running", "install-runtime", data_root=str(data_root), mounts=public_mounts(mounts))
        comfy, python, torch_info = install_runtime(data_root, comfy_commit)
        ffmpeg, ffprobe = install_ffmpeg(python, python.parent)
        model_paths = write_model_paths(comfy, data_root)
        status(status_path, "running", "start-comfyui", data_root=str(data_root), torch=torch_info)
        workers = start_workers(
            data_root,
            comfy,
            python,
            model_paths,
            int(torch_info["count"]),
            comfy_commit,
        )
        state = {
            "version": 1,
            "data_root": str(data_root),
            "comfyui": str(comfy),
            "python": str(python),
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "torch": torch_info,
            "workers": workers,
            "comfyui_commit": comfy_commit,
        }
        atomic_json(project_root / "state.json", state)
        status(
            status_path,
            "complete",
            "ready",
            data_root=str(data_root),
            free_bytes=shutil.disk_usage(data_root).free,
            torch=torch_info,
            workers=workers,
            comfyui_commit=comfy_commit,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        return 0
    except Exception as error:
        status(status_path, "failed", "error", error=f"{type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
