import argparse
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.voice import remote, serve
from ffrd.voice.render import PORT

# Made-up host name. Never a real host.
SYNTHETIC_HOST = "synthetic-host"
LAUNCH = (
    "{ setsid nohup nix develop path:./tools/voice#cuda --command llama-server -m models/voice.gguf --host 127.0.0.1 "
    "--port 8737 -c 4096 -np 4 -ngl 999 > /dev/null 2>&1 < /dev/null & echo $! > serve.pid; }"
)
STOP = "if [ -f serve.pid ]; then kill -TERM -- -$(cat serve.pid) 2>/dev/null; rm -f serve.pid; fi"
RUNNING = "test -f serve.pid && kill -0 -- -$(cat serve.pid)"


@dataclass
class Stub:
    port: int = 0
    statuses: list[int] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)


@contextmanager
def _serve() -> Iterator[Stub]:
    stub = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            stub.paths.append(self.path)
            status = stub.statuses.pop(0) if stub.statuses else 200
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub.port = server.server_port
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def stub() -> Iterator[Stub]:
    with _serve() as s:
        yield s


@dataclass
class Host:
    """Stands in for the GPU host. Tracks whether llama-server is up and records every shell command."""

    stub: Stub
    up: bool = False
    stays_up: bool = True
    commands: list[str] = field(default_factory=list)
    syncs: int = 0
    tunnels: list[int] = field(default_factory=list)

    def shell(self, command: str) -> None:
        self.commands.append(command)
        if command == RUNNING:
            if not self.up:
                raise Fail("gnomon command exited 1")
        elif command.startswith("{ setsid nohup "):
            self.up = self.stays_up
        elif command == STOP:
            self.up = False
        else:
            raise AssertionError(f"unexpected remote command: {command}")

    def sync(self) -> None:
        self.syncs += 1

    @contextmanager
    def tunnel(self, remote_port: int) -> Iterator[int]:
        self.tunnels.append(remote_port)
        yield self.stub.port


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, stub: Stub) -> Host:
    monkeypatch.setenv("FFR_VOICE_HOST", SYNTHETIC_HOST)
    h = Host(stub=stub)
    monkeypatch.setattr(remote, "shell", h.shell)
    monkeypatch.setattr(remote, "sync", h.sync)
    monkeypatch.setattr(remote, "tunnel", h.tunnel)
    monkeypatch.setattr(serve, "POLL_SECONDS", 0.0)
    return h


class Clock:
    """Advances by a fixed step on every read, so the ready timeout passes after a known number of polls."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        now = self.now
        self.now += self.step
        return now


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_frozen_constants() -> None:
    assert serve.PIDFILE == "serve.pid"
    assert serve.CONTEXT == 4096
    assert serve.SLOTS == 4
    assert serve.READY_TIMEOUT == 300.0
    assert serve.POLL_SECONDS == 2.0


def test_server_argv_is_exact() -> None:
    assert serve.server_argv(8737, "models/voice.gguf") == [
        "llama-server",
        "-m",
        "models/voice.gguf",
        "--host",
        "127.0.0.1",
        "--port",
        "8737",
        "-c",
        "4096",
        "-np",
        "4",
        "-ngl",
        "999",
    ]


def test_running_follows_the_kill_probe(host: Host) -> None:
    host.up = True
    assert serve.running() is True
    host.up = False
    assert serve.running() is False
    assert host.commands == [RUNNING, RUNNING]


def test_start_launches_and_waits_past_non_200_polls(host: Host, stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
    stub.statuses = [503, 503, 503, 200]
    serve.start()
    assert host.syncs == 1
    assert host.commands == [RUNNING, LAUNCH, RUNNING, RUNNING, RUNNING]
    assert host.tunnels == [PORT]
    assert stub.paths == ["/health"] * 4
    assert capsys.readouterr().out == f"llama-server ready on {SYNTHETIC_HOST} 127.0.0.1:{PORT}\n"


def test_start_uses_the_given_port_and_gguf(host: Host) -> None:
    serve.start(9000, "models/other.gguf")
    assert host.commands[1] == (
        "{ setsid nohup nix develop path:./tools/voice#cuda --command llama-server -m models/other.gguf --host 127.0.0.1 "
        "--port 9000 -c 4096 -np 4 -ngl 999 > /dev/null 2>&1 < /dev/null & echo $! > serve.pid; }"
    )
    assert host.tunnels == [9000]


def test_already_running_returns_without_launching(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.up = True
    serve.start()
    assert host.commands == [RUNNING]
    assert host.tunnels == []
    assert capsys.readouterr().out == f"llama-server already running on {SYNTHETIC_HOST}\n"


def test_early_exit_raises_without_stopping(host: Host, stub: Stub) -> None:
    host.stays_up = False
    stub.statuses = [503]
    with pytest.raises(Fail) as exc:
        serve.start()
    assert str(exc.value) == f"llama-server exited on {SYNTHETIC_HOST} before it was ready"
    assert host.commands == [RUNNING, LAUNCH, RUNNING]


def test_timeout_stops_and_raises(host: Host, stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(step=100.0)
    monkeypatch.setattr(serve, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=lambda _s: None))
    stub.statuses = [503] * 10
    with pytest.raises(Fail) as exc:
        serve.start()
    assert str(exc.value) == f"llama-server was not ready on {SYNTHETIC_HOST} within 300 s"
    assert host.commands[-1] == STOP
    assert host.up is False
    assert len(stub.paths) == 3


def test_stop_runs_the_stop_command_and_prints(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.up = True
    serve.stop()
    assert host.commands == [STOP]
    assert capsys.readouterr().out == f"llama-server stopped on {SYNTHETIC_HOST}\n"


def test_health_check_is_proxy_free(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = f"http://127.0.0.1:{_closed_port()}"
    monkeypatch.setenv("http_proxy", proxy)
    monkeypatch.setenv("HTTP_PROXY", proxy)
    assert serve._healthy(stub.port) is True


def test_health_is_false_for_non_200_and_for_refused_connections(stub: Stub) -> None:
    stub.statuses = [503]
    assert serve._healthy(stub.port) is False
    assert serve._healthy(_closed_port()) is False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    serve.register(parser.add_subparsers(required=True))
    return parser


def test_register_routes_serve_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(serve, "start", lambda port: calls.append(("start", port)))
    monkeypatch.setattr(serve, "stop", lambda: calls.append(("stop",)))
    args = _parser().parse_args(["serve"])
    assert args.stop is False
    assert args.port == PORT
    args.fn(args)
    assert calls == [("start", PORT)]


def test_register_routes_port_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(serve, "start", lambda port: calls.append(("start", port)))
    monkeypatch.setattr(serve, "stop", lambda: calls.append(("stop",)))
    args = _parser().parse_args(["serve", "--port", "9000"])
    args.fn(args)
    assert calls == [("start", 9000)]


def test_register_routes_stop_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(serve, "start", lambda port: calls.append(("start", port)))
    monkeypatch.setattr(serve, "stop", lambda: calls.append(("stop",)))
    args = _parser().parse_args(["serve", "--stop"])
    args.fn(args)
    assert calls == [("stop",)]


def test_launch_through_a_real_shell_writes_the_pidfile_in_scratch_and_stop_ends_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # remote.shell joins its scratch-dir prefix with &&, so the launch must keep its & inside a group:
    # run the composed command through a local sh (standing in for ssh) with sleep standing in for llama-server.
    home = tmp_path / "home"
    home.mkdir()
    scratch = tmp_path / "scratch"
    real_run = subprocess.run

    def local_sh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return real_run(["sh", "-c", args[2]], cwd=home, **kwargs)

    monkeypatch.setenv("FFR_VOICE_SCRATCH", str(scratch))
    monkeypatch.setattr(remote.subprocess, "run", local_sh)
    monkeypatch.setattr(remote, "env", lambda argv: "sleep 30")

    serve.launch(PORT, "models/voice.gguf")

    assert (scratch / "serve.pid").is_file()
    assert not (home / "serve.pid").exists()
    # The pidfile is written right after the fork, so the new process group can take a moment to exist.
    deadline = time.monotonic() + 5
    while not serve.running():
        assert time.monotonic() < deadline, "the launched process group never appeared"
        time.sleep(0.05)
    serve.stop()
    assert not (scratch / "serve.pid").exists()
    assert not serve.running()
