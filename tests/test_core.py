import json
import os
import random
import stat

import pytest

from soundcloud_rpc_tui import core
from soundcloud_rpc_tui.core import Engagement, PlayQueue, Settings, SettingsStore, SoundCloudItem, atomic_write, compact_count, normalize_track_info, seconds_to_time


def tracks(count):
    return [SoundCloudItem(item_id=str(index), title=f"T{index}") for index in range(count)]


def test_queue_advances_and_stops_at_end():
    queue = PlayQueue()
    queue.load(tracks(2), 0)
    assert queue.advance().item_id == "1"
    assert queue.advance() is None


def test_repeat_one_only_repeats_when_track_ends_naturally():
    queue = PlayQueue()
    queue.load(tracks(3), 0)
    queue.repeat = "one"
    assert queue.advance().item_id == "0"
    assert queue.advance(manual=True).item_id == "1"


def test_repeat_all_wraps_both_ways():
    queue = PlayQueue()
    queue.load(tracks(2), 1)
    queue.repeat = "all"
    assert queue.advance().item_id == "0"
    assert queue.back().item_id == "1"


def test_add_next_keeps_hand_queued_order():
    queue = PlayQueue()
    queue.load(tracks(3), 0)
    first, second = SoundCloudItem(item_id="a"), SoundCloudItem(item_id="b")
    queue.add_next(first)
    queue.add_next(second)
    assert [item.item_id for item in queue.upcoming] == ["a", "b", "1", "2"]


def test_add_next_on_empty_queue_makes_it_current():
    queue = PlayQueue()
    item = SoundCloudItem(item_id="a")
    queue.add_next(item)
    assert queue.current is item


def test_shuffle_keeps_current_and_all_tracks_then_restores_order():
    queue = PlayQueue(random.Random(4))
    items = tracks(20)
    queue.load(items, 5)
    queue.toggle_shuffle()
    assert queue.current is items[5]
    assert sorted(item.item_id for item in queue.items) == sorted(item.item_id for item in items)
    assert queue.items != items
    queue.toggle_shuffle()
    assert queue.items == items
    assert queue.current is items[5]


def test_load_with_shuffle_on_keeps_chosen_track_first():
    queue = PlayQueue(random.Random(1))
    queue.shuffle = True
    items = tracks(10)
    queue.load(items, 3)
    assert queue.current is items[3]


def test_remove_and_jump_within_upcoming():
    queue = PlayQueue()
    queue.load(tracks(4), 0)
    assert queue.remove_upcoming(0).item_id == "1"
    assert queue.jump_to_upcoming(1).item_id == "3"


def test_settings_drop_lastfm_and_region_from_old_files(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "lastfm_enabled": True,
                "lastfm_api_key": "k",
                "lastfm_secret": "s",
                "lastfm_session_key": "x",
                "soundcloud_region": "us",
                "soundcloud_username": "me",
                "theme": "textual-dark",
            }
        )
    )
    store = SettingsStore(path)
    assert store.settings.soundcloud_username == "me"
    assert store.settings.theme == "soundcloud"
    store.save()
    saved = json.loads(path.read_text())
    assert not [key for key in saved if key.startswith("lastfm") or key == "soundcloud_region"]
    assert saved["schema_version"] == 2


def test_settings_keep_a_theme_chosen_after_migration(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": 2, "theme": "textual-dark"}))
    assert SettingsStore(path).settings.theme == "textual-dark"


def test_settings_survive_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert SettingsStore(path).settings == Settings()


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "sub" / "secret.json"
    atomic_write(path, "{}")
    atomic_write(path, '{"a": 1}')
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert [entry.name for entry in path.parent.iterdir()] == ["secret.json"]


@pytest.mark.skipif(os.name == "nt", reason="Windows protects the profile folder instead of file modes")
def test_atomic_write_sets_private_mode(tmp_path):
    path = tmp_path / "secret.json"
    atomic_write(path, "{}")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_settings_keep_non_ascii_text_on_every_platform(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.settings.soundcloud_username = "björk-ñ-日本"
    store.save()
    assert SettingsStore(path).settings.soundcloud_username == "björk-ñ-日本"


def test_xdg_folders_win_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "k"))
    assert core.config_dir() == tmp_path / "c" / "soundcloud-rpc-tui"
    assert core.cache_dir() == tmp_path / "k" / "soundcloud-rpc-tui"


@pytest.mark.skipif(os.name != "nt", reason="Windows folders")
def test_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert core.config_dir() == tmp_path / "Roaming" / "soundcloud-rpc-tui"
    assert core.cache_dir() == tmp_path / "Local" / "soundcloud-rpc-tui" / "Cache"


def test_engagement_tracks_each_kind_separately():
    state = Engagement()
    track = SoundCloudItem(kind="track", item_id="1")
    playlist = SoundCloudItem(kind="playlist", item_id="1")
    user = SoundCloudItem(kind="user", item_id="1")
    state.set_liked(track, True)
    assert state.is_liked(track) and not state.is_liked(playlist) and not state.is_liked(user)
    state.set_liked(user, True)
    assert state.followings == {"1"}


def test_formatters():
    assert seconds_to_time(3725) == "1:02:05"
    assert seconds_to_time(65) == "1:05"
    assert compact_count(999) == "999"
    assert compact_count(1000) == "1K"
    assert compact_count(25_103_210) == "25.1M"


def test_title_parser():
    assert normalize_track_info("Artist - Song", "uploader").artist == "Artist"
    assert normalize_track_info("Song", "uploader").artist == "uploader"
    assert normalize_track_info("A - B", "uploader", use_track_parser=False).track == "A - B"
