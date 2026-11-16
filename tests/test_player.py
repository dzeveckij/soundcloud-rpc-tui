import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from soundcloud_rpc_tui import player as player_module
from soundcloud_rpc_tui.player import FfplayBackend, MpvBackend, Player, PlayerUnavailable


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


FAKE_FFPLAY = """
import sys, time, json, os
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
start = float(args[args.index("-ss") + 1]) if "-ss" in args else 0.0
sys.stderr.write("    nan    :  0.000 fd=   0 aq=    0KB vq=    0KB sq=    0B \\r")
sys.stderr.flush()
mode = os.environ.get("FAKE_MODE", "long")
if mode == "error":
    sys.stderr.write("https://x: Server returned 403 Forbidden\\n")
    sys.stderr.flush()
    sys.exit(1)
for step in range(400 if mode == "long" else 3):
    sys.stderr.write(f"{{start + step * 0.05:7.2f}} M-A:  0.000 fd=   0 aq=   19KB vq=    0KB sq=    0B \\r")
    sys.stderr.flush()
    time.sleep(0.05)
"""


@pytest.fixture
def fake_ffplay(tmp_path, monkeypatch):
    """A Python stand-in for ffplay. It runs through the interpreter so it works on Windows too."""
    script = tmp_path / "ffplay.py"
    script.write_text(FAKE_FFPLAY.format(), encoding="utf-8")
    log = tmp_path / "ffplay.log"
    log.touch()
    monkeypatch.setenv("FAKE_LOG", str(log))
    real_popen = subprocess.Popen

    def popen(command, **kwargs):
        if command and command[0] == str(script):
            command = [sys.executable, *command]
        return real_popen(command, **kwargs)

    monkeypatch.setattr(player_module.subprocess, "Popen", popen)
    return script, log


def launches(log: Path):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_ffplay_reports_real_audio_clock_not_wall_time(fake_ffplay):
    script, _ = fake_ffplay
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    assert backend.buffering
    assert wait_for(lambda: backend.started)
    assert not backend.buffering
    assert wait_for(lambda: backend.position > 0.1)
    backend.close()


def test_ffplay_pause_freezes_the_clock_and_resume_continues(fake_ffplay):
    script, _ = fake_ffplay
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    assert wait_for(lambda: backend.position > 0.1)
    backend.pause()
    time.sleep(0.15)
    frozen = backend.position
    time.sleep(0.25)
    assert backend.position == frozen and backend.paused
    backend.resume()
    assert wait_for(lambda: backend.position > frozen)
    backend.close()


def test_ffplay_seek_restarts_at_the_position(fake_ffplay):
    script, log = fake_ffplay
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    assert wait_for(lambda: backend.started)
    backend.seek(90)
    assert wait_for(lambda: len(launches(log)) == 2)
    assert wait_for(lambda: backend.position >= 90)
    assert launches(log)[-1][launches(log)[-1].index("-ss") + 1] == "90.00"
    backend.close()


def test_ffplay_seek_while_paused_waits_for_resume(fake_ffplay):
    script, log = fake_ffplay
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    assert wait_for(lambda: backend.started)
    backend.pause()
    backend.seek(42)
    assert backend.position == 42 and len(launches(log)) == 1
    assert not backend.finished
    backend.resume()
    assert wait_for(lambda: len(launches(log)) == 2)
    assert launches(log)[-1][launches(log)[-1].index("-ss") + 1] == "42.00"
    backend.close()


def test_ffplay_volume_restarts_from_current_position(fake_ffplay):
    script, log = fake_ffplay
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    assert wait_for(lambda: backend.position > 0.1)
    backend.set_volume(20)
    assert wait_for(lambda: len(launches(log)) == 2)
    args = launches(log)[-1]
    assert args[args.index("-volume") + 1] == "20" and "-ss" in args
    backend.close()


def test_ffplay_finished_and_error_reporting(fake_ffplay, monkeypatch):
    script, _ = fake_ffplay
    monkeypatch.setenv("FAKE_MODE", "short")
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    assert wait_for(lambda: backend.finished)
    assert backend.error == ""
    monkeypatch.setenv("FAKE_MODE", "error")
    backend.load("https://stream")
    assert wait_for(lambda: backend.finished and backend.error)
    assert "403" in backend.error


def test_ffplay_stop_is_not_reported_as_finished(fake_ffplay):
    script, _ = fake_ffplay
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    backend.stop()
    time.sleep(0.1)
    assert not backend.finished and not backend.active


def test_ffplay_stop_does_not_block(fake_ffplay):
    script, _ = fake_ffplay
    backend = FfplayBackend(50, str(script))
    backend.load("https://stream")
    began = time.perf_counter()
    backend.stop()
    assert time.perf_counter() - began < 0.1


class FakeMpv:
    """Speaks the JSON IPC protocol documented by mpv, on the path mpv was given.

    That path is a Unix socket on macOS and Linux and a named pipe on Windows,
    so this exercises the same transport the real app uses on each.
    """

    def __init__(self, path: str) -> None:
        self.commands: list[list] = []
        self.connection = None
        if os.name == "nt":
            from multiprocessing.connection import Listener

            self.server = Listener(path, family="AF_PIPE")
        else:
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(path)
            self.server.listen(1)
        threading.Thread(target=self._serve, daemon=True).start()

    def _accept(self):
        if os.name == "nt":
            return self.server.accept()
        connection, _ = self.server.accept()
        return connection

    def _receive(self) -> bytes:
        if os.name == "nt":
            return self.connection.recv_bytes()
        return self.connection.recv(4096)

    def _serve(self) -> None:
        self.connection = self._accept()
        buffer = b""
        while True:
            try:
                chunk = self._receive()
            except (OSError, EOFError):
                return
            if not chunk:
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                self.commands.append(json.loads(line)["command"])

    def emit(self, **event) -> None:
        payload = json.dumps(event).encode() + b"\n"
        if os.name == "nt":
            self.connection.send_bytes(payload)
        else:
            self.connection.sendall(payload)

    def drop(self) -> None:
        self.connection.close()


@pytest.fixture
def mpv(tmp_path, monkeypatch):
    fakes = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            socket_path = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--input-ipc-server="))
            fakes.append(FakeMpv(socket_path))

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(player_module.subprocess, "Popen", FakeProcess)
    backend = MpvBackend(70, "mpv")
    assert wait_for(lambda: fakes and len(fakes[0].commands) >= 3)
    yield backend, fakes[0]
    backend.close()


def test_mpv_observes_position_and_loads_files(mpv):
    backend, fake = mpv
    assert ["observe_property", 1, "time-pos"] in fake.commands
    backend.load("https://stream")
    assert wait_for(lambda: ["loadfile", "https://stream", "replace"] in fake.commands)
    assert backend.buffering
    fake.emit(event="property-change", name="time-pos", data=12.5)
    assert wait_for(lambda: backend.position == 12.5)
    assert not backend.buffering


def test_mpv_start_position_applies_after_file_loads(mpv):
    backend, fake = mpv
    backend.load("https://stream", start=90)
    fake.emit(event="file-loaded")
    assert wait_for(lambda: ["seek", 90, "absolute"] in fake.commands)


def test_mpv_only_eof_and_error_count_as_finished(mpv):
    backend, fake = mpv
    backend.load("https://stream")
    fake.emit(event="end-file", reason="stop")
    time.sleep(0.05)
    assert not backend.finished
    fake.emit(event="end-file", reason="eof")
    assert wait_for(lambda: backend.finished)
    backend.load("https://stream")
    fake.emit(event="end-file", reason="error", file_error="loading failed")
    assert wait_for(lambda: backend.finished and backend.error == "loading failed")


def test_mpv_live_controls_use_ipc(mpv):
    backend, fake = mpv
    backend.load("https://stream")
    backend.pause()
    backend.seek(30)
    backend.set_volume(40)
    backend.resume()
    assert wait_for(lambda: ["set_property", "volume", 40] in fake.commands and ["seek", 30.0, "absolute"] in fake.commands)
    assert ["set_property", "pause", True] in fake.commands


def test_mpv_marks_itself_dead_when_connection_drops(mpv):
    backend, fake = mpv
    backend.load("https://stream")
    fake.drop()
    assert wait_for(lambda: backend.dead and backend.finished)


def test_player_falls_back_to_ffplay_when_mpv_fails(monkeypatch, fake_ffplay):
    script, _ = fake_ffplay

    def broken_mpv(*args, **kwargs):
        raise PlayerUnavailable("mpv did not start")

    monkeypatch.setattr(player_module, "find_binary", {"mpv": "mpv", "ffplay": str(script)}.get)
    monkeypatch.setattr(player_module, "MpvBackend", broken_mpv)
    instance = Player("auto", 50)
    instance.load("https://stream")
    assert instance.name == "ffplay"
    instance.close()


def test_player_falls_back_to_ffplay_on_any_mpv_error(monkeypatch, fake_ffplay):
    """An unexpected failure, like a missing socket type, must not block playback."""
    script, _ = fake_ffplay

    def broken_mpv(*args, **kwargs):
        raise AttributeError("module 'socket' has no attribute 'AF_UNIX'")

    monkeypatch.setattr(player_module, "find_binary", {"mpv": "mpv", "ffplay": str(script)}.get)
    monkeypatch.setattr(player_module, "MpvBackend", broken_mpv)
    instance = Player("auto", 50)
    instance.load("https://stream")
    assert instance.name == "ffplay"
    instance.close()


def test_player_without_any_backend_explains_what_to_install(monkeypatch):
    monkeypatch.setattr(player_module, "find_binary", lambda name: None)
    with pytest.raises(PlayerUnavailable, match="Install mpv"):
        Player("auto").load("https://stream")


def test_player_requested_mpv_does_not_silently_use_ffplay(monkeypatch):
    monkeypatch.setattr(player_module, "find_binary", lambda name: "ffplay" if name == "ffplay" else None)
    with pytest.raises(PlayerUnavailable, match="mpv"):
        Player("mpv").load("https://stream")


def test_mpv_process_is_stopped_when_it_never_answers(monkeypatch):
    stopped = []

    class SilentProcess:
        def __init__(self, command, **kwargs):
            pass

        def poll(self):
            return None

        def terminate(self):
            stopped.append(True)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(player_module.subprocess, "Popen", SilentProcess)
    with pytest.raises(PlayerUnavailable):
        MpvBackend(70, "mpv", connect_timeout=0.1)
    assert stopped


def test_mpv_launched_through_a_launcher_that_exits_still_connects(monkeypatch):
    """Chocolatey's shim starts mpv and exits 0 at once; the pipe still appears."""
    fakes = []

    class Launcher:
        def __init__(self, command, **kwargs):
            path = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--input-ipc-server="))
            fakes.append(FakeMpv(path))

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(player_module.subprocess, "Popen", Launcher)
    backend = MpvBackend(70, "mpv", connect_timeout=2)
    assert wait_for(lambda: fakes and len(fakes[0].commands) >= 3)
    backend.close()


def test_windows_install_locations_cover_winget_scoop_and_program_files(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("SCOOP", str(tmp_path / "scoop"))
    package = tmp_path / "Local" / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_x" / "ffmpeg-8.0-full_build" / "bin"
    package.mkdir(parents=True)
    (package / "ffplay.exe").touch()
    places = [str(path) for path in player_module._windows_install_locations("ffplay")]
    assert str(package / "ffplay.exe") in places
    assert str(tmp_path / "Local" / "Microsoft" / "WinGet" / "Links" / "ffplay.exe") in places
    assert str(tmp_path / "scoop" / "shims" / "ffplay.exe") in places
    monkeypatch.setenv("ChocolateyInstall", str(tmp_path / "chocolatey"))
    choco = tmp_path / "chocolatey" / "lib" / "mpvio.install" / "tools"
    choco.mkdir(parents=True)
    (choco / "mpv.exe").touch()
    mpv_places = [str(path) for path in player_module._windows_install_locations("mpv")]
    assert str(choco / "mpv.exe") in mpv_places
    assert str(tmp_path / "Program Files" / "MPV Player" / "mpv.exe") in mpv_places


def test_windows_finds_a_player_that_is_installed_but_not_on_path(monkeypatch, tmp_path):
    binary = tmp_path / "mpv.exe"
    binary.touch()
    monkeypatch.setattr(player_module, "WINDOWS", True)
    monkeypatch.setattr(player_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(player_module, "_windows_install_locations", lambda name: [tmp_path / "missing.exe", binary])
    assert player_module.find_binary("mpv") == str(binary)


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_windows_job_ends_children_when_the_app_exits():
    import ctypes

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job = player_module._create_kill_on_close_job()
    assert job
    assert player_module._assign_to_job(job, child)
    ctypes.WinDLL("kernel32").CloseHandle(job)  # What happens when the app's process ends.
    assert child.wait(timeout=10) is not None


def test_pause_freezes_a_real_process_in_place():
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", "import time\nwhile True:\n    print(time.monotonic(), flush=True)\n    time.sleep(0.02)"],
        stdout=subprocess.PIPE,
    )
    try:
        child.stdout.readline()
        assert player_module._set_suspended(child, True)
        time.sleep(0.3)
        _drain(child)  # Lines written before the suspend took effect.
        time.sleep(0.3)
        assert _drain(child) == b"", "the process kept running while suspended"
        assert player_module._set_suspended(child, False)
        assert child.stdout.readline()
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def _drain(process) -> bytes:
    """Read whatever is waiting on stdout without blocking."""
    received = b""
    if os.name == "nt":
        import _winapi
        import msvcrt

        handle = msvcrt.get_osfhandle(process.stdout.fileno())
        available, _ = _winapi.PeekNamedPipe(handle, 0)
        if available:
            received = os.read(process.stdout.fileno(), available)
    else:
        import select

        while select.select([process.stdout], [], [], 0)[0]:
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            received += chunk
    return received


@pytest.mark.skipif(
    not player_module.find_binary("mpv") and not os.environ.get("REQUIRE_MPV"),
    reason="mpv isn't installed",
)
def test_real_mpv_starts_and_answers_over_ipc():
    """Run the installed mpv. On Windows this checks the named-pipe transport for real.

    CI sets REQUIRE_MPV, so not finding the mpv it just installed is a failure, not a skip.
    """
    binary = player_module.find_binary("mpv")
    assert binary, "mpv is installed but the app can't find it"
    backend = MpvBackend(30, binary)
    try:
        assert not backend.dead
        backend.set_volume(25)
        backend.pause()
        backend.resume()
        time.sleep(0.2)
        assert not backend.dead
    finally:
        backend.close()
    assert backend._process.poll() is not None
