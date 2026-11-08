import base64
import json
import time

import pytest

from auth import Session, SessionManager, SessionStore, SignInError, SignInRequired
from conftest import FakeHttp, FakeResponse, raw_track
from core import SoundCloudItem
from soundcloud import AlreadyInPlaylist, NotFound, SoundCloudClient, SoundCloudError, parse_item, playable_transcodings


def jwt(exp_offset=3600, client_id="tokenclient"):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode({})}.{encode({'exp': time.time() + exp_offset, 'client_id': client_id})}.sig"


def make_client(http, settings, tmp_path, session=None, post=None, client_id="cid"):
    store = SessionStore(tmp_path / "auth.json")
    if session:
        store.save(session)
    client = SoundCloudClient(settings, SessionManager(store, http_post=post), http=http)
    client._client_id = client_id
    return client


@pytest.fixture
def signed_in(http, settings, tmp_path):
    return make_client(http, settings, tmp_path, Session("tok", user_id="42", username="me"))


@pytest.fixture
def anon(http, settings, tmp_path):
    return make_client(http, settings, tmp_path)


TRACK = SoundCloudItem(kind="track", item_id="5", urn="soundcloud:tracks:5")
PLAYLIST = SoundCloudItem(kind="playlist", item_id="9")


# Routes and payloads below are checked against SoundCloud's own web bundle.
@pytest.mark.parametrize(
    "action,method,path,body",
    [
        (lambda c: c.set_liked(TRACK, True), "PUT", "/users/42/track_likes/5", None),
        (lambda c: c.set_liked(TRACK, False), "DELETE", "/users/42/track_likes/5", None),
        (lambda c: c.set_liked(PLAYLIST, True), "PUT", "/users/42/playlist_likes/9", None),
        (lambda c: c.set_liked(PLAYLIST, False), "DELETE", "/users/42/playlist_likes/9", None),
        (lambda c: c.set_reposted(TRACK, True), "PUT", "/me/track_reposts/5", None),
        (lambda c: c.set_reposted(TRACK, False), "DELETE", "/me/track_reposts/5", None),
        (lambda c: c.set_reposted(PLAYLIST, True), "PUT", "/me/playlist_reposts/9", None),
        (lambda c: c.set_following(SoundCloudItem(kind="user", item_id="7"), True), "POST", "/me/followings/7", None),
        (lambda c: c.set_following(SoundCloudItem(kind="user", item_id="7"), False), "DELETE", "/me/followings/7", None),
        (lambda c: c.delete_playlist("9"), "DELETE", "/playlists/9", None),
        (lambda c: c.record_play(TRACK), "POST", "/me/play-history", {"track_urn": "soundcloud:tracks:5"}),
    ],
)
def test_write_routes(signed_in, http, action, method, path, body):
    http.on(method, path, FakeResponse(200, {}))
    action(signed_in)
    call = http.calls[-1]
    assert call["method"] == method
    assert call["url"] == f"https://api-v2.soundcloud.com{path}"
    assert call["headers"]["Authorization"] == "OAuth tok"
    assert call["body"] == body


def test_system_playlist_like_uses_urn_route(signed_in, http):
    http.on("PUT", "system_playlist_likes", FakeResponse(200, {}))
    signed_in.set_liked(SoundCloudItem(kind="system-playlist", item_id="soundcloud:system-playlists:x"), True)
    assert http.calls[-1]["url"].endswith("/users/42/system_playlist_likes/soundcloud:system-playlists:x")


def test_writes_without_sign_in_never_reach_the_network(anon, http):
    with pytest.raises(SignInRequired):
        anon.set_liked(TRACK, True)
    with pytest.raises(SignInRequired):
        anon.create_playlist("x", ["1"])
    assert http.calls == []


def test_create_playlist_payload(signed_in, http):
    http.on("POST", "/playlists", {"kind": "playlist", "id": 3, "title": "New", "tracks": [{"id": 1}]})
    created = signed_in.create_playlist("New", ["1", "2"], private=True)
    assert http.calls[-1]["body"] == {"playlist": {"title": "New", "sharing": "private", "tracks": [1, 2], "_resource_type": "playlist"}}
    assert created.owned and created.item_id == "3"


def test_add_to_playlist_appends_and_keeps_existing_tracks(signed_in, http):
    http.on("GET", "/playlists/9", {"track_count": 2, "tracks": [{"id": 1}, {"id": 2}]})
    http.on("PUT", "/playlists/9", FakeResponse(200, {}))
    signed_in.add_to_playlist("9", "3")
    assert http.calls[-1]["body"] == {"playlist": {"tracks": [1, 2, 3]}}


def test_add_duplicate_does_not_write(signed_in, http):
    http.on("GET", "/playlists/9", {"track_count": 1, "tracks": [{"id": 3}]})
    with pytest.raises(AlreadyInPlaylist):
        signed_in.add_to_playlist("9", "3")
    assert not http.calls_to("PUT", "/playlists/9")


def test_incomplete_playlist_read_refuses_to_overwrite(signed_in, http):
    """Saving replaces the whole list, so a short read would silently delete tracks."""
    http.on("GET", "/playlists/9", {"track_count": 500, "tracks": [{"id": 1}]})
    with pytest.raises(SoundCloudError):
        signed_in.add_to_playlist("9", "3")
    with pytest.raises(SoundCloudError):
        signed_in.remove_from_playlist("9", "1")
    assert not http.calls_to("PUT", "/playlists/9")


def test_remove_from_playlist(signed_in, http):
    http.on("GET", "/playlists/9", {"track_count": 3, "tracks": [{"id": 1}, {"id": 2}, {"id": 3}]})
    http.on("PUT", "/playlists/9", FakeResponse(200, {}))
    signed_in.remove_from_playlist("9", "2")
    assert http.calls[-1]["body"] == {"playlist": {"tracks": [1, 3]}}


def test_engagement_reads_every_id_list(signed_in, http):
    http.on("GET", "/me/track_likes/ids", {"collection": [1, 2]})
    http.on("GET", "/me/playlist_likes/ids", {"collection": [3]})
    http.on("GET", "/me/track_reposts/ids", {"collection": [4]})
    http.on("GET", "/me/playlist_reposts/ids", {"collection": [5]})
    http.on("GET", "/users/42/followings/ids", {"collection": [6]})
    state = signed_in.engagement()
    assert state.liked_tracks == {"1", "2"} and state.liked_playlists == {"3"}
    assert state.reposted_tracks == {"4"} and state.reposted_playlists == {"5"} and state.followings == {"6"}


def test_one_failing_id_list_does_not_lose_the_others(signed_in, http):
    http.on("GET", "/me/track_likes/ids", {"collection": [1]})
    assert signed_in.engagement().liked_tracks == {"1"}


def test_engagement_follows_pagination(signed_in, http):
    next_url = "https://api-v2.soundcloud.com/me/track_likes/ids?cursor=2"
    http.on("GET", "/me/track_likes/ids", lambda call: {"collection": [2]} if call["params"].get("cursor") else {"collection": [1], "next_href": next_url})
    assert signed_in.engagement().liked_tracks == {"1", "2"}


def test_library_filters_by_row_type_and_marks_owned(signed_in, http):
    http.on(
        "GET",
        "/me/library/all",
        {
            "collection": [
                {"type": "playlist", "playlist": {"kind": "playlist", "id": 1, "title": "Mine", "user": {"id": 42}}},
                {"type": "playlist-like", "playlist": {"kind": "playlist", "id": 2, "title": "Liked", "user": {"id": 8}}},
                {"type": "album", "playlist": {"kind": "playlist", "id": 3, "title": "Al", "is_album": True, "user": {"id": 42}}},
                {"type": "system-playlist-like", "system_playlist": {"kind": "system-playlist", "urn": "s:1", "title": "Sys"}},
            ]
        },
    )
    page = signed_in.playlists()
    assert [(item.item_id, item.owned) for item in page.items] == [("1", True), ("2", False), ("s:1", False)]
    assert [item.kind for item in signed_in.albums().items] == ["album"]
    assert [item.title for item in signed_in.my_playlists()] == ["Mine"]


def test_public_profile_browsing_without_sign_in(http, settings, tmp_path):
    settings.soundcloud_username = "https://soundcloud.com/someone"
    client = make_client(http, settings, tmp_path)
    http.on("GET", "/resolve", {"kind": "user", "id": 55})
    http.on("GET", "/users/55/track_likes", {"collection": [{"kind": "like", "track": raw_track(1)}]})
    assert client.likes().items[0].title == "Track 1"
    client.likes()
    assert len(http.calls_to("GET", "/resolve")) == 1


def test_library_views_without_sign_in_or_profile_ask_for_sign_in(anon):
    with pytest.raises(SignInRequired):
        anon.likes()


def test_feed_requires_sign_in(anon):
    with pytest.raises(SignInRequired):
        anon.feed()


def test_parse_wrappers():
    assert parse_item({"kind": "like", "track": raw_track(1)}).item_id == "1"
    assert parse_item({"track": raw_track(2), "score": 9}).item_id == "2"
    assert parse_item({"type": "track-repost", "track": raw_track(3), "user": {"id": 1}}).item_id == "3"
    assert parse_item({"played_at": 1, "track": raw_track(4)}).item_id == "4"
    assert parse_item({"kind": "user", "id": 5, "username": "u"}).kind == "user"
    assert parse_item({"kind": "unknown"}) is None
    assert parse_item("nope") is None


def test_parse_track_fields():
    track = parse_item(raw_track(1, duration=90_500, playback_count=7, artwork_url=None, user={"id": 3, "username": "a", "avatar_url": "https://i/a-large.jpg"}))
    assert track.duration == 90 and track.plays == 7
    assert track.artwork == "https://i/a-large.jpg"  # Falls back to the avatar.
    assert not track.is_stub
    assert parse_item({"kind": "track", "id": 9, "policy": "ALLOW", "monetization_model": "x"}).is_stub


def test_parse_album_and_system_playlist():
    assert parse_item({"kind": "playlist", "id": 1, "is_album": True}).kind == "album"
    system = parse_item({"kind": "system-playlist", "urn": "soundcloud:system-playlists:a", "id": "x", "tracks": [{}, {}]})
    assert system.kind == "system-playlist" and system.item_id == "soundcloud:system-playlists:a" and system.track_count == 2


def test_stream_prefers_quality_and_skips_drm_and_previews():
    item = SoundCloudItem(
        transcodings=[
            {"url": "u1", "preset": "aac_96k", "format": {"protocol": "hls"}},
            {"url": "u2", "preset": "aac_160k", "format": {"protocol": "hls"}},
            {"url": "u3", "preset": "aac_160k", "format": {"protocol": "ctr-encrypted-hls"}},
            {"url": "u4", "preset": "aac_160k", "snipped": True, "format": {"protocol": "hls"}},
            {"url": "u5", "preset": "abr_sq", "format": {"protocol": "hls"}},
        ]
    )
    assert [choice["url"] for choice in playable_transcodings(item)] == ["u2", "u1", "u5", "u4"]


def test_stream_tries_next_option_when_one_fails(anon, http):
    item = parse_item(raw_track(1))
    item.transcodings = [
        {"url": "https://api-v2.soundcloud.com/media/bad", "preset": "aac_160k", "format": {"protocol": "hls"}},
        {"url": "https://api-v2.soundcloud.com/media/good", "preset": "aac_96k", "format": {"protocol": "hls"}},
    ]
    http.on("GET", "/media/good", {"url": "https://cdn/stream.m3u8"})
    assert anon.stream_url(item) == "https://cdn/stream.m3u8"
    assert http.calls_to("GET", "/media/good")[0]["params"]["track_authorization"] == "auth"


def test_blocked_track_explains_itself(anon):
    with pytest.raises(SoundCloudError, match="region"):
        anon.stream_url(SoundCloudItem(item_id="1", policy="BLOCK", transcodings=[{"url": "x"}]))


def test_stream_hydrates_stub_first(anon, http):
    http.on("GET", "/tracks/1", raw_track(1))
    http.on("GET", "/media/1/aac", {"url": "https://cdn/s"})
    assert anon.stream_url(SoundCloudItem(item_id="1", is_stub=True)) == "https://cdn/s"


def test_playlist_hydration_keeps_order_and_drops_unavailable(anon, http):
    http.on(
        "GET",
        "/playlists/1",
        {"tracks": [raw_track(1), {"kind": "track", "id": 2}, {"kind": "track", "id": 3}, {"kind": "track", "id": 4}]},
    )
    http.on("GET", "/tracks", [raw_track(4), raw_track(2)])
    page = anon.collection_tracks(SoundCloudItem(kind="playlist", item_id="1"))
    assert [item.item_id for item in page.items] == ["1", "2", "4"]


def test_hydration_batches_large_playlists(anon, http):
    stubs = [{"kind": "track", "id": index} for index in range(1, 121)]
    http.on("GET", "/playlists/1", {"tracks": stubs})
    http.on("GET", "/tracks", lambda call: [raw_track(int(value)) for value in call["params"]["ids"].split(",")])
    page = anon.collection_tracks(SoundCloudItem(kind="playlist", item_id="1"))
    assert [item.item_id for item in page.items] == [str(index) for index in range(1, 121)]
    batches = http.calls_to("GET", "/tracks")
    assert len(batches) == 3 and max(len(call["params"]["ids"].split(",")) for call in batches) == 50


def test_search_scopes(anon, http):
    http.on("GET", "/search", {"collection": []})
    for scope, path in [("all", "/search"), ("tracks", "/search/tracks"), ("albums", "/search/albums"), ("people", "/search/users")]:
        anon.search("x", scope)
        assert http.calls[-1]["url"].endswith(path)
    assert anon.search("   ").items == []


def test_pagination_merges_query_without_duplicates(anon, http):
    href = "https://api-v2.soundcloud.com/search/tracks?q=x&offset=30&client_id=old"
    http.on("GET", "/search/tracks", {"collection": [raw_track(1)], "next_href": href})
    page = anon.search("x", "tracks")
    anon.next_page(page)
    call = http.calls[-1]
    assert call["url"] == "https://api-v2.soundcloud.com/search/tracks"
    assert call["params"]["offset"] == "30" and call["params"]["client_id"] == "cid"


@pytest.mark.parametrize("href", ["https://evil.example/steal", "http://api-v2.soundcloud.com/x", "https://api-v2.soundcloud.com.evil.example/x"])
def test_token_never_goes_to_another_host(signed_in, http, href):
    with pytest.raises(SoundCloudError):
        signed_in._request("GET", href)
    assert http.calls == []


def test_token_client_id_claim_is_preferred(http, settings, tmp_path):
    client = make_client(http, settings, tmp_path, Session(jwt(), user_id="42"))
    http.on("GET", "/me", {"id": 42})
    client.me()
    assert http.calls[-1]["params"]["client_id"] == "tokenclient"


def test_401_refreshes_token_and_retries_once(http, settings, tmp_path):
    old, new = jwt(), jwt(client_id="c2")
    client = make_client(
        http, settings, tmp_path, Session(old, "r", user_id="42"),
        post=lambda *a, **k: FakeResponse(200, {"access_token": new, "refresh_token": "r2"}),
    )
    http.on("GET", "/me/track_likes/ids", lambda call: {"collection": [1]} if call["headers"]["Authorization"] == f"OAuth {new}" else FakeResponse(401, {}))
    assert client._ids("/me/track_likes/ids") == {"1"}


def test_expired_session_required_action_asks_for_sign_in(http, settings, tmp_path):
    old = jwt()
    client = make_client(http, settings, tmp_path, Session(old, "r"), post=lambda *a, **k: FakeResponse(400, {}))
    http.on("GET", "/me", FakeResponse(401, {}))
    with pytest.raises(SignInRequired):
        client.me()
    assert client.auth.expired


def test_expired_session_public_browsing_continues_anonymously(http, settings, tmp_path):
    client = make_client(http, settings, tmp_path, Session(jwt(), "r"), post=lambda *a, **k: FakeResponse(400, {}))
    http.on("GET", "/charts", lambda call: {"collection": []} if "Authorization" not in call["headers"] else FakeResponse(401, {}))
    assert client.trending().items == []
    assert client.auth.expired


def test_client_id_is_reused_from_disk_with_no_page_fetch(http, settings, tmp_path):
    first = make_client(http, settings, tmp_path, client_id="")
    http.on("GET", "soundcloud.com/discover", FakeResponse(200, text='"hydratable":"apiClient","data":{"id":"abc","isExpiring":true}'))
    assert first.client_id() == "abc"
    http.calls.clear()
    second = make_client(http, settings, tmp_path, client_id="")
    assert second.client_id() == "abc"
    assert http.calls == []


def test_client_id_falls_back_to_mobile_site_when_main_site_challenges(http, settings, tmp_path):
    client = make_client(http, settings, tmp_path, client_id="")
    http.on("GET", "soundcloud.com/discover", FakeResponse(202, text="challenge"))
    http.on("GET", "m.soundcloud.com", FakeResponse(200, text='{"clientId":"KKzJxmw11tYpCs6T24P4uUYhqmjalG6M"}'))
    assert client.client_id() == "KKzJxmw11tYpCs6T24P4uUYhqmjalG6M"


def test_client_id_failure_message_is_actionable(http, settings, tmp_path):
    client = make_client(http, settings, tmp_path, client_id="")
    with pytest.raises(SoundCloudError, match="isn't accepting"):
        client.client_id()


def test_rejected_client_id_is_replaced_and_retried(http, settings, tmp_path):
    client = make_client(http, settings, tmp_path, client_id="stale")
    http.on("GET", "soundcloud.com/discover", FakeResponse(200, text='"hydratable":"apiClient","data":{"id":"fresh"}'))
    http.on("GET", "/charts", lambda call: {"collection": []} if call["params"]["client_id"] == "fresh" else FakeResponse(401, {}))
    assert client.trending().items == []
    assert client.client_id() == "fresh"


def test_network_failure_has_clear_message(anon, http):
    import requests

    def down(*args, **kwargs):
        raise requests.ConnectionError("x")

    http.request = down
    with pytest.raises(SoundCloudError, match="Can't reach SoundCloud"):
        anon.trending()


def test_rate_limit_retries_get_but_not_writes(signed_in, http, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    attempts = []

    def limited(call):
        attempts.append(1)
        return {"collection": []} if len(attempts) > 1 else FakeResponse(429, {}, headers={"Retry-After": "1"})

    http.on("GET", "/charts", limited)
    assert signed_in.trending().items == []
    http.on("PUT", "track_likes", FakeResponse(429, {}))
    with pytest.raises(SoundCloudError, match="rate limiting"):
        signed_in.set_liked(TRACK, True)
    assert len(http.calls_to("PUT", "track_likes")) == 1


def test_404_raises_not_found(anon):
    with pytest.raises(NotFound):
        anon.trending()


def test_resolve_only_accepts_soundcloud_links(anon, http):
    with pytest.raises(SoundCloudError, match="isn't a SoundCloud link"):
        anon.resolve("https://evil.example/track")
    assert http.calls == []


def test_sign_in_validates_token_and_keeps_previous_session_on_failure(http, settings, tmp_path):
    client = make_client(http, settings, tmp_path, Session("good", user_id="1", username="old"))
    http.on("GET", "/me", lambda call: FakeResponse(401, {}) if "bad" in call["headers"]["Authorization"] else {"id": 2, "username": "new"})
    with pytest.raises(SignInError):
        client.sign_in_with_token("bad")
    assert client.auth.session.username == "old"
    session = client.sign_in_with_token("  newtok ", " refresh ")
    assert (session.access_token, session.refresh_token, session.username, session.user_id) == ("newtok", "refresh", "new", "2")
    assert SessionStore(tmp_path / "auth.json").load().username == "new"


def test_waveform_ignores_non_json_and_failures(anon, http):
    assert anon.waveform(SoundCloudItem(waveform_url="https://w/x.png")) == []
    http.on("GET", "wave", {"samples": [1, 2, 3]})
    assert anon.waveform(SoundCloudItem(waveform_url="https://wave/x.json")) == [1, 2, 3]
