import base64
import json
import sqlite3
import stat
import time

import pytest

from soundcloud_rpc_tui.auth import Session, SessionManager, SessionStore, SignInError, SignInRequired, decode_jwt_claims, detect_browsers, read_browser_tokens, read_cookie_file
from conftest import FakeResponse


def jwt(exp_offset: float = 3600, client_id: str = "cid") -> str:
    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode({'exp': time.time() + exp_offset, 'client_id': client_id})}.sig"


def manager(tmp_path, post=None, browser=None, session=None) -> SessionManager:
    store = SessionStore(tmp_path / "auth.json")
    if session:
        store.save(session)
    return SessionManager(store, http_post=post, browser_reader=browser or (lambda source: ("", "", source)))


def test_jwt_claims_and_legacy_tokens():
    assert decode_jwt_claims(jwt(client_id="abc"))["client_id"] == "abc"
    assert decode_jwt_claims("2-123456-789-abcdef") == {}
    assert decode_jwt_claims("a.@@@.c") == {}
    assert Session("2-123456-789-abcdef").expires_at is None
    assert not Session("2-123456-789-abcdef").is_expired()


def test_session_file_is_private_and_round_trips(tmp_path):
    store = SessionStore(tmp_path / "auth.json")
    store.save(Session("token", "refresh", "firefox", "1", "me"))
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load().refresh_token == "refresh"
    store.clear()
    assert store.load() is None
    store.clear()


def test_unexpired_token_needs_no_refresh(tmp_path):
    calls = []
    token = jwt()
    auth = manager(tmp_path, post=lambda *a, **k: calls.append(a), session=Session(token, "r"))
    assert auth.valid_token() == token
    assert calls == []


def test_browser_is_read_before_spending_refresh_token(tmp_path):
    spent = []
    fresh = jwt()
    auth = manager(
        tmp_path,
        post=lambda *a, **k: spent.append(a),
        browser=lambda source: (fresh, "browser-refresh", source),
        session=Session(jwt(-100), "old", "firefox"),
    )
    assert auth.valid_token() == fresh
    assert spent == []
    assert SessionStore(tmp_path / "auth.json").load().refresh_token == "browser-refresh"


def test_refresh_grant_used_when_browser_is_also_stale_and_uses_browsers_newest_token(tmp_path):
    requests_seen = []
    old = jwt(-100, client_id="tokenclient")
    new = jwt()

    def post(url, data=None, headers=None, timeout=None):
        requests_seen.append((url, data))
        return FakeResponse(200, {"access_token": new, "refresh_token": "next"})

    auth = manager(tmp_path, post=post, browser=lambda source: (old, "browser-newest", source), session=Session(old, "older", "firefox"))
    assert auth.valid_token() == new
    url, data = requests_seen[0]
    assert url == "https://secure.soundcloud.com/oauth/token"
    assert data == {"client_id": "tokenclient", "grant_type": "refresh_token", "refresh_token": "browser-newest"}
    # A single-use token must be on disk before anything else can fail.
    saved = SessionStore(tmp_path / "auth.json").load()
    assert saved.access_token == new and saved.refresh_token == "next"


def test_refresh_token_is_never_spent_twice_for_one_stale_token(tmp_path):
    spent = []
    old, new = jwt(-100), jwt()

    def post(url, data=None, headers=None, timeout=None):
        spent.append(data["refresh_token"])
        return FakeResponse(200, {"access_token": new, "refresh_token": "next"})

    auth = manager(tmp_path, post=post, session=Session(old, "r1"))
    assert auth.refresh(old).access_token == new
    assert auth.refresh(old).access_token == new
    assert spent == ["r1"]


def test_refresh_picks_up_token_saved_by_another_copy_of_the_app(tmp_path):
    spent = []
    old, new = jwt(-100), jwt()
    auth = manager(tmp_path, post=lambda *a, **k: spent.append(a), session=Session(old, "r1"))
    SessionStore(tmp_path / "auth.json").save(Session(new, "r2"))
    assert auth.refresh(old).access_token == new
    assert spent == []


def test_failed_refresh_marks_session_expired_and_keeps_it_for_display(tmp_path):
    old = jwt(-100)
    auth = manager(tmp_path, post=lambda *a, **k: FakeResponse(400, {"error": "invalid_grant"}), session=Session(old, "bad"))
    assert auth.valid_token() is None
    assert auth.expired and not auth.signed_in and auth.session is not None
    with pytest.raises(SignInRequired):
        auth.refresh(old)


def test_denied_browser_access_falls_through_to_refresh_grant(tmp_path):
    new = jwt()

    def denied(source):
        raise RuntimeError("keychain denied")

    auth = manager(tmp_path, post=lambda *a, **k: FakeResponse(200, {"access_token": new}), browser=denied, session=Session(jwt(-100), "r", "chrome"))
    assert auth.valid_token() == new


def test_network_error_during_refresh_is_not_a_crash(tmp_path):
    import requests

    def post(*args, **kwargs):
        raise requests.ConnectionError("down")

    auth = manager(tmp_path, post=post, session=Session(jwt(-100), "r"))
    assert auth.valid_token() is None


def test_sign_out_removes_saved_tokens(tmp_path):
    auth = manager(tmp_path, session=Session("t"))
    auth.sign_out()
    assert auth.session is None and not (tmp_path / "auth.json").exists()


def test_cookie_file_reads_both_tokens_and_ignores_other_sites(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".soundcloud.com\tTRUE\t/\tTRUE\t2147483647\toauth_token\taccess\n"
        ".soundcloud.com\tTRUE\t/\tTRUE\t2147483647\toauth_refresh_token\trefresh\n"
        ".evil.example\tTRUE\t/\tTRUE\t2147483647\toauth_token\tstolen\n"
    )
    assert read_cookie_file(str(path)) == ("access", "refresh")


def test_cookie_file_errors_are_readable(tmp_path):
    with pytest.raises(SignInError):
        read_cookie_file(str(tmp_path / "missing.txt"))


def test_reads_tokens_from_a_real_firefox_cookie_database(tmp_path):
    """Runs yt-dlp's real cookie reader against a throwaway Firefox profile."""
    profile = tmp_path / "profile"
    profile.mkdir()
    connection = sqlite3.connect(profile / "cookies.sqlite")
    connection.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '', name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER)"
    )
    rows = [("oauth_token", "access", ".soundcloud.com"), ("oauth_refresh_token", "refresh", ".soundcloud.com"), ("oauth_token", "stolen", ".evil.example")]
    connection.executemany(
        "INSERT INTO moz_cookies (name, value, host, path, expiry, lastAccessed, creationTime, isSecure, isHttpOnly) VALUES (?, ?, ?, '/', 2147483647, 0, 0, 1, 0)",
        rows,
    )
    connection.commit()
    connection.close()
    access, refresh, source = read_browser_tokens(f"firefox:{profile}")
    assert (access, refresh) == ("access", "refresh")
    assert source == f"firefox:{profile}"


def test_detect_browsers_survives_unreadable_directories(monkeypatch):
    from pathlib import Path

    def denied(self):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(Path, "iterdir", denied)
    assert isinstance(detect_browsers(), list)
