from __future__ import annotations

import asyncio
import hashlib
import time
import webbrowser
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

from pypresence import ActivityType, AioPresence

from core import PlaybackState, Settings, TrackInfo, normalize_track_info, time_to_seconds

DISCORD_CLIENT_ID = "1090770350251458592"


@dataclass(slots=True)
class IntegrationStatus:
    discord: str = "disabled"
    lastfm: str = "disabled"


def _api_signature(params: dict[str, str], secret: str) -> str:
    payload = "".join(f"{key}{params[key]}" for key in sorted(params)) + secret
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


class DiscordPresence:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._rpc: Any | None = None
        self._task: asyncio.Task | None = None
        self._last_payload: dict[str, Any] | None = None
        self.status = "disabled"

    async def connect(self) -> None:
        if not self.settings.discord_rich_presence:
            self.status = "disabled"
            return
        try:
            self._rpc = AioPresence(DISCORD_CLIENT_ID)
            await self._rpc.connect()
            self.status = "connected"
        except Exception as exc:
            self._rpc = None
            self.status = f"error: {exc}"

    def _run_task(self, payload: dict[str, Any] | None) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._apply(payload))

    def update(self, track: TrackInfo) -> None:
        if not self.settings.discord_rich_presence:
            self.status = "disabled"
            self.clear()
            return
        self._run_task(self._payload(track))

    def _payload(self, track: TrackInfo) -> dict[str, Any] | None:
        if track.is_playing:
            normalized = normalize_track_info(track.title, track.author, self.settings.track_parser_enabled)
            elapsed = max(0, time_to_seconds(track.elapsed))
            duration = time_to_seconds(track.duration)
            if duration < 0:
                duration = elapsed + abs(duration)
            title = (normalized.track or track.title or "Unknown Track")[:128]
            artist = (normalized.artist or track.author or "Unknown Artist")[:128]
            payload: dict[str, Any] = {
                "activity_type": ActivityType.LISTENING,
                "name": "SoundCloud",
                "details": title,
                "state": artist,
                "start": int(time.time()) - elapsed,
                "instance": False,
            }
            artwork = track.artwork.replace("50x50.", "500x500.") if track.artwork else ""
            if artwork.startswith(("http://", "https://")):
                payload["large_image"] = artwork
                payload["large_text"] = f"{artist} - {title}"
            if duration > 0:
                payload["end"] = payload["start"] + duration
            if self.settings.display_buttons and track.url:
                payload["buttons"] = [{"label": "Listen on SoundCloud", "url": track.url}]
            return {key: value for key, value in payload.items() if value}

        if self.settings.display_when_idling:
            return {
                "activity_type": ActivityType.LISTENING,
                "name": "SoundCloud",
                "details": "SoundCloud",
                "state": "Paused",
                "instance": False,
            }
        return None

    async def _apply(self, payload: dict[str, Any] | None) -> None:
        try:
            if self._rpc is None:
                await self.connect()
            if self._rpc is None:
                return
            if payload == self._last_payload:
                return
            if payload:
                await self._rpc.update(**payload)
            else:
                await self._rpc.clear()
            self._last_payload = payload
            self.status = "connected"
        except Exception as exc:
            self.status = f"error: {exc}"

    def clear(self) -> None:
        if self._rpc is not None:
            self._run_task(None)

    def close(self) -> None:
        if self._rpc is None:
            return
        try:
            self._rpc.close()
        except Exception:
            pass
        self._rpc = None


class LastFmClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.status = "disabled"
        self._last_now_playing_key = ""
        self._last_now_playing_at = 0.0

    def auth_url(self) -> str | None:
        if not self.settings.lastfm_api_key:
            return None
        return "https://www.last.fm/api/auth/?" + urlencode(
            {"api_key": self.settings.lastfm_api_key, "cb": "https://soundcloud.com/discover"}
        )

    def _request(self, method: str, params: dict[str, str]) -> dict:
        params["api_sig"] = _api_signature(params, self.settings.lastfm_secret)
        params["format"] = "json"
        kwargs = {"timeout": 15}
        if method == "GET":
            response = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, **kwargs)
        else:
            response = requests.post("https://ws.audioscrobbler.com/2.0/", data=params, **kwargs)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(data.get("message", f"Last.fm {method} failed"))
        return data

    def exchange_token(self, token: str) -> str:
        if not self.settings.lastfm_api_key or not self.settings.lastfm_secret:
            raise RuntimeError("Last.fm API key and secret are required")
        data = self._request("GET", {
            "method": "auth.getSession",
            "api_key": self.settings.lastfm_api_key,
            "token": token,
        })
        return str(data["session"]["key"])

    def _track_action(self, method: str, track: TrackInfo, **extra: str) -> None:
        normalized = normalize_track_info(track.title, track.author, self.settings.track_parser_enabled)
        self._request("POST", {
            "method": method,
            "api_key": self.settings.lastfm_api_key,
            "sk": self.settings.lastfm_session_key,
            "artist": normalized.artist,
            "track": normalized.track,
            **extra,
        })

    def update_now_playing(self, track: TrackInfo) -> None:
        if not self._ready():
            return
        key = f"{track.author}\0{track.title}"
        now = time.time()
        if key == self._last_now_playing_key and now - self._last_now_playing_at < 30:
            return
        self._track_action("track.updateNowPlaying", track)
        self._last_now_playing_key = key
        self._last_now_playing_at = now
        self.status = "now playing"

    def scrobble(self, track: TrackInfo) -> None:
        if not self._ready():
            return
        self._track_action("track.scrobble", track, timestamp=str(int(time.time())))
        self.status = "scrobbled"

    def _ready(self) -> bool:
        if not self.settings.lastfm_enabled:
            self.status = "disabled"
            return False
        if not (self.settings.lastfm_api_key and self.settings.lastfm_secret and self.settings.lastfm_session_key):
            self.status = "not authenticated"
            return False
        return True


class IntegrationHub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.discord = DiscordPresence(settings)
        self.lastfm = LastFmClient(settings)

    def update(self, state: PlaybackState | None) -> IntegrationStatus:
        if state is None:
            self.discord.clear()
            return self.status()

        self.discord.update(state.track)
        if state.track.is_playing:
            try:
                self.lastfm.update_now_playing(state.track)
            except Exception as exc:
                self.lastfm.status = f"error: {exc}"

            duration = max(1, abs(time_to_seconds(state.track.duration)))
            played = time.time() - state.started_at - state.paused_seconds
            if not state.scrobbled and played >= min(duration / 2, 240):
                try:
                    self.lastfm.scrobble(state.track)
                    state.scrobbled = True
                except Exception as exc:
                    self.lastfm.status = f"error: {exc}"
        return self.status()

    def status(self) -> IntegrationStatus:
        return IntegrationStatus(
            discord=self.discord.status,
            lastfm=self.lastfm.status,
        )

    def open_track(self, track: TrackInfo) -> None:
        if track.url and self.settings.open_browser_on_track:
            webbrowser.open(track.url)

    def close(self) -> None:
        self.discord.close()