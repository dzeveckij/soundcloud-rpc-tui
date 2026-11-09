from __future__ import annotations

import asyncio
import time
import webbrowser
from typing import Any

from pypresence import ActivityType, AioPresence, StatusDisplayType

from .core import NowPlaying, Settings, normalize_track_info
from .soundcloud import sized_artwork

DISCORD_CLIENT_ID = "1090770350251458592"
RECONNECT_SECONDS = 15
ANCHOR_DRIFT_SECONDS = 3


class DiscordPresence:
    """Mirrors playback to Discord, sending an update only when something changed.

    Discord throttles Rich Presence, and its timestamps count up on their own, so
    the track's start time is held steady instead of being recalculated each tick.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.status = "off"
        self._rpc: Any | None = None
        self._task: asyncio.Task | None = None
        self._applied: dict[str, Any] | None = None
        self._has_applied = False
        self._next_connect = 0.0

    def sync(self, now: NowPlaying | None) -> None:
        """Call this regularly. Always converges on the latest state."""
        if self._task and not self._task.done():
            return
        if not self.settings.discord_rich_presence:
            self.status = "off"
            if self._rpc is not None:
                self._task = asyncio.create_task(self._disconnect())
            return
        payload = self._payload(now)
        if self._has_applied and self._same(payload, self._applied):
            return
        self._task = asyncio.create_task(self._apply(payload))

    def _payload(self, now: NowPlaying | None) -> dict[str, Any] | None:
        if now is None or now.buffering:
            return self._applied if (now is not None and self._has_applied) else None
        if not now.playing:
            if not self.settings.display_when_idling:
                return None
            return {
                "activity_type": ActivityType.LISTENING,
                "name": "SoundCloud",
                "details": "SoundCloud",
                "state": "Paused",
                "instance": False,
            }

        item = now.item
        normalized = normalize_track_info(item.title, item.author, self.settings.track_parser_enabled)
        title = (normalized.track or "Unknown Track").ljust(2)[:128]
        artist = (normalized.artist or "Unknown Artist").ljust(2)[:128]
        start = int(time.time() - now.position)
        payload: dict[str, Any] = {
            "activity_type": ActivityType.LISTENING,
            "status_display_type": self._status_display(),
            "name": "SoundCloud",
            "details": title,
            "state": artist,
            "start": start,
            "instance": False,
        }
        artwork = sized_artwork(item.artwork, "t500x500")
        if artwork.startswith("https://"):
            payload["large_image"] = artwork
            payload["large_text"] = f"{artist} - {title}"[:128]
        if now.duration > 0:
            payload["end"] = start + now.duration
        if self.settings.display_buttons and item.url:
            payload["buttons"] = [{"label": "Listen on SoundCloud", "url": item.url}]
        return payload

    def _status_display(self) -> StatusDisplayType:
        try:
            return StatusDisplayType(self.settings.status_display_type)
        except ValueError:
            return StatusDisplayType.STATE

    @staticmethod
    def _same(new: dict[str, Any] | None, old: dict[str, Any] | None) -> bool:
        if new is None or old is None:
            return new is old
        strip = lambda payload: {key: value for key, value in payload.items() if key not in ("start", "end")}
        if strip(new) != strip(old):
            return False
        return abs(new.get("start", 0) - old.get("start", 0)) <= ANCHOR_DRIFT_SECONDS

    async def _connect(self) -> bool:
        if self._rpc is not None:
            return True
        if time.monotonic() < self._next_connect:
            return False
        self._next_connect = time.monotonic() + RECONNECT_SECONDS
        try:
            rpc = AioPresence(DISCORD_CLIENT_ID)
            await rpc.connect()
        except Exception:
            self.status = "Discord isn't running"
            return False
        self._rpc = rpc
        self.status = "connected"
        return True

    async def _apply(self, payload: dict[str, Any] | None) -> None:
        if not await self._connect() or self._rpc is None:
            return
        try:
            if payload:
                await self._rpc.update(**payload)
            else:
                await self._rpc.clear()
            self._applied = payload
            self._has_applied = True
            self.status = "connected"
        except Exception:
            self.status = "reconnecting"
            self._drop_connection()

    async def _disconnect(self) -> None:
        try:
            if self._rpc is not None:
                await self._rpc.clear()
        except Exception:
            pass
        self._drop_connection()

    def _drop_connection(self) -> None:
        rpc, self._rpc = self._rpc, None
        self._applied = None
        self._has_applied = False
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                pass

    def close(self) -> None:
        self._drop_connection()


class IntegrationHub:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.discord = DiscordPresence(settings)

    def sync(self, now: NowPlaying | None) -> None:
        self.discord.sync(now)

    @staticmethod
    def open_in_browser(url: str) -> None:
        if url:
            webbrowser.open(url)

    def close(self) -> None:
        self.discord.close()
