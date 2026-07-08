from __future__ import annotations

import asyncio
import time
import webbrowser
from typing import Any, Iterable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane

from core import (
    PlaybackState,
    SettingsStore,
    SoundCloudItem,
    TrackInfo,
    normalize_track_info,
    seconds_to_time,
    time_to_seconds,
)
from integrations import IntegrationHub
from soundcloud import SoundCloudClient, SoundCloudPlayer


class PromptScreen(ModalScreen[str | None]):
    CSS = """
    PromptScreen {
        align: center middle;
    }
    #dialog {
        width: 72;
        max-width: 95%;
        height: auto;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    #dialog Input {
        margin-bottom: 1;
    }
    #buttons {
        height: auto;
        align-horizontal: right;
    }
    """

    def __init__(self, title: str, placeholder: str, initial_value: str, button_text: str) -> None:
        super().__init__()
        self.title_text = title
        self.placeholder_text = placeholder
        self.initial_value = initial_value
        self.button_text = button_text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(self.initial_value, placeholder=self.placeholder_text, id="prompt-input")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.button_text, id="save", variant="primary")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value.strip() or None)


class SettingsScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-container {
        width: 84;
        max-width: 96%;
        height: 92%;
        max-height: 92%;
        border: solid $accent;
        background: $surface;
    }
    #settings-container Input, #settings-container Select {
        margin-bottom: 1;
    }
    #settings-container Button.toggle {
        width: 100%;
        margin-bottom: 1;
    }
    TabPane {
        padding: 1 2;
    }
    #buttons {
        height: auto;
        align-horizontal: right;
        padding: 0 2 1 2;
    }
    """

    TOGGLE_LABELS = {
        "discord": "Discord Rich Presence",
        "idle": "Show paused status",
        "buttons-enabled": "Listen button in Discord",
        "parser": "Track parser",
        "browser": "Open browser on play",
        "lastfm": "Last.fm",
    }

    def __init__(self, store: SettingsStore, hub: IntegrationHub) -> None:
        super().__init__()
        self.store = store
        self.hub = hub
        settings = store.settings
        self.boolean_settings = {
            "discord": settings.discord_rich_presence,
            "idle": settings.display_when_idling,
            "buttons-enabled": settings.display_buttons,
            "parser": settings.track_parser_enabled,
            "browser": settings.open_browser_on_track,
            "lastfm": settings.lastfm_enabled,
        }

    def _toggle_label(self, key: str, label: str) -> str:
        return f"{label}: {'ON' if self.boolean_settings[key] else 'OFF'}"

    def _toggle_button(self, key: str, label: str) -> Button:
        return Button(self._toggle_label(key, label), id=key, classes="toggle")

    def _input(self, label: str, value: str, placeholder: str, id: str, password: bool = False) -> Iterable[Any]:
        yield Label(label)
        yield Input(value, placeholder=placeholder, id=id, password=password)

    def compose(self) -> ComposeResult:
        settings = self.store.settings
        with Vertical(id="settings-container"):
            with TabbedContent():
                with TabPane("SoundCloud", id="tab-sc"):
                    with VerticalScroll():
                        yield from self._input("Username or profile URL", settings.soundcloud_username, "SoundCloud username or profile URL", "sc-user")
                        yield from self._input("cookies.txt path", settings.soundcloud_cookie_file, "cookies.txt path for login", "sc-cookie-file")
                        yield from self._input("Browser cookies", settings.soundcloud_cookies_from_browser, "Browser cookies, e.g. firefox, chrome, chromium", "sc-browser")
                        yield from self._input("Charts region", settings.soundcloud_region, "Charts region, e.g. us, de, gb", "sc-region")
                        yield from self._input("Default search", settings.default_search, "Default search", "default-search")
                        yield Button("Open Login in Browser", id="sc-login")

                with TabPane("Discord RPC", id="tab-discord"):
                    with VerticalScroll():
                        yield self._toggle_button("discord", self.TOGGLE_LABELS["discord"])
                        yield self._toggle_button("idle", self.TOGGLE_LABELS["idle"])
                        yield self._toggle_button("buttons-enabled", self.TOGGLE_LABELS["buttons-enabled"])

                with TabPane("Last.fm", id="tab-lastfm"):
                    with VerticalScroll():
                        yield self._toggle_button("lastfm", self.TOGGLE_LABELS["lastfm"])
                        yield from self._input("Last.fm API key", settings.lastfm_api_key, "Last.fm API key", "lastfm-key")
                        yield from self._input("Last.fm API secret", settings.lastfm_secret, "Last.fm API secret", "lastfm-secret", password=True)
                        yield from self._input("Last.fm session key", settings.lastfm_session_key, "Last.fm session key", "lastfm-session", password=True)
                        yield from self._input("Last.fm token to exchange", "", "Last.fm token to exchange", "lastfm-token")
                        yield Button("Get Auth URL", id="auth-url")

                with TabPane("Appearance", id="tab-appearance"):
                    with VerticalScroll():
                        yield Label("Theme")
                        yield Select(
                            options=[
                                ("Textual Dark", "textual-dark"),
                                ("Textual Light", "textual-light"),
                                ("Dracula", "dracula"),
                                ("Nord", "nord"),
                                ("Tokyo Night", "tokyo-night"),
                                ("Monokai", "monokai"),
                            ],
                            value=settings.theme,
                            id="theme-select",
                        )
                        yield self._toggle_button("parser", self.TOGGLE_LABELS["parser"])
                        yield self._toggle_button("browser", self.TOGGLE_LABELS["browser"])
            
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def _val(self, id: str) -> str:
        return self.query_one(f"#{id}", Input).value.strip()

    @on(Select.Changed, "#theme-select")
    def on_theme_changed(self, event: Select.Changed) -> None:
        if event.value:
            self.app.theme = str(event.value)

    @on(Button.Pressed, "#sc-login")
    def open_soundcloud_login(self) -> None:
        webbrowser.open("https://soundcloud.com/signin")
        self.notify("Opened SoundCloud login in your browser")

    @on(Button.Pressed, "#auth-url")
    def open_auth_url(self) -> None:
        key = self._val("lastfm-key")
        self.store.settings.lastfm_api_key = key
        url = self.hub.lastfm.auth_url()
        if url:
            webbrowser.open(url)
            self.notify("Opened Last.fm authorization URL")
        else:
            self.notify("Enter a Last.fm API key first", severity="warning")

    @on(Button.Pressed, ".toggle")
    def toggle_setting(self, event: Button.Pressed) -> None:
        key = event.button.id or ""
        if key not in self.boolean_settings:
            return
        self.boolean_settings[key] = not self.boolean_settings[key]
        event.button.label = self._toggle_label(key, self.TOGGLE_LABELS[key])

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.app.theme = self.store.settings.theme
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        self.store.update(
            soundcloud_username=self._val("sc-user"),
            soundcloud_cookie_file=self._val("sc-cookie-file"),
            soundcloud_cookies_from_browser=self._val("sc-browser"),
            soundcloud_region=self._val("sc-region") or "us",
            default_search=self._val("default-search"),
            discord_rich_presence=self.boolean_settings["discord"],
            display_when_idling=self.boolean_settings["idle"],
            display_buttons=self.boolean_settings["buttons-enabled"],
            track_parser_enabled=self.boolean_settings["parser"],
            open_browser_on_track=self.boolean_settings["browser"],
            lastfm_enabled=self.boolean_settings["lastfm"],
            lastfm_api_key=self._val("lastfm-key"),
            lastfm_secret=self._val("lastfm-secret"),
            lastfm_session_key=self._val("lastfm-session"),
            theme=str(self.query_one("#theme-select", Select).value),
        )

        token = self._val("lastfm-token")
        if token:
            try:
                self.store.settings.lastfm_session_key = self.hub.lastfm.exchange_token(token)
                self.store.save()
                self.notify("Last.fm session key saved")
            except Exception as exc:
                self.notify(f"Last.fm auth failed: {exc}", severity="error")
                return
        self.dismiss(None)


class SoundCloudRpcTui(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }
    #main {
        height: 1fr;
    }
    #left {
        width: 2fr;
        min-width: 56;
        padding: 1 2;
    }
    #right {
        width: 1fr;
        min-width: 34;
        padding: 1 2;
        border-left: solid $primary;
    }
    .panel-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #now {
        height: 8;
        border: solid $primary;
        padding: 1 2;
        margin-bottom: 1;
    }
    #library-title {
        height: 1;
        text-style: bold;
    }
    #table {
        height: 1fr;
    }
    #status {
        height: 10;
        border: solid $secondary;
        padding: 1 2;
    }
    #actions Button {
        width: 1fr;
    }
    #help {
        height: auto;
        color: $text-muted;
    }
    """

    TITLE = "SoundCloud TUI"
    BINDINGS = [
        Binding("/", "search", "Search"),
        Binding("c", "charts", "Charts"),
        Binding("l", "likes", "Likes"),
        Binding("p", "playlists", "Playlists"),
        Binding("t", "tracks", "Tracks"),
        Binding("g", "recommended", "Recommended"),
        Binding("enter", "play_selected", "Play"),
        Binding("space", "toggle_play", "Pause/Resume"),
        Binding("y", "toggle_repeat_one", "Repeat"),
        Binding("o", "open_track", "Browser"),
        Binding("s", "settings", "Settings"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.store = SettingsStore()
        self.client = SoundCloudClient(self.store.settings)
        self.hub = IntegrationHub(self.store.settings)
        self.player = SoundCloudPlayer(self.client)
        self.state: PlaybackState | None = None
        self.items: list[SoundCloudItem] = []
        self.current_item_index: int | None = None
        self.repeat_one = False
        self._play_request_id = 0
        self._items_render_key: tuple[tuple[str, str, str, str], ...] = ()
        self.view_title = "Charts"
        self.last_query = self.store.settings.default_search

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("", id="now")
                yield Static("", id="library-title")
                table = DataTable(id="table", cursor_type="row")
                table.add_columns("Title", "Artist", "Time", "Type")
                yield table
            with Vertical(id="right"):
                yield Static("SoundCloud", classes="panel-title")
                with Vertical(id="actions"):
                    yield Button("Charts", id="charts")
                    yield Button("Search", id="search")
                    yield Button("Recommended", id="recommended")
                    yield Button("Likes", id="likes")
                    yield Button("Playlists", id="playlists")
                    yield Button("Tracks", id="tracks")
                yield Static("", id="status")
                yield Static(
                    "/ search/url  enter play  space pause/resume  y repeat  s settings",
                    id="help",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.theme = self.store.settings.theme
        self.set_interval(1, self.tick)
        self.action_charts()
        self.refresh_display()

    @on(Button.Pressed, "#search")
    def action_search(self) -> None:
        self.push_screen(PromptScreen("Search / URL", "Artist, track, playlist, genre, or URL", self.last_query, "Search"), self._search_submitted)

    @on(Button.Pressed, "#charts")
    def action_charts(self) -> None:
        self._load_items("Charts", lambda: self.client.charts())

    @on(Button.Pressed, "#likes")
    def action_likes(self) -> None:
        self._load_items("Likes", lambda: self.client.likes())

    @on(Button.Pressed, "#playlists")
    def action_playlists(self) -> None:
        self._load_items("Playlists", lambda: self.client.playlists())

    @on(Button.Pressed, "#tracks")
    def action_tracks(self) -> None:
        self._load_items("Your Tracks", lambda: self.client.user_tracks())

    @on(Button.Pressed, "#recommended")
    def action_recommended(self) -> None:
        current_url = self.state.track.url if self.state else ""
        title = "Recommended" if current_url else "Recommended Charts"
        self._load_items(title, lambda: self.client.recommended_for(current_url))

    @on(DataTable.RowSelected)
    def action_play_selected(self) -> None:
        table = self.query_one("#table", DataTable)
        item = self._selected_item()
        if item is None:
            return
        if item.kind == "playlist":
            self._load_items(item.title, lambda: self.client.playlist_tracks(item.url))
            return
        self._play_track(item.to_track(), table.cursor_row)

    def action_toggle_play(self) -> None:
        if self.state is None:
            self.action_play_selected()
            return
        track = self.state.track
        now = time.time()
        if self.player.status.message == "loading":
            self.notify("Track is still loading", severity="warning")
            return
        if track.is_playing:
            if self.player.pause():
                track.is_playing = False
                self.state.paused_at = now
            else:
                track.is_playing = False
                self.state.paused_at = None
                self.notify("Playback is not running", severity="warning")
        elif self.state.paused_at is not None:
            if self.player.resume():
                self.state.paused_seconds += now - self.state.paused_at
                self.state.paused_at = None
                track.is_playing = True
            else:
                self.state.paused_at = None
                self.notify("Could not resume playback", severity="warning")
        elif track.url:
            self._play_track(track, self.current_item_index)
            return
        self.refresh_display()

    def action_toggle_repeat_one(self) -> None:
        self.repeat_one = not self.repeat_one
        self.notify(f"Repeat one {'enabled' if self.repeat_one else 'disabled'}")
        self.refresh_display()

    def action_open_track(self) -> None:
        if self.state:
            self.hub.open_track(self.state.track)

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.store, self.hub), lambda _: self.refresh_display())

    def action_refresh(self) -> None:
        actions = {
            "Charts": self.action_charts,
            "Likes": self.action_likes,
            "Playlists": self.action_playlists,
            "Your Tracks": self.action_tracks,
        }
        if self.view_title in actions:
            actions[self.view_title]()
        elif self.last_query:
            self._search_submitted(self.last_query)
        else:
            self.refresh_display()

    def _search_submitted(self, query: str | None) -> None:
        if not query:
            return
        if query.startswith(("http://", "https://")) or "soundcloud.com/" in query:
            self._url_submitted(query)
            return
        self.last_query = query
        self.store.settings.default_search = query
        self.store.save()
        self._load_items(f"Search: {query}", lambda: self.client.search(query))

    def _url_submitted(self, url: str | None) -> None:
        if not url:
            return
        if "/sets/" in url or "/likes" in url or "/tracks" in url:
            self._load_items(url, lambda: self.client.playlist_tracks(url))
            return
        try:
            self._play_track(self.client.resolve_track(url))
        except Exception as exc:
            self.notify(f"Could not load URL: {exc}", severity="error")

    def _load_items(self, title: str, loader) -> None:
        self.view_title = title
        try:
            self.items = loader()
            self.current_item_index = None
        except Exception as exc:
            self.items = []
            self.current_item_index = None
            self.notify(str(exc), severity="error")
        self.refresh_display()

    def _selected_item(self) -> SoundCloudItem | None:
        table = self.query_one("#table", DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self.items):
            return None
        return self.items[table.cursor_row]

    def _play_track(self, track: TrackInfo, item_index: int | None = None) -> None:
        if not track.url:
            self.notify("No SoundCloud URL for selected item", severity="warning")
            return
        self._play_request_id += 1
        request_id = self._play_request_id
        self.current_item_index = item_index
        self.player.stop()
        track.is_playing = False
        self.state = PlaybackState(track=track, started_at=time.time())
        self.player.status.message = "loading"
        self.refresh_display()
        asyncio.create_task(self._resolve_and_play(track, item_index, request_id))

    async def _resolve_and_play(self, track: TrackInfo, item_index: int | None, request_id: int) -> None:
        try:
            resolved = await asyncio.to_thread(self.client.resolve_track, track.url)
            next_track = TrackInfo(
                title=resolved.title or track.title,
                author=resolved.author or track.author,
                artwork=resolved.artwork or track.artwork,
                elapsed="0:00",
                duration=resolved.duration or track.duration,
                is_playing=True,
                url=resolved.url or track.url,
            )
            await asyncio.to_thread(self.player.play, next_track.url)
        except Exception as exc:
            if request_id == self._play_request_id:
                self.notify(f"Playback failed: {exc}", severity="warning")
                self.player.status.message = "error"
            return
        if request_id != self._play_request_id:
            return
        self.current_item_index = item_index
        self.state = PlaybackState(track=next_track, started_at=time.time())
        if self.store.settings.open_browser_on_track:
            self.hub.open_track(next_track)
        self.refresh_display()

    def _play_next_track(self) -> None:
        if self.repeat_one and self.state and self.state.track.url:
            self._play_track(self.state.track, self.current_item_index)
            return

        if self.current_item_index is None:
            self.state.track.is_playing = False
            self.player.stop()
            return

        for next_index in range(self.current_item_index + 1, len(self.items)):
            item = self.items[next_index]
            if item.kind != "track":
                continue
            try:
                table = self.query_one("#table", DataTable)
                table.move_cursor(row=next_index)
            except Exception:
                pass
            self._play_track(item.to_track(), next_index)
            return

        self.state.track.is_playing = False
        self.player.stop()
        self.notify("Queue finished")

    def tick(self) -> None:
        if self.state and self.state.track.is_playing:
            elapsed = int(time.time() - self.state.started_at - self.state.paused_seconds)
            duration = time_to_seconds(self.state.track.duration)
            if self.player.finished() or (duration > 0 and elapsed >= duration + 2):
                self._play_next_track()
                self.refresh_display()
                return
            self.state.track.elapsed = seconds_to_time(elapsed)
        self.refresh_display()

    def refresh_display(self) -> None:
        status = self.hub.update(self.state)
        now = self.query_one("#now", Static)
        table = self.query_one("#table", DataTable)
        title = self.query_one("#library-title", Static)
        status_panel = self.query_one("#status", Static)

        title.update(f"{self.view_title} ({len(self.items)})")
        render_key = tuple((item.title, item.author, item.duration, item.kind) for item in self.items)
        if render_key != self._items_render_key:
            cursor_row = table.cursor_row
            table.clear()
            for item in self.items:
                table.add_row(item.title, item.author, item.duration, item.kind)
            if 0 <= cursor_row < len(self.items):
                table.move_cursor(row=cursor_row)
            self._items_render_key = render_key

        if self.state is None:
            now.update("No track playing\n\nUse Charts, Search, Likes, Playlists, or URL.")
        else:
            track = self.state.track
            normalized = normalize_track_info(track.title, track.author, self.store.settings.track_parser_enabled)
            state_text = "Playing" if track.is_playing else "Paused"
            now.update(
                f"{state_text}\n"
                f"{normalized.artist}\n"
                f"{normalized.track}\n"
                f"{track.elapsed} / {track.duration or '0:00'}"
            )

        sc_status = self.client.status()
        status_panel.update(
            f"Login: {sc_status.message}\n"
            f"User: {self.store.settings.soundcloud_username or 'not set'}\n"
            f"Player: {self.player.status.message}\n"
            f"Repeat: {'one' if self.repeat_one else 'off'}\n"
            f"Discord: {status.discord}\n"
            f"Last.fm: {status.lastfm}\n"
            f"Settings: {self.store.path}"
        )

    def on_exit_app(self) -> None:
        self.player.stop()
        self.hub.close()


def main() -> None:
    SoundCloudRpcTui().run()


if __name__ == "__main__":
    main()
