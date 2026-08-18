"""Regression tests for GPU worker isolation, scheduling, and media normalization."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from remote import bootstrap, generate
from remote.media_contract import mp4_has_faststart, video_contract_errors


def passing_rocm_report(count: int = 2) -> dict[str, Any]:
    return {
        "torch": "test",
        "hip": "7.2",
        "available": True,
        "count": count,
        "devices": [
            {
                "index": index,
                "name": f"gpu-{index}",
                "bytes": 48 * 1024**3,
                "compute": {
                    "fp16": {"passed": True, "finite": True, "mean_abs": 1.0},
                    "bf16": {"passed": True, "finite": True, "mean_abs": 1.0},
                },
            }
            for index in range(count)
        ],
    }


def mp4_box(name: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + name + payload


def test_rocm_report_requires_fp16_and_bf16_compute_on_every_gpu() -> None:
    report = passing_rocm_report()

    bootstrap.validate_rocm_report(report)
    report["devices"][1]["compute"]["bf16"] = {"passed": False, "error": "unsupported"}

    with pytest.raises(RuntimeError, match="gpu 1 bf16"):
        bootstrap.validate_rocm_report(report)


def test_unknown_process_on_project_port_is_never_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "port_open", lambda _port: True)

    with pytest.raises(RuntimeError, match="unknown process"):
        bootstrap.validate_existing_worker(
            tmp_path / "processes",
            tmp_path,
            tmp_path / "ComfyUI",
            tmp_path / "venv" / "bin" / "python",
            tmp_path / "ComfyUI" / "extra_model_paths.yaml",
            tmp_path / "input",
            tmp_path / "output",
            "a" * 40,
            0,
            bootstrap.WORKER_PORT_BASE,
        )


def test_exact_project_worker_contract_can_be_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_root = tmp_path / "data"
    process_root = data_root / "processes"
    process_root.mkdir(parents=True)
    comfy = data_root / "runtime" / "ComfyUI"
    python = data_root / "runtime" / "venv" / "bin" / "python"
    model_paths = comfy / "extra_model_paths.yaml"
    input_root = data_root / "comfy-input"
    output_root = data_root / "comfy-output"
    port = bootstrap.WORKER_PORT_BASE
    pid = 321
    commit = "b" * 40
    command = bootstrap.worker_command(comfy, python, model_paths, input_root, output_root, port, 0)
    isolation = bootstrap.worker_environment(data_root, 0)
    pid_path, marker_path = bootstrap._marker_paths(process_root, 0)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")
    marker_path.write_text(
        json.dumps(
            {
                "version": bootstrap.WORKER_MARKER_VERSION,
                "project": "ai-comic-series",
                "pid": pid,
                "gpu": 0,
                "port": port,
                "comfy_commit": commit,
                "command": command,
                "isolation": isolation,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "port_open", lambda _port: True)
    monkeypatch.setattr(bootstrap, "_read_process_command", lambda _pid: command)
    monkeypatch.setattr(bootstrap, "_read_process_environment", lambda _pid: isolation)

    worker = bootstrap.validate_existing_worker(
        process_root,
        data_root,
        comfy,
        python,
        model_paths,
        input_root,
        output_root,
        commit,
        0,
        port,
    )

    assert worker is not None
    assert worker["state"] == "reused"
    assert worker["port"] == bootstrap.WORKER_PORT_BASE


def test_start_workers_uses_project_high_ports_for_verified_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_ports: list[int] = []

    def existing(*args: object) -> dict[str, object]:
        gpu = int(args[-2])
        port = int(args[-1])
        observed_ports.append(port)
        return {"gpu": gpu, "port": port, "pid": 100 + gpu, "state": "reused"}

    monkeypatch.setattr(bootstrap, "validate_existing_worker", existing)
    monkeypatch.setattr(bootstrap, "worker_runtime_info", lambda _port: {"devices": [{"name": "gpu"}]})

    workers = bootstrap.start_workers(
        tmp_path,
        tmp_path / "ComfyUI",
        tmp_path / "venv" / "bin" / "python",
        tmp_path / "ComfyUI" / "extra_model_paths.yaml",
        2,
        "c" * 40,
    )

    assert observed_ports == [bootstrap.WORKER_PORT_BASE, bootstrap.WORKER_PORT_BASE + 1]
    assert [worker["port"] for worker in workers] == observed_ports


def test_generation_scheduler_never_leases_a_busy_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = {
        "data_root": str(tmp_path / "data"),
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "workers": [{"port": 18888}, {"port": 18889}],
    }
    worker = generate.GenerationWorker(tmp_path, state, tmp_path / "status.json", "anchors")
    active: set[int] = set()
    used: set[int] = set()
    overlaps: list[int] = []
    lock = threading.Lock()

    def process(job: dict[str, Any], port: int) -> dict[str, object]:
        with lock:
            if port in active:
                overlaps.append(port)
            active.add(port)
            used.add(port)
        time.sleep(0.15 if job["id"] == "job-0" else 0.01)
        with lock:
            active.remove(port)
        return {"id": job["id"], "port": port}

    monkeypatch.setattr(worker, "process", process)
    jobs = [{"id": f"job-{index}", "stage": "anchors"} for index in range(6)]

    assert worker.run(jobs, max_workers=2) == 0
    assert overlaps == []
    assert used == {18888, 18889}
    assert len(worker.completed) == len(jobs)


def test_standardize_video_transcodes_even_an_mp4_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "comfy-output.mp4"
    target = tmp_path / "normalized.mp4"
    source.write_bytes(b"source")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"normalized")
        return subprocess.CompletedProcess(command, 0, "", "")

    expected_probe: dict[str, Any] = {"streams": [{"codec_name": "h264"}], "format": {"duration": "1"}}
    monkeypatch.setattr(generate.subprocess, "run", run)
    monkeypatch.setattr(generate, "video_probe", lambda *_args, **_kwargs: expected_probe)

    assert (
        generate.standardize_video(
            source,
            target,
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            fps=16.0,
        )
        == expected_probe
    )
    command = commands[0]
    assert command[command.index("-i") + 1] == str(source)
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-fps_mode") + 1] == "cfr"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert target.read_bytes() == b"normalized"


def test_mp4_faststart_and_video_contract_validation(tmp_path: Path) -> None:
    fast = tmp_path / "fast.mp4"
    slow = tmp_path / "slow.mp4"
    fast.write_bytes(mp4_box(b"ftyp") + mp4_box(b"moov") + mp4_box(b"mdat"))
    slow.write_bytes(mp4_box(b"ftyp") + mp4_box(b"mdat") + mp4_box(b"moov"))
    valid = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "16/1",
                "r_frame_rate": "16/1",
            }
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.0"},
    }

    assert mp4_has_faststart(fast) is True
    assert mp4_has_faststart(slow) is False
    assert video_contract_errors(valid, expected_fps=16, faststart=True) == []
    valid["streams"][0].update({"codec_name": "hevc", "pix_fmt": "yuv444p", "avg_frame_rate": "15/1"})

    errors = video_contract_errors(valid, expected_fps=16, faststart=False)
    assert any("expected h264" in error for error in errors)
    assert any("expected yuv420p" in error for error in errors)
    assert any("not CFR" in error for error in errors)
    assert any("+faststart" in error for error in errors)


def test_install_ffmpeg_rejects_missing_ffprobe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    with pytest.raises(RuntimeError, match="ffprobe"):
        bootstrap.install_ffmpeg(tmp_path / "python", tmp_path)
