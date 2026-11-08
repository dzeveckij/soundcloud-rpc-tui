"""Audio playback through mpv (preferred) or ffplay, running as a child process."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque


class PlayerUnavailable(RuntimeError):
    pass


INSTALL_HINT = "No audio player found. Install mpv or FFmpeg (for ffplay), then try again."
STATS_RE = re.compile(r"^\s*(-?\d+\.\d+)\s")


class FfplayBackend:
    """ffplay has no control channel, so seeking and volume restart the process."""

    name = "ffplay"
    live_controls = False

    def __init__(self, volume: int = 80, binary: str = "ffplay") -> None:
        self.binary = binary
        self.volume = volume
        self.position = 0.0
        self.paused = False
        self.started = False
        self.error = ""
        self._url = ""
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        self._errors: deque[str] = deque(maxlen=5)

    @property
    def finished(self) -> bool:
        process = self._process
        return process is not None and process.poll() is not None

    @property
    def active(self) -> bool:
        return bool(self._url)

    @property
    def buffering(self) -> bool:
        return self.active and not self.started and not self.paused

    @property
    def dead(self) -> bool:
        return False

    def _spawn(self, start: float) -> None:
        command = [
            self.binary,
            "-nodisp",
            "-autoexit",
            "-vn",
            "-sn",
            "-hide_banner",
            "-loglevel",
            "error",
            "-stats",
            "-volume",
            str(max(0, min(100, self.volume))),
        ]
        if start > 0:
            command += ["-ss", f"{start:.2f}"]
        command.append(self._url)
        self._errors.clear()
        self.error = ""
        self.started = False
        self.position = start
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._process = process
        threading.Thread(target=self._read_stats, args=(process,), daemon=True).start()

    def _read_stats(self, process: subprocess.Popen[bytes]) -> None:
        """Follow ffplay's status line, which reports the real audio clock."""
        stream = process.stderr
        if stream is None:
            return
        buffer = b""
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = re.split(rb"[\r\n]", buffer)
            for raw in lines:
                self._handle_line(process, raw.decode("utf-8", "replace"))
        if process is self._process and self._errors and not self.started:
            self.error = self._errors[-1]

    def _handle_line(self, process: subprocess.Popen[bytes], line: str) -> None:
        if process is not self._process or not line.strip():
            return
        match = STATS_RE.match(line)
        if match:
            self.position = max(0.0, float(match.group(1)))
            self.started = True
        elif "nan" not in line.split(":")[0]:
            self._errors.append(line.strip())

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return

        def reap() -> None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        try:
            if hasattr(signal, "SIGCONT"):
                process.send_signal(signal.SIGCONT)
            process.terminate()
        except OSError:
            return
        threading.Thread(target=reap, daemon=True).start()

    def load(self, url: str, start: float = 0.0) -> None:
        with self._lock:
            self._terminate(self._process)
            self._url = url
            self.paused = False
            self._spawn(start)

    def pause(self) -> None:
        with self._lock:
            process = self._process
            if self.paused or process is None or process.poll() is not None:
                return
            if hasattr(signal, "SIGSTOP"):
                process.send_signal(signal.SIGSTOP)
            else:
                self._terminate(process)
                self._process = None
            self.paused = True

    def resume(self) -> None:
        with self._lock:
            if not self.paused:
                return
            self.paused = False
            process = self._process
            if process is None:
                self._spawn(self.position)
            elif hasattr(signal, "SIGCONT"):
                process.send_signal(signal.SIGCONT)

    def seek(self, position: float) -> None:
        with self._lock:
            if not self._url:
                return
            self._terminate(self._process)
            if self.paused:
                self._process = None  # Resume starts a new process at this position.
                self.position = max(0.0, position)
            else:
                self._spawn(max(0.0, position))

    def set_volume(self, volume: int) -> None:
        with self._lock:
            self.volume = volume
            if self._url and not self.paused and self._process is not None and self._process.poll() is None:
                self._terminate(self._process)
                self._spawn(self.position)

    def stop(self) -> None:
        with self._lock:
            self._terminate(self._process)
            self._process = None
            self._url = ""
            self.paused = False
            self.started = False
            self.position = 0.0

    def close(self) -> None:
        self.stop()


class MpvBackend:
    """One long-running mpv, controlled over its JSON IPC socket."""

    name = "mpv"
    live_controls = True

    def __init__(self, volume: int = 80, binary: str = "mpv", connect_timeout: float = 3.0) -> None:
        self.volume = volume
        self.position = 0.0
        self.paused = False
        self.started = False
        self.error = ""
        self._active = False
        self._finished = False
        self._cache_wait = False
        self._pending_start = 0.0
        self._dead = False
        self._send_lock = threading.Lock()
        self._dir = tempfile.mkdtemp(prefix="sc-tui-")
        self._socket_path = os.path.join(self._dir, "mpv.sock")
        self._process = subprocess.Popen(
            [
                binary,
                "--idle=yes",
                "--no-video",
                "--no-terminal",
                "--ytdl=no",
                "--audio-display=no",
                "--force-seekable=yes",
                "--cache=yes",
                f"--volume={volume}",
                f"--input-ipc-server={self._socket_path}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._socket = self._connect(connect_timeout)
        threading.Thread(target=self._read_events, daemon=True).start()
        for observer_id, name in enumerate(("time-pos", "pause", "paused-for-cache"), start=1):
            self._send(["observe_property", observer_id, name])

    def _connect(self, timeout: float) -> socket.socket:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(self._socket_path)
                return connection
            except OSError:
                connection.close()
                time.sleep(0.03)
        self._shutdown_process()
        raise PlayerUnavailable("mpv did not start")

    def _send(self, command: list) -> None:
        payload = json.dumps({"command": command}).encode() + b"\n"
        try:
            with self._send_lock:
                self._socket.sendall(payload)
        except OSError:
            self._mark_dead()

    def _mark_dead(self) -> None:
        self._dead = True
        if self._active:
            self._finished = True

    def _read_events(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = self._socket.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                self._mark_dead()
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                try:
                    self._handle_event(json.loads(line))
                except ValueError:
                    continue

    def _handle_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "property-change":
            prop, data = event.get("name"), event.get("data")
            if prop == "time-pos" and isinstance(data, (int, float)):
                self.position = max(0.0, float(data))
                self.started = True
            elif prop == "paused-for-cache":
                self._cache_wait = bool(data)
        elif name == "file-loaded":
            if self._pending_start > 0:
                self._send(["seek", self._pending_start, "absolute"])
                self._pending_start = 0.0
        elif name == "end-file":
            reason = event.get("reason")
            if reason == "eof":
                self._finished = True
            elif reason == "error":
                self.error = str(event.get("file_error") or "could not open the stream")
                self._finished = True

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def active(self) -> bool:
        return self._active

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def buffering(self) -> bool:
        return self._active and not self.paused and (not self.started or self._cache_wait)

    def load(self, url: str, start: float = 0.0) -> None:
        self.position = start
        self.started = False
        self.paused = False
        self.error = ""
        self._finished = False
        self._cache_wait = False
        self._active = True
        self._pending_start = start
        self._send(["set_property", "pause", False])
        self._send(["loadfile", url, "replace"])

    def pause(self) -> None:
        self.paused = True
        self._send(["set_property", "pause", True])

    def resume(self) -> None:
        self.paused = False
        self._send(["set_property", "pause", False])

    def seek(self, position: float) -> None:
        self.position = max(0.0, position)
        self._send(["seek", self.position, "absolute"])

    def set_volume(self, volume: int) -> None:
        self.volume = volume
        self._send(["set_property", "volume", volume])

    def stop(self) -> None:
        self._active = False
        self._finished = False
        self.paused = False
        self.started = False
        self.position = 0.0
        self._send(["stop"])

    def _shutdown_process(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass

    def close(self) -> None:
        self._send(["quit"])
        try:
            self._socket.close()
        except OSError:
            pass
        self._shutdown_process()


class Player:
    """Starts a backend on first use, since launching one can take a moment."""

    def __init__(self, preference: str = "auto", volume: int = 80) -> None:
        self.preference = preference
        self.volume = volume
        self._backend: FfplayBackend | MpvBackend | None = None
        self._lock = threading.Lock()

    def _start_backend(self) -> FfplayBackend | MpvBackend:
        if self.preference in ("auto", "mpv"):
            binary = shutil.which("mpv")
            if binary:
                try:
                    return MpvBackend(self.volume, binary)
                except (PlayerUnavailable, OSError):
                    if self.preference == "mpv":
                        raise PlayerUnavailable("mpv could not start. Choose ffplay in settings.") from None
            elif self.preference == "mpv":
                raise PlayerUnavailable("mpv isn't installed. Install it, or choose ffplay in settings.")
        binary = shutil.which("ffplay")
        if binary:
            return FfplayBackend(self.volume, binary)
        raise PlayerUnavailable(INSTALL_HINT)

    def _ensure(self) -> FfplayBackend | MpvBackend:
        with self._lock:
            if self._backend is None or self._backend.dead:
                if self._backend is not None:
                    self._backend.close()
                self._backend = self._start_backend()
            return self._backend

    @property
    def name(self) -> str:
        return self._backend.name if self._backend else "not started"

    @property
    def live_controls(self) -> bool:
        return self._backend.live_controls if self._backend else True

    @property
    def position(self) -> float:
        return self._backend.position if self._backend else 0.0

    @property
    def paused(self) -> bool:
        return self._backend.paused if self._backend else False

    @property
    def buffering(self) -> bool:
        return self._backend.buffering if self._backend else False

    @property
    def finished(self) -> bool:
        return self._backend.finished if self._backend else False

    @property
    def error(self) -> str:
        return self._backend.error if self._backend else ""

    def load(self, url: str, start: float = 0.0) -> None:
        self._ensure().load(url, start)

    def pause(self) -> None:
        if self._backend:
            self._backend.pause()

    def resume(self) -> None:
        if self._backend:
            self._backend.resume()

    def seek(self, position: float) -> None:
        if self._backend:
            self._backend.seek(position)

    def set_volume(self, volume: int) -> None:
        self.volume = volume
        if self._backend:
            self._backend.set_volume(volume)

    def stop(self) -> None:
        if self._backend:
            self._backend.stop()

    def set_preference(self, preference: str) -> None:
        """Apply a new backend choice at the next track."""
        if preference == self.preference:
            return
        self.preference = preference
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None

    def close(self) -> None:
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
