from __future__ import annotations

import json
from typing import Any

import pytest

from soundcloud_rpc_tui import core


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, text: str = "", headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode()
        self.headers = headers or {}
        self.url = ""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))


class FakeHttp:
    """Records requests and answers from a list of (matcher, response) rules."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.rules: list[tuple[str, str, Any]] = []

    def on(self, method: str, url_part: str, response: Any) -> None:
        self.rules.insert(0, (method, url_part, response))

    def request(self, method: str, url: str, params=None, data=None, headers=None, timeout=None) -> FakeResponse:
        call = {"method": method, "url": url, "params": params or {}, "body": json.loads(data) if data else None, "headers": headers or {}}
        self.calls.append(call)
        for rule_method, part, response in self.rules:
            if rule_method == method and part in url:
                result = response(call) if callable(response) else response
                return result if isinstance(result, FakeResponse) else FakeResponse(200, result)
        return FakeResponse(404, {})

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self.request("GET", url, params=kwargs.get("params"), headers=kwargs.get("headers"))

    def head(self, url: str, **kwargs) -> FakeResponse:
        return FakeResponse(200, {})

    def calls_to(self, method: str, url_part: str) -> list[dict]:
        return [call for call in self.calls if call["method"] == method and url_part in call["url"]]


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Every test gets throwaway config and cache directories."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def settings() -> core.Settings:
    return core.Settings(discord_rich_presence=False)


def raw_track(track_id: int = 1, **extra) -> dict:
    data = {
        "kind": "track",
        "id": track_id,
        "urn": f"soundcloud:tracks:{track_id}",
        "title": f"Track {track_id}",
        "duration": 200_000,
        "permalink_url": f"https://soundcloud.com/a/t{track_id}",
        "policy": "ALLOW",
        "user": {"id": 77, "username": "artist", "permalink_url": "https://soundcloud.com/a"},
        "track_authorization": "auth",
        "waveform_url": "https://wave.sndcdn.com/x_m.json",
        "media": {
            "transcodings": [
                {"url": f"https://api-v2.soundcloud.com/media/{track_id}/aac", "preset": "aac_160k", "format": {"protocol": "hls"}},
            ]
        },
    }
    data.update(extra)
    return data
