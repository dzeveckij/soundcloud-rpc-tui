import json
import os
import socket
import stat
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


FAKE_FFPLAY = """#!{python}
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
    script = tmp_path / "ffplay"
    script.write_text(FAKE_FFPLAY.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "ffplay.log"
    log.touch()
    monkeypatch.setenv("FAKE_LOG", str(log))
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
    """Speaks the JSON IPC protocol documented by mpv, on the path mpv was given."""

    def __init__(self, path: str) -> None:
        self.commands: list[list] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        self.connection: socket.socket | None = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.connection, _ = self.server.accept()
        buffer = b""
        while True:
            try:
                chunk = self.connection.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                self.commands.append(json.loads(line)["command"])

    def emit(self, **event) -> None:
        self.connection.sendall(json.dumps(event).encode() + b"\n")


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
    fake.connection.close()
    assert wait_for(lambda: backend.dead and backend.finished)


def test_player_falls_back_to_ffplay_when_mpv_fails(monkeypatch, fake_ffplay):
    script, _ = fake_ffplay

    def which(name):
        return {"mpv": "/bin/mpv", "ffplay": str(script)}.get(name)

    def broken_mpv(*args, **kwargs):
        raise PlayerUnavailable("mpv did not start")

    monkeypatch.setattr(player_module.shutil, "which", which)
    monkeypatch.setattr(player_module, "MpvBackend", broken_mpv)
    instance = Player("auto", 50)
    instance.load("https://stream")
    assert instance.name == "ffplay"
    instance.close()


def test_player_without_any_backend_explains_what_to_install(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: None)
    with pytest.raises(PlayerUnavailable, match="Install mpv or FFmpeg"):
        Player("auto").load("https://stream")


def test_player_requested_mpv_does_not_silently_use_ffplay(monkeypatch):
    monkeypatch.setattr(player_module.shutil, "which", lambda name: "/bin/ffplay" if name == "ffplay" else None)
    with pytest.raises(PlayerUnavailable, match="mpv"):
        Player("mpv").load("https://stream")


def test_idle_player_is_safe_to_control():
    instance = Player("auto")
    instance.pause()
    instance.resume()
    instance.seek(5)
    instance.set_volume(10)
    instance.stop()
    instance.close()
    assert instance.position == 0 and not instance.finished and instance.volume == 10
