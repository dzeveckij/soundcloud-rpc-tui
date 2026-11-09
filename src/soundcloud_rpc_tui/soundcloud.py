"""Direct client for SoundCloud's api-v2, the API its web app uses."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from .auth import Session, SessionManager, SignInRequired, read_browser_tokens, read_cookie_file, SignInError
from .core import Engagement, Settings, SoundCloudItem, atomic_write, cache_dir

API_HOST = "api-v2.soundcloud.com"
API_ROOT = f"https://{API_HOST}"
SITE_URL = "https://soundcloud.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT = (3.05, 12)
CLIENT_ID_RETRY_SECONDS = 60
CLIENT_ID_SOURCES = (
    (f"{SITE_URL}/discover", r'"hydratable":"apiClient","data":\{"id":"([^"]+)"'),
    ("https://m.soundcloud.com/", r'"clientId":"([0-9a-zA-Z]{32})"'),
)
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TRACK_BATCH_SIZE = 50
PLAYLIST_TRACK_LIMIT = 500
STREAM_PRESETS = ("aac_256k", "aac_160k", "mp3_1_0", "mp3_0_1", "mp3_0_0", "mp3_standard", "opus_0_0", "aac_96k")
SEARCH_PATHS = {
    "all": "/search",
    "tracks": "/search/tracks",
    "playlists": "/search/playlists",
    "albums": "/search/albums",
    "people": "/search/users",
}


class SoundCloudError(RuntimeError):
    """An API failure. The message is safe to show the user."""


class NotFound(SoundCloudError):
    pass


class AlreadyInPlaylist(SoundCloudError):
    pass


@dataclass(slots=True)
class Page:
    items: list[SoundCloudItem] = field(default_factory=list)
    next_href: str = ""
    keep: Callable[[dict], bool] | None = None


def sized_artwork(url: str, size: str) -> str:
    return url.replace("-large.", f"-{size}.") if url else ""


def _user_fields(user: dict) -> dict[str, str]:
    return {
        "author": str(user.get("username") or ""),
        "user_id": str(user.get("id") or ""),
        "user_url": str(user.get("permalink_url") or ""),
    }


def _parse_track(raw: dict) -> SoundCloudItem:
    user = raw.get("user") or {}
    return SoundCloudItem(
        kind="track",
        item_id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        url=str(raw.get("permalink_url") or ""),
        duration=round((raw.get("duration") or 0) / 1000),
        artwork=str(raw.get("artwork_url") or user.get("avatar_url") or ""),
        urn=str(raw.get("urn") or ""),
        plays=int(raw.get("playback_count") or 0),
        likes=int(raw.get("likes_count") or 0),
        reposts=int(raw.get("reposts_count") or 0),
        genre=str(raw.get("genre") or ""),
        created_at=str(raw.get("created_at") or ""),
        waveform_url=str(raw.get("waveform_url") or ""),
        policy=str(raw.get("policy") or ""),
        track_authorization=str(raw.get("track_authorization") or ""),
        transcodings=list((raw.get("media") or {}).get("transcodings") or []),
        sharing=str(raw.get("sharing") or ""),
        is_stub="title" not in raw,
        **_user_fields(user),
    )


def _parse_collection(raw: dict) -> SoundCloudItem:
    user = raw.get("user") or {}
    is_system = raw.get("kind") == "system-playlist"
    kind = "system-playlist" if is_system else ("album" if raw.get("is_album") else "playlist")
    tracks = raw.get("tracks") or []
    return SoundCloudItem(
        kind=kind,
        item_id=str(raw.get("urn") if is_system else raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        url=str(raw.get("permalink_url") or ""),
        duration=round((raw.get("duration") or 0) / 1000),
        artwork=str(raw.get("artwork_url") or raw.get("calculated_artwork_url") or user.get("avatar_url") or ""),
        urn=str(raw.get("urn") or ""),
        likes=int(raw.get("likes_count") or 0),
        reposts=int(raw.get("reposts_count") or 0),
        genre=str(raw.get("genre") or ""),
        created_at=str(raw.get("created_at") or ""),
        track_count=int(raw.get("track_count") or len(tracks)),
        sharing=str(raw.get("sharing") or ""),
        **_user_fields(user),
    )


def _parse_user(raw: dict) -> SoundCloudItem:
    return SoundCloudItem(
        kind="user",
        item_id=str(raw.get("id") or ""),
        title=str(raw.get("username") or ""),
        author=str(raw.get("city") or raw.get("country_code") or ""),
        url=str(raw.get("permalink_url") or ""),
        artwork=str(raw.get("avatar_url") or ""),
        urn=str(raw.get("urn") or ""),
        user_id=str(raw.get("id") or ""),
        user_url=str(raw.get("permalink_url") or ""),
        followers=int(raw.get("followers_count") or 0),
        track_count=int(raw.get("track_count") or 0),
    )


def parse_item(raw: Any) -> SoundCloudItem | None:
    """Parse any API row: a resource, or a wrapper such as a like or feed entry."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "track":
        return _parse_track(raw)
    if kind in ("playlist", "system-playlist"):
        return _parse_collection(raw)
    if kind == "user":
        return _parse_user(raw)
    for key in ("track", "playlist", "system_playlist"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            item = parse_item(nested)
            if item is not None and raw.get("type") in ("playlist", "album"):
                item.owned = True
            return item
    if isinstance(raw.get("user"), dict) and "kind" not in raw:
        return parse_item(raw["user"])
    return None


def _stream_rank(transcoding: dict) -> tuple[int, int]:
    preset = str(transcoding.get("preset") or "")
    position = STREAM_PRESETS.index(preset) if preset in STREAM_PRESETS else len(STREAM_PRESETS)
    return (1 if transcoding.get("snipped") else 0, position)


def playable_transcodings(item: SoundCloudItem) -> list[dict]:
    """Transcodings a plain media player can open, best first. Skips DRM variants."""
    usable = [
        transcoding
        for transcoding in item.transcodings
        if (transcoding.get("format") or {}).get("protocol") in ("hls", "progressive") and transcoding.get("url")
    ]
    return sorted(usable, key=_stream_rank)


class SoundCloudClient:
    def __init__(
        self,
        settings: Settings,
        auth: SessionManager | None = None,
        http: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth or SessionManager()
        self.auth.client_id_provider = self.client_id
        self.http = http or self._build_http()
        self._client_id = ""
        self._app_version = ""
        self._client_id_at = 0.0
        self._client_lock = threading.Lock()
        self._public_user_id = ""
        self._public_user_source = ""

    @staticmethod
    def _build_http() -> requests.Session:
        http = requests.Session()
        http.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": SITE_URL,
                "Referer": f"{SITE_URL}/",
            }
        )
        adapter = HTTPAdapter(pool_connections=6, pool_maxsize=12)
        http.mount("https://", adapter)
        return http

    # Client ID

    def _client_id_cache(self):
        return cache_dir() / "client.json"

    def client_id(self) -> str:
        """The API client ID, reused for as long as the API keeps accepting it."""
        if self._client_id:
            return self._client_id
        with self._client_lock:
            if self._client_id:
                return self._client_id
            try:
                cached = json.loads(self._client_id_cache().read_text(encoding="utf-8"))
                if cached.get("client_id"):
                    self._client_id = str(cached["client_id"])
                    self._app_version = str(cached.get("app_version") or "")
                    return self._client_id
            except (OSError, ValueError, AttributeError):
                pass
            return self._fetch_client_id()

    def _fetch_client_id(self) -> str:
        """Find a client ID. The main site can answer scripts with a bot challenge,
        so this only runs when there's no working ID, and tries the mobile site next."""
        self._client_id_at = time.time()
        reached_soundcloud = False
        for url, pattern in CLIENT_ID_SOURCES:
            try:
                response = self.http.get(url, headers=PAGE_HEADERS, timeout=TIMEOUT)
            except requests.RequestException:
                continue
            reached_soundcloud = True
            match = re.search(pattern, response.text) if response.status_code == 200 else None
            if match:
                version = re.search(r'__sc_version\s*=\s*"(\d+)"', response.text)
                self._store_client_id(match.group(1), version.group(1) if version else "")
                return self._client_id
        if not reached_soundcloud:
            raise SoundCloudError("Can't reach SoundCloud. Check your connection, then try again.")
        raise SoundCloudError("SoundCloud isn't accepting this app right now. Try again later, or update the app.")

    def _store_client_id(self, client_id: str, app_version: str) -> None:
        self._client_id = client_id
        self._app_version = app_version
        try:
            atomic_write(self._client_id_cache(), json.dumps({"client_id": client_id, "app_version": app_version}))
        except OSError:
            pass  # The cache only saves a request at the next launch.

    def _refresh_client_id(self) -> None:
        with self._client_lock:
            if time.time() - self._client_id_at < CLIENT_ID_RETRY_SECONDS:
                return
            try:
                self._fetch_client_id()
            except SoundCloudError:
                pass  # Keep the current ID. The caller reports the original failure.

    def warm_up(self) -> None:
        """Fetch the client ID and open the connection so the first request is fast."""
        self.client_id()
        try:
            self.http.head(API_ROOT, timeout=TIMEOUT)
        except requests.RequestException:
            pass

    # HTTP

    def _build_request(self, url: str, params: dict | None) -> tuple[str, dict]:
        if url.startswith("/"):
            url = API_ROOT + url
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != API_HOST:
            raise SoundCloudError(f"Refusing to call unexpected host {parts.netloc}")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(params or {})
        # A token only works with the client it was issued to, so prefer that client.
        session = self.auth.session
        claim = session.client_id_claim if session is not None and self.auth.signed_in else ""
        query["client_id"] = claim or self.client_id()
        query.setdefault("app_locale", "en")
        if self._app_version:
            query.setdefault("app_version", self._app_version)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), query

    def _request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        body: Any = None,
        auth_required: bool = False,
    ) -> Any:
        tried_auth = False
        tried_client = False
        tried_network = False
        for attempt in range(5):
            target, query = self._build_request(url, params)
            token = self.auth.valid_token()
            if auth_required and not token:
                raise SignInRequired("Sign in to use this.")
            headers = {"Authorization": f"OAuth {token}"} if token else {}
            if body is not None:
                headers["Content-Type"] = "application/json"

            try:
                response = self.http.request(
                    method,
                    target,
                    params=query,
                    data=json.dumps(body) if body is not None else None,
                    headers=headers,
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                if method == "GET" and not tried_network:
                    tried_network = True
                    continue
                raise SoundCloudError("Can't reach SoundCloud. Check your connection, then try again.") from exc

            status = response.status_code
            if status == 401 and token and not tried_auth:
                tried_auth = True
                try:
                    self.auth.refresh(token)
                except SignInRequired:
                    if auth_required:
                        raise
                continue
            if status == 401 and auth_required:
                raise SignInRequired("Your SoundCloud session expired. Sign in again.")
            if status in (401, 403) and not tried_client and (method == "GET" or status == 401):
                tried_client = True
                self._refresh_client_id()
                continue
            if status == 429 and method == "GET":
                delay = response.headers.get("Retry-After", "")
                time.sleep(min(float(delay) if delay.isdigit() else 1 + attempt, 5))
                continue
            if status >= 500 and method == "GET" and attempt < 2:
                time.sleep(0.3 * (attempt + 1))
                continue
            if status == 404:
                raise NotFound("SoundCloud couldn't find that.")
            if status == 403:
                raise SoundCloudError("SoundCloud refused that request. Your account may not have access.")
            if status == 429:
                raise SoundCloudError("SoundCloud is rate limiting requests. Wait a moment, then try again.")
            if status >= 400:
                raise SoundCloudError(f"SoundCloud returned an error ({status}). Try again.")
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {}
        raise SoundCloudError("SoundCloud is not responding. Try again in a moment.")

    def _page(
        self,
        url: str,
        params: dict | None = None,
        auth_required: bool = False,
        keep: Callable[[dict], bool] | None = None,
    ) -> Page:
        data = self._request("GET", url, params=params, auth_required=auth_required)
        rows = data.get("collection") or []
        items = [parse_item(row) for row in rows if keep is None or keep(row)]
        return Page([item for item in items if item is not None], str(data.get("next_href") or ""), keep)

    def next_page(self, page: Page) -> Page:
        if not page.next_href:
            return Page()
        return self._page(page.next_href, keep=page.keep)

    # Browsing

    def search(self, query: str, scope: str = "all", limit: int = 30) -> Page:
        query = query.strip()
        if not query:
            return Page()
        return self._page(SEARCH_PATHS.get(scope, "/search"), {"q": query, "limit": limit})

    def trending(self, limit: int = 50) -> Page:
        return self._page("/charts", {"kind": "trending", "genre": "soundcloud:genres:all-music", "limit": limit})

    def discover(self) -> Page:
        data = self._request("GET", "/mixed-selections", {"limit": 10})
        items: list[SoundCloudItem] = []
        seen: set[str] = set()
        for selection in data.get("collection") or []:
            for raw in (selection.get("items") or {}).get("collection") or []:
                item = parse_item(raw)
                if item is not None and item.key not in seen:
                    seen.add(item.key)
                    items.append(item)
        return Page(items)

    def feed(self, limit: int = 40) -> Page:
        return self._page("/stream", {"limit": limit}, auth_required=True)

    def history(self, limit: int = 50) -> Page:
        return self._page("/me/play-history/tracks", {"limit": limit}, auth_required=True)

    def _library(self, types: tuple[str, ...]) -> Page:
        return self._page(
            "/me/library/all",
            {"limit": 50},
            auth_required=True,
            keep=lambda row: row.get("type") in types,
        )

    def _profile_user_id(self) -> str:
        """The account ID to browse: the signed-in user, else the configured profile."""
        if self.auth.signed_in and self.auth.session and self.auth.session.user_id:
            return self.auth.session.user_id
        username = self.settings.soundcloud_username.strip()
        if not username:
            raise SignInRequired("Sign in, or set a public profile in settings, to see this.")
        if username != self._public_user_source:
            url = username if username.startswith("http") else f"{SITE_URL}/{username.strip('/')}"
            data = self._request("GET", "/resolve", {"url": url})
            self._public_user_id = str(data.get("id") or "")
            self._public_user_source = username
        if not self._public_user_id:
            raise NotFound("SoundCloud couldn't find that profile. Check it in settings.")
        return self._public_user_id

    def likes(self, limit: int = 50) -> Page:
        return self._page(f"/users/{self._profile_user_id()}/track_likes", {"limit": limit})

    def user_tracks(self, user_id: str | None = None, limit: int = 50) -> Page:
        return self._page(f"/users/{user_id or self._profile_user_id()}/tracks", {"limit": limit})

    def playlists(self, limit: int = 50) -> Page:
        if self.auth.signed_in:
            return self._library(("playlist", "playlist-like", "system-playlist-like"))
        return self._page(f"/users/{self._profile_user_id()}/playlists_without_albums", {"limit": limit})

    def albums(self, limit: int = 50) -> Page:
        if self.auth.signed_in:
            return self._library(("album", "album-like"))
        return self._page(f"/users/{self._profile_user_id()}/albums", {"limit": limit})

    def followings(self, limit: int = 50) -> Page:
        return self._page(f"/users/{self._profile_user_id()}/followings", {"limit": limit})

    def related(self, track_id: str, limit: int = 50) -> Page:
        return self._page(f"/tracks/{track_id}/related", {"limit": limit})

    def collection_tracks(self, item: SoundCloudItem) -> Page:
        """All tracks in a playlist, album, or system playlist, in order."""
        path = f"/system-playlists/{item.item_id}" if item.kind == "system-playlist" else f"/playlists/{item.item_id}"
        data = self._request("GET", path)
        tracks = [parsed for parsed in (parse_item(raw) for raw in data.get("tracks") or []) if parsed]
        return Page(self._hydrate(tracks))

    def _hydrate(self, tracks: list[SoundCloudItem]) -> list[SoundCloudItem]:
        """Playlists return only stubs after the first few tracks. Fill them in, keeping order."""
        missing = [track.item_id for track in tracks if track.is_stub]
        if not missing:
            return tracks
        batches = [missing[start : start + TRACK_BATCH_SIZE] for start in range(0, len(missing), TRACK_BATCH_SIZE)]

        def fetch(batch: list[str]) -> list[Any]:
            return self._request("GET", "/tracks", {"ids": ",".join(batch)}) or []

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch, batches))
        full: dict[str, SoundCloudItem] = {}
        for rows in results:
            for raw in rows if isinstance(rows, list) else []:
                parsed = parse_item(raw)
                if parsed is not None:
                    full[parsed.item_id] = parsed
        # A stub missing from the response is a track that is no longer available.
        return [full[track.item_id] if track.is_stub else track for track in tracks if not track.is_stub or track.item_id in full]

    def resolve(self, url: str) -> SoundCloudItem:
        url = url.strip()
        if not url.startswith("http"):
            url = f"https://{url}"
        host = urlsplit(url).netloc
        if host not in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"):
            raise SoundCloudError("That isn't a SoundCloud link.")
        if host == "on.soundcloud.com":
            try:
                url = self.http.head(url, allow_redirects=True, timeout=TIMEOUT).url
            except requests.RequestException as exc:
                raise SoundCloudError("Can't reach SoundCloud. Check your connection, then try again.") from exc
        item = parse_item(self._request("GET", "/resolve", {"url": url}))
        if item is None:
            raise NotFound("SoundCloud couldn't open that link.")
        return item

    # Playback

    def stream_url(self, item: SoundCloudItem) -> str:
        if item.is_stub or not item.transcodings:
            fresh = parse_item(self._request("GET", f"/tracks/{item.item_id}"))
            if fresh is not None:
                item.transcodings = fresh.transcodings
                item.track_authorization = fresh.track_authorization
                item.policy = fresh.policy
        if item.is_blocked:
            raise SoundCloudError("This track isn't available in your region.")

        last_error: Exception | None = None
        for transcoding in playable_transcodings(item):
            params = {"track_authorization": item.track_authorization} if item.track_authorization else None
            try:
                url = str(self._request("GET", transcoding["url"], params).get("url") or "")
            except SoundCloudError as exc:
                last_error = exc
                continue
            if url:
                return url
        try:
            return self._stream_url_with_ytdlp(item)
        except Exception as exc:
            raise SoundCloudError(
                "This track can't be played here. It may be protected or need SoundCloud Go+."
            ) from (last_error or exc)

    def _stream_url_with_ytdlp(self, item: SoundCloudItem) -> str:
        import yt_dlp

        options: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True, "format": "bestaudio/best"}
        token = self.auth.valid_token()
        if token:
            options.update(username="oauth", password=token)
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(item.url, download=False)
        url = str((info or {}).get("url") or "")
        if not url:
            raise SoundCloudError("No playable stream")
        return url

    def record_play(self, item: SoundCloudItem) -> None:
        """Add a track to the account's listening history, as the web player does."""
        if self.auth.signed_in and item.urn:
            self._request("POST", "/me/play-history", body={"track_urn": item.urn}, auth_required=True)

    def waveform(self, item: SoundCloudItem) -> list[int]:
        if not item.waveform_url.endswith(".json"):
            return []
        try:
            response = self.http.get(item.waveform_url, timeout=TIMEOUT)
            response.raise_for_status()
            return [int(sample) for sample in response.json().get("samples") or []]
        except (requests.RequestException, ValueError, TypeError):
            return []

    def artwork_bytes(self, url: str) -> bytes:
        if not url:
            return b""
        try:
            response = self.http.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            return response.content
        except requests.RequestException:
            return b""

    # Sign-in

    def me(self) -> dict:
        return self._request("GET", "/me", auth_required=True)

    def _adopt_validated(self, access: str, refresh: str, source: str) -> Session:
        previous, previous_expired = self.auth.session, self.auth.expired
        session = Session(access_token=access, refresh_token=refresh, source=source)
        self.auth.session = session
        self.auth.expired = False
        self.auth.validating = True
        try:
            profile = self.me()
        except (SignInRequired, SoundCloudError) as exc:
            self.auth.session, self.auth.expired = previous, previous_expired
            raise SignInError("SoundCloud rejected that session. Sign in again, then retry.") from exc
        finally:
            self.auth.validating = False
        current = self.auth.session or session
        current.user_id = str(profile.get("id") or "")
        current.username = str(profile.get("username") or "")
        current.profile_url = str(profile.get("permalink_url") or "")
        self.auth.adopt(current)
        return current

    def sign_in_with_browser(self, source: str) -> Session:
        try:
            access, refresh, remembered = read_browser_tokens(source)
        except Exception as exc:
            raise SignInError(_browser_error_message(source, exc)) from exc
        if not access:
            raise SignInError(
                "That browser isn't signed in to SoundCloud. Sign in on soundcloud.com, then import again."
            )
        session = self._adopt_validated(access, refresh, remembered)
        self.settings.soundcloud_cookies_from_browser = remembered
        return session

    def sign_in_with_cookie_file(self, path: str) -> Session:
        access, refresh = read_cookie_file(path)
        if not access:
            raise SignInError("That cookies.txt file has no SoundCloud session in it.")
        return self._adopt_validated(access, refresh, f"file:{path}")

    def sign_in_with_token(self, access: str, refresh: str = "") -> Session:
        return self._adopt_validated(access.strip(), refresh.strip(), "")

    def restore_session(self) -> Session | None:
        """Validate the saved session at launch, or import from a configured source."""
        if self.auth.session is None:
            if self.settings.soundcloud_cookie_file:
                return self.sign_in_with_cookie_file(self.settings.soundcloud_cookie_file)
            if self.settings.soundcloud_cookies_from_browser:
                return self.sign_in_with_browser(self.settings.soundcloud_cookies_from_browser)
            return None
        profile = self.me()
        session = self.auth.session
        if session is not None:
            session.user_id = str(profile.get("id") or session.user_id)
            session.username = str(profile.get("username") or session.username)
            session.profile_url = str(profile.get("permalink_url") or session.profile_url)
            self.auth.adopt(session)
        return session

    # Account state

    def _ids(self, path: str) -> set[str]:
        found: set[str] = set()
        url = path
        params: dict | None = {"limit": 5000}
        for _ in range(20):
            data = self._request("GET", url, params, auth_required=True)
            found.update(str(value) for value in data.get("collection") or [])
            url = str(data.get("next_href") or "")
            params = None
            if not url:
                break
        return found

    def engagement(self) -> Engagement:
        session = self.auth.session
        if session is None:
            return Engagement()
        paths = {
            "liked_tracks": "/me/track_likes/ids",
            "liked_playlists": "/me/playlist_likes/ids",
            "reposted_tracks": "/me/track_reposts/ids",
            "reposted_playlists": "/me/playlist_reposts/ids",
            "followings": f"/users/{session.user_id}/followings/ids",
        }

        def fetch(path: str) -> set[str]:
            try:
                return self._ids(path)
            except SoundCloudError:
                return set()

        with ThreadPoolExecutor(max_workers=len(paths)) as pool:
            results = list(pool.map(fetch, paths.values()))
        return Engagement(**dict(zip(paths, results)))

    def _my_id(self) -> str:
        session = self.auth.session
        if not self.auth.signed_in or session is None:
            raise SignInRequired("Sign in to use this.")
        if not session.user_id:
            session.user_id = str(self.me().get("id") or "")
        return session.user_id

    def set_liked(self, item: SoundCloudItem, liked: bool) -> None:
        method = "PUT" if liked else "DELETE"
        me = self._my_id()
        if item.is_track:
            path = f"/users/{me}/track_likes/{item.item_id}"
        elif item.kind == "system-playlist":
            path = f"/users/{me}/system_playlist_likes/{item.item_id}"
        else:
            path = f"/users/{me}/playlist_likes/{item.item_id}"
        self._request(method, path, auth_required=True)

    def set_reposted(self, item: SoundCloudItem, reposted: bool) -> None:
        kind = "track_reposts" if item.is_track else "playlist_reposts"
        self._my_id()
        self._request("PUT" if reposted else "DELETE", f"/me/{kind}/{item.item_id}", auth_required=True)

    def set_following(self, user: SoundCloudItem, following: bool) -> None:
        self._my_id()
        self._request("POST" if following else "DELETE", f"/me/followings/{user.item_id}", auth_required=True)

    # Playlists

    def my_playlists(self, max_pages: int = 10) -> list[SoundCloudItem]:
        """Playlists the account can edit, including private ones."""
        me = self._my_id()
        page = self._library(("playlist",))
        items = list(page.items)
        for _ in range(max_pages - 1):
            if not page.next_href:
                break
            page = self.next_page(page)
            items.extend(page.items)
        return [item for item in items if not item.user_id or item.user_id == me]

    def create_playlist(self, title: str, tracks: Iterable[str], private: bool = True) -> SoundCloudItem:
        body = {
            "playlist": {
                "title": title,
                "sharing": "private" if private else "public",
                "tracks": [int(track_id) for track_id in tracks],
                "_resource_type": "playlist",
            }
        }
        item = parse_item(self._request("POST", "/playlists", body=body, auth_required=True))
        if item is None:
            raise SoundCloudError("SoundCloud didn't confirm the new playlist. Check your playlists.")
        item.owned = True
        return item

    def _playlist_track_ids(self, playlist_id: str) -> list[int]:
        data = self._request("GET", f"/playlists/{playlist_id}", auth_required=True)
        ids = [int(raw["id"]) for raw in data.get("tracks") or [] if raw.get("id")]
        expected = int(data.get("track_count") or 0)
        if expected > len(ids):
            # Saving replaces the whole list, so a short read would delete tracks.
            raise SoundCloudError("SoundCloud returned an incomplete playlist, so nothing was changed. Try again.")
        return ids

    def _save_playlist_tracks(self, playlist_id: str, ids: list[int]) -> None:
        self._request("PUT", f"/playlists/{playlist_id}", body={"playlist": {"tracks": ids}}, auth_required=True)

    def add_to_playlist(self, playlist_id: str, track_id: str) -> None:
        ids = self._playlist_track_ids(playlist_id)
        if int(track_id) in ids:
            raise AlreadyInPlaylist("That track is already in this playlist.")
        if len(ids) >= PLAYLIST_TRACK_LIMIT:
            raise SoundCloudError(f"Playlists can hold up to {PLAYLIST_TRACK_LIMIT} tracks.")
        self._save_playlist_tracks(playlist_id, [*ids, int(track_id)])

    def remove_from_playlist(self, playlist_id: str, track_id: str) -> None:
        ids = self._playlist_track_ids(playlist_id)
        self._save_playlist_tracks(playlist_id, [existing for existing in ids if existing != int(track_id)])

    def delete_playlist(self, playlist_id: str) -> None:
        self._request("DELETE", f"/playlists/{playlist_id}", auth_required=True)


def _browser_error_message(source: str, exc: Exception) -> str:
    text = str(exc).lower()
    if source.startswith("safari") or "operation not permitted" in text or "permission" in text:
        return (
            "macOS blocked access to Safari's cookies. Give your terminal Full Disk Access in "
            "System Settings, or import from another browser."
        )
    if "keyring" in text or "keychain" in text or "safe storage" in text or "decrypt" in text:
        return "Could not unlock that browser's cookies. Allow Keychain access, or paste a token instead."
    if "could not find" in text:
        return "No cookies found for that browser. Sign in on soundcloud.com first, or choose another browser."
    return f"Could not read that browser's cookies: {exc}"
