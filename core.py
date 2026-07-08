from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TrackInfo:
    title: str = ""
    author: str = ""
    artwork: str = ""
    elapsed: str = "0:00"
    duration: str = "0:00"
    is_playing: bool = False
    url: str = ""


@dataclass(slots=True)
class SoundCloudItem:
    title: str
    author: str = ""
    url: str = ""
    duration: str = ""
    artwork: str = ""
    kind: str = "track"
    item_id: str = ""

    def to_track(self) -> TrackInfo:
        return TrackInfo(
            title=self.title,
            author=self.author,
            artwork=self.artwork,
            elapsed="0:00",
            duration=self.duration or "0:00",
            is_playing=False,
            url=self.url,
        )


@dataclass(slots=True)
class NormalizedTrackInfo:
    artist: str
    track: str


@dataclass(slots=True)
class PlaybackState:
    track: TrackInfo
    started_at: float
    paused_at: float | None = None
    paused_seconds: float = 0
    scrobbled: bool = False


@dataclass(slots=True)
class Settings:
    soundcloud_username: str = ""
    soundcloud_cookie_file: str = ""
    soundcloud_cookies_from_browser: str = ""
    soundcloud_region: str = "us"
    default_search: str = ""
    discord_rich_presence: bool = True
    display_when_idling: bool = False
    display_sc_small_icon: bool = False
    display_buttons: bool = False
    status_display_type: int = 1
    track_parser_enabled: bool = True
    lastfm_enabled: bool = False
    lastfm_api_key: str = ""
    lastfm_secret: str = ""
    lastfm_session_key: str = ""
    open_browser_on_track: bool = False
    theme: str = "textual-dark"


def config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        return Path(root) / "soundcloud-rpc-tui"
    return Path.home() / ".config" / "soundcloud-rpc-tui"


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "settings.json"
        self.settings = self.load()

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Settings()

        allowed = {field.name for field in fields(Settings)}
        values: dict[str, Any] = {key: value for key, value in data.items() if key in allowed}
        return Settings(**values)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.settings), indent=2, sort_keys=True), encoding="utf-8")

    def update(self, **values: Any) -> None:
        for key, value in values.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.save()


DASHES = r"[\-\u2013\u2014\u2015]"
SEPARATOR_RE = re.compile(rf"(\s+{DASHES}+\s+|{DASHES}{{2,}})")
INVALID_PATTERNS = (
    re.compile(r"^\s*$"),
    re.compile(rf"^{DASHES}"),
    re.compile(rf"{DASHES}$"),
    re.compile(r"(.)\1{4,}"),
    re.compile(rf"{DASHES}{{2,}}"),
)


def _has_invalid_patterns(text: str) -> bool:
    return any(pattern.search(text) for pattern in INVALID_PATTERNS)


def parse_soundcloud_title(title: str) -> tuple[str | None, str]:
    if not isinstance(title, str) or not title:
        return None, ""

    clean_title = title.splitlines()[0].strip()
    match = SEPARATOR_RE.search(clean_title)
    if match and match.start() > 0:
        artist = clean_title[: match.start()].strip()
        track = clean_title[match.end() :].strip()
        if artist and track and not _has_invalid_patterns(artist) and not _has_invalid_patterns(track):
            return artist, track

    return None, clean_title


def normalize_track_info(title: str, author: str, use_track_parser: bool = True) -> NormalizedTrackInfo:
    artist, track = None, None
    if use_track_parser and title:
        artist, track = parse_soundcloud_title(title)
    else:
        track = title.splitlines()[0].strip() if title else ""
        
    return NormalizedTrackInfo(
        artist=artist or author or "Unknown Artist",
        track=track or "Unknown Track",
    )


def time_to_seconds(value: str | None) -> int:
    if not value:
        return 0
    raw = value.strip()
    if not raw:
        return 0
    negative = raw.startswith("-")
    if negative:
        raw = raw[1:].strip()
    seconds = 0
    for part in raw.split(":"):
        try:
            number = int(part)
        except ValueError:
            number = 0
        seconds = seconds * 60 + number
    return -seconds if negative else seconds


def seconds_to_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"