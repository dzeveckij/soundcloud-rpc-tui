"""End-to-end UI tests. The real app, with a fake network and a fake audio player."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.css.query import NoMatches
from textual.widgets import Input

from soundcloud_rpc_tui.ui.app import SoundCloudRpcTui
from soundcloud_rpc_tui.auth import Session, SessionManager, SessionStore, SignInRequired
from soundcloud_rpc_tui.core import Engagement, Settings, SettingsStore, SoundCloudItem
from soundcloud_rpc_tui.integrations import DiscordPresence, IntegrationHub
from soundcloud_rpc_tui.core import NowPlaying
from soundcloud_rpc_tui.ui.screens import ConfirmScreen, HelpScreen, InfoScreen, NewPlaylistScreen, PlaylistPickerScreen, SettingsScreen, SignInScreen
from soundcloud_rpc_tui.soundcloud import AlreadyInPlaylist, Page, SoundCloudError
from soundcloud_rpc_tui.ui.widgets import PlayerBar, TrackTable, WaveformBar, bucket_samples, render_artwork, waveform_rows


def track(index: int, **extra) -> SoundCloudItem:
    return SoundCloudItem(kind="track", item_id=str(index), title=f"Song {index}", author="Artist", duration=200, url=f"https://soundcloud.com/a/{index}", user_id="9", **extra)


class FakePlayer:
    name = "fake"
    live_controls = True

    def __init__(self) -> None:
        self.position = 0.0
        self.paused = False
        self.buffering = False
        self.finished = False
        self.error = ""
        self.loaded: list[tuple[str, float]] = []
        self.seeks: list[float] = []
        self.volume = 80
        self.closed = False

    def load(self, url, start=0.0):
        self.loaded.append((url, start))
        self.position = start
        self.finished = False
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def seek(self, position):
        self.seeks.append(position)
        self.position = position

    def set_volume(self, volume):
        self.volume = volume

    def set_preference(self, preference):
        pass

    def stop(self):
        self.position = 0

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, auth: SessionManager) -> None:
        self.auth = auth
        self.settings = Settings()
        self.calls: list[tuple] = []
        self.pages: dict[str, Page] = {}
        self.fail_like = False
        self.playlists_list = [SoundCloudItem(kind="playlist", item_id="50", title="Road trip", track_count=2, user_id="1", owned=True)]
        self.engagement_state = Engagement(liked_tracks={"2"})
        self.search_gate: asyncio.Event | None = None

    def warm_up(self):
        pass

    def restore_session(self):
        return self.auth.session

    def engagement(self):
        return self.engagement_state

    def trending(self):
        return self.pages.get("trending", Page([track(index) for index in range(5)]))

    def discover(self):
        return Page([SoundCloudItem(kind="playlist", item_id="50", title="Mix", track_count=2)])

    def feed(self):
        if not self.auth.signed_in:
            raise SignInRequired("Sign in to use this.")
        return Page([track(100)])

    def likes(self):
        return Page([track(2)])

    def playlists(self):
        return Page(list(self.playlists_list))

    def albums(self):
        return Page()

    def user_tracks(self, user_id=None):
        return Page([track(300)])

    def followings(self):
        return Page([SoundCloudItem(kind="user", item_id="9", title="Artist", user_id="9", url="https://soundcloud.com/a")])

    def history(self):
        return Page()

    def related(self, track_id, limit=50):
        return Page([track(400)])

    def search(self, query, scope="all"):
        self.calls.append(("search", query, scope))
        if query == "boom":
            raise SoundCloudError("Can't reach SoundCloud. Check your connection, then try again.")
        return Page([track(index) for index in range(10, 13)], next_href="more")

    def next_page(self, page):
        self.calls.append(("next",))
        return Page([track(99)])

    def collection_tracks(self, item):
        self.calls.append(("open", item.item_id))
        return Page([track(201), track(202)])

    def resolve(self, url):
        self.calls.append(("resolve", url))
        return track(500)

    def stream_url(self, item):
        if item.is_blocked:
            raise SoundCloudError("This track isn't available in your region.")
        return f"https://stream/{item.item_id}"

    def waveform(self, item):
        return [10, 20, 30, 40]

    def artwork_bytes(self, url):
        return b""

    def record_play(self, item):
        self.calls.append(("history", item.item_id))

    def set_liked(self, item, liked):
        self.calls.append(("like", item.item_id, liked))
        if self.fail_like:
            raise SoundCloudError("SoundCloud refused that request.")

    def set_following(self, user, following):
        self.calls.append(("follow", user.item_id, following))

    def set_reposted(self, item, reposted):
        self.calls.append(("repost", item.item_id, reposted))

    def my_playlists(self):
        return list(self.playlists_list)

    def add_to_playlist(self, playlist_id, track_id):
        self.calls.append(("add", playlist_id, track_id))
        if track_id == "1":
            raise AlreadyInPlaylist("That track is already in this playlist.")

    def create_playlist(self, title, tracks, private=True):
        self.calls.append(("create", title, list(tracks), private))
        return SoundCloudItem(kind="playlist", item_id="60", title=title, owned=True)

    def remove_from_playlist(self, playlist_id, track_id):
        self.calls.append(("remove", playlist_id, track_id))

    def delete_playlist(self, playlist_id):
        self.calls.append(("delete", playlist_id))
        self.playlists_list = []

    def sign_in_with_token(self, token, refresh=""):
        self.calls.append(("signin", token))
        session = Session(token, refresh, username="tester", user_id="1")
        self.auth.adopt(session)
        return session


def build_app(tmp_path: Path, signed_in: bool = True):
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.discord_rich_presence = False
    session_store = SessionStore(tmp_path / "auth.json")
    if signed_in:
        session_store.save(Session("tok", username="tester", user_id="1"))
    auth = SessionManager(session_store)
    client = FakeClient(auth)
    player = FakePlayer()
    return SoundCloudRpcTui(store=store, auth=auth, client=client, player=player, hub=IntegrationHub(store.settings)), client, player


async def settle(pilot, condition, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.02)):
        await pilot.pause(0.02)
        if condition():
            return
    raise AssertionError("condition was not met in time")


def run_app(tmp_path, scenario, signed_in=True, size=(120, 40)):
    app, client, player = build_app(tmp_path, signed_in)

    async def runner():
        async with app.run_test(size=size) as pilot:
            await settle(pilot, lambda: app.items and not app.view_loading)
            await pilot.pause(0.05)
            await scenario(app, client, player, pilot)

    asyncio.run(runner())
    return app, client, player


def test_opens_on_trending_with_keyboard_focus_on_the_list(tmp_path):
    async def scenario(app, client, player, pilot):
        assert app.view.key == "trending" and app.query_one(TrackTable).row_count == 5
        assert isinstance(app.focused, TrackTable)

    run_app(tmp_path, scenario)


def test_titles_with_markup_characters_render_literally(tmp_path):
    async def scenario(app, client, player, pilot):
        table = app.query_one(TrackTable)
        assert "[FREE DL] Song [/bold]" in table.get_row_at(0)[2].plain
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        await pilot.press("i")
        await pilot.pause(0.1)
        assert isinstance(app.screen, InfoScreen)

    app, client, player = build_app(tmp_path)
    client.pages["trending"] = Page([SoundCloudItem(kind="track", item_id="1", title="[FREE DL] Song [/bold]", author="[x]", duration=10, url="https://x")])

    async def runner():
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, lambda: app.items)
            await scenario(app, client, player, pilot)

    asyncio.run(runner())


def test_typing_searches_once_after_a_pause_and_letter_keys_do_not_trigger_commands(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("slash")
        assert isinstance(app.focused, Input)
        await pilot.press(*"quit space")  # q would quit and space would pause if they leaked.
        await settle(pilot, lambda: app.view.key.startswith("search:"))
        searches = [call for call in client.calls if call[0] == "search"]
        assert searches == [("search", "quit space", "all")]
        assert app.is_running
        await pilot.press("escape")
        assert isinstance(app.focused, TrackTable)

    run_app(tmp_path, scenario)


def test_repeat_search_is_served_from_cache(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("slash", *"abc", "enter")
        await settle(pilot, lambda: not app.view_loading and app.view.key == "search:all:abc")
        await pilot.press("c")
        await settle(pilot, lambda: app.view.key == "trending")
        count = sum(1 for call in client.calls if call[0] == "search")
        await pilot.press("backspace")
        await settle(pilot, lambda: app.view.key == "search:all:abc" and app.items)
        assert sum(1 for call in client.calls if call[0] == "search") == count
        await pilot.press("r")
        await settle(pilot, lambda: sum(1 for call in client.calls if call[0] == "search") == count + 1)

    run_app(tmp_path, scenario)


def test_search_filter_changes_scope_and_search_error_is_shown_not_raised(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("slash", *"abc", "enter")
        await settle(pilot, lambda: app.view.key == "search:all:abc" and not app.view_loading)
        await pilot.press("right_square_bracket")
        await settle(pilot, lambda: ("search", "abc", "tracks") in client.calls)
        app.query_one("#search", Input).value = ""
        await pilot.press("slash", *"boom", "enter")
        await settle(pilot, lambda: app.view_error != "")
        assert "Press r to try again" in app.view_error
        assert app.query_one("#empty").has_class("-visible")

    run_app(tmp_path, scenario)


def test_pasted_link_plays_without_search(tmp_path):
    async def scenario(app, client, player, pilot):
        input_box = app.query_one("#search", Input)
        input_box.focus()
        input_box.value = "https://soundcloud.com/a/500"
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        assert ("resolve", "https://soundcloud.com/a/500") in client.calls
        assert not [call for call in client.calls if call[0] == "search"]
        assert player.loaded[-1][0] == "https://stream/500"

    run_app(tmp_path, scenario)


def test_playing_reports_history_shows_waveform_and_marks_row(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded and ("history", "0") in client.calls)
        await settle(pilot, lambda: app.query_one(WaveformBar).samples)
        assert player.loaded == [("https://stream/0", 0.0)]
        assert app.query_one(TrackTable).get_row_at(0)[0].plain == "▶"
        assert app.queue.upcoming[0].item_id == "1"

    run_app(tmp_path, scenario)


def test_play_history_setting_is_respected(tmp_path):
    async def scenario(app, client, player, pilot):
        app.store.settings.report_play_history = False
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        await pilot.pause(0.1)
        assert not [call for call in client.calls if call[0] == "history"]

    run_app(tmp_path, scenario)


def test_queue_survives_changing_view(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        await pilot.press("l")
        await settle(pilot, lambda: app.view.key == "likes")
        await pilot.press("n")
        await settle(pilot, lambda: player.loaded[-1][0] == "https://stream/1")

    run_app(tmp_path, scenario)


def test_track_end_advances_and_end_of_queue_stops(tmp_path):
    async def scenario(app, client, player, pilot):
        app.query_one(TrackTable).move_cursor(row=4)
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        await pilot.pause(0.05)
        player.position = 200
        player.finished = True
        await settle(pilot, lambda: not app.now.playing)

    run_app(tmp_path, scenario)


def test_track_end_moves_to_next(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        await pilot.pause(0.05)
        player.position = 199
        player.finished = True
        await settle(pilot, lambda: player.loaded[-1][0] == "https://stream/1")

    run_app(tmp_path, scenario)


def test_dropped_stream_resumes_where_it_stopped_with_a_new_link(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        await pilot.pause(0.05)
        player.position = 60
        await pilot.pause(0.3)
        player.finished = True
        await settle(pilot, lambda: len(player.loaded) == 2)
        assert player.loaded[-1] == ("https://stream/0", 60)
        assert app.now.item.item_id == "0"

    run_app(tmp_path, scenario)


def test_repeatedly_failing_stream_moves_on_instead_of_looping(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        for _ in range(3):
            await pilot.pause(0.3)
            player.position = 60
            player.error = "403"
            player.finished = True
            await pilot.pause(0.3)
        await settle(pilot, lambda: app.now.item.item_id == "1")

    run_app(tmp_path, scenario)


def test_unplayable_track_is_skipped_with_a_message(tmp_path):
    app, client, player = build_app(tmp_path)
    client.pages["trending"] = Page([SoundCloudItem(kind="track", item_id="1", title="Blocked", policy="BLOCK", duration=10), track(2)])

    async def runner():
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, lambda: app.items)
            await pilot.press("enter")
            await settle(pilot, lambda: player.loaded)
            assert player.loaded[-1][0] == "https://stream/2"

    asyncio.run(runner())


def test_all_tracks_unplayable_stops_trying(tmp_path):
    app, client, player = build_app(tmp_path)
    client.pages["trending"] = Page([SoundCloudItem(kind="track", item_id=str(index), title="B", policy="BLOCK", duration=10) for index in range(30)])

    async def runner():
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, lambda: app.items)
            await pilot.press("enter")
            await settle(pilot, lambda: not app.now.playing)
            assert app.queue.index < 6
            assert player.loaded == []

    asyncio.run(runner())


def test_pause_resume_and_seek_debounce(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded and not app._stream_loading)
        await pilot.press("space")
        assert player.paused and not app.now.playing
        await pilot.press("space")
        assert not player.paused and app.now.playing
        await pilot.press("right", "right", "right")
        assert player.seeks == []
        await settle(pilot, lambda: player.seeks)
        assert player.seeks == [30]

    run_app(tmp_path, scenario)


def test_seek_is_clamped_to_track_length(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded and not app._stream_loading)
        player.position = 195
        await pilot.pause(0.3)
        await pilot.press("right")
        await settle(pilot, lambda: player.seeks)
        assert player.seeks == [199]

    run_app(tmp_path, scenario)


def test_clicking_the_waveform_seeks(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded and not app._stream_loading)
        wave = app.query_one(WaveformBar)
        await pilot.click(WaveformBar, offset=(wave.size.width // 2, 0))
        await settle(pilot, lambda: player.seeks)
        assert 95 <= player.seeks[0] <= 105

    run_app(tmp_path, scenario)


def test_volume_is_clamped_debounced_and_saved(tmp_path):
    async def scenario(app, client, player, pilot):
        app.store.settings.volume = 95
        await pilot.press("plus", "plus", "plus")
        assert player.volume == 80
        await settle(pilot, lambda: player.volume == 100)
        assert app.store.settings.volume == 100
        assert SettingsStore(app.store.path).settings.volume == 100

    run_app(tmp_path, scenario)


def test_repeat_and_shuffle_persist(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("y", "z")
        saved = SettingsStore(app.store.path).settings
        assert saved.repeat == "all" and saved.shuffle

    run_app(tmp_path, scenario)


def test_like_updates_instantly_and_shows_heart(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("f")
        assert "0" in app.engagement.liked_tracks
        assert app.query_one(TrackTable).get_row_at(0)[1].plain == "♥"
        await settle(pilot, lambda: ("like", "0", True) in client.calls)
        await pilot.press("f")
        await settle(pilot, lambda: ("like", "0", False) in client.calls)
        assert "0" not in app.engagement.liked_tracks

    run_app(tmp_path, scenario)


def test_failed_like_rolls_back(tmp_path):
    async def scenario(app, client, player, pilot):
        client.fail_like = True
        await pilot.press("f")
        await settle(pilot, lambda: ("like", "0", True) in client.calls)
        await settle(pilot, lambda: "0" not in app.engagement.liked_tracks)
        assert app.query_one(TrackTable).get_row_at(0)[1].plain == " "

    run_app(tmp_path, scenario)


def test_liked_state_from_account_appears_in_rows(tmp_path):
    async def scenario(app, client, player, pilot):
        await settle(pilot, lambda: app.query_one(TrackTable).get_row_at(2)[1].plain == "♥")

    run_app(tmp_path, scenario)


def test_like_while_signed_out_prompts_without_network(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("f")
        await pilot.pause(0.05)
        assert not [call for call in client.calls if call[0] == "like"]
        assert "0" not in app.engagement.liked_tracks

    run_app(tmp_path, scenario, signed_in=False)


def test_feed_signed_out_says_how_to_sign_in(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("w")
        await settle(pilot, lambda: app.view_error)
        assert app.view_error == "Sign in to use this. Press L."

    run_app(tmp_path, scenario, signed_in=False)


def test_shortcuts_still_work_on_an_empty_view(tmp_path):
    """The empty state must not move focus to the search box, where keys would be typed."""

    async def scenario(app, client, player, pilot):
        await pilot.press("w")
        await settle(pilot, lambda: app.view_error)
        await pilot.pause(0.05)
        assert isinstance(app.focused, TrackTable)
        await pilot.press("L")
        await settle(pilot, lambda: isinstance(app.screen, SignInScreen))
        assert app.query_one("#search", Input).value == ""

    run_app(tmp_path, scenario, signed_in=False)


def test_follow_person_from_following_view(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("P")
        await settle(pilot, lambda: app.view.key == "following" and app.items)
        await pilot.press("f")
        await settle(pilot, lambda: ("follow", "9", True) in client.calls)

    run_app(tmp_path, scenario)


def test_repost(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("R")
        await settle(pilot, lambda: ("repost", "0", True) in client.calls)

    run_app(tmp_path, scenario)


def test_add_to_existing_playlist(tmp_path):
    async def scenario(app, client, player, pilot):
        app.query_one(TrackTable).move_cursor(row=3)
        await pilot.press("a")
        await settle(pilot, lambda: isinstance(app.screen, PlaylistPickerScreen) and app.screen.playlists is not None)
        await pilot.press(*"road", "enter")
        await settle(pilot, lambda: ("add", "50", "3") in client.calls)
        assert not isinstance(app.screen, PlaylistPickerScreen)

    run_app(tmp_path, scenario)


def test_add_to_new_playlist_is_private_by_default(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("a")
        await settle(pilot, lambda: isinstance(app.screen, PlaylistPickerScreen) and app.screen.playlists is not None)
        await pilot.press("up", "enter")
        await settle(pilot, lambda: isinstance(app.screen, NewPlaylistScreen) and app.focused is not None)
        await pilot.press(*"Late", "enter")
        await settle(pilot, lambda: ("create", "Late", ["0"], True) in client.calls)

    run_app(tmp_path, scenario)


def test_add_while_signed_out_prompts(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("a")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, PlaylistPickerScreen)

    run_app(tmp_path, scenario, signed_in=False)


def test_deleting_a_playlist_asks_first_and_cancel_does_nothing(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("p")
        await settle(pilot, lambda: app.view.key == "playlists" and app.items)
        await pilot.press("d")
        await settle(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not [call for call in client.calls if call[0] == "delete"]
        await pilot.press("d")
        await settle(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.click("#ok")
        await settle(pilot, lambda: ("delete", "50") in client.calls)
        await settle(pilot, lambda: app.items == [])

    run_app(tmp_path, scenario)


def test_remove_track_from_my_playlist(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("p")
        await settle(pilot, lambda: app.view.key == "playlists" and app.items)
        await pilot.press("enter")
        await settle(pilot, lambda: app.view.key.startswith("collection:") and app.items)
        await pilot.press("d")
        await settle(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.click("#ok")
        await settle(pilot, lambda: ("remove", "50", "201") in client.calls)

    run_app(tmp_path, scenario)


def test_cannot_remove_from_someone_elses_playlist(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("D")
        await settle(pilot, lambda: app.view.key == "discover" and app.items)
        await pilot.press("d")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, ConfirmScreen)
        await pilot.press("enter")
        await settle(pilot, lambda: app.view.key.startswith("collection:") and app.items)
        await pilot.press("d")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, ConfirmScreen)

    run_app(tmp_path, scenario)


def test_enqueue_and_manage_up_next(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        app.query_one(TrackTable).move_cursor(row=3)
        await pilot.press("e")
        assert app.queue.upcoming[0].item_id == "3"
        await pilot.press("v")
        await settle(pilot, lambda: app.view.key == "queue" and app.items)
        assert app.items[0].item_id == "3"
        await pilot.press("d")
        await settle(pilot, lambda: app.items[0].item_id == "1")

    run_app(tmp_path, scenario)


def test_infinite_scroll_appends_rows_and_extends_queue(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("slash", *"abc", "enter")
        await settle(pilot, lambda: app.view.key == "search:all:abc" and not app.view_loading and app.items)
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        await settle(pilot, lambda: ("next",) in client.calls)
        await settle(pilot, lambda: app.query_one(TrackTable).row_count == 4)
        assert app.queue.items[-1].item_id == "99"

    run_app(tmp_path, scenario)


def test_sign_in_with_pasted_token(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("L")
        await settle(pilot, lambda: isinstance(app.screen, SignInScreen))
        app.screen.query_one("#token", Input).value = "pasted-token"
        app.screen.query_one("#use-token").press()
        await settle(pilot, lambda: ("signin", "pasted-token") in client.calls)
        await settle(pilot, lambda: not isinstance(app.screen, SignInScreen))
        assert app.auth.signed_in
        await settle(pilot, lambda: "2" in app.engagement.liked_tracks)

    run_app(tmp_path, scenario, signed_in=False)


def test_sign_out_clears_account_state(tmp_path):
    async def scenario(app, client, player, pilot):
        await settle(pilot, lambda: "2" in app.engagement.liked_tracks)
        await pilot.press("L")
        await settle(pilot, lambda: isinstance(app.screen, SignInScreen))
        app.screen.query_one("#sign-out").press()
        await settle(pilot, lambda: not isinstance(app.screen, SignInScreen))
        assert not app.auth.signed_in and not (tmp_path / "auth.json").exists()
        assert app.engagement.liked_tracks == set()

    run_app(tmp_path, scenario)


def test_expired_session_at_launch_notifies(tmp_path):
    app, client, player = build_app(tmp_path)

    def expired():
        app.auth.expired = True
        raise SignInRequired("expired")

    client.restore_session = expired

    async def runner():
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, lambda: app.auth.expired)
            await pilot.pause(0.1)
            assert app.is_running

    asyncio.run(runner())


def test_every_modal_opens_and_closes(tmp_path):
    async def scenario(app, client, player, pilot):
        for key, screen in [("question_mark", HelpScreen), ("s", SettingsScreen), ("L", SignInScreen), ("i", InfoScreen)]:
            await pilot.press(key)
            await settle(pilot, lambda: isinstance(app.screen, screen))
            await pilot.press("escape")
            await settle(pilot, lambda: not isinstance(app.screen, screen))

    run_app(tmp_path, scenario)


def test_every_modal_takes_keyboard_focus(tmp_path):
    async def scenario(app, client, player, pilot):
        for key, screen, widget in [("L", SignInScreen, None), ("s", SettingsScreen, None), ("a", PlaylistPickerScreen, Input)]:
            await pilot.press(key)
            await settle(pilot, lambda: isinstance(app.screen, screen) and app.focused is not None)
            if widget is not None:
                assert isinstance(app.focused, widget)
            await pilot.press("escape")
            await settle(pilot, lambda: not isinstance(app.screen, screen))
        assert isinstance(app.focused, TrackTable)

    run_app(tmp_path, scenario)


def test_playback_keeps_running_behind_every_modal(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded and not app._stream_loading)
        for key, screen in [("question_mark", HelpScreen), ("s", SettingsScreen), ("L", SignInScreen), ("i", InfoScreen), ("a", PlaylistPickerScreen)]:
            await pilot.press(key)
            await settle(pilot, lambda: isinstance(app.screen, screen))
            player.position += 5
            await pilot.pause(0.35)
            assert app.now.position == player.position
            await pilot.press("escape")
            await settle(pilot, lambda: not isinstance(app.screen, screen))
        # The track ends while a dialog is open: the next one starts behind it.
        await pilot.press("question_mark")
        await settle(pilot, lambda: isinstance(app.screen, HelpScreen))
        player.position = 199
        player.finished = True
        await settle(pilot, lambda: player.loaded[-1][0] == "https://stream/1")
        assert isinstance(app.screen, HelpScreen)

    run_app(tmp_path, scenario)


def test_settings_save_and_cancel(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("s")
        await settle(pilot, lambda: isinstance(app.screen, SettingsScreen))
        app.screen.query_one("#soundcloud_username", Input).value = "changed"
        await pilot.press("escape")
        await settle(pilot, lambda: not isinstance(app.screen, SettingsScreen))
        assert app.store.settings.soundcloud_username == ""
        await pilot.press("s")
        await settle(pilot, lambda: isinstance(app.screen, SettingsScreen))
        app.screen.query_one("#soundcloud_username", Input).value = "changed"
        await pilot.press("ctrl+s")
        await settle(pilot, lambda: not isinstance(app.screen, SettingsScreen))
        assert SettingsStore(app.store.path).settings.soundcloud_username == "changed"

    run_app(tmp_path, scenario)


def test_narrow_terminal_hides_sidebar_and_keeps_working(tmp_path):
    async def scenario(app, client, player, pilot):
        assert not app.query_one("#sidebar").display
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded)
        columns = {str(key.value) for key in app.query_one(TrackTable).columns}
        assert "plays" not in columns and "title" in columns

    run_app(tmp_path, scenario, size=(70, 24))


@pytest.mark.parametrize("width", [70, 80, 100, 130, 200])
def test_columns_fit_the_table_after_resizing(tmp_path, width):
    async def scenario(app, client, player, pilot):
        await pilot.resize_terminal(width, 40)
        await pilot.pause(0.15)
        table = app.query_one(TrackTable)
        total = sum(column.get_render_width(table) for column in table.columns.values())
        assert total <= table.size.width
        assert total >= table.size.width - 4  # No wasted space on the right either.
        assert table.max_scroll_x == 0

    run_app(tmp_path, scenario)


def test_playlists_and_people_show_track_counts_not_play_counts(tmp_path):
    from soundcloud_rpc_tui.ui.widgets import item_cells

    playlist = SoundCloudItem(kind="playlist", title="P", likes=9, track_count=31)
    title, _, plays, length = item_cells(playlist, True)
    assert title.plain == "≡ P" and plays.plain == "" and length.plain == "31 tracks"
    person = SoundCloudItem(kind="user", title="U", followers=500, track_count=1)
    title, _, plays, length = item_cells(person, True)
    assert title.plain == "@ U" and plays.plain == "" and length.plain == "1 track"


def test_resizing_keeps_rows_and_cursor(tmp_path):
    async def scenario(app, client, player, pilot):
        table = app.query_one(TrackTable)
        table.move_cursor(row=3)
        await pilot.resize_terminal(180, 50)
        await pilot.pause(0.1)
        assert table.row_count == 5 and table.cursor_row == 3

    run_app(tmp_path, scenario)


def test_late_updates_during_shutdown_do_not_crash(tmp_path, monkeypatch):
    async def scenario(app, client, player, pilot):
        await pilot.press("enter")
        await settle(pilot, lambda: player.loaded and not app._stream_loading)

        def gone(*args, **kwargs):
            raise NoMatches("screen removed")

        monkeypatch.setattr(app, "query_one", gone)
        app._tick()
        app._call_ui(app._waveform_ready, app._play_generation, [1, 2, 3])
        await pilot.pause(0.05)
        monkeypatch.undo()
        assert app.is_running

    run_app(tmp_path, scenario)


def test_quit_closes_player(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("q")

    app, client, player = run_app(tmp_path, scenario)
    assert player.closed


def test_no_lastfm_anywhere_in_the_interface(tmp_path):
    async def scenario(app, client, player, pilot):
        await pilot.press("s")
        await settle(pilot, lambda: isinstance(app.screen, SettingsScreen))
        assert not app.screen.query("#lastfm-key")
        text = " ".join(str(getattr(widget, "renderable", "")) for widget in app.screen.walk_children())
        assert "last.fm" not in text.lower()

    run_app(tmp_path, scenario)


def test_waveform_drawing():
    assert bucket_samples([], 5) == [0.12] * 5
    assert bucket_samples([1, 2], 0) == []
    levels = bucket_samples([100, 110, 120, 130, 140] * 100, 40)
    assert len(levels) == 40 and all(0 <= level <= 1 for level in levels)
    loud_track = [130] * 900 + [60] * 900
    drawn = bucket_samples(loud_track, 20)
    assert max(drawn) - min(drawn) > 0.5  # Dynamic range stays visible on mastered audio.
    top, bottom = waveform_rows([0.0, 0.5, 1.0])
    assert len(top) == len(bottom) == 3 and bottom[2] == "█" and top[2] == "█"


def test_artwork_rendering_handles_bad_data():
    assert render_artwork(b"not an image", 4, 2) is None
    assert render_artwork(b"", 4, 2) is None
    import io

    from PIL import Image

    data = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 50, 10)).save(data, "PNG")
    art = render_artwork(data.getvalue(), 4, 2)
    assert art.plain == "▀▀▀▀\n▀▀▀▀"


def discord_payloads(now, settings=None):
    return DiscordPresence(settings or Settings())._payload(now)


def test_discord_start_time_is_stable_between_ticks():
    presence = DiscordPresence(Settings())
    item = track(1, artwork="https://i/a-large.jpg")
    first = presence._payload(NowPlaying(item, position=10, playing=True, buffering=False))
    second = presence._payload(NowPlaying(item, position=11, playing=True, buffering=False))
    assert first["large_image"] == "https://i/a-t500x500.jpg"
    assert first["end"] - first["start"] == 200
    assert DiscordPresence._same(second, first)
    jumped = presence._payload(NowPlaying(item, position=60, playing=True, buffering=False))
    assert not DiscordPresence._same(jumped, first)


def test_discord_paused_clears_unless_idle_display_is_on():
    item = track(1)
    assert discord_payloads(NowPlaying(item, playing=False, buffering=False)) is None
    assert discord_payloads(NowPlaying(item, playing=False, buffering=False), Settings(display_when_idling=True))["state"] == "Paused"


def test_discord_listen_button_and_status_type():
    item = track(1)
    payload = discord_payloads(NowPlaying(item, playing=True, buffering=False), Settings(display_buttons=True, status_display_type=2))
    assert payload["buttons"][0]["url"] == item.url
    assert payload["status_display_type"].value == 2


def test_discord_does_not_retry_connection_every_tick(monkeypatch):
    attempts = []

    class Broken:
        def __init__(self, client_id):
            attempts.append(client_id)

        async def connect(self):
            raise OSError("no discord")

    monkeypatch.setattr("soundcloud_rpc_tui.integrations.AioPresence", Broken)

    async def run():
        presence = DiscordPresence(Settings())
        now = NowPlaying(track(1), playing=True, buffering=False)
        for _ in range(5):
            presence.sync(now)
            await asyncio.sleep(0.01)
        assert presence.status == "Discord isn't running"

    asyncio.run(run())
    assert len(attempts) == 1


def test_discord_sends_only_on_change(monkeypatch):
    updates = []

    class Fake:
        def __init__(self, client_id):
            pass

        async def connect(self):
            pass

        async def update(self, **payload):
            updates.append(payload)

        async def clear(self):
            updates.append(None)

        def close(self):
            pass

    monkeypatch.setattr("soundcloud_rpc_tui.integrations.AioPresence", Fake)

    async def run():
        presence = DiscordPresence(Settings())
        item = track(1)
        for position in range(10):
            presence.sync(NowPlaying(item, position=position / 5, playing=True, buffering=False))
            await asyncio.sleep(0.005)
        assert len(updates) == 1
        presence.sync(NowPlaying(track(2), position=0, playing=True, buffering=False))
        await asyncio.sleep(0.01)
        assert len(updates) == 2
        presence.sync(None)
        await asyncio.sleep(0.01)
        assert updates[-1] is None

    asyncio.run(run())
