"""Resumable multi-GPU image/keyframe/video generation worker."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

VIDEO_KINDS = frozenset({"wan-i2v", "wan-flf2v"})

try:
    from .comfy_client import ComfyClient
    from .media_contract import mp4_has_faststart, video_contract_errors
    from .runtime_common import atomic_json
    from .video_motion import analyze_video_motion
except ImportError:  # Script execution on the remote node.
    from comfy_client import ComfyClient
    from media_contract import mp4_has_faststart, video_contract_errors
    from runtime_common import atomic_json
    from video_motion import analyze_video_motion


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument(
        "--stage",
        choices=[
            "anchors",
            "cover-drafts",
            "covers",
            "keyframes",
            "motion-keyframes",
            "video-sample",
            "videos",
        ],
        required=True,
    )
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


def video_probe(path: Path, ffprobe: str, *, expected_fps: float | None = None) -> dict[str, Any]:
    """Probe and enforce the normalized H.264/CFR/faststart video contract."""

    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_frames:"
            "format=format_name,duration"
        ),
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-2000:]
        raise RuntimeError(f"FFprobe failed for {path}: {detail}")
    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"FFprobe returned invalid JSON for {path}") from error
    errors = video_contract_errors(data, expected_fps=expected_fps, faststart=mp4_has_faststart(path))
    if errors:
        raise RuntimeError(f"Generated video violates the normalized media contract: {errors}")
    return data


def standardize_video(
    source: Path,
    target: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    fps: float,
) -> dict[str, Any]:
    """Always transcode a generated clip to browser-safe H.264/yuv420p CFR MP4."""

    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    fps_text = f"{fps:.6f}".rstrip("0").rstrip(".")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "16",
        "-pix_fmt",
        "yuv420p",
        "-r",
        fps_text,
        "-fps_mode",
        "cfr",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not target.is_file():
        target.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout)[-3000:]
        raise RuntimeError(f"FFmpeg video standardization failed: {detail}")
    try:
        return video_probe(target, ffprobe, expected_fps=fps)
    except Exception:
        target.unlink(missing_ok=True)
        raise


class GenerationWorker:
    """One process-level coordinator with one ComfyUI client per visible GPU."""

    def __init__(self, project_root: Path, state: dict[str, Any], status_path: Path, stage: str) -> None:
        self.project_root = project_root
        self.data_root = Path(state["data_root"])
        self.assets_root = self.data_root / "project-assets"
        self.mirror_root = project_root / "artifacts"
        self.workflow_root = project_root / "workflows" / "comfyui" / "api"
        self.ffmpeg = str(state.get("ffmpeg") or shutil.which("ffmpeg") or "ffmpeg")
        self.ffprobe = str(state.get("ffprobe") or shutil.which("ffprobe") or Path(self.ffmpeg).with_name("ffprobe"))
        self.ports = [int(worker["port"]) for worker in state.get("workers", [])]
        if not self.ports:
            raise RuntimeError("No ready ComfyUI workers are recorded in state.json")
        self.status_path = status_path
        self.stage = stage
        self.lock = threading.Lock()
        self.completed: list[dict[str, object]] = []
        self.failed: list[dict[str, object]] = []
        self.force_regenerate_ids: set[str] = set()

    def _approval(self, filename: str) -> dict[str, Any]:
        path = self.project_root / "production" / filename
        if not path.is_file():
            raise RuntimeError(f"Required review gate is missing: production/{filename}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("state") != "approved":
            raise RuntimeError(f"Review gate is not approved: production/{filename}")
        return value

    def _verify_approval_assets(self, approval: dict[str, Any], required_paths: set[str]) -> None:
        entries = approval.get("assets", [])
        by_path = {str(entry.get("path")): entry for entry in entries if isinstance(entry, dict)}
        missing = sorted(required_paths - set(by_path))
        if missing:
            raise RuntimeError(f"Approval omits required assets: {missing}")
        for relative in sorted(required_paths):
            path = self._asset(relative)
            if not path.is_file():
                raise FileNotFoundError(f"Approved remote asset is missing: {relative}")
            expected = str(by_path[relative].get("sha256", ""))
            actual = sha256(path)
            if expected != actual:
                raise RuntimeError(f"Approved asset changed after review: {relative} ({actual} != {expected})")

    def enforce_review_gates(self, jobs: list[dict[str, Any]]) -> None:
        if self.stage in {"anchors", "cover-drafts"}:
            return
        if self.stage in {"keyframes", "covers"}:
            approval = self._approval("anchor-approval.json")
            required = {str(path) for job in jobs for path in job.get("references", []) if "/anchors/" in str(path)}
            self._verify_approval_assets(approval, required)
            return
        if self.stage == "motion-keyframes":
            anchor_approval = self._approval("anchor-approval.json")
            anchor_paths = {
                str(path)
                for job in jobs
                for path in job.get("references", [])
                if "/anchors/" in str(path)
            }
            self._verify_approval_assets(anchor_approval, anchor_paths)
            keyframe_approval = self._approval("keyframe-approval.json")
            start_paths = {
                str(path)
                for job in jobs
                for path in job.get("references", [])
                if "/keyframes/" in str(path)
            }
            self._verify_approval_assets(keyframe_approval, start_paths)
            return
        keyframe_approval = self._approval("keyframe-approval.json")
        keyframes = {
            str(path)
            for job in jobs
            for path in job.get("references", [])
            if "/keyframes/" in str(path)
        }
        self._verify_approval_assets(keyframe_approval, keyframes)
        endframes = {
            str(path)
            for job in jobs
            for path in job.get("references", [])
            if "/endframes/" in str(path)
        }
        if endframes:
            self._verify_approval_assets(self._approval("motion-endframe-approval.json"), endframes)
        if self.stage == "videos":
            sample_approval = self._approval("video-sample-approval.json")
            expected_scene_ids = {
                str(job["id"]).removeprefix("video-") for job in jobs if job.get("representativeSample") is True
            }
            if not expected_scene_ids:
                raise RuntimeError("Full video queue has no representative sample configuration")
            rejected_scene_ids = set(sample_approval.get("rejectedSceneIds", []))
            high_risk_sample_ids = {
                str(job["id"]).removeprefix("video-")
                for job in jobs
                if job.get("representativeSample") is True and job.get("highRisk") is True
            }
            rejected_high_risk = sorted(rejected_scene_ids & high_risk_sample_ids)
            if rejected_high_risk:
                raise RuntimeError(
                    f"Every high-risk representative video must pass visual review: {rejected_high_risk}"
                )
            approval_ids = {
                str(entry.get("id")) for entry in sample_approval.get("assets", []) if isinstance(entry, dict)
            }
            if approval_ids != expected_scene_ids:
                raise RuntimeError("Representative video approval does not cover the configured sample exactly")
            computed_pass_rate = (len(expected_scene_ids) - len(rejected_scene_ids)) / len(expected_scene_ids)
            if abs(float(sample_approval.get("passRate", 0)) - computed_pass_rate) > 1e-9:
                raise RuntimeError("Representative video approval pass rate is inconsistent with rejected ids")
            if computed_pass_rate < 0.9:
                raise RuntimeError("Representative video sample pass rate is below 90%")
            accepted_paths = {
                str(entry["path"])
                for entry in sample_approval.get("assets", [])
                if isinstance(entry, dict) and str(entry.get("id")) not in rejected_scene_ids
            }
            self._verify_approval_assets(sample_approval, accepted_paths)
            self.force_regenerate_ids = {f"video-{scene_id}" for scene_id in rejected_scene_ids}

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

    def _template_path(self, job: dict[str, Any]) -> Path:
        template_name = {
            "qwen-t2i": "qwen_image_2512_t2i.json",
            "qwen-edit": "qwen_image_edit_2511.json",
            "wan-i2v": "wan22_i2v_14b.json",
            "wan-flf2v": "wan22_flf2v_14b.json",
        }[str(job["kind"])]
        return self.workflow_root / template_name

    def _load_template(self, job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        document = json.loads(self._template_path(job).read_text(encoding="utf-8"))
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
        elif job["kind"] == "wan-flf2v":
            reference_nodes = list(bindings["referenceNodes"])
            if len(uploaded) != 2 or len(reference_nodes) != 2:
                raise RuntimeError("Wan FLF2V requires exactly one approved start and one approved end frame")
            for node, remote_name in zip(reference_nodes, uploaded, strict=True):
                prompt[node]["inputs"]["image"] = remote_name
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
        elif job["kind"] in VIDEO_KINDS:
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
        hasher.update(sha256(self._template_path(job)).encode("ascii"))
        for relative in job.get("references", []):
            path = self._asset(str(relative))
            hasher.update(sha256(path).encode("ascii"))
        return hasher.hexdigest()

    def _reusable(self, job: dict[str, Any], fingerprint: str) -> bool:
        if str(job["id"]) in self.force_regenerate_ids:
            return False
        target = self._asset(str(job["output"]))
        metadata = target.with_suffix(target.suffix + ".meta.json")
        if not target.is_file() or not metadata.is_file() or target.stat().st_size == 0:
            return False
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if value.get("fingerprint") != fingerprint or value.get("sha256") != sha256(target):
            return False
        if job["kind"] in VIDEO_KINDS:
            try:
                media = video_probe(target, self.ffprobe, expected_fps=float(job["fps"]))
                duration = float(media.get("format", {}).get("duration", 0))
                evidence = analyze_video_motion(
                    target,
                    ffmpeg=self.ffmpeg,
                    duration_seconds=duration,
                    trim_fraction=0.08,
                )
            except (OSError, RuntimeError, ValueError):
                return False
            if evidence.get("status") != "passed":
                return False
        return True

    def _archive_existing(self, target: Path) -> None:
        if not target.is_file():
            return
        previous_sha = sha256(target)
        rejected = self.assets_root / "assets" / "generated" / "rejected"
        rejected.mkdir(parents=True, exist_ok=True)
        archived = rejected / f"{target.stem}-{previous_sha[:12]}{target.suffix}"
        if not archived.exists():
            shutil.copy2(target, archived)
            previous_metadata = target.with_suffix(target.suffix + ".meta.json")
            if previous_metadata.is_file():
                shutil.copy2(previous_metadata, archived.with_suffix(archived.suffix + ".meta.json"))

    def _materialize(
        self,
        client: ComfyClient,
        job: dict[str, Any],
        record: dict[str, Any],
        fingerprint: str,
        attempt: int,
        prompt_id: str,
    ) -> dict[str, object]:
        files = client.output_files(record)
        wanted = ".mp4" if job["kind"] in VIDEO_KINDS else ".png"
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
        probe: dict[str, Any] | None = None
        motion_evidence: dict[str, object] | None = None
        if job["kind"] in VIDEO_KINDS:
            candidate = target.with_suffix(f".attempt-{attempt}.candidate.mp4")
            try:
                probe = standardize_video(
                    temporary,
                    candidate,
                    ffmpeg=self.ffmpeg,
                    ffprobe=self.ffprobe,
                    fps=float(job["fps"]),
                )
            finally:
                temporary.unlink(missing_ok=True)
            generated_duration = float(probe.get("format", {}).get("duration", 0))
            motion_evidence = analyze_video_motion(
                candidate,
                ffmpeg=self.ffmpeg,
                duration_seconds=generated_duration,
                trim_fraction=0.08,
            )
            if motion_evidence.get("status") != "passed":
                rejected = self.assets_root / "assets" / "generated" / "rejected"
                rejected.mkdir(parents=True, exist_ok=True)
                rejected_path = rejected / f"{target.stem}-attempt-{attempt}-{sha256(candidate)[:12]}.mp4"
                os.replace(candidate, rejected_path)
                atomic_json(
                    rejected_path.with_suffix(rejected_path.suffix + ".motion.json"),
                    motion_evidence,
                )
                raise RuntimeError(f"Generated clip failed real-motion gate: {motion_evidence.get('errors', [])}")
            self._archive_existing(target)
            os.replace(candidate, target)
        else:
            self._archive_existing(target)
            normalize_image(
                temporary,
                target,
                width=int(job.get("targetWidth", 1920)),
                height=int(job.get("targetHeight", 1080)),
            )
            temporary.unlink()
        digest = sha256(target)
        metadata = {
            "job": job["id"],
            "kind": job["kind"],
            "attempt": attempt,
            "promptId": prompt_id,
            "fingerprint": fingerprint,
            "workflowSha256": sha256(self._template_path(job)),
            "sha256": digest,
            "bytes": target.stat().st_size,
            "probe": probe,
            "motionEvidence": motion_evidence,
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
                    return self._materialize(client, job, record, fingerprint, attempt, prompt_id)
                except Exception as error:
                    last_error = error
                    time.sleep(min(20, attempt * 5))
            assert last_error is not None
            raise last_error
        finally:
            client.close()

    def _process_with_available_port(
        self,
        job: dict[str, Any],
        available_ports: queue.Queue[int],
    ) -> dict[str, object]:
        """Lease exactly one idle ComfyUI port for the duration of a job."""

        port = available_ports.get()
        try:
            return self.process(job, port)
        finally:
            available_ports.put(port)

    def run(self, jobs: list[dict[str, Any]], max_workers: int) -> int:
        if self.stage == "video-sample":
            selected = [job for job in jobs if job.get("stage") == "videos" and job.get("representativeSample") is True]
        else:
            selected = [job for job in jobs if job.get("stage") == self.stage]
        if not selected:
            raise RuntimeError(f"Queue contains no jobs for stage {self.stage}")
        self.enforce_review_gates(selected)
        self.write_status("running", "generate", total=len(selected), ports=self.ports)
        workers = max(1, min(max_workers, len(self.ports), len(selected)))
        available_ports: queue.Queue[int] = queue.Queue()
        for port in self.ports[:workers]:
            available_ports.put(port)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._process_with_available_port, job, available_ports): job for job in selected
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
