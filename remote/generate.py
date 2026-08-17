"""Resumable multi-GPU image/keyframe/video generation worker."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from comfy_client import ComfyClient
from runtime_common import atomic_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--stage", choices=["anchors", "keyframes", "videos"], required=True)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--inside-venv", action="store_true")
    return parser.parse_args()


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def set_binding(prompt: dict[str, Any], binding: list[str], value: object) -> None:
    node, input_name = binding
    prompt[node]["inputs"][input_name] = value


def normalize_image(source: Path, target: Path, width: int = 1920, height: int = 1080) -> None:
    from PIL import Image, ImageOps

    with Image.open(source) as image:
        converted = image.convert("RGB")
        fitted = ImageOps.fit(converted, (width, height), method=Image.Resampling.LANCZOS)
        target.parent.mkdir(parents=True, exist_ok=True)
        fitted.save(target, format="PNG", optimize=True)


def video_probe(path: Path, ffprobe: str) -> dict[str, object]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams or float(data.get("format", {}).get("duration", 0)) <= 0:
        raise RuntimeError(f"Generated video has no valid stream: {path}")
    return data


class GenerationWorker:
    """One process-level coordinator with one ComfyUI client per visible GPU."""

    def __init__(self, project_root: Path, state: dict[str, Any], status_path: Path, stage: str) -> None:
        self.project_root = project_root
        self.data_root = Path(state["data_root"])
        self.assets_root = self.data_root / "project-assets"
        self.mirror_root = project_root / "artifacts"
        self.workflow_root = project_root / "workflows" / "comfyui" / "api"
        self.ffprobe = str(Path(state["ffmpeg"]).with_name("ffprobe"))
        if not Path(self.ffprobe).exists():
            self.ffprobe = shutil.which("ffprobe") or "ffprobe"
        self.ports = [int(worker["port"]) for worker in state.get("workers", [])]
        if not self.ports:
            raise RuntimeError("No ready ComfyUI workers are recorded in state.json")
        self.status_path = status_path
        self.stage = stage
        self.lock = threading.Lock()
        self.completed: list[dict[str, object]] = []
        self.failed: list[dict[str, object]] = []

    def write_status(self, state: str, phase: str, **details: object) -> None:
        with self.lock:
            atomic_json(
                self.status_path,
                {
                    "job": f"generate-{self.stage}",
                    "state": state,
                    "phase": phase,
                    "stage": self.stage,
                    "updated": time.time(),
                    "completed": self.completed,
                    "failed": self.failed,
                    **details,
                },
            )

    def _load_template(self, job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        template_name = {
            "qwen-t2i": "qwen_image_2512_t2i.json",
            "qwen-edit": "qwen_image_edit_2511.json",
            "wan-i2v": "wan22_i2v_14b.json",
        }[str(job["kind"])]
        document = json.loads((self.workflow_root / template_name).read_text(encoding="utf-8"))
        return copy.deepcopy(document["prompt"]), document["bindings"]

    def _asset(self, relative: str) -> Path:
        candidate = (self.assets_root / relative).resolve()
        if self.assets_root.resolve() not in candidate.parents:
            raise RuntimeError(f"Asset path escapes project root: {relative}")
        return candidate

    def _upload_references(
        self,
        client: ComfyClient,
        job: dict[str, Any],
        prompt: dict[str, Any],
        bindings: dict[str, Any],
    ) -> list[str]:
        uploaded: list[str] = []
        for index, relative in enumerate(job.get("references", []), start=1):
            path = self._asset(str(relative))
            if not path.is_file():
                raise FileNotFoundError(f"Required reference is missing: {relative}")
            remote_name = f"ai-comic/{job['id']}/reference-{index}{path.suffix.lower()}"
            uploaded.append(client.upload_image(path, remote_name))
        if job["kind"] == "wan-i2v":
            prompt[bindings["referenceNode"]]["inputs"]["image"] = uploaded[0]
        elif job["kind"] == "qwen-edit":
            reference_nodes = list(bindings["referenceNodes"])
            positive = prompt[bindings["positiveNode"]]["inputs"]
            negative = prompt[bindings["negativeNode"]]["inputs"]
            for index, node in enumerate(reference_nodes):
                input_name = f"image{index + 1}"
                if index < len(uploaded):
                    prompt[node]["inputs"]["image"] = uploaded[index]
                else:
                    prompt.pop(node, None)
                    positive.pop(input_name, None)
                    negative.pop(input_name, None)
        return uploaded

    def _prepare_prompt(
        self,
        client: ComfyClient,
        job: dict[str, Any],
        attempt: int,
    ) -> tuple[dict[str, Any], str]:
        prompt, bindings = self._load_template(job)
        self._upload_references(client, job, prompt, bindings)
        seed = int(job["seed"]) + attempt - 1
        prefix = f"ai-comic/{job['id']}/attempt-{attempt}"
        if job["kind"] == "qwen-t2i":
            set_binding(prompt, bindings["positive"], job["prompt"])
            set_binding(prompt, bindings["negative"], job["negativePrompt"])
            set_binding(prompt, bindings["width"], int(job.get("width", 1328)))
            set_binding(prompt, bindings["height"], int(job.get("height", 768)))
            set_binding(prompt, bindings["seed"], seed)
            set_binding(prompt, bindings["filenamePrefix"], prefix)
        elif job["kind"] == "qwen-edit":
            prompt[bindings["positiveNode"]]["inputs"]["prompt"] = job["prompt"]
            prompt[bindings["negativeNode"]]["inputs"]["prompt"] = job["negativePrompt"]
            set_binding(prompt, bindings["seed"], seed)
            set_binding(prompt, bindings["filenamePrefix"], prefix)
        elif job["kind"] == "wan-i2v":
            set_binding(prompt, bindings["positive"], job["prompt"])
            set_binding(prompt, bindings["negative"], job["negativePrompt"])
            set_binding(prompt, bindings["width"], int(job["width"]))
            set_binding(prompt, bindings["height"], int(job["height"]))
            raw_frames = math.ceil(float(job["duration"]) * float(job["fps"]))
            frames = math.ceil(max(17, raw_frames - 1) / 4) * 4 + 1
            set_binding(prompt, bindings["length"], frames)
            set_binding(prompt, bindings["seed"], seed)
            set_binding(prompt, bindings["filenamePrefix"], prefix)
            prompt["14"]["inputs"]["fps"] = float(job["fps"])
        return prompt, prefix

    def _fingerprint(self, job: dict[str, Any]) -> str:
        hasher = hashlib.sha256(json.dumps(job, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        for relative in job.get("references", []):
            path = self._asset(str(relative))
            hasher.update(sha256(path).encode("ascii"))
        return hasher.hexdigest()

    def _reusable(self, job: dict[str, Any], fingerprint: str) -> bool:
        target = self._asset(str(job["output"]))
        metadata = target.with_suffix(target.suffix + ".meta.json")
        if not target.is_file() or not metadata.is_file() or target.stat().st_size == 0:
            return False
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return value.get("fingerprint") == fingerprint and value.get("sha256") == sha256(target)

    def _materialize(
        self,
        client: ComfyClient,
        job: dict[str, Any],
        record: dict[str, Any],
        fingerprint: str,
        attempt: int,
    ) -> dict[str, object]:
        files = client.output_files(record)
        wanted = ".mp4" if job["kind"] == "wan-i2v" else ".png"
        descriptor = next((item for item in files if item["filename"].lower().endswith(wanted)), None)
        if descriptor is None:
            descriptor = next(
                (
                    item
                    for item in files
                    if Path(item["filename"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm"}
                ),
                None,
            )
        if descriptor is None:
            raise RuntimeError(f"ComfyUI history contains no supported media output: {files}")
        target = self._asset(str(job["output"]))
        temporary = target.with_suffix(".download" + Path(descriptor["filename"]).suffix)
        client.download(descriptor, temporary)
        target.parent.mkdir(parents=True, exist_ok=True)
        probe: dict[str, object] | None = None
        if job["kind"] == "wan-i2v":
            if temporary.suffix.lower() != ".mp4":
                ffmpeg = str(Path(self.ffprobe).with_name("ffmpeg"))
                subprocess.run(
                    [ffmpeg, "-y", "-i", str(temporary), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
                    check=True,
                )
                temporary.unlink()
            else:
                os.replace(temporary, target)
            probe = video_probe(target, self.ffprobe)
        else:
            normalize_image(temporary, target)
            temporary.unlink()
        digest = sha256(target)
        metadata = {
            "job": job["id"],
            "kind": job["kind"],
            "attempt": attempt,
            "fingerprint": fingerprint,
            "sha256": digest,
            "bytes": target.stat().st_size,
            "probe": probe,
            "created": time.time(),
        }
        atomic_json(target.with_suffix(target.suffix + ".meta.json"), metadata)
        mirror = (self.mirror_root / str(job["output"])).resolve()
        mirror.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, mirror)
        shutil.copy2(target.with_suffix(target.suffix + ".meta.json"), mirror.with_suffix(mirror.suffix + ".meta.json"))
        return {
            "id": job["id"],
            "target": job["output"],
            "mirror": str(mirror.relative_to(self.project_root)).replace("\\", "/"),
            "attempt": attempt,
            "sha256": digest,
            "bytes": target.stat().st_size,
            "reused": False,
        }

    def process(self, job: dict[str, Any], port: int) -> dict[str, object]:
        if (self.project_root / "production" / "STOP").exists():
            raise RuntimeError("Generation stopped by production/STOP")
        fingerprint = self._fingerprint(job)
        if self._reusable(job, fingerprint):
            target = self._asset(str(job["output"]))
            mirror = (self.mirror_root / str(job["output"])).resolve()
            mirror.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, mirror)
            metadata = target.with_suffix(target.suffix + ".meta.json")
            if metadata.is_file():
                shutil.copy2(metadata, mirror.with_suffix(mirror.suffix + ".meta.json"))
            return {
                "id": job["id"],
                "target": job["output"],
                "mirror": str(mirror.relative_to(self.project_root)).replace("\\", "/"),
                "attempt": 0,
                "sha256": sha256(target),
                "bytes": target.stat().st_size,
                "reused": True,
            }
        client = ComfyClient(port)
        try:
            client.health()
            last_error: Exception | None = None
            for attempt in range(1, int(job.get("maxAttempts", 3)) + 1):
                try:
                    prompt, _ = self._prepare_prompt(client, job, attempt)
                    prompt_id = client.submit(prompt)
                    record = client.wait(prompt_id, float(job.get("timeoutSeconds", 7200)))
                    return self._materialize(client, job, record, fingerprint, attempt)
                except Exception as error:
                    last_error = error
                    time.sleep(min(20, attempt * 5))
            assert last_error is not None
            raise last_error
        finally:
            client.close()

    def run(self, jobs: list[dict[str, Any]], max_workers: int) -> int:
        selected = [job for job in jobs if job.get("stage") == self.stage]
        if not selected:
            raise RuntimeError(f"Queue contains no jobs for stage {self.stage}")
        self.write_status("running", "generate", total=len(selected), ports=self.ports)
        workers = max(1, min(max_workers, len(self.ports), len(selected)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.process, job, self.ports[index % len(self.ports)]): job
                for index, job in enumerate(selected)
            }
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                    with self.lock:
                        self.completed.append(result)
                except Exception as error:
                    with self.lock:
                        self.failed.append({"id": job["id"], "error": f"{type(error).__name__}: {error}"})
                self.write_status("running", "generate", total=len(selected), ports=self.ports)
        final_state = "complete" if not self.failed else "failed"
        self.write_status(final_state, "finished", total=len(selected), ports=self.ports)
        return 0 if not self.failed else 1


def main() -> int:
    args = parse_args()
    project_root = Path.cwd()
    state = json.loads((project_root / "state.json").read_text(encoding="utf-8"))
    venv_python = Path(state["python"])
    if not args.inside_venv and Path(sys.executable).resolve() != venv_python.resolve():
        command = [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:], "--inside-venv"]
        return subprocess.run(command, cwd=project_root, check=False).returncode
    queue = json.loads((project_root / args.queue).read_text(encoding="utf-8"))
    jobs = queue.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("Generation queue must contain a jobs array")
    worker = GenerationWorker(project_root, state, (project_root / args.status).resolve(), args.stage)
    return worker.run(jobs, max_workers=args.max_workers)


if __name__ == "__main__":
    raise SystemExit(main())
