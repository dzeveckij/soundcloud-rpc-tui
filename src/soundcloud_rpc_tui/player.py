"""Audio playback through mpv (preferred) or ffplay, running as a child process."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path

WINDOWS = os.name == "nt"


class PlayerUnavailable(RuntimeError):
    pass


if WINDOWS:
    INSTALL_HINT = (
        "No audio player found. Install mpv (winget install shinchiro.mpv) or FFmpeg "
        "(winget install Gyan.FFmpeg), then open a new terminal so it's on your PATH."
    )
elif sys.platform == "darwin":
    INSTALL_HINT = "No audio player found. Install mpv (brew install mpv) or FFmpeg, then try again."
else:
    INSTALL_HINT = "No audio player found. Install mpv or FFmpeg with your package manager, then try again."
STATS_RE = re.compile(r"^\s*(-?\d+\.\d+)\s")


# Child processes

_job_lock = threading.Lock()
_job_handle: int | None = None


def _create_kill_on_close_job() -> int | None:
    """A Windows job object that ends every process in it when this app exits.

    Windows has no equivalent of a terminal hangup, so without this, closing the
    terminal window would leave the player running in the background.
    """
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    extended_limit_information = 9
    if not kernel32.SetInformationJobObject(job, extended_limit_information, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(job)
        return None
    return job


def _assign_to_job(job: int, process: subprocess.Popen) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    return bool(kernel32.AssignProcessToJobObject(job, int(process._handle)))  # type: ignore[attr-defined]


def _bind_to_app(process: subprocess.Popen) -> None:
    """On Windows, make sure the child ends when the app does, however it ends."""
    global _job_handle
    if not WINDOWS:
        return
    try:
        with _job_lock:
            if _job_handle is None:
                _job_handle = _create_kill_on_close_job()
            job = _job_handle
        if job:
            _assign_to_job(job, process)
    except (OSError, AttributeError, ValueError):
        pass  # Best effort. Quitting normally still stops the player.


def _spawn_child(command: list[str], **kwargs) -> subprocess.Popen:
    if WINDOWS:
        # No console window of its own, and no share in ours.
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(command, **kwargs)
    _bind_to_app(process)
    return process


def _set_suspended(process: subprocess.Popen, suspended: bool) -> bool:
    """Freeze or thaw a process in place. Returns False if this system can't."""
    if WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            ntdll = ctypes.WinDLL("ntdll")
            function = ntdll.NtSuspendProcess if suspended else ntdll.NtResumeProcess
            function.argtypes = [wintypes.HANDLE]
            function.restype = ctypes.c_long
            return function(int(process._handle)) == 0  # type: ignore[attr-defined]
        except (OSError, AttributeError, ValueError):
            return False
    name = "SIGSTOP" if suspended else "SIGCONT"
    if not hasattr(signal, name):
        return False
    try:
        process.send_signal(getattr(signal, name))
    except OSError:
        return False
    return True


def _windows_install_locations(name: str) -> list[Path]:
    """Where common Windows installers put mpv and FFmpeg, for when PATH is stale.

    A terminal opened before the install doesn't see the new PATH, so look in the
    usual places directly instead of telling people to restart everything.
    """
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    program_files = [Path(os.environ.get(key) or default) for key, default in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    )]
    executable = f"{name}.exe"
    places = [local / "Microsoft" / "WinGet" / "Links" / executable]
    places += [root / folder / executable for root in program_files for folder in ("MPV Player", "mpv")]
    places += [local / "Programs" / "mpv" / executable]
    places += [root / "FFmpeg" / "bin" / executable for root in program_files]
    places += [Path(os.environ.get("SCOOP") or home / "scoop") / "shims" / executable]
    places += [Path(os.environ.get("ChocolateyInstall") or r"C:\ProgramData\chocolatey") / "bin" / executable]
    packages = local / "Microsoft" / "WinGet" / "Packages"
    for pattern in (f"*/{executable}", f"*/*/{executable}", f"*/*/bin/{executable}"):
        try:
            places += sorted(packages.glob(pattern))
        except OSError:
            break
    return places


def find_binary(name: str) -> str | None:
    if not WINDOWS:
        return shutil.which(name)
    # Prefer the real executable over mpv.com, a console wrapper that starts a second process.
    found = shutil.which(f"{name}.exe") or shutil.which(name)
    if found:
        return found
    for candidate in _windows_install_locations(name):
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


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
        process = _spawn_child(
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
            if not WINDOWS:
                _set_suspended(process, False)  # A stopped process can't act on SIGTERM.
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
            if not _set_suspended(process, True):
                self._terminate(process)
                self._process = None  # Resume starts a new process where this one stopped.
            self.paused = True

    def resume(self) -> None:
        with self._lock:
            if not self.paused:
                return
            self.paused = False
            process = self._process
            if process is None:
                self._spawn(self.position)
            elif not _set_suspended(process, False):
                self._terminate(process)
                self._spawn(self.position)

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


class _SocketTransport:
    """mpv's IPC on macOS and Linux: a Unix domain socket."""

    def __init__(self, path: str) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        try:
            connection.connect(path)
        except OSError:
            connection.close()
            raise
        self._socket = connection

    def send(self, data: bytes) -> None:
        self._socket.sendall(data)

    def receive(self) -> bytes:
        return self._socket.recv(65536)

    def close(self) -> None:
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()


class _PipeTransport:
    """mpv's IPC on Windows: a named pipe.

    A pipe opened this way handles one operation at a time, so a reader blocked
    in ReadFile would stall every command. Reading only what PeekNamedPipe says
    is waiting keeps reads instant and leaves the pipe free for writes.
    """

    POLL_SECONDS = 0.02

    def __init__(self, path: str) -> None:
        import _winapi  # type: ignore[import-not-found]
        import msvcrt  # type: ignore[import-not-found]

        self._winapi = _winapi
        self._file = open(path, "r+b", buffering=0)  # noqa: SIM115
        self._handle = msvcrt.get_osfhandle(self._file.fileno())
        self._closed = False

    def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = self._file.write(view)
            if not written:
                raise OSError("mpv stopped reading")
            view = view[written:]

    def receive(self) -> bytes:
        while not self._closed:
            available, _ = self._winapi.PeekNamedPipe(self._handle, 0)
            if available:
                return self._file.read(min(available, 65536)) or b""
            time.sleep(self.POLL_SECONDS)
        return b""

    def close(self) -> None:
        self._closed = True
        self._file.close()


def _ipc_endpoint() -> tuple[str, str | None]:
    """An IPC address for mpv, plus a directory to remove afterwards (if any)."""
    if WINDOWS:
        return rf"\\.\pipe\soundcloud-rpc-tui-{os.getpid()}-{uuid.uuid4().hex[:8]}", None
    directory = tempfile.mkdtemp(prefix="sc-tui-")
    return os.path.join(directory, "mpv.sock"), directory


def _connect_transport(path: str) -> _SocketTransport | _PipeTransport:
    return _PipeTransport(path) if WINDOWS else _SocketTransport(path)


class MpvBackend:
    """One long-running mpv, controlled over its JSON IPC socket (a named pipe on Windows)."""

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
        self._socket_path, self._dir = _ipc_endpoint()
        self._process = _spawn_child(
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
        try:
            self._socket = self._connect(connect_timeout)
        except BaseException:
            self._shutdown_process()
            raise
        threading.Thread(target=self._read_events, daemon=True).start()
        for observer_id, name in enumerate(("time-pos", "pause", "paused-for-cache"), start=1):
            self._send(["observe_property", observer_id, name])

    def _connect(self, timeout: float) -> _SocketTransport | _PipeTransport:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Launchers such as Chocolatey's shims can start mpv and exit straight away with
            # success, so only a failed exit means mpv itself didn't start.
            if self._process.poll() not in (None, 0):
                break
            try:
                return _connect_transport(self._socket_path)
            except OSError:
                time.sleep(0.03)  # mpv hasn't created it yet.
        self._shutdown_process()
        raise PlayerUnavailable("mpv did not start")

    def _send(self, command: list) -> None:
        payload = json.dumps({"command": command}).encode() + b"\n"
        try:
            with self._send_lock:
                self._socket.send(payload)
        except (OSError, ValueError):
            self._mark_dead()

    def _mark_dead(self) -> None:
        self._dead = True
        if self._active:
            self._finished = True

    def _read_events(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = self._socket.receive()
            except (OSError, ValueError):
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
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        if self._dir is None:
            return
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
        except (OSError, ValueError):
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
            binary = find_binary("mpv")
            if binary:
                try:
                    return MpvBackend(self.volume, binary)
                except Exception:  # Whatever went wrong, ffplay may still work.
                    if self.preference == "mpv":
                        raise PlayerUnavailable("mpv could not start. Choose ffplay in settings.") from None
            elif self.preference == "mpv":
                raise PlayerUnavailable("mpv isn't installed. Install it, or choose ffplay in settings.")
        binary = find_binary("ffplay")
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
