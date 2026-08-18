"""Local fake-server coverage for the bearer direct control plane."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from ai_comic_series.config import ProjectSettings, RemoteSettings
from ai_comic_series.direct_transport import DirectCredentials, DirectManager, DirectTransport
from ai_comic_series.exceptions import IntegrityError, RemoteExecutionError, RemoteProtocolError
from remote.direct_gateway import GatewayLimits, create_server

TOKEN = "direct-test-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@pytest.fixture
def direct_server(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    for directory in ("remote", "config", "workflows", "production", "status", "logs", "artifacts"):
        (tmp_path / directory).mkdir()
    (tmp_path / "config" / "models.json").write_text(
        json.dumps({"profiles": {"wan-quality": {"files": []}}}),
        encoding="utf-8",
    )
    server = create_server(
        tmp_path,
        TOKEN,
        port=0,
        limits=GatewayLimits(max_sync_bytes=1024 * 1024, max_artifact_bytes=8 * 1024 * 1024),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        assert host == "127.0.0.1"
        yield tmp_path, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def transport(base_url: str, token: str = TOKEN) -> DirectTransport:
    return DirectTransport(base_url, DirectCredentials(token), timeout_seconds=5)


def project_settings(root: Path) -> ProjectSettings:
    return ProjectSettings(
        root=root,
        project_id="direct-test",
        title="Direct Test",
        episode="ep01",
        remote=RemoteSettings(
            base_url="https://developer.amd.com.cn/radeon/instances/test",
            remote_root="ai-comic-series",
            data_root="/ai-comic-series",
            kernel_name="python3",
            request_timeout_seconds=5,
            execution_timeout_seconds=5,
        ),
        raw={},
    )


def test_gateway_requires_bearer_and_redacts_all_representations(direct_server: tuple[Path, str]) -> None:
    root, base_url = direct_server
    (root / "status" / "bootstrap.json").write_text('{"state":"complete"}\n', encoding="utf-8")

    assert httpx.get(f"{base_url}/v1/status", params={"job": "bootstrap"}).status_code == 401
    assert (
        httpx.get(
            f"{base_url}/v1/status",
            params={"job": "bootstrap"},
            headers={"Authorization": "Bearer wrong-token-that-is-long-enough-000000000"},
        ).status_code
        == 401
    )
    credentials = DirectCredentials(TOKEN)
    with transport(base_url) as client:
        assert client.read_status("bootstrap")["state"] == "complete"
        assert TOKEN not in repr(client)
    assert TOKEN not in repr(credentials)


def test_sync_is_hash_bound_bounded_and_path_confined(direct_server: tuple[Path, str]) -> None:
    root, base_url = direct_server
    content = b"print('safe sync')\n"
    with transport(base_url) as client:
        result = client.sync_file("remote/example.py", content)
        assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert (root / "remote" / "example.py").read_bytes() == content

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Content-SHA256": "0" * 64,
        "Content-Type": "application/octet-stream",
    }
    mismatch = httpx.put(f"{base_url}/v1/sync", params={"path": "config/bad.json"}, headers=headers, content=b"{}")
    traversal = httpx.put(
        f"{base_url}/v1/sync",
        params={"path": "remote/../../escape.py"},
        headers={**headers, "X-Content-SHA256": hashlib.sha256(b"x").hexdigest()},
        content=b"x",
    )
    assert mismatch.status_code == 422
    assert traversal.status_code in {400, 403}
    assert not (root.parent / "escape.py").exists()


def test_status_log_stop_resume_and_job_whitelist(
    direct_server: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base_url = direct_server
    (root / "logs" / "models.log").write_bytes(b"old\nlatest-line\n")
    (root / "remote" / "generate.py").write_text(
        "import os\n"
        "print('direct=' + os.environ.get('AI_COMIC_DIRECT_TOKEN', 'missing'))\n"
        "print('jupyter=' + os.environ.get('AI_COMIC_JUPYTER_COOKIE', 'missing'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_COMIC_DIRECT_TOKEN", "must-not-reach-job-child-0123456789-ABCDEFGHIJ")
    monkeypatch.setenv("AI_COMIC_JUPYTER_COOKIE", "must-not-reach-job-child")
    with transport(base_url) as client:
        assert client.read_status("models") == {"state": "not-started", "job": "models"}
        assert client.read_log_tail("models", max_bytes=12).endswith("latest-line\n")
        with pytest.raises(RemoteExecutionError, match="Unknown start-job kind"):
            client.start_job({"kind": "arbitrary", "script": "/bin/sh"})
        started = client.start_job({"kind": "generate", "stage": "videos", "maxWorkers": 1})
        assert started["job"] == "generate-videos"
        assert int(started["pid"]) > 0
        generation_log = root / "logs" / "generate-videos.log"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "jupyter=missing" not in generation_log.read_text(
            encoding="utf-8", errors="replace"
        ):
            time.sleep(0.02)
        log_text = generation_log.read_text(encoding="utf-8")
        assert "direct=missing" in log_text
        assert "jupyter=missing" in log_text
        assert client.stop()["stopSignal"] == "production/STOP"
        assert (root / "production" / "STOP").is_file()
        assert client.resume()["removedStopSignal"] == "production/STOP"
        assert not (root / "production" / "STOP").exists()


def test_streaming_fetch_verifies_sha_and_supports_verified_delete(
    direct_server: tuple[Path, str], tmp_path: Path
) -> None:
    root, base_url = direct_server
    content = (b"generated-video-frame-data" * 50_000)[:1_100_000]
    artifact = root / "artifacts" / "assets" / "generated" / "video" / "scene.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "downloaded.mp4"
    with transport(base_url) as client:
        info = client.artifact_info("artifacts/assets/generated/video/scene.mp4")
        assert info == {"bytes": len(content), "sha256": digest}
        result = client.download_to(
            "artifacts/assets/generated/video/scene.mp4",
            target,
            expected_sha256=digest,
            expected_bytes=len(content),
        )
        assert result["sha256"] == digest
        with pytest.raises(IntegrityError, match="expected SHA-256 mismatch"):
            client.download_to(
                "artifacts/assets/generated/video/scene.mp4",
                tmp_path / "bad.mp4",
                expected_sha256="f" * 64,
            )
        assert client.delete_artifact("artifacts/assets/generated/video/scene.mp4")["deleted"] is True
    assert target.read_bytes() == content
    assert not artifact.exists()
    assert not (tmp_path / "bad.mp4").exists()


def test_direct_manager_fetches_metadata_atomically_and_matches_start_interface(
    direct_server: tuple[Path, str],
) -> None:
    root, base_url = direct_server
    content = b"wan-video"
    metadata = b'{"kind":"wan-i2v"}\n'
    mirror = root / "artifacts" / "assets" / "generated" / "video" / "s001.mp4"
    mirror.parent.mkdir(parents=True)
    mirror.write_bytes(content)
    mirror.with_suffix(".mp4.meta.json").write_bytes(metadata)
    (root / "status" / "generate-videos.json").write_text(
        json.dumps(
            {
                "state": "complete",
                "completed": [
                    {
                        "mirror": "artifacts/assets/generated/video/s001.mp4",
                        "target": "assets/generated/video/s001.mp4",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "bytes": len(content),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with transport(base_url) as client:
        manager = DirectManager(project_settings(root), client)
        request = manager._start_request(  # noqa: SLF001 - verifies the compatibility boundary without spawning
            "generate-videos",
            "generate.py",
            [
                "--status",
                "status/generate-videos.json",
                "--queue",
                "production/generation-queue.json",
                "--stage",
                "videos",
                "--max-workers",
                "4",
            ],
        )
        assert request == {"kind": "generate", "stage": "videos", "maxWorkers": 4}
        motion_request = manager._start_request(  # noqa: SLF001
            "generate-motion-keyframes",
            "generate.py",
            [
                "--status",
                "status/generate-motion-keyframes.json",
                "--queue",
                "production/generation-queue.json",
                "--stage",
                "motion-keyframes",
                "--max-workers",
                "2",
            ],
        )
        assert motion_request == {"kind": "generate", "stage": "motion-keyframes", "maxWorkers": 2}
        with pytest.raises(RemoteProtocolError, match="non-whitelisted"):
            manager._start_request("shell", "shell.py", [])  # noqa: SLF001
        fetched = manager.fetch_artifacts("generate-videos")
    target = root / "assets" / "generated" / "video" / "s001.mp4"
    assert fetched == [
        {
            "target": "assets/generated/video/s001.mp4",
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
    ]
    assert target.read_bytes() == content
    assert target.with_suffix(".mp4.meta.json").read_bytes() == metadata
    assert not mirror.exists()
    assert not mirror.with_suffix(".mp4.meta.json").exists()
