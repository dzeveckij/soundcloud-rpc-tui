from __future__ import annotations

import io
from typing import Iterable

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import DataTable, OptionList, Static
from textual.widgets.option_list import Option

from ..core import compact_count, normalize_track_info, seconds_to_time

LIKED_COLOR = "#ff4f8b"
BLOCKS = " ▁▂▃▄▅▆▇█"

THEMES = [
    Theme(
        name="soundcloud",
        primary="#ff5500",
        secondary="#c98b6b",
        accent="#ffa45c",
        foreground="#f2e8df",
        background="#191320",
        surface="#211929",
        panel="#2a2134",
        success="#7bc98f",
        warning="#f0b35a",
        error="#ff6b6b",
        dark=True,
    ),
    Theme(
        name="soundcloud-paper",
        primary="#d94700",
        secondary="#8c5a3c",
        accent="#b83b00",
        foreground="#2a1f18",
        background="#fbf6f1",
        surface="#f3ebe3",
        panel="#e9dfd5",
        success="#2f7d4a",
        warning="#9a6200",
        error="#c0342c",
        dark=False,
    ),
]

THEME_CHOICES = [
    ("SoundCloud dusk", "soundcloud"),
    ("SoundCloud paper", "soundcloud-paper"),
    ("Textual dark", "textual-dark"),
    ("Textual light", "textual-light"),
    ("Dracula", "dracula"),
    ("Nord", "nord"),
    ("Tokyo Night", "tokyo-night"),
    ("Monokai", "monokai"),
]

NAV_ITEMS: list[tuple[str, str, str] | str] = [
    ("discover", "Discover", "D"),
    ("trending", "Trending", "c"),
    ("search", "Search", "/"),
    "Your library",
    ("feed", "Feed", "w"),
    ("likes", "Likes", "l"),
    ("playlists", "Playlists", "p"),
    ("albums", "Albums", "A"),
    ("tracks", "Your tracks", "t"),
    ("following", "Following", "P"),
    ("history", "History", "h"),
    "Listening",
    ("queue", "Up next", "v"),
    ("related", "Related", "g"),
]
NAV_WIDTH = 22
TIME_COLUMN_WIDTH = 9


def bucket_samples(samples: list[int], width: int) -> list[float]:
    """Reduce waveform samples to one 0..1 level per terminal column.

    Mastered tracks sit near full scale, which would draw a solid block, so levels
    are stretched between the track's own quiet and loud points to keep its shape.
    """
    if width <= 0:
        return []
    if not samples:
        return [0.12] * width
    count = len(samples)
    buckets: list[float] = []
    for column in range(width):
        start = column * count // width
        stop = max(start + 1, (column + 1) * count // width)
        bucket = samples[start:stop]
        buckets.append(0.5 * max(bucket) + 0.5 * sum(bucket) / len(bucket))
    ordered = sorted(buckets)
    quiet = ordered[len(ordered) // 20] * 0.75
    loud = ordered[-1 - len(ordered) // 50]
    spread = (loud - quiet) or 1.0
    return [max(0.08, min(1.0, (value - quiet) / spread)) ** 1.4 for value in buckets]


def waveform_rows(levels: Iterable[float]) -> tuple[str, str]:
    """Draw levels as two stacked rows of eighth blocks."""
    top: list[str] = []
    bottom: list[str] = []
    for level in levels:
        height = max(1, round(level * 16))
        bottom.append(BLOCKS[min(8, height)])
        top.append(BLOCKS[max(0, height - 8)])
    return "".join(top), "".join(bottom)


def render_artwork(data: bytes, columns: int, rows: int) -> Text | None:
    """Turn cover art into half-block characters, two image pixels per cell."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB").resize((columns, rows * 2), Image.Resampling.LANCZOS)
    except Exception:
        return None
    pixels = image.load()
    text = Text(no_wrap=True)
    for row in range(rows):
        for column in range(columns):
            upper = pixels[column, row * 2]
            lower = pixels[column, row * 2 + 1]
            text.append("▀", Style(color=Color.from_rgb(*upper), bgcolor=Color.from_rgb(*lower)))
        if row < rows - 1:
            text.append("\n")
    return text


class WaveformBar(Widget):
    """The track's own waveform, doubling as the progress bar. Click to seek."""

    DEFAULT_CSS = """
    WaveformBar {
        height: 2;
        width: 1fr;
    }
    """

    class Seek(Message):
        def __init__(self, ratio: float) -> None:
            super().__init__()
            self.ratio = ratio

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.samples: list[int] = []
        self.progress = 0.0
        self.active = False
        self._cached_width = -1
        self._rows: tuple[str, str] = ("", "")
        self._played_column = -1

    def set_samples(self, samples: list[int]) -> None:
        self.samples = samples
        self._cached_width = -1
        self.refresh()

    def set_progress(self, progress: float, active: bool = True) -> None:
        self.progress = max(0.0, min(1.0, progress))
        changed = active != self.active
        self.active = active
        column = int(self.progress * self.size.width)
        if column != self._played_column or changed:
            self._played_column = column
            self.refresh()

    def render(self) -> Text:
        width = self.size.width
        if width <= 0:
            return Text("")
        if width != self._cached_width:
            self._rows = waveform_rows(bucket_samples(self.samples, width))
            self._cached_width = width
        theme = self.app.current_theme
        played = theme.primary
        rest = self.app.get_css_variables().get("panel-lighten-2", "#555555")
        head = theme.foreground or "#ffffff"
        column = int(self.progress * width) if self.active else 0
        text = Text(no_wrap=True, end="")
        for index, row in enumerate(self._rows):
            text.append(row[:column], played)
            if self.active and column < width:
                text.append(row[column], head)
                text.append(row[column + 1 :], rest)
            else:
                text.append(row[column:], rest)
            if index == 0:
                text.append("\n")
        return text

    def on_click(self, event) -> None:
        if self.active and self.size.width:
            self.post_message(self.Seek(event.x / self.size.width))


class PlayerBar(Vertical):
    DEFAULT_CSS = """
    PlayerBar {
        height: 5;
        background: $panel;
        padding: 0 2;
        border-top: tall $surface;
    }
    PlayerBar #pb-top {
        height: 2;
    }
    PlayerBar #pb-meta {
        width: 1fr;
        height: 2;
    }
    PlayerBar #pb-controls {
        width: auto;
        height: 2;
        text-align: right;
        color: $text-muted;
        link-color: $text-muted;
        link-background: transparent;
        link-style: none;
        link-color-hover: $foreground;
        link-background-hover: transparent;
        link-style-hover: underline;
    }
    PlayerBar #pb-wave-row {
        height: 2;
    }
    PlayerBar .pb-time {
        width: 8;
        height: 2;
        color: $text-muted;
        content-align: left bottom;
    }
    PlayerBar #pb-total {
        content-align: right bottom;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="pb-top"):
            yield Static(Text("Choose a track to start listening", no_wrap=True), id="pb-meta", markup=False)
            yield Static("", id="pb-controls")
        with Horizontal(id="pb-wave-row"):
            yield Static("", id="pb-elapsed", classes="pb-time", markup=False)
            yield WaveformBar(id="pb-wave")
            yield Static("", id="pb-total", classes="pb-time", markup=False)

    def update_meta(self, status: str, title: str, artist: str, note: str = "") -> None:
        theme = self.app.current_theme
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(f"{status}  ", theme.primary)
        text.append(title, "bold")
        text.append("\n")
        text.append("   ")
        text.append(artist, "dim")
        if note:
            text.append(f"  {note}", theme.warning or "yellow")
        self.query_one("#pb-meta", Static).update(text)

    def clear_meta(self, message: str) -> None:
        self.query_one("#pb-meta", Static).update(Text(message, no_wrap=True))

    def update_controls(self, liked: bool, repeat: str, shuffle: bool, volume: int) -> None:
        active = self.app.current_theme.primary
        heart = f"[{LIKED_COLOR}]♥ Liked[/]" if liked else "♡ Like"
        repeat_text = {"off": "Repeat off", "all": f"[{active}]Repeat all[/]", "one": f"[{active}]Repeat one[/]"}[repeat]
        shuffle_text = f"[{active}]Shuffle on[/]" if shuffle else "Shuffle off"
        self.query_one("#pb-controls", Static).update(
            f"[@click=app.toggle_like_current]{heart}[/]   "
            f"[@click=app.toggle_repeat_one]{repeat_text}[/]   "
            f"[@click=app.toggle_shuffle]{shuffle_text}[/]   "
            f"[@click=app.volume_down]-[/] {volume}% [@click=app.volume_up]+[/]\n"
            f"[@click=app.previous_track]Previous[/]   "
            f"[@click=app.toggle_play]Play/pause[/]   "
            f"[@click=app.next_track]Next[/]"
        )

    def update_time(self, position: float, duration: int) -> None:
        self.query_one("#pb-elapsed", Static).update(seconds_to_time(position))
        self.query_one("#pb-total", Static).update(seconds_to_time(duration))

    def clear_time(self) -> None:
        self.query_one("#pb-elapsed", Static).update("")
        self.query_one("#pb-total", Static).update("")


class NavList(OptionList):
    def __init__(self, **kwargs) -> None:
        super().__init__(*self._options({}), **kwargs)

    @staticmethod
    def _prompt(label: str, key: str, count: int | None = None) -> Text:
        left = f"{label} {count}" if count else label
        text = Text(no_wrap=True)
        text.append(left.ljust(NAV_WIDTH - 1)[: NAV_WIDTH - 1])
        text.append(key, "dim")
        return text

    @classmethod
    def _options(cls, counts: dict[str, int]) -> list[Option]:
        options: list[Option] = []
        for entry in NAV_ITEMS:
            if isinstance(entry, str):
                options.append(Option(Text(" ", no_wrap=True), disabled=True))
                options.append(Option(Text(entry, style="dim"), disabled=True))
            else:
                key, label, shortcut = entry
                options.append(Option(cls._prompt(label, shortcut, counts.get(key)), id=key))
        return options

    def set_count(self, nav_key: str, count: int | None) -> None:
        for entry in NAV_ITEMS:
            if not isinstance(entry, str) and entry[0] == nav_key:
                self.replace_option_prompt(nav_key, self._prompt(entry[1], entry[2], count))
                return

    def mark_current(self, nav_key: str | None) -> None:
        if nav_key is None:
            return
        try:
            self.highlighted = self.get_option_index(nav_key)
        except Exception:
            pass


class TrackTable(DataTable):
    """The main list. Left and right seek instead of scrolling sideways."""

    BINDINGS = [
        Binding("left", "app.seek(-10)", "Back 10s", show=False),
        Binding("right", "app.seek(10)", "Forward 10s", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    class NearEnd(Message):
        pass

    class Resized(Message):
        pass

    def on_resize(self) -> None:
        self.post_message(self.Resized())

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.max_scroll_y and new_value >= self.max_scroll_y - 8:
            self.post_message(self.NearEnd())


def item_cells(item, parse_titles: bool) -> tuple[Text, Text, Text, Text]:
    """Title, artist, plays, and length cells for one row. Plain text, never markup."""
    if item.is_track:
        normalized = normalize_track_info(item.title, item.author, parse_titles)
        title = normalized.track
        if item.is_preview:
            title += " (preview)"
        style = "dim" if item.is_blocked else ""
        return (
            Text(title, style=style, no_wrap=True, overflow="ellipsis"),
            Text(normalized.artist, style="dim", no_wrap=True, overflow="ellipsis"),
            Text(compact_count(item.plays) if item.plays else "", style="dim", justify="right"),
            Text(seconds_to_time(item.duration) if item.duration else "", style="dim", justify="right"),
        )
    count = Text(_track_count(item.track_count), style="dim", justify="right")
    plays_column_is_for_tracks = Text("")
    if item.is_user:
        return (
            Text(f"@ {item.title}", no_wrap=True, overflow="ellipsis"),
            Text(item.author, style="dim", no_wrap=True, overflow="ellipsis"),
            plays_column_is_for_tracks,
            count,
        )
    return (
        Text(f"≡ {item.title}", no_wrap=True, overflow="ellipsis"),
        Text(item.author, style="dim", no_wrap=True, overflow="ellipsis"),
        plays_column_is_for_tracks,
        count,
    )


def _track_count(count: int) -> str:
    if not count:
        return ""
    return "1 track" if count == 1 else f"{compact_count(count)} tracks"
