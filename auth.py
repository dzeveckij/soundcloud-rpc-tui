"""SoundCloud sign-in: browser session import, token storage, and refresh.

SoundCloud's web sessions are a short-lived `oauth_token` plus a single-use
`oauth_refresh_token`. Both live in cookies on soundcloud.com. Because a refresh
token can only be spent once, the browser and this app share one chain:

* Re-reading the browser comes first, since that costs the browser nothing.
* The refresh grant is the fallback. It replaces the browser's token, so the
  browser may have to sign in again the next time it tries to refresh.
* A new refresh token is written to disk before it is used for anything else.
"""

from __future__ import annotations

import base64
import binascii
import http.cookiejar
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

import requests

from core import atomic_write, config_dir

TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
ACCESS_COOKIE = "oauth_token"
REFRESH_COOKIE = "oauth_refresh_token"
EXPIRY_MARGIN_SECONDS = 60


class SignInRequired(RuntimeError):
    """The action needs a signed-in account and there is no usable session."""


class SignInError(RuntimeError):
    """A sign-in attempt failed. The message is safe to show the user."""


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read JWT claims without verifying them. Older tokens are not JWTs."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


@dataclass(slots=True)
class Session:
    access_token: str
    refresh_token: str = ""
    source: str = ""
    user_id: str = ""
    username: str = ""
    profile_url: str = ""

    @property
    def expires_at(self) -> float | None:
        exp = decode_jwt_claims(self.access_token).get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None

    @property
    def client_id_claim(self) -> str:
        return str(decode_jwt_claims(self.access_token).get("client_id") or "")

    def is_expired(self, margin: float = EXPIRY_MARGIN_SECONDS) -> bool:
        expires_at = self.expires_at
        return expires_at is not None and time.time() >= expires_at - margin


class SessionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "auth.json"

    def load(self) -> Session | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        allowed = {field_.name for field_ in fields(Session)}
        return Session(**{key: str(value) for key, value in data.items() if key in allowed})

    def save(self, session: Session) -> None:
        atomic_write(self.path, json.dumps(asdict(session), indent=2), mode=0o600)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _app_support_roots() -> tuple[Path, Path, Path]:
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support"
        return root, root, root
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return local, roaming, local
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return config, home / ".mozilla", config


def _chromium_roots() -> list[tuple[str, str, Path]]:
    root, _, _ = _app_support_roots()
    if sys.platform == "darwin":
        return [
            ("Chrome", "chrome", root / "Google" / "Chrome"),
            ("Chrome Beta", "chrome", root / "Google" / "Chrome Beta"),
            ("Chrome Dev", "chrome", root / "Google" / "Chrome Dev"),
            ("Brave", "brave", root / "BraveSoftware" / "Brave-Browser"),
            ("Edge", "edge", root / "Microsoft Edge"),
            ("Chromium", "chromium", root / "Chromium"),
            ("Vivaldi", "vivaldi", root / "Vivaldi"),
            ("Opera", "opera", root / "com.operasoftware.Opera"),
        ]
    if os.name == "nt":
        return [
            ("Chrome", "chrome", root / "Google" / "Chrome" / "User Data"),
            ("Brave", "brave", root / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("Edge", "edge", root / "Microsoft" / "Edge" / "User Data"),
            ("Chromium", "chromium", root / "Chromium" / "User Data"),
            ("Vivaldi", "vivaldi", root / "Vivaldi" / "User Data"),
        ]
    return [
        ("Chrome", "chrome", root / "google-chrome"),
        ("Chrome Beta", "chrome", root / "google-chrome-beta"),
        ("Brave", "brave", root / "BraveSoftware" / "Brave-Browser"),
        ("Edge", "edge", root / "microsoft-edge"),
        ("Chromium", "chromium", root / "chromium"),
        ("Vivaldi", "vivaldi", root / "vivaldi"),
        ("Opera", "opera", root / "opera"),
    ]


def _firefox_profile_root() -> Path:
    _, firefox_root, _ = _app_support_roots()
    if sys.platform == "darwin":
        return firefox_root / "Firefox" / "Profiles"
    if os.name == "nt":
        return firefox_root / "Mozilla" / "Firefox" / "Profiles"
    return firefox_root / "firefox"


def _subdirectories(root: Path) -> list[Path]:
    """List a directory, returning nothing if the OS refuses to show it."""
    try:
        return list(root.iterdir())
    except OSError:
        return []


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _chromium_profiles(root: Path) -> list[Path]:
    """Every profile under a Chromium user-data directory, most recently used first."""
    found: list[tuple[float, Path]] = []
    for profile in _subdirectories(root):
        for cookies in (profile / "Cookies", profile / "Network" / "Cookies"):
            modified = _mtime(cookies)
            if modified is not None:
                found.append((modified, profile))
                break
    return [profile for _, profile in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _firefox_profiles(root: Path) -> list[Path]:
    found: list[tuple[float, Path]] = []
    for profile in _subdirectories(root):
        modified = _mtime(profile / "cookies.sqlite")
        if modified is not None:
            found.append((modified, profile))
    return [profile for _, profile in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _is_visible(path: Path) -> bool:
    """True when a path exists, including when the OS won't let us look inside it."""
    try:
        return path.exists()
    except OSError:
        return True


def detect_browsers() -> list[tuple[str, str]]:
    """Browsers with cookie stores on this machine, as (label, source) pairs."""
    browsers: list[tuple[str, str]] = []
    if _is_visible(_firefox_profile_root()):
        browsers.append(("Firefox", "firefox"))
    for label, browser, root in _chromium_roots():
        if _is_visible(root):
            browsers.append((label, f"{browser}|{root}"))
    if sys.platform == "darwin":
        browsers.append(("Safari", "safari"))
    return browsers


def _tokens_from_cookies(cookies: Any) -> tuple[str, str]:
    access, refresh = "", ""
    for cookie in cookies:
        if not (cookie.domain or "").lstrip(".").endswith("soundcloud.com"):
            continue
        if cookie.name == ACCESS_COOKIE:
            access = cookie.value or ""
        elif cookie.name == REFRESH_COOKIE:
            refresh = cookie.value or ""
    return access, refresh


def _read_profile(browser: str, profile: str | None) -> tuple[str, str]:
    from yt_dlp.cookies import extract_cookies_from_browser

    return _tokens_from_cookies(extract_cookies_from_browser(browser, profile))


def read_browser_tokens(source: str) -> tuple[str, str, str]:
    """Read tokens from a browser. Returns (access, refresh, source to remember).

    `source` is one of `firefox`, `safari`, `<browser>:<profile>`, or
    `<browser>|<user-data dir>`, which searches each profile in that directory.
    """
    if "|" in source:
        browser, root = source.split("|", 1)
        profiles = _chromium_profiles(Path(root))
    elif source == "firefox":
        browser = "firefox"
        profiles = _firefox_profiles(_firefox_profile_root())
    elif ":" in source:
        browser, profile = source.split(":", 1)
        access, refresh = _read_profile(browser, profile)
        return access, refresh, source
    else:
        access, refresh = _read_profile(source, None)
        return access, refresh, source

    if not profiles:
        # Listing failed or found nothing. Let yt-dlp report what's really wrong.
        access, refresh = _read_profile(browser, None)
        return access, refresh, browser

    last_error: Exception | None = None
    for profile_path in profiles:
        try:
            access, refresh = _read_profile(browser, str(profile_path))
        except Exception as exc:
            last_error = exc
            continue
        if access:
            return access, refresh, f"{browser}:{profile_path}"
    if last_error is not None:
        raise last_error
    return "", "", source


def read_cookie_file(path: str) -> tuple[str, str]:
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(os.path.expanduser(path), ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise SignInError(f"Could not read that cookies.txt file: {exc}") from exc
    return _tokens_from_cookies(jar)


class SessionManager:
    """Holds the current session and keeps it fresh. Safe to call from any thread."""

    def __init__(
        self,
        store: SessionStore | None = None,
        http_post: Callable[..., requests.Response] | None = None,
        browser_reader: Callable[[str], tuple[str, str, str]] = read_browser_tokens,
    ) -> None:
        self.store = store or SessionStore()
        self.session: Session | None = self.store.load()
        self.expired = False
        self._post = http_post or requests.post
        self._read_browser = browser_reader
        self._lock = threading.RLock()
        self.client_id_provider: Callable[[], str] | None = None
        self.validating = False

    @property
    def signed_in(self) -> bool:
        return self.session is not None and not self.expired

    def valid_token(self) -> str | None:
        """The access token to send, refreshing first if it is about to expire."""
        session = self.session
        if session is None or self.expired:
            return None
        if session.is_expired():
            try:
                return self.refresh(session.access_token).access_token
            except SignInRequired:
                return None
        return session.access_token

    def adopt(self, session: Session) -> None:
        with self._lock:
            self.session = session
            self.expired = False
            self.store.save(session)

    def sign_out(self) -> None:
        with self._lock:
            self.session = None
            self.expired = False
            self.store.clear()

    def refresh(self, stale_token: str) -> Session:
        """Replace `stale_token`. Raises SignInRequired when nothing works."""
        with self._lock:
            session = self.session
            if session is None:
                raise SignInRequired("Sign in to continue")
            if session.access_token != stale_token and not session.is_expired():
                return session  # Another thread already refreshed it.

            # While checking a new sign-in, the saved session may belong to someone else.
            on_disk = None if self.validating else self.store.load()
            if on_disk and on_disk.access_token != stale_token and not on_disk.is_expired():
                self.session = on_disk  # Another running copy of the app refreshed it.
                return on_disk

            if session.source:
                renewed = self._renew_from_source(session, stale_token)
                if renewed is not None:
                    return renewed

            if session.refresh_token:
                renewed = self._spend_refresh_token(session)
                if renewed is not None:
                    return renewed

            self.expired = True
            raise SignInRequired("Your SoundCloud session expired. Sign in again.")

    def _renew_from_source(self, session: Session, stale_token: str) -> Session | None:
        try:
            if session.source.startswith("file:"):
                access, refresh = read_cookie_file(session.source.removeprefix("file:"))
            else:
                access, refresh, _ = self._read_browser(session.source)
        except Exception:
            return None  # A denied Keychain prompt or locked database is not fatal.
        if refresh:
            session.refresh_token = refresh
        if not access or access == stale_token:
            return None
        candidate = Session(
            access_token=access,
            refresh_token=refresh or session.refresh_token,
            source=session.source,
            user_id=session.user_id,
            username=session.username,
            profile_url=session.profile_url,
        )
        if candidate.is_expired():
            return None
        self.adopt(candidate)
        return candidate

    def _spend_refresh_token(self, session: Session) -> Session | None:
        try:
            client_id = session.client_id_claim or (self.client_id_provider() if self.client_id_provider else "")
        except RuntimeError:
            return None
        if not client_id:
            return None
        try:
            response = self._post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": session.refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=15,
            )
            if response.status_code != 200:
                return None
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None
        access = payload.get("access_token")
        if not access:
            return None
        renewed = Session(
            access_token=str(access),
            refresh_token=str(payload.get("refresh_token") or ""),
            source=session.source,
            user_id=session.user_id,
            username=session.username,
            profile_url=session.profile_url,
        )
        self.adopt(renewed)
        return renewed
