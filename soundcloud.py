from __future__ import annotations

import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Iterable

from core import Settings, SoundCloudItem, TrackInfo, seconds_to_time


@dataclass(slots=True)
class SoundCloudStatus:
    authenticated: bool
    message: str


@dataclass(slots=True)
class PlayerStatus:
    available: bool
    message: str


class SoundCloudClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def status(self) -> SoundCloudStatus:
        if self.settings.soundcloud_cookie_file:
            return SoundCloudStatus(True, f"cookies.txt: {self.settings.soundcloud_cookie_file}")
        if self.settings.soundcloud_cookies_from_browser:
            return SoundCloudStatus(True, f"browser cookies: {self.settings.soundcloud_cookies_from_browser}")
        return SoundCloudStatus(False, "public mode")

    def search(self, query: str, limit: int = 30) -> list[SoundCloudItem]:
        query = query.strip()
        if not query:
            return []
        return self._playlist_items(f"scsearch{limit}:{query}", limit=limit)

    def charts(self, genre: str = "all-music", limit: int = 50) -> list[SoundCloudItem]:
        return self.search(f"soundcloud {genre} trending", limit=limit)

    def _user_items(self, endpoint: str, limit: int) -> list[SoundCloudItem]:
        username = self._require_username()
        return self._playlist_items(f"https://soundcloud.com/{username}/{endpoint}", limit=limit)

    def user_tracks(self, limit: int = 50) -> list[SoundCloudItem]:
        return self._user_items("tracks", limit)

    def likes(self, limit: int = 50) -> list[SoundCloudItem]:
        return self._user_items("likes", limit)

    def playlists(self, limit: int = 50) -> list[SoundCloudItem]:
        return self._user_items("sets", limit)

    def recommended_for(self, track_url: str, limit: int = 50) -> list[SoundCloudItem]:
        if not track_url:
            return self.charts(limit=limit)
        return self._playlist_items(track_url.rstrip("/") + "/recommended", limit=limit)

    def playlist_tracks(self, playlist_url: str, limit: int = 100) -> list[SoundCloudItem]:
        return self._playlist_items(playlist_url, limit=limit)

    def resolve_track(self, url: str) -> TrackInfo:
        info = self._extract(url)
        return self._item_from_info(info).to_track()

    def stream_url(self, url: str) -> str:
        info = self._extract(url)
        stream = str(info.get("url") or "")
        if not stream:
            raise RuntimeError("No playable stream URL returned")
        return stream

    def _require_username(self) -> str:
        username = self.settings.soundcloud_username.strip().strip("/")
        if not username:
            raise RuntimeError("Set your SoundCloud username in settings first")
        if username.startswith("https://soundcloud.com/"):
            username = username.removeprefix("https://soundcloud.com/").strip("/")
        return username.split("/")[0]

    def _playlist_items(self, url: str, limit: int) -> list[SoundCloudItem]:
        info = self._extract(url, playlist=True)
        entries = info.get("entries") or []
        return [self._item_from_info(entry) for entry in self._take(entries, limit) if entry]

    def _extract(self, url: str, playlist: bool = False) -> dict:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp is required for SoundCloud browsing") from exc

        options: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestaudio/best",
            "extract_flat": "in_playlist" if playlist else False,
            "playlistend": 100 if playlist else None,
        }
        if self.settings.soundcloud_cookie_file:
            options["cookiefile"] = self.settings.soundcloud_cookie_file
        if self.settings.soundcloud_cookies_from_browser:
            options["cookiesfrombrowser"] = (self.settings.soundcloud_cookies_from_browser,)

        with yt_dlp.YoutubeDL({key: value for key, value in options.items() if value is not None}) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("No SoundCloud data returned")
        return dict(info)

    def _item_from_info(self, info: dict) -> SoundCloudItem:
        duration = int(info.get("duration") or 0)
        webpage_url = str(info.get("webpage_url") or info.get("url") or "")
        title = str(info.get("title") or "Untitled")
        author = str(info.get("uploader") or info.get("artist") or info.get("creator") or "")
        kind = "playlist" if info.get("_type") == "playlist" or "/sets/" in webpage_url else "track"
        return SoundCloudItem(
            title=title,
            author=author,
            url=webpage_url,
            duration=seconds_to_time(duration) if duration else "",
            artwork=str(info.get("thumbnail") or ""),
            kind=kind,
            item_id=str(info.get("id") or ""),
        )

    @staticmethod
    def _take(entries: Iterable[dict], limit: int) -> Iterable[dict]:
        for index, entry in enumerate(entries):
            if index >= limit:
                break
            yield entry


class SoundCloudPlayer:
    def __init__(self, client: SoundCloudClient) -> None:
        self.client = client
        self.process: subprocess.Popen[bytes] | None = None
        self.status = PlayerStatus(False, "not checked")
        self._paused = False

    def check(self) -> PlayerStatus:
        if shutil.which("ffplay") is None:
            self.status = PlayerStatus(False, "install ffplay")
        else:
            self.status = PlayerStatus(True, "ready")
        return self.status

    def play(self, url: str) -> None:
        if not url:
            raise RuntimeError("Track URL is required for playback")
        if self.check().available is False:
            raise RuntimeError(self.status.message)
        stream_url = self.client.stream_url(url)
        self.stop()
        command = [
            "ffplay",
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "error",
            stream_url,
        ]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.status = PlayerStatus(True, "playing")
        self._paused = False

    def finished(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def pause(self) -> bool:
        if self.process is None or self.process.poll() is not None:
            self.status = PlayerStatus(self.status.available, "stopped")
            self._paused = False
            return False
        if self._paused:
            self.status = PlayerStatus(True, "paused")
            return True
        try:
            self.process.send_signal(signal.SIGSTOP)
            self.status = PlayerStatus(True, "paused")
            self._paused = True
            return True
        except (AttributeError, OSError, ValueError):
            if not self.process.stdin:
                self.status = PlayerStatus(True, "player pipe closed")
                return False
            try:
                self.process.stdin.write(b"p")
                self.process.stdin.flush()
                self.status = PlayerStatus(True, "paused")
                self._paused = True
                return True
            except OSError:
                self.status = PlayerStatus(True, "player pipe closed")
                return False

    def resume(self) -> bool:
        if self.process is None or self.process.poll() is not None:
            self.status = PlayerStatus(self.status.available, "stopped")
            self._paused = False
            return False
        if not self._paused:
            self.status = PlayerStatus(True, "playing")
            return True
        try:
            self.process.send_signal(signal.SIGCONT)
            self.status = PlayerStatus(True, "playing")
            self._paused = False
            return True
        except (AttributeError, OSError, ValueError):
            if not self.process.stdin:
                self.status = PlayerStatus(True, "player pipe closed")
                return False
            try:
                self.process.stdin.write(b"p")
                self.process.stdin.flush()
                self.status = PlayerStatus(True, "playing")
                self._paused = False
                return True
            except OSError:
                self.status = PlayerStatus(True, "player pipe closed")
                return False

    def stop(self) -> None:
        if self.process is None:
            self._paused = False
            return
        if self.process.poll() is None:
            try:
                if self._paused:
                    self.process.send_signal(signal.SIGCONT)
                if self.process.stdin:
                    self.process.stdin.write(b"q")
                    self.process.stdin.flush()
                self.process.wait(timeout=0.3)
            except Exception:
                self.process.terminate()
                try:
                    self.process.wait(timeout=0.3)
                except Exception:
                    self.process.kill()
        self.process = None
        self._paused = False
        self.status = PlayerStatus(self.status.available, "stopped")
