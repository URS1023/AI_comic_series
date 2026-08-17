"""Idempotent AMD ROCm, ComfyUI, and worker bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from runtime_common import atomic_json, public_mounts, select_data_root


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


def install_ffmpeg(venv_python: Path, venv_bin: Path) -> str:
    existing = shutil.which("ffmpeg")
    if existing:
        return existing
    if hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("apt-get"):
        run(["apt-get", "update", "-qq"])
        run(["apt-get", "install", "-y", "-qq", "ffmpeg", "git"])
        existing = shutil.which("ffmpeg")
        if existing:
            return existing
    run([str(venv_python), "-m", "pip", "install", "imageio-ffmpeg"])
    code = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
    source = subprocess.check_output([str(venv_python), "-c", code], text=True).strip()
    target = venv_bin / "ffmpeg"
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(source)
    return str(target)


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
    run([*pip, "install", "huggingface_hub[hf_xet]", "imageio-ffmpeg", "requests"])

    verification_code = """
import json, torch
print(json.dumps({
  'torch': torch.__version__,
  'hip': torch.version.hip,
  'available': torch.cuda.is_available(),
  'count': torch.cuda.device_count(),
  'devices': [
    {'index': i, 'name': torch.cuda.get_device_name(i), 'bytes': torch.cuda.get_device_properties(i).total_memory}
    for i in range(torch.cuda.device_count())
  ],
}))
"""
    torch_info = json.loads(subprocess.check_output([str(python), "-c", verification_code], text=True).strip())
    if not torch_info.get("hip") or not torch_info.get("available") or int(torch_info.get("count", 0)) < 1:
        raise RuntimeError(f"ROCm PyTorch verification failed: {torch_info}")
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


def start_workers(
    data_root: Path, comfy: Path, python: Path, model_paths: Path, gpu_count: int
) -> list[dict[str, object]]:
    process_root = data_root / "processes"
    log_root = data_root / "logs"
    output_root = data_root / "comfy-output"
    input_root = data_root / "comfy-input"
    for folder in (process_root, log_root, output_root, input_root):
        folder.mkdir(parents=True, exist_ok=True)
    workers: list[dict[str, object]] = []
    for gpu in range(gpu_count):
        port = 8188 + gpu
        pid_file = process_root / f"comfyui-gpu{gpu}.pid"
        if port_open(port):
            workers.append({"gpu": gpu, "port": port, "state": "reused"})
            continue
        environment = os.environ.copy()
        environment.update(
            {
                "HIP_VISIBLE_DEVICES": str(gpu),
                "ROCR_VISIBLE_DEVICES": str(gpu),
                "PYTORCH_ROCM_ARCH": "gfx1100",
                "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
                "HF_HOME": str(data_root / "cache" / "huggingface"),
            }
        )
        log_path = log_root / f"comfyui-gpu{gpu}.log"
        command = [
            str(python),
            str(comfy / "main.py"),
            "--listen",
            "127.0.0.1",
            "--port",
            str(port),
            "--extra-model-paths-config",
            str(model_paths),
            "--input-directory",
            str(input_root),
            "--output-directory",
            str(output_root / f"gpu{gpu}"),
            "--disable-auto-launch",
            "--use-pytorch-cross-attention",
        ]
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
        pid_file.write_text(str(process.pid) + "\n", encoding="utf-8")
        workers.append({"gpu": gpu, "port": port, "pid": process.pid, "state": "started", "log": str(log_path)})

    deadline = time.monotonic() + 180
    pending = {int(worker["port"]) for worker in workers}
    while pending and time.monotonic() < deadline:
        for port in list(pending):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats", timeout=2) as response:
                    if response.status == 200:
                        pending.remove(port)
            except OSError:
                pass
        if pending:
            time.sleep(2)
    if pending:
        raise RuntimeError(f"ComfyUI workers failed to become ready on ports: {sorted(pending)}")
    return workers


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
        ffmpeg = install_ffmpeg(python, python.parent)
        model_paths = write_model_paths(comfy, data_root)
        status(status_path, "running", "start-comfyui", data_root=str(data_root), torch=torch_info)
        workers = start_workers(data_root, comfy, python, model_paths, int(torch_info["count"]))
        state = {
            "version": 1,
            "data_root": str(data_root),
            "comfyui": str(comfy),
            "python": str(python),
            "ffmpeg": ffmpeg,
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
        )
        return 0
    except Exception as error:
        status(status_path, "failed", "error", error=f"{type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
