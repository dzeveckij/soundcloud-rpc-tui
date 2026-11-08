"""The real client and the real UI together, signed in, against a fake SoundCloud.

The other UI tests fake the client and the client tests have no UI, so this is what
catches the two disagreeing. It asserts what would actually be sent to SoundCloud.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input

from app import SoundCloudRpcTui
from auth import SessionManager, SessionStore
from conftest import FakeHttp, FakeResponse, raw_track
from core import SettingsStore
from integrations import IntegrationHub
from screens import PlaylistPickerScreen, SignInScreen
from soundcloud import SoundCloudClient
from test_app import FakePlayer, settle
from widgets import TrackTable

PLAYLIST_RAW = {"kind": "playlist", "id": 50, "title": "Road trip", "track_count": 1, "sharing": "private", "user": {"id": 42}}


def fake_soundcloud() -> FakeHttp:
    http = FakeHttp()
    http.on("GET", "/charts", {"collection": [{"track": raw_track(1), "score": 1}, {"track": raw_track(2), "score": 1}]})
    http.on("GET", "/me", lambda call: {"id": 42, "username": "tester", "permalink_url": "https://soundcloud.com/tester"} if call["headers"].get("Authorization") == "OAuth pasted" else FakeResponse(401, {}))
    http.on("GET", "/me/track_likes/ids", {"collection": [2]})
    http.on("GET", "/me/playlist_likes/ids", {"collection": []})
    http.on("GET", "/me/track_reposts/ids", {"collection": []})
    http.on("GET", "/me/playlist_reposts/ids", {"collection": []})
    http.on("GET", "/users/42/followings/ids", {"collection": []})
    http.on("GET", "/me/library/all", {"collection": [{"type": "playlist", "playlist": PLAYLIST_RAW}]})
    http.on("GET", "/playlists/50", {**PLAYLIST_RAW, "tracks": [{"kind": "track", "id": 9}]})
    http.on("GET", "/users/42/track_likes", {"collection": [{"kind": "like", "track": raw_track(2)}]})
    http.on("GET", "/stream", {"collection": [{"type": "track-repost", "track": raw_track(3), "user": {"id": 8}}]})
    http.on("GET", "/me/play-history/tracks", {"collection": [{"played_at": 1, "track": raw_track(1)}]})
    http.on("GET", "/media/", {"url": "https://cdn.example/audio.m3u8"})
    for method, route in [("PUT", "/users/42/track_likes/"), ("DELETE", "/users/42/track_likes/"), ("PUT", "/playlists/50"), ("POST", "/me/play-history"), ("PUT", "/me/track_reposts/")]:
        http.on(method, route, FakeResponse(200, {}))
    http.on("POST", "/playlists", {"kind": "playlist", "id": 70, "title": "Fresh"})
    return http


def test_sign_in_then_like_add_and_browse(tmp_path):
    http = fake_soundcloud()
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.discord_rich_presence = False
    auth = SessionManager(SessionStore(tmp_path / "auth.json"))
    client = SoundCloudClient(store.settings, auth, http=http)
    client._client_id = "cid"
    player = FakePlayer()
    app = SoundCloudRpcTui(store=store, auth=auth, client=client, player=player, hub=IntegrationHub(store.settings))

    async def runner():
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, lambda: app.items)

            # Sign in. Liked tracks should then appear without further action.
            await pilot.press("L")
            await settle(pilot, lambda: isinstance(app.screen, SignInScreen))
            app.screen.query_one("#token", Input).value = "pasted"
            app.screen.query_one("#use-token").press()
            await settle(pilot, lambda: not isinstance(app.screen, SignInScreen))
            assert auth.signed_in and auth.session.username == "tester"
            assert SessionStore(tmp_path / "auth.json").load().access_token == "pasted"
            await settle(pilot, lambda: app.engagement.liked_tracks == {"2"})
            table = app.query_one(TrackTable)
            await settle(pilot, lambda: table.get_row_at(1)[1].plain == "♥")

            # Like.
            await pilot.press("f")
            await settle(pilot, lambda: http.calls_to("PUT", "/users/42/track_likes/1"))
            assert http.calls_to("PUT", "/users/42/track_likes/1")[0]["headers"]["Authorization"] == "OAuth pasted"

            # Unlike a track that was already liked.
            table.move_cursor(row=1)
            await pilot.press("f")
            await settle(pilot, lambda: http.calls_to("DELETE", "/users/42/track_likes/2"))

            # Add to an existing playlist: existing tracks must be kept.
            await pilot.press("a")
            await settle(pilot, lambda: isinstance(app.screen, PlaylistPickerScreen) and app.screen.playlists)
            await pilot.press("enter")
            await settle(pilot, lambda: http.calls_to("PUT", "/playlists/50"))
            assert http.calls_to("PUT", "/playlists/50")[0]["body"] == {"playlist": {"tracks": [9, 2]}}

            # Play, which also reports listening history.
            await pilot.press("enter")
            await settle(pilot, lambda: player.loaded == [("https://cdn.example/audio.m3u8", 0.0)])
            await settle(pilot, lambda: http.calls_to("POST", "/me/play-history"))
            assert http.calls_to("POST", "/me/play-history")[0]["body"] == {"track_urn": "soundcloud:tracks:2"}

            # Account-only views.
            for key, view, title in [("w", "feed", "Track 3"), ("l", "likes", "Track 2"), ("h", "history", "Track 1"), ("p", "playlists", "Road trip")]:
                await pilot.press(key)
                await settle(pilot, lambda: app.view.key == view and not app.view_loading and app.items)
                assert app.items[0].title == title

    asyncio.run(runner())


def test_rejected_token_at_sign_in_shows_error_and_stays_signed_out(tmp_path):
    http = fake_soundcloud()
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.discord_rich_presence = False
    auth = SessionManager(SessionStore(tmp_path / "auth.json"))
    client = SoundCloudClient(store.settings, auth, http=http)
    client._client_id = "cid"
    app = SoundCloudRpcTui(store=store, auth=auth, client=client, player=FakePlayer(), hub=IntegrationHub(store.settings))

    async def runner():
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, lambda: app.items)
            await pilot.press("L")
            await settle(pilot, lambda: isinstance(app.screen, SignInScreen))
            app.screen.query_one("#token", Input).value = "wrong"
            app.screen.query_one("#use-token").press()
            status = app.screen.query_one("#signin-status")
            await settle(pilot, lambda: status.has_class("-error"))
            assert isinstance(app.screen, SignInScreen)
            assert not auth.signed_in and not (tmp_path / "auth.json").exists()

    asyncio.run(runner())
