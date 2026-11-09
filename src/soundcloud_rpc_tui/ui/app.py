from __future__ import annotations

import atexit
import os
import signal
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Input, OptionList, Static, Tab, Tabs

from ..auth import Session, SessionManager, SignInError, SignInRequired
from ..core import DEFAULT_THEME, Engagement, NowPlaying, PlayQueue, SettingsStore, SoundCloudItem, normalize_track_info
from ..integrations import IntegrationHub
from ..player import Player, PlayerUnavailable
from .screens import ConfirmScreen, HelpScreen, InfoScreen, NewPlaylistScreen, PlaylistPickerScreen, SettingsScreen, SignInScreen
from ..soundcloud import Page, SoundCloudClient, SoundCloudError, sized_artwork
from .widgets import LIKED_COLOR, THEMES, TIME_COLUMN_WIDTH, NavList, PlayerBar, TrackTable, WaveformBar, item_cells, render_artwork

VIEW_TTL_SECONDS = 300
SEARCH_DEBOUNCE_SECONDS = 0.28
SEEK_DEBOUNCE_SECONDS = 0.25
VOLUME_DEBOUNCE_SECONDS = 0.35
MAX_STREAM_RECOVERIES = 2
MAX_SKIPS_IN_A_ROW = 5
RECOVERY_RESET_SECONDS = 60
STALL_SECONDS = 25
ART_COLUMNS = 24
ART_ROWS = 12
ART_MIN_TERMINAL_HEIGHT = 38
SEARCH_SCOPES = ("all", "tracks", "playlists", "albums", "people")


@dataclass(slots=True)
class View:
    key: str
    title: str
    load: Callable[[], Page]
    nav: str | None = None
    collection: SoundCloudItem | None = None
    cacheable: bool = True
    empty_message: str = "Nothing here yet."


class SoundCloudRpcTui(App[None]):
    TITLE = "SoundCloud"
    AUTO_FOCUS = "#table"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (92, "-wide")]
    CSS = """
    * {
        scrollbar-color: $panel-lighten-2;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-corner-color: $background;
    }
    Screen {
        layout: vertical;
        background: $background;
    }
    #body {
        height: 1fr;
    }
    #sidebar {
        width: 26;
        background: $surface;
        padding: 1 0 0 0;
    }
    .-narrow #sidebar {
        display: none;
    }
    #brand {
        height: 2;
        padding: 0 2;
        color: $primary;
        text-style: bold;
    }
    #nav {
        height: 1fr;
        background: $surface;
        border: none;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    #nav > .option-list--option-highlighted {
        background: $surface;
        color: $primary;
        text-style: bold;
    }
    #nav:focus > .option-list--option-highlighted {
        background: $primary 30%;
        color: $foreground;
    }
    #account {
        link-color: $text-muted;
        link-background: transparent;
        link-style: none;
        link-color-hover: $foreground;
        link-background-hover: transparent;
        link-style-hover: underline;
    }
    #art {
        height: 12;
        width: 24;
        margin: 0 1;
        display: none;
    }
    #art.-visible {
        display: block;
    }
    #account {
        height: 2;
        padding: 1 2 0 2;
        color: $text-muted;
    }
    #main {
        width: 1fr;
        padding: 1 2 0 2;
    }
    #search {
        height: 3;
        margin-bottom: 1;
        border: tall $surface;
        background: $surface;
    }
    #search:focus {
        border: tall $primary;
    }
    #scopes {
        height: 2;
        display: none;
    }
    #scopes.-visible {
        display: block;
    }
    #view-title {
        height: 2;
        padding: 0 1;
    }
    #table {
        height: 1fr;
        background: $background;
        scrollbar-size-vertical: 1;
        overflow-x: hidden;
    }
    #table.-empty {
        height: 1;
    }
    #table > .datatable--header {
        background: $background;
        color: $text-muted;
    }
    #table > .datatable--cursor {
        background: $primary 25%;
        color: $foreground;
    }
    #table:focus > .datatable--cursor {
        background: $primary 40%;
    }
    #table > .datatable--hover {
        background: $surface;
    }
    #empty {
        height: 1fr;
        padding: 2 1;
        color: $text-muted;
        display: none;
    }
    #empty.-visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("/", "focus_search", "Search"),
        Binding("space", "toggle_play", "Play/pause"),
        Binding("n", "next_track", "Next"),
        Binding("b", "previous_track", "Previous", show=False),
        Binding("f", "toggle_like", "Like"),
        Binding("F", "toggle_like_current", "Like playing", show=False),
        Binding("a", "add_to_playlist", "Add to playlist"),
        Binding("e", "enqueue", "Queue"),
        Binding("R", "toggle_repost", "Repost", show=False),
        Binding("d", "remove", "Remove", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("L", "sign_in", "Sign in"),
        Binding("s", "settings", "Settings"),
        Binding("q", "quit", "Quit"),
        Binding("D", "open_nav('discover')", show=False),
        Binding("c", "open_nav('trending')", show=False),
        Binding("w", "open_nav('feed')", show=False),
        Binding("l", "open_nav('likes')", show=False),
        Binding("p", "open_nav('playlists')", show=False),
        Binding("A", "open_nav('albums')", show=False),
        Binding("t", "open_nav('tracks')", show=False),
        Binding("P", "open_nav('following')", show=False),
        Binding("h", "open_nav('history')", show=False),
        Binding("v", "open_nav('queue')", show=False),
        Binding("g", "open_nav('related')", show=False),
        Binding("y", "toggle_repeat_one", "Repeat", show=False),
        Binding("z", "toggle_shuffle", "Shuffle", show=False),
        Binding("left", "seek(-10)", show=False),
        Binding("right", "seek(10)", show=False),
        Binding("plus,equals_sign", "volume(5)", show=False),
        Binding("minus", "volume(-5)", show=False),
        Binding("o", "open_in_browser", show=False),
        Binding("u", "open_artist", show=False),
        Binding("i", "info", show=False),
        Binding("r", "refresh_view", show=False),
        Binding("backspace", "go_back", show=False),
        Binding("escape", "escape", show=False),
        Binding("left_square_bracket", "cycle_scope(-1)", show=False),
        Binding("right_square_bracket", "cycle_scope(1)", show=False),
    ]

    def __init__(
        self,
        store: SettingsStore | None = None,
        auth: SessionManager | None = None,
        client: SoundCloudClient | None = None,
        player: Player | None = None,
        hub: IntegrationHub | None = None,
    ) -> None:
        super().__init__()
        self.store = store or SettingsStore()
        settings = self.store.settings
        self.auth = auth or SessionManager()
        self.client = client or SoundCloudClient(settings, self.auth)
        self.player = player or Player(settings.player_backend, settings.volume)
        self.hub = hub or IntegrationHub(settings)
        atexit.register(self.player.close)

        self.engagement = Engagement()
        self.queue = PlayQueue()
        self.queue.shuffle = settings.shuffle
        self.queue.repeat = settings.repeat if settings.repeat in PlayQueue.REPEAT_MODES else "off"
        self.now: NowPlaying | None = None

        self.view: View | None = None
        self.view_stack: list[View] = []
        self.page = Page()
        self.items: list[SoundCloudItem] = []
        self.view_error = ""
        self.view_loading = False
        self.search_scope = "all"
        self._view_cache: OrderedDict[str, tuple[float, Page]] = OrderedDict()
        self._cursors: dict[str, int] = {}
        self._view_generation = 0
        self._loading_more = False

        self._play_generation = 0
        self._stream_loading = False
        self._stream_recoveries = 0
        self._recovered_at = 0.0
        self._skips_in_a_row = 0
        self._last_position = 0.0
        self._last_progress_at = 0.0
        self._pending_seek: float | None = None
        self._seek_timer: Timer | None = None
        self._volume_timer: Timer | None = None
        self._search_timer: Timer | None = None
        self._title_width = -1
        self._artist_width = -1
        self._marks: dict[int, tuple[bool, bool]] = {}
        self._waveforms: OrderedDict[str, list[int]] = OrderedDict()
        self._art: OrderedDict[str, Text | None] = OrderedDict()
        self._my_playlists: list[SoundCloudItem] | None = None
        self._last_discord_sync = 0.0

    # Layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("▂▅▇▅▃▂▄▆ SoundCloud", id="brand", markup=False)
                yield NavList(id="nav")
                yield Static("", id="art", markup=False)
                yield Static("", id="account")
            with Vertical(id="main"):
                yield Input(placeholder="Search tracks, people, and playlists, or paste a link", id="search")
                yield Tabs(
                    Tab("All", id="all"),
                    Tab("Tracks", id="tracks"),
                    Tab("Playlists", id="playlists"),
                    Tab("Albums", id="albums"),
                    Tab("People", id="people"),
                    id="scopes",
                )
                yield Static("", id="view-title", markup=False)
                yield TrackTable(id="table", cursor_type="row", zebra_stripes=False, cell_padding=1)
                yield Static("", id="empty", markup=False)
        yield PlayerBar(id="player")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        wanted = self.store.settings.theme
        self.theme = wanted if wanted in self.available_themes else DEFAULT_THEME
        self.theme_changed_signal.subscribe(self, self._theme_changed)
        self.query_one("#search", Input).value = self.store.settings.default_search
        self._render_account()
        self._render_player()
        self.set_interval(0.25, self._tick)
        self.run_worker(self._bootstrap, thread=True, exit_on_error=False, group="bootstrap")
        self.open_nav("trending")

    def _theme_changed(self, _theme: object) -> None:
        self._rebuild_table()
        self._render_player()

    def _call_ui(self, callback: Callable, *args) -> None:
        """Queue a callback on the UI thread from a worker. Never blocks, so quitting can't hang."""

        def guarded() -> None:
            try:
                callback(*args)
            except NoMatches:
                pass  # The main screen is gone, so the app is shutting down.

        try:
            self.call_later(guarded)
        except RuntimeError:
            pass  # The event loop has already closed.

    def _bootstrap(self) -> None:
        try:
            self.client.warm_up()
        except SoundCloudError:
            pass
        try:
            session = self.client.restore_session()
        except SignInRequired:
            self._call_ui(self._session_expired)
            return
        except (SoundCloudError, SignInError):
            session = self.auth.session  # Offline at launch. Keep the saved session.
        if session is not None:
            self._call_ui(self._render_account)
            self._load_engagement()

    def _load_engagement(self) -> None:
        engagement = self.client.engagement()
        self._call_ui(self._engagement_loaded, engagement)

    def _engagement_loaded(self, engagement: Engagement) -> None:
        self.engagement = engagement
        self.query_one(NavList).set_count("likes", len(engagement.liked_tracks) or None)
        self._refresh_marks()
        self._render_player()

    def _session_expired(self) -> None:
        self._render_account()
        self.notify("Your SoundCloud session expired. Press L to sign in again.", severity="warning", markup=False)

    def _render_account(self) -> None:
        account = self.query_one("#account", Static)
        session = self.auth.session
        if self.auth.signed_in and session is not None:
            account.update(f"[@click=app.sign_in]● {_markup_safe(session.username or 'Signed in')}[/]")
        elif session is not None:
            account.update("[@click=app.sign_in]○ Session expired, sign in[/]")
        else:
            account.update("[@click=app.sign_in]○ Sign in  L[/]")

    # Views

    def _nav_view(self, key: str) -> View | None:
        client = self.client
        views: dict[str, Callable[[], View]] = {
            "discover": lambda: View("discover", "Discover", client.discover, "discover"),
            "trending": lambda: View("trending", "Trending", client.trending, "trending"),
            "feed": lambda: View("feed", "Feed", client.feed, "feed", empty_message="Follow people to fill your feed."),
            "likes": lambda: View("likes", "Likes", client.likes, "likes", empty_message="Tracks you like show up here. Press f on any track."),
            "playlists": lambda: View("playlists", "Playlists", client.playlists, "playlists", empty_message="Press a on a track to start a playlist."),
            "albums": lambda: View("albums", "Albums", client.albums, "albums", empty_message="Albums you like show up here."),
            "tracks": lambda: View("tracks", "Your tracks", client.user_tracks, "tracks", empty_message="Tracks you upload show up here."),
            "following": lambda: View("following", "Following", client.followings, "following", empty_message="People you follow show up here."),
            "history": lambda: View("history", "History", client.history, "history", cacheable=False, empty_message="Tracks you play show up here."),
            "queue": lambda: View("queue", "Up next", lambda: Page(list(self.queue.upcoming)), "queue", cacheable=False, empty_message="Press e on a track to add it to Up next."),
        }
        factory = views.get(key)
        return factory() if factory else None

    def open_nav(self, key: str) -> None:
        if key == "search":
            self.action_focus_search()
            return
        if key == "related":
            current = self.now.item if self.now else self._target_track()
            if current is None:
                self.notify("Play or select a track to see related tracks.", markup=False)
                return
            self.open_view(
                View(
                    f"related:{current.item_id}",
                    f"Related to {current.title}",
                    lambda: self.client.related(current.item_id),
                    "related",
                )
            )
            return
        view = self._nav_view(key)
        if view is not None:
            self.open_view(view)

    def action_open_nav(self, key: str) -> None:
        self.open_nav(key)

    @on(OptionList.OptionSelected, "#nav")
    def nav_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.open_nav(event.option_id)

    def _remember_cursor(self) -> None:
        if self.view is not None:
            self._cursors[self.view.key] = self.query_one(TrackTable).cursor_row

    def open_view(self, view: View, push: bool = True, force: bool = False) -> None:
        if self.view is not None and self.view.key == view.key and not force:
            self.query_one(TrackTable).focus()
            return
        self._remember_cursor()
        if push and self.view is not None and self.view.key != view.key:
            self.view_stack.append(self.view)
            del self.view_stack[:-30]
        self.view = view
        self.view_error = ""
        self._view_generation += 1
        generation = self._view_generation
        self.query_one("#scopes", Tabs).set_class(view.key.startswith("search:"), "-visible")
        self.query_one(NavList).mark_current(view.nav)

        cached = self._view_cache.get(view.key)
        if cached and not force and time.monotonic() - cached[0] < VIEW_TTL_SECONDS:
            self._show_page(cached[1])
            return

        self.view_loading = True
        if not view.key.startswith("search:"):
            self.page = Page()
            self.items = []
            self._rebuild_table()
        self._render_view_header()
        self.run_worker(lambda: self._load_view(view, generation), thread=True, exit_on_error=False, group="view")

    def _load_view(self, view: View, generation: int) -> None:
        error = ""
        page = Page()
        try:
            page = view.load()
        except SignInRequired as exc:
            error = f"{exc} Press L."
        except SoundCloudError as exc:
            error = f"{exc} Press r to try again."
        self._call_ui(self._view_loaded, view, generation, page, error)

    def _view_loaded(self, view: View, generation: int, page: Page, error: str) -> None:
        if generation != self._view_generation:
            return
        self.view_loading = False
        self.view_error = error
        if not error and view.cacheable:
            self._view_cache[view.key] = (time.monotonic(), page)
            self._view_cache.move_to_end(view.key)
            while len(self._view_cache) > 40:
                self._view_cache.popitem(last=False)
        self._show_page(page)

    def _show_page(self, page: Page) -> None:
        self.page = page
        self.items = page.items
        self.view_loading = False
        self._rebuild_table()
        cursor = self._cursors.get(self.view.key, 0) if self.view else 0
        if self.items:
            self.query_one(TrackTable).move_cursor(row=min(cursor, len(self.items) - 1), animate=False)

    def _render_view_header(self) -> None:
        view = self.view
        if view is None:
            return
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(view.title, "bold")
        muted = "dim"
        if self.view_loading:
            text.append("   Loading…", muted)
        elif self.items:
            count = len(self.items)
            suffix = "+" if self.page.next_href else ""
            noun = "result" if view.key.startswith("search:") else "item"
            text.append(f"   {count}{suffix} {noun}{'' if count == 1 and not suffix else 's'}", muted)
        self.query_one("#view-title", Static).update(text)

        empty = self.query_one("#empty", Static)
        table = self.query_one(TrackTable)
        show_empty = not self.items and not self.view_loading
        empty.set_class(show_empty, "-visible")
        # Shrink instead of hiding it: a hidden table would pass keyboard focus to the
        # search box, and shortcuts like L would type into it.
        table.set_class(show_empty, "-empty")
        if show_empty:
            if self.view_error:
                empty.update(Text(self.view_error))
            elif view.key.startswith("search:"):
                empty.update(Text("No results. Try different words, or change the filter with [ and ]."))
            else:
                empty.update(Text(view.empty_message))

    def action_go_back(self) -> None:
        if not self.view_stack:
            return
        self.open_view(self.view_stack.pop(), push=False, force=False)

    def action_refresh_view(self) -> None:
        if self.view is None:
            return
        self._view_cache.pop(self.view.key, None)
        self.open_view(self.view, push=False, force=True)

    def action_escape(self) -> None:
        table = self.query_one(TrackTable)
        if self.focused is not table:
            table.focus()
        else:
            self.action_go_back()

    # Table

    def _column_widths(self) -> tuple[int, int, bool]:
        table = self.query_one(TrackTable)
        total = table.size.width or max(40, self.size.width - (30 if self.size.width >= 92 else 4))
        show_plays = total >= 70
        fixed = (1 + 1 + (7 if show_plays else 0) + TIME_COLUMN_WIDTH) + (6 if show_plays else 5) * 2 + 2
        remaining = max(20, total - fixed)
        title = max(12, int(remaining * 0.62))
        return title, max(8, remaining - title), show_plays

    def _rebuild_table(self) -> None:
        table = self.query_one(TrackTable)
        title_width, artist_width, show_plays = self._column_widths()
        self._title_width, self._artist_width = title_width, artist_width
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_column("", width=1, key="now")
        table.add_column("", width=1, key="like")
        table.add_column("Title", width=title_width, key="title")
        table.add_column("Artist", width=artist_width, key="artist")
        if show_plays:
            table.add_column(Text("Plays", justify="right"), width=7, key="plays")
        table.add_column(Text("Length", justify="right"), width=TIME_COLUMN_WIDTH, key="time")
        self._marks = {}
        for index, item in enumerate(self.items):
            self._add_row(table, index, item, show_plays)
        if 0 <= cursor < len(self.items):
            table.move_cursor(row=cursor, animate=False)
        self._render_view_header()

    def _mark_cells(self, index: int, item: SoundCloudItem) -> tuple[Text, Text]:
        playing = self.now is not None and self.now.item.key == item.key
        liked = self.engagement.is_liked(item)
        self._marks[index] = (playing, liked)
        return (
            Text("▶" if playing else " ", style=self.current_theme.primary),
            Text("♥" if liked else " ", style=LIKED_COLOR),
        )

    def _add_row(self, table: DataTable, index: int, item: SoundCloudItem, show_plays: bool) -> None:
        now_cell, like_cell = self._mark_cells(index, item)
        title, artist, plays, duration = item_cells(item, self.store.settings.track_parser_enabled)
        cells = [now_cell, like_cell, title, artist]
        if show_plays:
            cells.append(plays)
        cells.append(duration)
        table.add_row(*cells, key=str(index))

    def _refresh_marks(self) -> None:
        table = self.query_one(TrackTable)
        if table.row_count != len(self.items):
            return
        for index, item in enumerate(self.items):
            playing = self.now is not None and self.now.item.key == item.key
            liked = self.engagement.is_liked(item)
            if self._marks.get(index) == (playing, liked):
                continue
            now_cell, like_cell = self._mark_cells(index, item)
            table.update_cell(str(index), "now", now_cell)
            table.update_cell(str(index), "like", like_cell)

    def on_resize(self) -> None:
        self.call_after_refresh(self._update_artwork_visibility)

    def _update_artwork_visibility(self) -> None:
        art = self.query_one("#art", Static)
        art.set_class(self.now is not None and self.size.height >= ART_MIN_TERMINAL_HEIGHT, "-visible")

    @on(TrackTable.Resized)
    def table_resized(self) -> None:
        title_width, artist_width, _ = self._column_widths()
        if (title_width, artist_width) != (self._title_width, self._artist_width):
            self._rebuild_table()

    @on(DataTable.RowSelected, "#table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        self._activate_row(event.cursor_row)

    @on(DataTable.RowHighlighted, "#table")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row >= len(self.items) - 10:
            self._load_more()

    @on(TrackTable.NearEnd)
    def near_end(self) -> None:
        self._load_more()

    def _load_more(self) -> None:
        if self._loading_more or not self.page.next_href or self.view is None:
            return
        self._loading_more = True
        page = self.page
        generation = self._view_generation

        def work() -> None:
            try:
                more = self.client.next_page(page)
            except (SoundCloudError, SignInRequired) as exc:
                self._call_ui(self._more_failed, page, str(exc))
                return
            self._call_ui(self._more_loaded, page, generation, more)

        self.run_worker(work, thread=True, exit_on_error=False, group="more")

    def _more_failed(self, page: Page, message: str) -> None:
        self._loading_more = False
        page.next_href = ""
        self.notify(message, severity="warning", markup=False)
        self._render_view_header()

    def _more_loaded(self, page: Page, generation: int, more: Page) -> None:
        self._loading_more = False
        page.items.extend(more.items)
        page.next_href = more.next_href
        if self.view is not None and self.queue.source_key == self.view.key:
            self.queue.extend(item for item in more.items if item.is_track)
        if generation != self._view_generation:
            return
        table = self.query_one(TrackTable)
        show_plays = "plays" in {str(key.value) for key in table.columns}
        start = table.row_count
        self.items = page.items
        for index in range(start, len(self.items)):
            self._add_row(table, index, self.items[index], show_plays)
        self._render_view_header()

    def _selected_item(self) -> SoundCloudItem | None:
        table = self.query_one(TrackTable)
        if 0 <= table.cursor_row < len(self.items):
            return self.items[table.cursor_row]
        return None

    def _target_track(self) -> SoundCloudItem | None:
        selected = self._selected_item()
        if selected is not None and selected.is_track:
            return selected
        return self.now.item if self.now else None

    def _activate_row(self, row: int) -> None:
        if not 0 <= row < len(self.items):
            return
        item = self.items[row]
        if item.is_collection:
            self.open_view(
                View(
                    f"collection:{item.key}",
                    item.title,
                    lambda: self.client.collection_tracks(item),
                    self.view.nav if self.view else None,
                    collection=item,
                    empty_message="This playlist has no tracks you can play.",
                )
            )
        elif item.is_user:
            self.open_view(
                View(
                    f"user:{item.item_id}",
                    item.title,
                    lambda: self.client.user_tracks(item.item_id),
                    None,
                    empty_message="This person hasn't posted any tracks.",
                )
            )
        elif self.view is not None and self.view.key == "queue":
            if self.queue.jump_to_upcoming(row) is not None:
                self._start_current()
                self.action_refresh_view()
        else:
            tracks = [candidate for candidate in self.items if candidate.is_track]
            start = sum(1 for candidate in self.items[:row] if candidate.is_track)
            self.queue.load(tracks, start, self.view.key if self.view else "")
            self._skips_in_a_row = 0
            self._start_current()
            self._queue_changed()

    def _queue_changed(self) -> None:
        self.query_one(NavList).set_count("queue", len(self.queue.upcoming) or None)
        if self.view is not None and self.view.key == "queue":
            self.items = self.page.items = list(self.queue.upcoming)
            self._rebuild_table()

    # Search

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        if self.focused is not event.input:
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        query = event.value.strip()
        if len(query) < 2 or _looks_like_link(query):
            return
        self._search_timer = self.set_timer(SEARCH_DEBOUNCE_SECONDS, lambda: self._run_search(query))

    @on(Input.Submitted, "#search")
    def search_submitted(self, event: Input.Submitted) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
        query = event.value.strip()
        if not query:
            return
        if _looks_like_link(query):
            self._open_link(query)
        else:
            self._run_search(query)
            self.store.update(default_search=query)
        self.query_one(TrackTable).focus()

    def _run_search(self, query: str) -> None:
        scope = self.search_scope
        self.open_view(
            View(
                f"search:{scope}:{query.lower()}",
                f"Search: {query}",
                lambda: self.client.search(query, scope),
                "search",
            )
        )

    @on(Tabs.TabActivated, "#scopes")
    def scope_changed(self, event: Tabs.TabActivated) -> None:
        scope = event.tab.id or "all"
        if scope == self.search_scope:
            return
        self.search_scope = scope
        query = self.query_one("#search", Input).value.strip()
        if query and self.view is not None and self.view.key.startswith("search:"):
            self._run_search(query)

    def action_cycle_scope(self, step: int) -> None:
        if self.view is None or not self.view.key.startswith("search:"):
            return
        position = SEARCH_SCOPES.index(self.search_scope)
        self.query_one("#scopes", Tabs).active = SEARCH_SCOPES[(position + step) % len(SEARCH_SCOPES)]

    def _open_link(self, url: str) -> None:
        self.view_error = ""

        def work() -> None:
            try:
                item = self.client.resolve(url)
            except SoundCloudError as exc:
                self._call_ui(lambda message=str(exc): self.notify(message, severity="error", markup=False))
                return
            self._call_ui(self._open_item, item)

        self.run_worker(work, thread=True, exit_on_error=False, group="link")

    def _open_item(self, item: SoundCloudItem) -> None:
        if item.is_track:
            self.queue.load([item], 0)
            self._start_current()
            self._queue_changed()
            return
        self.items = [item]
        self._activate_row(0)

    # Playback

    def _start_current(self, start: float = 0.0) -> None:
        item = self.queue.current
        if item is None:
            return
        self._play_generation += 1
        generation = self._play_generation
        if start == 0.0:
            self._stream_recoveries = 0
            self.query_one(WaveformBar).set_samples(self._waveforms.get(item.item_id, []))
        self.now = NowPlaying(item, position=start, playing=True, buffering=True)
        self._stream_loading = True
        self._pending_seek = None
        self._last_position = start
        self._last_progress_at = time.monotonic()
        self._refresh_marks()
        self._render_player()
        self.run_worker(lambda: self._load_stream(item, generation, start), thread=True, exit_on_error=False, group="stream")
        if start == 0.0:
            self.run_worker(lambda: self._load_track_media(item, generation), thread=True, exit_on_error=False, group="media")
            if self.store.settings.open_browser_on_track:
                self.hub.open_in_browser(item.url)

    def _load_stream(self, item: SoundCloudItem, generation: int, start: float) -> None:
        try:
            url = self.client.stream_url(item)
            if generation != self._play_generation:
                return
            if start == 0.0:
                self.player.set_preference(self.store.settings.player_backend)
            self.player.load(url, start)
        except (SoundCloudError, SignInRequired, PlayerUnavailable, OSError) as exc:
            self._call_ui(self._stream_failed, generation, str(exc))
            return
        self._call_ui(self._stream_started, generation, item, start)

    def _stream_started(self, generation: int, item: SoundCloudItem, start: float) -> None:
        if generation != self._play_generation:
            return
        self._stream_loading = False
        self._last_progress_at = time.monotonic()
        if start == 0.0 and self.store.settings.report_play_history and self.auth.signed_in:
            self.run_worker(lambda: self._report_play(item), thread=True, exit_on_error=False, group="history")

    def _report_play(self, item: SoundCloudItem) -> None:
        try:
            self.client.record_play(item)
        except (SoundCloudError, SignInRequired):
            pass  # History is a nicety. It should never interrupt listening.

    def _stream_failed(self, generation: int, message: str) -> None:
        if generation != self._play_generation:
            return
        self._stream_loading = False
        self.notify(message, severity="warning", markup=False)
        self._skip_after_failure()

    def _skip_after_failure(self) -> None:
        self._skips_in_a_row += 1
        if self._skips_in_a_row < MAX_SKIPS_IN_A_ROW and self.queue.advance(manual=True) is not None:
            self._start_current()
            self._queue_changed()
        else:
            self.player.stop()
            if self.now:
                self.now.playing = False
                self.now.buffering = False
            self._render_player()

    def _load_track_media(self, item: SoundCloudItem, generation: int) -> None:
        samples = self._waveforms.get(item.item_id)
        if samples is None:
            samples = self.client.waveform(item)
            _remember(self._waveforms, item.item_id, samples, 50)
        self._call_ui(self._waveform_ready, generation, samples)
        url = sized_artwork(item.artwork, "t67x67")
        if not url:
            self._call_ui(self._artwork_ready, generation, None)
            return
        if url not in self._art:
            _remember(self._art, url, render_artwork(self.client.artwork_bytes(url), ART_COLUMNS, ART_ROWS), 50)
        self._call_ui(self._artwork_ready, generation, self._art.get(url))

    def _waveform_ready(self, generation: int, samples: list[int]) -> None:
        if generation == self._play_generation:
            self.query_one(WaveformBar).set_samples(samples)

    def _artwork_ready(self, generation: int, art: Text | None) -> None:
        if generation != self._play_generation:
            return
        widget = self.query_one("#art", Static)
        widget.update(art if art is not None else "")
        widget.set_class(art is not None and self.size.height >= ART_MIN_TERMINAL_HEIGHT, "-visible")

    def action_toggle_play(self) -> None:
        now = self.now
        if now is None:
            selected = self._selected_item()
            table = self.query_one(TrackTable)
            if selected is not None:
                self._activate_row(table.cursor_row)
            return
        if self._stream_loading:
            return
        if self.player.finished or (not now.playing and self.player.position == 0 and not self.player.paused):
            self._start_current()
            return
        if now.playing:
            self.player.pause()
            now.playing = False
        else:
            self.player.resume()
            now.playing = True
            self._last_progress_at = time.monotonic()
        self._render_player()
        self.hub.sync(self.now)

    def action_next_track(self) -> None:
        if self.queue.advance(manual=True) is not None:
            self._skips_in_a_row = 0
            self._start_current()
            self._queue_changed()
        else:
            self._queue_finished()

    def action_previous_track(self) -> None:
        if self.now is not None and self.now.position > 5:
            self._apply_seek(0.0)
            return
        if self.queue.back() is not None:
            self._skips_in_a_row = 0
            self._start_current()
            self._queue_changed()

    def _queue_finished(self) -> None:
        source = self._view_cache.get(self.queue.source_key)
        if source and source[1].next_href:
            self._continue_from_source(source[1])
            return
        current = self.queue.current
        if self.store.settings.autoplay_related and current is not None:
            self._continue_with_related(current)
            return
        self.player.stop()
        if self.now:
            self.now.playing = False
            self.now.buffering = False
            self.now.position = 0.0
        self._render_player()
        self.notify("You've reached the end of the queue.", markup=False)

    def _continue_from_source(self, page: Page) -> None:
        def work() -> None:
            try:
                more = self.client.next_page(page)
            except (SoundCloudError, SignInRequired):
                more = Page()
            self._call_ui(self._source_extended, page, more)

        self.run_worker(work, thread=True, exit_on_error=False, group="more")

    def _source_extended(self, page: Page, more: Page) -> None:
        page.items.extend(more.items)
        page.next_href = more.next_href
        tracks = [item for item in more.items if item.is_track]
        if tracks:
            self.queue.extend(tracks)
            self.action_next_track()
        else:
            page.next_href = ""
            self._queue_finished()
        if self.view is not None and self.page is page:
            self.items = page.items
            self._rebuild_table()

    def _continue_with_related(self, current: SoundCloudItem) -> None:
        def work() -> None:
            try:
                related = self.client.related(current.item_id, limit=25)
            except (SoundCloudError, SignInRequired):
                related = Page()
            self._call_ui(self._related_ready, related)

        self.run_worker(work, thread=True, exit_on_error=False, group="more")

    def _related_ready(self, related: Page) -> None:
        tracks = [item for item in related.items if item.is_track]
        if not tracks:
            self.player.stop()
            if self.now:
                self.now.playing = False
                self.now.buffering = False
            self._render_player()
            return
        self.queue.extend(tracks)
        self.action_next_track()

    def action_seek(self, delta: float) -> None:
        now = self.now
        if now is None or self._stream_loading:
            return
        base = self._pending_seek if self._pending_seek is not None else now.position
        self._queue_seek(base + delta)

    @on(WaveformBar.Seek)
    def waveform_clicked(self, event: WaveformBar.Seek) -> None:
        if self.now is not None and self.now.duration and not self._stream_loading:
            self._queue_seek(event.ratio * self.now.duration)

    def _queue_seek(self, target: float) -> None:
        now = self.now
        if now is None:
            return
        limit = max(0.0, now.duration - 1.0) if now.duration else target
        self._pending_seek = max(0.0, min(target, limit))
        self._render_player()
        if self._seek_timer is not None:
            self._seek_timer.stop()
        self._seek_timer = self.set_timer(SEEK_DEBOUNCE_SECONDS, self._flush_seek)

    def _flush_seek(self) -> None:
        if self._pending_seek is not None:
            self._apply_seek(self._pending_seek)

    def _apply_seek(self, target: float) -> None:
        self.player.seek(target)
        self._pending_seek = None
        self._last_position = target
        self._last_progress_at = time.monotonic()
        if self.now:
            self.now.position = target
        self._render_player()

    def action_volume(self, delta: int) -> None:
        settings = self.store.settings
        settings.volume = max(0, min(100, settings.volume + delta))
        self._render_player()
        if self._volume_timer is not None:
            self._volume_timer.stop()
        self._volume_timer = self.set_timer(VOLUME_DEBOUNCE_SECONDS, self._flush_volume)

    def action_volume_down(self) -> None:
        self.action_volume(-5)

    def action_volume_up(self) -> None:
        self.action_volume(5)

    def _flush_volume(self) -> None:
        self.player.set_volume(self.store.settings.volume)
        self.store.save()

    def action_toggle_repeat_one(self) -> None:
        self.store.update(repeat=self.queue.cycle_repeat())
        self._render_player()

    def action_toggle_shuffle(self) -> None:
        self.store.update(shuffle=self.queue.toggle_shuffle())
        self._render_player()
        self._queue_changed()

    def _tick(self) -> None:
        try:
            self._update_playback()
        except NoMatches:
            pass  # Screens close before timers stop when the app shuts down.

    def _update_playback(self) -> None:
        now = self.now
        if now is None:
            return
        monotonic = time.monotonic()
        if not self._stream_loading:
            now.buffering = self.player.buffering
            if self._pending_seek is None:
                now.position = self.player.position
            if abs(now.position - self._last_position) > 0.01:
                self._last_position = now.position
                self._last_progress_at = monotonic
            if self._stream_recoveries and now.position - self._recovered_at > RECOVERY_RESET_SECONDS:
                self._stream_recoveries = 0
            if self.player.finished and now.playing:
                self._track_ended()
                return
            if now.playing and monotonic - self._last_progress_at > STALL_SECONDS:
                self._recover_stream("The stream stalled.")
                return
        self._render_player_progress()
        if monotonic - self._last_discord_sync >= 1:
            self._last_discord_sync = monotonic
            self.hub.sync(now)

    def _track_ended(self) -> None:
        now = self.now
        if now is None:
            return
        error = self.player.error
        ended_early = now.duration > 0 and now.position < now.duration - 5
        if (ended_early or error) and self._stream_recoveries < MAX_STREAM_RECOVERIES:
            self._recover_stream(error)
            return
        if ended_early or error:
            self.notify(f"Couldn't finish {now.item.title}. {error}".strip(), severity="warning", markup=False)
            self._skip_after_failure()
            return
        self._skips_in_a_row = 0
        if self.queue.advance() is not None:
            self._start_current()
            self._queue_changed()
        else:
            self._queue_finished()

    def _recover_stream(self, reason: str) -> None:
        """Signed stream links expire and connections drop. Get a new link and continue."""
        now = self.now
        if now is None or self.queue.current is None:
            return
        if self._stream_recoveries >= MAX_STREAM_RECOVERIES:
            self.notify(reason or "The stream stopped.", severity="warning", markup=False)
            self._skip_after_failure()
            return
        self._stream_recoveries += 1
        self._recovered_at = now.position
        self._start_current(start=now.position)

    # Rendering

    def _render_player(self) -> None:
        bar = self.query_one(PlayerBar)
        settings = self.store.settings
        now = self.now
        liked = now is not None and self.engagement.is_liked(now.item)
        bar.update_controls(liked, self.queue.repeat, self.queue.shuffle, settings.volume)
        if now is None:
            bar.clear_meta("Choose a track to start listening")
            bar.clear_time()
            bar.query_one(WaveformBar).set_progress(0, active=False)
            return
        item = now.item
        normalized = normalize_track_info(item.title, item.author, settings.track_parser_enabled)
        if now.buffering and now.playing:
            status = "…"
        elif now.playing:
            status = "▶"
        else:
            status = "⏸"
        bar.update_meta(status, normalized.track, normalized.artist, "30-second preview" if item.is_preview else "")
        self._render_player_progress()

    def _render_player_progress(self) -> None:
        now = self.now
        if now is None:
            return
        bar = self.query_one(PlayerBar)
        position = self._pending_seek if self._pending_seek is not None else now.position
        bar.update_time(position, now.duration)
        progress = position / now.duration if now.duration else 0.0
        bar.query_one(WaveformBar).set_progress(progress)
        status_key = (now.playing, now.buffering)
        if status_key != getattr(self, "_status_key", None):
            self._status_key = status_key
            self._render_player()

    # Account actions

    def _require_sign_in(self, what: str) -> bool:
        if self.auth.signed_in:
            return True
        self.notify(f"Sign in to {what}. Press L.", severity="warning", markup=False)
        return False

    def _background(
        self,
        work: Callable[[], object],
        on_success: Callable[[object], None],
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        def run() -> None:
            try:
                result = work()
            except (SoundCloudError, SignInRequired) as exc:
                message = str(exc)
                if isinstance(exc, SignInRequired):
                    self._call_ui(self._render_account)
                if on_failure is not None:
                    self._call_ui(on_failure, message)
                else:
                    self._call_ui(lambda: self.notify(message, severity="error", markup=False))
                return
            self._call_ui(on_success, result)

        self.run_worker(run, thread=True, exit_on_error=False, group="account")

    def _invalidate(self, *keys: str) -> None:
        for key in keys:
            self._view_cache.pop(key, None)

    def action_toggle_like(self) -> None:
        item = self._selected_item() or (self.now.item if self.now else None)
        if item is not None:
            self._toggle_like_item(item)

    def action_toggle_like_current(self) -> None:
        if self.now is not None:
            self._toggle_like_item(self.now.item)

    def _toggle_like_item(self, item: SoundCloudItem) -> None:
        if not self._require_sign_in("follow people" if item.is_user else "like tracks and playlists"):
            return
        target = not self.engagement.is_liked(item)
        self.engagement.set_liked(item, target)
        self._refresh_marks()
        self._render_player()
        nav = self.query_one(NavList)
        nav.set_count("likes", len(self.engagement.liked_tracks) or None)

        def work() -> None:
            if item.is_user:
                self.client.set_following(item, target)
            else:
                self.client.set_liked(item, target)

        def done(_: object) -> None:
            self._invalidate("following" if item.is_user else ("likes" if item.is_track else "playlists"))
            verb = ("Followed" if target else "Unfollowed") if item.is_user else ("Liked" if target else "Unliked")
            self.notify(f"{verb} {item.title}", markup=False)

        def failed(message: str) -> None:
            self.engagement.set_liked(item, not target)
            self._refresh_marks()
            self._render_player()
            nav.set_count("likes", len(self.engagement.liked_tracks) or None)
            self.notify(message, severity="error", markup=False)

        self._background(work, done, failed)

    def action_toggle_repost(self) -> None:
        item = self._selected_item() or (self.now.item if self.now else None)
        if item is None or item.is_user or item.kind == "system-playlist":
            return
        if not self._require_sign_in("repost"):
            return
        target = not self.engagement.is_reposted(item)
        self.engagement.set_reposted(item, target)

        def failed(message: str) -> None:
            self.engagement.set_reposted(item, not target)
            self.notify(message, severity="error", markup=False)

        self._background(
            lambda: self.client.set_reposted(item, target),
            lambda _: self.notify(f"{'Reposted' if target else 'Removed repost of'} {item.title}", markup=False),
            failed,
        )

    def action_enqueue(self) -> None:
        item = self._selected_item()
        if item is None or not item.is_track:
            self.notify("Select a track to add it to Up next.", markup=False)
            return
        was_idle = self.queue.current is None
        self.queue.add_next(item)
        if was_idle:
            self._start_current()
        self._queue_changed()
        self.notify(f"Added {item.title} to Up next", markup=False)

    def action_add_to_playlist(self) -> None:
        track = self._target_track()
        if track is None:
            self.notify("Select a track to add it to a playlist.", markup=False)
            return
        if not self._require_sign_in("add tracks to playlists"):
            return
        self.push_screen(
            PlaylistPickerScreen(self.client, track, self._my_playlists),
            lambda result: self._playlist_chosen(track, result),
        )

    def _playlist_chosen(self, track: SoundCloudItem, result: SoundCloudItem | str | None) -> None:
        if result is None:
            return
        if result == "new":
            self.push_screen(NewPlaylistScreen(), lambda answer: self._create_playlist(track, answer))
            return
        playlist = result
        assert isinstance(playlist, SoundCloudItem)

        def failed(message: str) -> None:
            self.notify(message, severity="warning", markup=False)

        def done(_: object) -> None:
            playlist.track_count += 1
            self._invalidate(f"collection:{playlist.key}", "playlists")
            self.notify(f"Added to {playlist.title}", markup=False)

        def work() -> None:
            self.client.add_to_playlist(playlist.item_id, track.item_id)

        self._background(work, done, failed)

    def _create_playlist(self, track: SoundCloudItem, answer: tuple[str, bool] | None) -> None:
        if answer is None:
            return
        title, private = answer

        def done(created: object) -> None:
            assert isinstance(created, SoundCloudItem)
            self._my_playlists = None
            self._invalidate("playlists")
            self.notify(f"Created {created.title or title}", markup=False)

        self._background(lambda: self.client.create_playlist(title, [track.item_id], private), done)

    def _is_mine(self, item: SoundCloudItem) -> bool:
        session = self.auth.session
        return item.owned or bool(session and item.user_id and item.user_id == session.user_id)

    def action_remove(self) -> None:
        item = self._selected_item()
        view = self.view
        if item is None or view is None:
            return
        if view.key == "queue":
            table = self.query_one(TrackTable)
            self.queue.remove_upcoming(table.cursor_row)
            self._queue_changed()
            return
        collection = view.collection
        if collection is not None and item.is_track and self._is_mine(collection) and collection.kind != "system-playlist":
            self.push_screen(
                ConfirmScreen(f"Remove “{item.title}” from “{collection.title}”?", "Remove track"),
                lambda ok: self._remove_from_playlist(collection, item) if ok else None,
            )
            return
        if item.is_collection and self._is_mine(item) and item.kind != "system-playlist":
            self.push_screen(
                ConfirmScreen(f"Delete “{item.title}”? This can't be undone.", "Delete playlist"),
                lambda ok: self._delete_playlist(item) if ok else None,
            )
            return
        self.notify("Only Up next and your own playlists can be changed.", markup=False)

    def _remove_from_playlist(self, collection: SoundCloudItem, item: SoundCloudItem) -> None:
        def done(_: object) -> None:
            self._my_playlists = None
            collection.track_count = max(0, collection.track_count - 1)
            self._invalidate(f"collection:{collection.key}", "playlists")
            self.notify(f"Removed {item.title}", markup=False)
            if self.view is not None and self.view.key == f"collection:{collection.key}":
                self.open_view(self.view, push=False, force=True)

        self._background(lambda: self.client.remove_from_playlist(collection.item_id, item.item_id), done)

    def _delete_playlist(self, item: SoundCloudItem) -> None:
        def done(_: object) -> None:
            self._my_playlists = None
            self._invalidate("playlists", f"collection:{item.key}")
            self.notify(f"Deleted {item.title}", markup=False)
            if self.view is not None and self.view.key == "playlists":
                self.open_view(self.view, push=False, force=True)

        self._background(lambda: self.client.delete_playlist(item.item_id), done)

    def action_sign_in(self) -> None:
        self.push_screen(SignInScreen(self.client), self._sign_in_finished)

    def _sign_in_finished(self, session: Session | None) -> None:
        self.store.save()
        self._render_account()
        self._my_playlists = None
        for key in ("likes", "playlists", "albums", "tracks", "following", "feed", "history", "discover"):
            self._invalidate(key)
        if session is None:
            self.engagement = Engagement()
            self.query_one(NavList).set_count("likes", None)
            self._refresh_marks()
            self._render_player()
            return
        self.notify(f"Signed in as {session.username or 'your account'}", markup=False)
        self.run_worker(self._load_engagement, thread=True, exit_on_error=False, group="engagement")
        if self.view is not None and self.view.nav in ("likes", "playlists", "albums", "tracks", "following", "feed", "history"):
            self.action_refresh_view()

    # Everything else

    def action_info(self) -> None:
        item = self._selected_item() or (self.now.item if self.now else None)
        if item is not None:
            reposted = not item.is_user and self.engagement.is_reposted(item)
            self.push_screen(InfoScreen(item, self.engagement.is_liked(item), reposted))

    def action_open_in_browser(self) -> None:
        item = self._selected_item() or (self.now.item if self.now else None)
        if item is not None:
            self.hub.open_in_browser(item.url)

    def action_open_artist(self) -> None:
        item = self._selected_item() or (self.now.item if self.now else None)
        if item is not None:
            self.hub.open_in_browser(item.user_url)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.store, self.hub, self.player.name), self._settings_closed)

    def _settings_closed(self, _: None) -> None:
        self._rebuild_table()
        self._render_player()
        self._invalidate("likes", "playlists", "albums", "tracks", "following")

    def on_unmount(self) -> None:
        self.store.save()
        self.player.close()
        self.hub.close()


def _looks_like_link(query: str) -> bool:
    return query.startswith(("http://", "https://")) or "soundcloud.com/" in query


def _markup_safe(value: str) -> str:
    return value.replace("[", "\\[")


def _remember(cache: OrderedDict, key, value, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def main() -> None:
    app = SoundCloudRpcTui()

    def shut_down(_signal: int, _frame: object) -> None:
        # Stop audio first. Otherwise closing the terminal would leave it playing.
        app.player.close()
        app.exit()

    signal.signal(signal.SIGTERM, shut_down)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, shut_down)

    app.run()
    app.player.close()
    sys.stdout.flush()
    # Workers may be waiting on the network. The terminal is restored, so don't wait for them.
    os._exit(app.return_code or 0)


if __name__ == "__main__":
    main()
