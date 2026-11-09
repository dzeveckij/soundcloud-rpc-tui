from __future__ import annotations

import os
import webbrowser
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static, Switch
from textual.widgets.option_list import Option

from ..auth import Session, SignInError, detect_browsers
from ..core import SettingsStore, SoundCloudItem, compact_count, seconds_to_time
from ..integrations import IntegrationHub
from ..soundcloud import SoundCloudClient, SoundCloudError
from .widgets import THEME_CHOICES

class ConfirmScreen(ModalScreen[bool]):
    AUTO_FOCUS = "*"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm { width: 60; max-width: 95%; height: auto; padding: 1 2; background: $surface; border: tall $panel; }
    #confirm-body { margin-bottom: 1; }
    #confirm-buttons { height: auto; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 1; }
    """

    def __init__(self, message: str, confirm_label: str) -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(Text(self.message), id="confirm-body", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="ok", variant="error")

    @on(Button.Pressed, "#ok")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class SignInScreen(ModalScreen[Session | None]):
    AUTO_FOCUS = "*"
    BINDINGS = [Binding("escape", "cancel", "Close")]
    CSS = """
    SignInScreen { align: center middle; }
    #signin { width: 74; max-width: 96%; height: auto; max-height: 96%; padding: 1 2; background: $surface; border: tall $panel; }
    #signin-title { text-style: bold; margin-bottom: 1; }
    .hint { color: $text-muted; margin-bottom: 1; }
    .step { margin-bottom: 1; height: auto; }
    #signin Select { width: 1fr; }
    #import-row { height: auto; margin-bottom: 1; }
    #import-row Button { margin-left: 1; }
    #signin Input { margin-bottom: 1; }
    #signin-status { height: auto; margin-bottom: 1; }
    #signin-status.-error { color: $error; }
    #signin-actions { height: auto; align-horizontal: right; }
    #signin-actions Button { margin-left: 1; }
    """

    def __init__(self, client: SoundCloudClient) -> None:
        super().__init__()
        self.client = client
        self.browsers = detect_browsers()

    def compose(self) -> ComposeResult:
        session = self.client.auth.session
        with VerticalScroll(id="signin"):
            yield Static("Sign in to SoundCloud", id="signin-title")
            if session is not None and self.client.auth.signed_in:
                yield Static(Text(f"Signed in as {session.username or 'your account'}."), classes="step", markup=False)
            yield Static(
                "This app uses the SoundCloud session in your browser. Your password never reaches it.",
                classes="hint",
            )
            yield Static("1. Sign in on soundcloud.com, if you haven't already.", classes="step")
            yield Button("Open sign-in page", id="open-signin", classes="step")
            yield Static("2. Choose that browser and import the session.", classes="step")
            with Horizontal(id="import-row"):
                options = self.browsers or [("No browsers found", "")]
                yield Select(options, value=options[0][1], allow_blank=False, id="browser")
                yield Button("Import session", id="import", variant="primary", disabled=not self.browsers)
            yield Static(
                "Sessions renew about every hour. The app reads your browser again first. If that session "
                "has also expired, the app renews it, and your browser may then ask you to sign in again.\n"
                "macOS may ask for Keychain access. Safari needs Full Disk Access for your terminal.",
                classes="hint",
            )
            yield Label("Or paste the oauth_token cookie, or a path to a cookies.txt file")
            yield Input(placeholder="Token or cookies.txt path", password=True, id="token")
            yield Input(placeholder="oauth_refresh_token cookie (optional, keeps you signed in)", password=True, id="refresh")
            yield Static("", id="signin-status", markup=False)
            with Horizontal(id="signin-actions"):
                if session is not None:
                    yield Button("Sign out", id="sign-out", variant="error")
                yield Button("Close", id="close")
                yield Button("Use token", id="use-token")

    @on(Button.Pressed, "#open-signin")
    def open_signin(self) -> None:
        webbrowser.open("https://soundcloud.com/signin")
        self._status("Sign in using the page that opened, then come back and import the session.")

    def _status(self, message: str, error: bool = False) -> None:
        status = self.query_one("#signin-status", Static)
        status.set_class(error, "-error")
        status.update(Text(message))

    def _attempt(self, sign_in) -> None:
        self._status("Checking your session…")

        def work() -> None:
            try:
                session = sign_in()
            except (SignInError, SoundCloudError) as exc:
                self.call_later(self._status, str(exc), True)
                return
            self.call_later(self._finish, session)

        self.run_worker(work, thread=True, exit_on_error=False, group="signin")

    def _finish(self, session: Session) -> None:
        self.dismiss(session)

    @on(Button.Pressed, "#import")
    def import_session(self) -> None:
        source = str(self.query_one("#browser", Select).value)
        self._attempt(lambda: self.client.sign_in_with_browser(source))

    @on(Button.Pressed, "#use-token")
    def use_token(self) -> None:
        value = self.query_one("#token", Input).value.strip()
        refresh = self.query_one("#refresh", Input).value.strip()
        if not value:
            self._status("Enter a token or a cookies.txt path first.", True)
            return
        if os.path.exists(os.path.expanduser(value)):
            self._attempt(lambda: self.client.sign_in_with_cookie_file(value))
        else:
            self._attempt(lambda: self.client.sign_in_with_token(value, refresh))

    @on(Button.Pressed, "#sign-out")
    def sign_out(self) -> None:
        self.client.auth.sign_out()
        self.client.settings.soundcloud_cookies_from_browser = ""
        self.client.settings.soundcloud_cookie_file = ""
        self.dismiss(None)

    @on(Button.Pressed, "#close")
    def action_cancel(self) -> None:
        self.dismiss(self.client.auth.session if self.client.auth.signed_in else None)


class NewPlaylistScreen(ModalScreen[tuple[str, bool] | None]):
    AUTO_FOCUS = "*"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    NewPlaylistScreen { align: center middle; }
    #new-playlist { width: 60; max-width: 95%; height: auto; padding: 1 2; background: $surface; border: tall $panel; }
    #new-playlist Input { margin-bottom: 1; }
    #private-row { height: auto; margin-bottom: 1; }
    #private-row Label { padding: 1 1 0 1; }
    #new-buttons { height: auto; align-horizontal: right; }
    #new-buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="new-playlist"):
            yield Label("Name your playlist")
            yield Input(placeholder="Playlist name", id="name")
            with Horizontal(id="private-row"):
                yield Switch(value=True, id="private")
                yield Label("Private")
            with Horizontal(id="new-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Create playlist", id="create", variant="primary")

    @on(Input.Submitted)
    @on(Button.Pressed, "#create")
    def create(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if name:
            self.dismiss((name, self.query_one("#private", Switch).value))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PlaylistPickerScreen(ModalScreen[SoundCloudItem | str | None]):
    """Choose a playlist for a track. Dismisses with a playlist, "new", or None."""

    AUTO_FOCUS = "*"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    PlaylistPickerScreen { align: center middle; }
    #picker { width: 70; max-width: 95%; height: 70%; padding: 1 2; background: $surface; border: tall $panel; }
    #picker-title { margin-bottom: 1; height: auto; }
    #picker Input { margin-bottom: 1; }
    #picker OptionList { height: 1fr; border: none; background: $surface; }
    """

    def __init__(self, client: SoundCloudClient, track: SoundCloudItem, playlists: list[SoundCloudItem] | None) -> None:
        super().__init__()
        self.client = client
        self.track = track
        self.playlists = playlists

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(Text(f"Add “{self.track.title}” to a playlist", no_wrap=True, overflow="ellipsis"), id="picker-title", markup=False)
            yield Input(placeholder="Filter your playlists", id="filter")
            yield OptionList(id="picker-list")

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()
        if self.playlists is None:
            self._render_options("", loading=True)
            self.run_worker(self._load, thread=True, exit_on_error=False, group="picker")
        else:
            self._render_options("")

    def _load(self) -> None:
        try:
            playlists = self.client.my_playlists()
        except (SoundCloudError, RuntimeError) as exc:
            message = str(exc)
            self.call_later(self._load_failed, message)
            return
        self.call_later(self._loaded, playlists)

    def _load_failed(self, message: str) -> None:
        self.app.notify(message, severity="error", markup=False)
        self.dismiss(None)

    def _loaded(self, playlists: list[SoundCloudItem]) -> None:
        self.playlists = playlists
        self._render_options(self.query_one("#filter", Input).value)

    def _render_options(self, query: str, loading: bool = False) -> None:
        options_list = self.query_one("#picker-list", OptionList)
        options_list.clear_options()
        options_list.add_option(Option(Text("+ New playlist"), id="new"))
        if loading:
            options_list.add_option(Option(Text("Loading your playlists…", style="dim"), disabled=True))
            return
        needle = query.strip().lower()
        matches = [item for item in self.playlists or [] if needle in item.title.lower()]
        for item in matches:
            label = Text(no_wrap=True, overflow="ellipsis")
            label.append(item.title)
            label.append(f"  {item.track_count} tracks", "dim")
            if item.sharing == "private":
                label.append("  private", "dim")
            options_list.add_option(Option(label, id=f"p{item.item_id}"))
        if self.playlists is not None and not matches and needle:
            options_list.add_option(Option(Text("No playlists match that filter.", style="dim"), disabled=True))
        options_list.highlighted = 1 if matches else 0

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        if self.playlists is not None:
            self._render_options(event.value)

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        options_list = self.query_one("#picker-list", OptionList)
        if options_list.highlighted is not None:
            options_list.action_select()

    def key_down(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_down()

    def key_up(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_up()

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id or ""
        if option_id == "new":
            self.dismiss("new")
            return
        for item in self.playlists or []:
            if f"p{item.item_id}" == option_id:
                self.dismiss(item)
                return

    def action_cancel(self) -> None:
        self.dismiss(None)


class InfoScreen(ModalScreen[None]):
    AUTO_FOCUS = "*"
    BINDINGS = [Binding("escape,i,q", "close", "Close")]
    CSS = """
    InfoScreen { align: center middle; }
    #info { width: 72; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: tall $panel; }
    """

    def __init__(self, item: SoundCloudItem, liked: bool, reposted: bool) -> None:
        super().__init__()
        self.item = item
        self.liked = liked
        self.reposted = reposted

    def compose(self) -> ComposeResult:
        item = self.item
        text = Text()
        text.append(f"{item.title}\n", "bold")
        text.append(f"{item.author}\n\n", "dim")
        rows: list[tuple[str, str]] = []
        if item.duration:
            rows.append(("Length", seconds_to_time(item.duration)))
        if item.is_track:
            rows.append(("Plays", compact_count(item.plays)))
        if item.track_count:
            rows.append(("Tracks", str(item.track_count)))
        if item.followers:
            rows.append(("Followers", compact_count(item.followers)))
        if item.likes:
            rows.append(("Likes", compact_count(item.likes)))
        if item.reposts:
            rows.append(("Reposts", compact_count(item.reposts)))
        if item.genre:
            rows.append(("Genre", item.genre))
        if item.created_at:
            rows.append(("Uploaded", item.created_at[:10]))
        if item.sharing:
            rows.append(("Visibility", item.sharing.capitalize()))
        if item.is_preview:
            rows.append(("Access", "30-second preview. The full track needs SoundCloud Go+."))
        if item.is_blocked:
            rows.append(("Access", "Not available in your region."))
        if self.liked:
            rows.append(("You", "Following" if item.is_user else "Liked"))
        if self.reposted:
            rows.append(("You", "Reposted"))
        rows.append(("Link", item.url))
        for label, value in rows:
            text.append(f"{label:<12}", "dim")
            text.append(f"{value}\n")
        with VerticalScroll(id="info"):
            yield Static(text, markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


HELP_SECTIONS = [
    (
        "Browse",
        [
            ("/", "Search, or paste a SoundCloud link"),
            ("enter", "Play a track, or open a playlist or person"),
            ("backspace", "Go back"),
            ("[ and ]", "Change the search filter"),
            ("r", "Refresh this view"),
            ("D c w", "Discover, Trending, Feed"),
            ("l p A", "Likes, Playlists, Albums"),
            ("t P h", "Your tracks, Following, History"),
            ("v g", "Up next, Related to this track"),
        ],
    ),
    (
        "Playback",
        [
            ("space", "Play or pause"),
            ("n b", "Next, Previous"),
            ("← →", "Back or forward 10 seconds"),
            ("- +", "Volume down or up"),
            ("y z", "Repeat mode, Shuffle"),
            ("e", "Add the selected track to Up next"),
        ],
    ),
    (
        "Your account",
        [
            ("f", "Like the selected row, or follow a person"),
            ("F", "Like the track that's playing"),
            ("a", "Add to a playlist"),
            ("R", "Repost or undo repost"),
            ("d", "Remove from Up next or your playlist, or delete your playlist"),
            ("L", "Sign in or sign out"),
        ],
    ),
    (
        "Other",
        [
            ("i", "Details about the selected row"),
            ("o u", "Open the selected row or its artist in your browser"),
            ("s", "Settings"),
            ("q", "Quit"),
        ],
    ),
]


class HelpScreen(ModalScreen[None]):
    AUTO_FOCUS = "*"
    BINDINGS = [Binding("escape,question_mark,q", "close", "Close")]
    CSS = """
    HelpScreen { align: center middle; }
    #help { width: 72; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: tall $panel; }
    """

    def compose(self) -> ComposeResult:
        text = Text()
        for index, (title, rows) in enumerate(HELP_SECTIONS):
            if index:
                text.append("\n")
            text.append(f"{title}\n", "bold")
            for keys, description in rows:
                text.append(f"  {keys:<12}", "dim")
                text.append(f"{description}\n")
        with VerticalScroll(id="help"):
            yield Static(text, markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen[None]):
    AUTO_FOCUS = "*"
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]
    CSS = """
    SettingsScreen { align: center middle; }
    #settings { width: 78; max-width: 96%; height: 90%; background: $surface; border: tall $panel; }
    #settings-body { height: 1fr; padding: 1 2; }
    .group { text-style: bold; margin-top: 1; margin-bottom: 1; }
    .group.-first { margin-top: 0; }
    .setting { height: auto; margin-bottom: 1; }
    .setting Label { padding: 1 1 0 1; width: 1fr; }
    #settings-body Input { margin-bottom: 1; }
    #settings-body Select { margin-bottom: 1; }
    #settings-buttons { height: auto; align-horizontal: right; padding: 0 2 1 2; }
    #settings-buttons Button { margin-left: 1; }
    .hint { color: $text-muted; margin-bottom: 1; }
    """

    SWITCHES = {
        "discord_rich_presence": "Show what I'm playing in Discord",
        "display_when_idling": "Show in Discord while paused",
        "display_buttons": "Add a Listen button in Discord",
        "report_play_history": "Add played tracks to my SoundCloud history",
        "autoplay_related": "Keep playing related tracks when the queue ends",
        "track_parser_enabled": "Read the artist from titles like Artist - Track",
        "open_browser_on_track": "Open each new track in my browser",
    }

    def __init__(self, store: SettingsStore, hub: IntegrationHub, player_name: str) -> None:
        super().__init__()
        self.store = store
        self.hub = hub
        self.player_name = player_name

    def _switch(self, key: str) -> ComposeResult:
        with Horizontal(classes="setting"):
            yield Switch(value=bool(getattr(self.store.settings, key)), id=key)
            yield Label(self.SWITCHES[key])

    def compose(self) -> ComposeResult:
        settings = self.store.settings
        with Vertical(id="settings"):
            with VerticalScroll(id="settings-body"):
                yield Static("Public profile", classes="group -first")
                yield Static(
                    "Used for Likes, Playlists, and Your tracks when you aren't signed in.",
                    classes="hint",
                )
                yield Input(settings.soundcloud_username, placeholder="Username or profile link", id="soundcloud_username")

                yield Static("Playback", classes="group")
                yield Label("Audio player")
                yield Select(
                    [("Automatic (mpv, then ffplay)", "auto"), ("mpv", "mpv"), ("ffplay", "ffplay")],
                    value=settings.player_backend,
                    allow_blank=False,
                    id="player_backend",
                )
                yield Static(Text(f"Using {self.player_name}."), classes="hint", markup=False)
                yield from self._switch("report_play_history")
                yield from self._switch("autoplay_related")
                yield from self._switch("open_browser_on_track")

                yield Static("Discord", classes="group")
                yield Static(Text(f"Status: {self.hub.discord.status}"), classes="hint", markup=False)
                yield from self._switch("discord_rich_presence")
                yield from self._switch("display_when_idling")
                yield from self._switch("display_buttons")
                yield Label("Discord status shows")
                yield Select(
                    [("App name", 0), ("Artist", 1), ("Track title", 2)],
                    value=settings.status_display_type if settings.status_display_type in (0, 1, 2) else 1,
                    allow_blank=False,
                    id="status_display_type",
                )

                yield Static("Appearance", classes="group")
                yield Label("Theme")
                choices = THEME_CHOICES
                yield Select(
                    choices,
                    value=settings.theme if settings.theme in {value for _, value in choices} else "soundcloud",
                    allow_blank=False,
                    id="theme",
                )
                yield from self._switch("track_parser_enabled")
            with Horizontal(id="settings-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save settings", id="save", variant="primary")

    @on(Select.Changed, "#theme")
    def preview_theme(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.app.theme = event.value

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.app.theme = self.store.settings.theme
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        values: dict[str, Any] = {key: self.query_one(f"#{key}", Switch).value for key in self.SWITCHES}
        values["soundcloud_username"] = self.query_one("#soundcloud_username", Input).value.strip()
        values["player_backend"] = str(self.query_one("#player_backend", Select).value)
        values["theme"] = str(self.query_one("#theme", Select).value)
        values["status_display_type"] = int(self.query_one("#status_display_type", Select).value)
        self.store.update(**values)
        self.dismiss(None)
