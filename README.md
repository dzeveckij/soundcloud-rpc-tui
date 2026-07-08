# SoundCloud TUI

Terminal SoundCloud app with browsing, playback, Discord Rich Presence, and Last.fm scrobbling.

The app is built with Textual, uses `yt-dlp` for SoundCloud metadata and streams, and plays audio through `ffplay`.

## Features

- Browse SoundCloud charts, search results, likes, playlists, and user tracks
- Paste a SoundCloud track or playlist URL to play or browse it
- Play tracks from the terminal with pause, resume, queue advance, and repeat-one
- Show the current track in Discord Rich Presence
- Optionally scrobble and update now-playing status on Last.fm
- Use cookies from a `cookies.txt` file or a supported browser for private or account-specific SoundCloud content
- Persist settings under the XDG config directory

## Requirements

- Python 3.10 or newer
- `ffplay`, usually provided by FFmpeg
- SoundCloud access through public metadata, browser cookies, or a `cookies.txt` export

Install `ffplay` with your system package manager. For example:

```bash
sudo apt install ffmpeg
```

## Install

From this repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

You can also install the dependencies directly:

```bash
python -m pip install -r requirements.txt
```

## Run

After an editable install:

```bash
soundcloud-rpc-tui
```

Or run the app module directly from the checkout:

```bash
python app.py
```

## Controls

| Key | Action |
| --- | --- |
| `/` | Search or enter a SoundCloud URL |
| `c` | Load charts |
| `l` | Load likes |
| `p` | Load playlists |
| `t` | Load your tracks |
| `g` | Load recommendations |
| `enter` | Play the selected item, or open a selected playlist |
| `space` | Pause or resume playback |
| `y` | Toggle repeat-one |
| `o` | Open the current track in the browser when browser opening is enabled |
| `s` | Open settings |
| `r` | Refresh the current view |
| `q` | Quit |

## Settings

Open settings with `s`. Settings are saved to:

```text
~/.config/soundcloud-rpc-tui/settings.json
```

If `XDG_CONFIG_HOME` is set, the file is saved under:

```text
$XDG_CONFIG_HOME/soundcloud-rpc-tui/settings.json
```

Useful settings include:

- SoundCloud username or profile URL for likes, playlists, and tracks
- `cookies.txt` path for authenticated SoundCloud requests
- Browser cookie source such as `firefox`, `chrome`, or `chromium`
- Charts region
- Default search
- Discord Rich Presence toggles
- Last.fm API credentials and session key
- Theme
- Track title parser
- Open browser on play

## SoundCloud Login

Public search and charts can work without login. For private, liked, or account-specific content, configure one of:

- A `cookies.txt` file path
- Browser cookies, for example `firefox`

The app passes these settings to `yt-dlp`.

## Discord Rich Presence

Discord Rich Presence is enabled by default. It requires Discord to be running locally and accepting RPC connections. If Discord is not available, playback still works and the status panel shows the integration error.

## Last.fm

To use Last.fm scrobbling:

1. Create a Last.fm API application.
2. Enter the API key and shared secret in settings.
3. Use the settings screen to open the auth URL.
4. Copy the returned token into the token field and save.

The app updates now playing while a track is playing and scrobbles after enough playback time has elapsed.

## Troubleshooting

If playback fails with `install ffplay`, install FFmpeg and make sure `ffplay` is on `PATH`.

If SoundCloud browsing fails, update `yt-dlp`:

```bash
python -m pip install -U yt-dlp
```

If likes, playlists, or user tracks fail, set your SoundCloud username or profile URL in settings. For content that requires login, configure cookies.

If Discord status does not update, check that Discord is running locally and that Rich Presence is enabled in settings.

If Last.fm does not update, confirm that Last.fm is enabled and all three credentials are saved: API key, API secret, and session key.
