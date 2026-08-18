"""Protocol-level tests that do not contact the real AMD server."""

from __future__ import annotations

import httpx

from ai_comic_series.config import RemoteCredentials, RemoteSettings
from ai_comic_series.jupyter import JupyterClient


def test_execute_message_uses_non_persistent_history() -> None:
    message = JupyterClient.build_execute_message("print('ok')", "session-id", "message-id")

    assert message["header"]["msg_id"] == "message-id"
    assert message["header"]["session"] == "session-id"
    assert message["content"]["store_history"] is False
    assert message["content"]["allow_stdin"] is False
    assert message["content"]["code"] == "print('ok')"


def test_contents_path_quotes_spaces_but_preserves_segments() -> None:
    assert JupyterClient._contents_path("project/a file.json") == "/api/contents/project/a%20file.json"


def test_masked_instance_404_is_an_auth_failure() -> None:
    response = httpx.Response(
        404,
        json={"detail": "Instance not found"},
        request=httpx.Request("GET", "https://example.test/api/contents/project"),
    )

    assert JupyterClient._is_auth_failure(response) is True


def test_ordinary_contents_404_is_not_an_auth_failure() -> None:
    response = httpx.Response(
        404,
        json={"message": "No such file or directory"},
        request=httpx.Request("GET", "https://example.test/api/contents/missing"),
    )

    assert JupyterClient._is_auth_failure(response) is False


def test_masked_instance_404_refreshes_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = RemoteSettings(
        "https://example.test/instance",
        "project",
        "/data",
        "python3",
        10,
        10,
    )
    client = JupyterClient(settings, RemoteCredentials(cookie="session=value"))
    responses = iter(
        [
            httpx.Response(
                404,
                json={"detail": "Instance not found"},
                request=httpx.Request("GET", "https://example.test/instance/api/contents/project"),
            ),
            httpx.Response(
                200,
                json={"type": "directory"},
                request=httpx.Request("GET", "https://example.test/instance/api/contents/project"),
            ),
        ]
    )
    refreshes: list[bool] = []
    monkeypatch.setattr(client, "_send", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(client, "_refresh_credentials", lambda: refreshes.append(True))

    try:
        response = client._request("GET", "/api/contents/project")
    finally:
        client.close()

    assert response.status_code == 200
    assert refreshes == [True]
