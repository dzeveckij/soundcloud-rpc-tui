from __future__ import annotations

import json
import os
import random
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

COLLECTION_KINDS = ("playlist", "album", "system-playlist")
SETTINGS_SCHEMA_VERSION = 2
DEFAULT_THEME = "soundcloud"


@dataclass(slots=True)
class SoundCloudItem:
    """A track, playlist, album, system playlist, or user returned by the API."""

    kind: str = "track"
    item_id: str = ""
    title: str = ""
    author: str = ""
    url: str = ""
    duration: int = 0
    artwork: str = ""
    urn: str = ""
    user_id: str = ""
    user_url: str = ""
    plays: int = 0
    likes: int = 0
    reposts: int = 0
    genre: str = ""
    created_at: str = ""
    waveform_url: str = ""
    policy: str = ""
    track_authorization: str = ""
    transcodings: list[dict] = field(default_factory=list)
    track_count: int = 0
    followers: int = 0
    sharing: str = ""
    owned: bool = False
    is_stub: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.item_id}"

    @property
    def is_track(self) -> bool:
        return self.kind == "track"

    @property
    def is_collection(self) -> bool:
        return self.kind in COLLECTION_KINDS

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_preview(self) -> bool:
        return self.policy == "SNIP"

    @property
    def is_blocked(self) -> bool:
        return self.policy == "BLOCK"


@dataclass(slots=True)
class Engagement:
    """What the signed-in account has liked, reposted, and followed."""

    liked_tracks: set[str] = field(default_factory=set)
    liked_playlists: set[str] = field(default_factory=set)
    reposted_tracks: set[str] = field(default_factory=set)
    reposted_playlists: set[str] = field(default_factory=set)
    followings: set[str] = field(default_factory=set)

    def is_liked(self, item: SoundCloudItem) -> bool:
        if item.is_user:
            return item.item_id in self.followings
        if item.is_track:
            return item.item_id in self.liked_tracks
        return item.item_id in self.liked_playlists

    def set_liked(self, item: SoundCloudItem, value: bool) -> None:
        if item.is_user:
            target = self.followings
        elif item.is_track:
            target = self.liked_tracks
        else:
            target = self.liked_playlists
        if value:
            target.add(item.item_id)
        else:
            target.discard(item.item_id)

    def is_reposted(self, item: SoundCloudItem) -> bool:
        target = self.reposted_tracks if item.is_track else self.reposted_playlists
        return item.item_id in target

    def set_reposted(self, item: SoundCloudItem, value: bool) -> None:
        target = self.reposted_tracks if item.is_track else self.reposted_playlists
        if value:
            target.add(item.item_id)
        else:
            target.discard(item.item_id)


@dataclass(slots=True)
class NormalizedTrackInfo:
    artist: str
    track: str


@dataclass(slots=True)
class NowPlaying:
    item: SoundCloudItem
    position: float = 0.0
    playing: bool = False
    buffering: bool = True

    @property
    def duration(self) -> int:
        return self.item.duration


class PlayQueue:
    """Play order, independent of whichever view is on screen."""

    REPEAT_MODES = ("off", "all", "one")

    def __init__(self, rng: random.Random | None = None) -> None:
        self.items: list[SoundCloudItem] = []
        self.index = -1
        self.repeat = "off"
        self.shuffle = False
        self.source_key = ""
        self._original: list[SoundCloudItem] = []
        self._queued = 0
        self._rng = rng or random.Random()

    @property
    def current(self) -> SoundCloudItem | None:
        if 0 <= self.index < len(self.items):
            return self.items[self.index]
        return None

    @property
    def upcoming(self) -> list[SoundCloudItem]:
        return self.items[self.index + 1 :]

    def load(self, tracks: Iterable[SoundCloudItem], start: int = 0, source_key: str = "") -> None:
        self.items = list(tracks)
        self._original = list(self.items)
        self.index = start if self.items else -1
        self.source_key = source_key
        self._queued = 0
        if self.shuffle:
            self._shuffle_remaining()

    def extend(self, tracks: Iterable[SoundCloudItem]) -> None:
        tracks = list(tracks)
        self._original.extend(tracks)
        if self.shuffle:
            insert_at = self.index + 1 + self._queued
            pool = self.items[insert_at:] + tracks
            self._rng.shuffle(pool)
            self.items[insert_at:] = pool
        else:
            self.items.extend(tracks)

    def add_next(self, track: SoundCloudItem) -> None:
        """Queue a track after anything already queued by hand."""
        if self.index < 0:
            self.items.append(track)
            self._original.append(track)
            self.index = len(self.items) - 1
            return
        self.items.insert(self.index + 1 + self._queued, track)
        self._original.append(track)
        self._queued += 1

    def advance(self, manual: bool = False) -> SoundCloudItem | None:
        if not self.items:
            return None
        if self.repeat == "one" and not manual:
            return self.current
        if self.index + 1 < len(self.items):
            self.index += 1
            self._queued = max(0, self._queued - 1)
            return self.current
        if self.repeat in ("all", "one"):
            self.index = 0
            self._queued = 0
            return self.current
        return None

    def back(self) -> SoundCloudItem | None:
        if not self.items:
            return None
        if self.index > 0:
            self.index -= 1
            return self.current
        if self.repeat in ("all", "one"):
            self.index = len(self.items) - 1
            return self.current
        return None

    def jump_to_upcoming(self, offset: int) -> SoundCloudItem | None:
        target = self.index + 1 + offset
        if not 0 <= target < len(self.items):
            return None
        self.index = target
        self._queued = max(0, self._queued - offset - 1)
        return self.current

    def remove_upcoming(self, offset: int) -> SoundCloudItem | None:
        target = self.index + 1 + offset
        if not 0 <= target < len(self.items):
            return None
        removed = self.items.pop(target)
        if offset < self._queued:
            self._queued -= 1
        for position, original in enumerate(self._original):
            if original is removed:
                del self._original[position]
                break
        return removed

    def cycle_repeat(self) -> str:
        position = self.REPEAT_MODES.index(self.repeat)
        self.repeat = self.REPEAT_MODES[(position + 1) % len(self.REPEAT_MODES)]
        return self.repeat

    def toggle_shuffle(self) -> bool:
        self.shuffle = not self.shuffle
        if self.shuffle:
            self._shuffle_remaining()
        else:
            self._restore_order()
        return self.shuffle

    def _shuffle_remaining(self) -> None:
        start = max(self.index, -1) + 1 + self._queued
        remaining = self.items[start:]
        self._rng.shuffle(remaining)
        self.items[start:] = remaining

    def _restore_order(self) -> None:
        current = self.current
        self.items = list(self._original)
        self._queued = 0
        if current is None:
            self.index = -1
            return
        for position, item in enumerate(self.items):
            if item is current:
                self.index = position
                return
        self.items.insert(0, current)
        self.index = 0


@dataclass(slots=True)
class Settings:
    schema_version: int = SETTINGS_SCHEMA_VERSION
    soundcloud_username: str = ""
    soundcloud_cookie_file: str = ""
    soundcloud_cookies_from_browser: str = ""
    default_search: str = ""
    discord_rich_presence: bool = True
    display_when_idling: bool = False
    display_buttons: bool = False
    status_display_type: int = 1
    track_parser_enabled: bool = True
    open_browser_on_track: bool = False
    report_play_history: bool = True
    autoplay_related: bool = False
    player_backend: str = "auto"
    volume: int = 80
    shuffle: bool = False
    repeat: str = "off"
    theme: str = DEFAULT_THEME


def config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        return Path(root) / "soundcloud-rpc-tui"
    return Path.home() / ".config" / "soundcloud-rpc-tui"


def cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    if root:
        return Path(root) / "soundcloud-rpc-tui"
    return Path.home() / ".cache" / "soundcloud-rpc-tui"


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write through a same-directory temp file so a crash cannot truncate `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


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
        if not isinstance(data, dict):
            return Settings()

        if data.get("schema_version", 1) < SETTINGS_SCHEMA_VERSION and data.get("theme") == "textual-dark":
            # The old default was never a real choice; move it to the new default.
            data["theme"] = DEFAULT_THEME

        allowed = {field_.name for field_ in fields(Settings)}
        values: dict[str, Any] = {key: value for key, value in data.items() if key in allowed}
        values["schema_version"] = SETTINGS_SCHEMA_VERSION
        return Settings(**values)

    def save(self) -> None:
        atomic_write(self.path, json.dumps(asdict(self.settings), indent=2, sort_keys=True))

    def update(self, **values: Any) -> None:
        for key, value in values.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.save()


DASHES = r"[\-–—―]"
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


def seconds_to_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def compact_count(value: int) -> str:
    if value < 1000:
        return str(value)
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            text = f"{value / limit:.1f}".removesuffix(".0")
            return f"{text}{suffix}"
    return str(value)
