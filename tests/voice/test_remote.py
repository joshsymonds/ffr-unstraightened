import io
import socket
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.voice import remote

REAL_RUN = subprocess.run

# The default scratch dir, prepared the way every remote command prepares it.
PREFIX = "umask 077 && mkdir -p ~/.local/share/ffr-unstraightened/voice/logs && cd ~/.local/share/ffr-unstraightened/voice"
DEVENV = "env -C tools/voice devenv shell --profile cuda -- env -C ../.."


class Ssh:
    """Stands in for subprocess.run. Records each call and plays back a prepared return code and stdout."""

    def __init__(self) -> None:
        self.returncode = 0
        self.payload = b""
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.sent: bytes | None = None

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((args, kwargs))
        stdin = kwargs.get("stdin")
        if isinstance(stdin, io.IOBase):
            self.sent = stdin.read()
        if "input" in kwargs:
            self.sent = kwargs["input"]
        if kwargs.get("stdout") is not None:
            kwargs["stdout"].write(self.payload)
        return subprocess.CompletedProcess(args, self.returncode)


class FakePipe:
    def __init__(self, data: bytes = b"") -> None:
        self.reads = io.BytesIO(data)
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        return self.reads.readline()

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, reply: bytes = b"", returncode: int = 0, hang: bool = False, exited: bool = False) -> None:
        self.stdin = FakePipe()
        self.stdout = FakePipe(reply)
        self.returncode = returncode
        self.hang = hang
        self.exited = exited
        self.terminated = False
        self.killed = False
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode if self.exited else None

    def terminate(self) -> None:
        self.terminated = True
        self.exited = True

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.hang and not self.killed and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.exited = True


class FakePopen:
    def __init__(self, process: FakeProcess, listeners: list[socket.socket] | None = None) -> None:
        self.process = process
        self.listeners = listeners
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
        if self.listeners is not None and "-L" in args:
            # Stand in for the forwarded port on gnomon: listen on the local end of the -L spec.
            local = int(args[args.index("-L") + 1].split(":")[1])
            sock = socket.socket()
            sock.bind(("127.0.0.1", local))
            sock.listen()
            self.listeners.append(sock)
        return self.process


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FFR_VOICE_HOST", raising=False)
    monkeypatch.delenv("FFR_VOICE_SCRATCH", raising=False)


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> Ssh:
    fake = Ssh()
    monkeypatch.setattr(remote.subprocess, "run", fake)
    return fake


def test_host_and_scratch_default_and_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert remote.host() == "gnomon"
    assert remote.scratch() == "~/.local/share/ffr-unstraightened/voice"
    monkeypatch.setenv("FFR_VOICE_HOST", "other")
    monkeypatch.setenv("FFR_VOICE_SCRATCH", "/data/voice")
    assert remote.host() == "other"
    assert remote.scratch() == "/data/voice"


def test_env_wraps_shell_quoted_argv_in_devenv_cuda_profile_from_scratch_root() -> None:
    assert remote.env(["python3", "-m", "pytest", "a b"]) == f"{DEVENV} python3 -m pytest 'a b'"


def test_shell_runs_prefix_then_command_in_one_ssh_call(ssh: Ssh) -> None:
    remote.shell("echo hi")
    assert ssh.calls == [(["ssh", "gnomon", f"{PREFIX} && echo hi"], {"check": False, "stdin": subprocess.DEVNULL})]


def test_shell_quotes_scratch_path_with_spaces(ssh: Ssh, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FFR_VOICE_SCRATCH", "~/my voice/run")
    remote.shell("echo hi")
    assert ssh.calls[0][0] == [
        "ssh",
        "gnomon",
        "umask 077 && mkdir -p ~/'my voice/run/logs' && cd ~/'my voice/run' && echo hi",
    ]


def test_shell_nonzero_exit_raises_fail_with_host_and_code_only(ssh: Ssh) -> None:
    ssh.returncode = 7
    with pytest.raises(Fail) as err:
        remote.shell("echo hi")
    assert str(err.value) == "gnomon command exited 7"


def test_run_appends_remote_stderr_to_remote_log(ssh: Ssh) -> None:
    remote.run(["python3", "x.py"])
    assert ssh.calls[0][0][2] == f"{PREFIX} && {DEVENV} python3 x.py 2>> logs/remote.log"


SYNC = "mkdir -p tools/voice && find tools/voice -mindepth 1 -maxdepth 1 ! -name .devenv -exec rm -rf -- {} + && tar -x -f -"


def test_sync_tar_holds_only_tools_voice_files_without_pycache_or_devenv(ssh: Ssh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = tmp_path / "tools" / "voice"
    (voice / "pkg").mkdir(parents=True)
    (voice / "__pycache__").mkdir()
    (voice / ".devenv" / "state").mkdir(parents=True)
    (voice / "devenv.nix").write_text("{}\n")
    (voice / "pkg" / "a.py").write_text("x = 1\n")
    (voice / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    (voice / ".devenv" / "state" / "local").write_text("local-state\n")
    (tmp_path / "ffrd").mkdir()
    (tmp_path / "ffrd" / "b.py").write_text("y = 2\n")
    monkeypatch.setattr(remote, "repo_root", lambda: tmp_path)

    remote.sync()

    args, kwargs = ssh.calls[0]
    assert args == ["ssh", "gnomon", f"{PREFIX} && {SYNC}"]
    with tarfile.open(fileobj=io.BytesIO(kwargs["input"])) as tar:
        names = sorted(tar.getnames())
    assert names == ["tools/voice/devenv.nix", "tools/voice/pkg/a.py"]


def test_sync_keeps_remote_devenv_state_and_drops_stale_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Runs the real remote command with sh in a fake scratch dir under a fake HOME, so the .devenv
    # preservation is checked by behavior rather than by the command text.
    home = tmp_path / "home"
    scratch_voice = home / ".local" / "share" / "ffr-unstraightened" / "voice" / "tools" / "voice"
    (scratch_voice / ".devenv").mkdir(parents=True)
    (scratch_voice / ".devenv" / "state").write_text("remote-state\n")
    (scratch_voice / "stale.py").write_text("old = 1\n")
    repo = tmp_path / "repo"
    (repo / "tools" / "voice" / ".devenv").mkdir(parents=True)
    (repo / "tools" / "voice" / ".devenv" / "state").write_text("local-state\n")
    (repo / "tools" / "voice" / "devenv.nix").write_text("{}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(remote, "repo_root", lambda: repo)

    def run_here(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return REAL_RUN(["sh", "-c", args[2]], input=kwargs["input"], check=False)

    monkeypatch.setattr(remote.subprocess, "run", run_here)
    remote.sync()

    assert (scratch_voice / ".devenv" / "state").read_text() == "remote-state\n"
    assert not (scratch_voice / "stale.py").exists()
    assert (scratch_voice / "devenv.nix").read_text() == "{}\n"


def test_push_sends_file_bytes_to_name_under_scratch(ssh: Ssh, tmp_path: Path) -> None:
    local = tmp_path / "in.bin"
    local.write_bytes(b"synthetic-bytes")
    remote.push(local, "models/in.bin")
    assert ssh.calls[0][0] == ["ssh", "gnomon", f"{PREFIX} && mkdir -p -- models && cat > models/in.bin"]
    assert ssh.sent == b"synthetic-bytes"


def test_pull_creates_parents_and_writes_local_after_full_copy(ssh: Ssh, tmp_path: Path) -> None:
    ssh.payload = b"synthetic-model"
    local = tmp_path / "nested" / "out.bin"
    remote.pull("runs/out.bin", local)
    assert local.read_bytes() == b"synthetic-model"
    assert ssh.calls[0][0] == ["ssh", "gnomon", f"{PREFIX} && cat -- runs/out.bin"]
    assert sorted(p.name for p in local.parent.iterdir()) == ["out.bin"]


def test_pull_leaves_existing_local_untouched_when_copy_fails(ssh: Ssh, tmp_path: Path) -> None:
    ssh.payload = b"partial"
    ssh.returncode = 1
    local = tmp_path / "out.bin"
    local.write_bytes(b"old")
    with pytest.raises(Fail, match="gnomon pull exited 1"):
        remote.pull("runs/out.bin", local)
    assert local.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.bin"]


@pytest.mark.parametrize("name", ["", "/abs/x", "../x", "a/../b", "a//b", "a/"])
def test_push_and_pull_reject_names_outside_scratch_before_ssh(ssh: Ssh, tmp_path: Path, name: str) -> None:
    local = tmp_path / "f"
    local.write_bytes(b"x")
    with pytest.raises(Fail):
        remote.push(local, name)
    with pytest.raises(Fail):
        remote.pull(name, tmp_path / "out")
    assert ssh.calls == []
    assert not (tmp_path / "out").exists()


def test_worker_ask_round_trips_one_json_line(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(reply=b'{"ok":true,"n":1}\n')
    popen = FakePopen(process)
    monkeypatch.setattr(remote.subprocess, "Popen", popen)

    with remote.Worker(["python3", "serve.py"]) as worker:
        reply = worker.ask({"op": "ping", "n": 1})

    assert reply == {"ok": True, "n": 1}
    assert process.stdin.written == b'{"op":"ping","n":1}\n'
    assert popen.calls[0][0] == [
        "ssh",
        "gnomon",
        f"{PREFIX} && {DEVENV} python3 serve.py 2>> logs/worker.log",
    ]
    assert process.stdin.closed
    assert process.waits == [30]


def test_worker_ask_raises_content_free_fail_when_worker_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(FakeProcess(reply=b"", returncode=3)))
    with remote.Worker(["x"]) as worker, pytest.raises(Fail) as err:
        worker.ask({"op": "ping"})
    assert str(err.value) == "remote worker exited 3"


@pytest.mark.parametrize("reply", [b"SECRET-REPLY not json\n", b'["SECRET"]\n'])
def test_worker_malformed_reply_raises_content_free_fail(monkeypatch: pytest.MonkeyPatch, reply: bytes) -> None:
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(FakeProcess(reply=reply)))
    with remote.Worker(["x"]) as worker, pytest.raises(Fail) as err:
        worker.ask({"op": "ping"})
    assert str(err.value) == "remote worker sent a malformed reply"
    assert "SECRET" not in str(err.value)


def test_worker_exit_kills_a_worker_that_ignores_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(hang=True)
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(process))
    with remote.Worker(["x"]):
        pass
    assert process.stdin.closed
    assert process.killed
    assert process.waits == [30, None]


@pytest.fixture
def listeners() -> Iterator[list[socket.socket]]:
    socks: list[socket.socket] = []
    yield socks
    for sock in socks:
        sock.close()


def test_tunnel_runs_ssh_forward_with_devnull_streams_and_yields_local_port(
    monkeypatch: pytest.MonkeyPatch, listeners: list[socket.socket]
) -> None:
    process = FakeProcess()
    popen = FakePopen(process, listeners)
    monkeypatch.setattr(remote.subprocess, "Popen", popen)

    with remote.tunnel(8080) as local:
        assert isinstance(local, int)
        assert local != 0

    args, kwargs = popen.calls[0]
    assert args == [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"127.0.0.1:{local}:127.0.0.1:8080",
        "gnomon",
    ]
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


def test_tunnel_terminates_ssh_on_normal_exit(monkeypatch: pytest.MonkeyPatch, listeners: list[socket.socket]) -> None:
    process = FakeProcess()
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(process, listeners))

    with remote.tunnel(8080):
        assert not process.terminated

    assert process.terminated
    assert process.waits == [remote.WAIT_SECONDS]
    assert not process.killed


def test_tunnel_terminates_ssh_when_body_raises(monkeypatch: pytest.MonkeyPatch, listeners: list[socket.socket]) -> None:
    process = FakeProcess()
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(process, listeners))

    with pytest.raises(RuntimeError, match="body failed"), remote.tunnel(8080):
        raise RuntimeError("body failed")

    assert process.terminated
    assert process.waits == [remote.WAIT_SECONDS]


def test_tunnel_kills_ssh_that_ignores_terminate(monkeypatch: pytest.MonkeyPatch, listeners: list[socket.socket]) -> None:
    process = FakeProcess(hang=True)
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(process, listeners))

    with remote.tunnel(8080):
        pass

    assert process.terminated
    assert process.killed
    assert process.waits == [remote.WAIT_SECONDS, None]


def test_tunnel_early_ssh_exit_raises_fail_and_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(returncode=255, exited=True)
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(process))

    with pytest.raises(Fail) as err, remote.tunnel(8080):
        pytest.fail("body must not run when ssh exits first")

    assert str(err.value) == "ssh tunnel to gnomon exited 255"
    assert process.terminated


def test_tunnel_without_ssh_raises_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(remote.subprocess, "Popen", missing)

    with pytest.raises(Fail) as err, remote.tunnel(8080):
        pytest.fail("body must not run without ssh")

    assert str(err.value) == "ssh not found on PATH; cannot reach gnomon"


def test_tunnel_never_ready_raises_fail_on_timeout_and_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "TUNNEL_TIMEOUT", 0.2)
    process = FakeProcess()
    monkeypatch.setattr(remote.subprocess, "Popen", FakePopen(process))

    with pytest.raises(Fail) as err, remote.tunnel(8080):
        pytest.fail("body must not run when the tunnel is never ready")

    assert str(err.value) == "ssh tunnel to gnomon was not ready within 0.2 s"
    assert process.terminated
    assert process.waits == [remote.WAIT_SECONDS]


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "work"
    monkeypatch.setattr(remote, "work_dir", lambda lens: root / lens)
    return root


# Made-up rows for stage. Not game text.
STAGE_ROWS = [{"text": "SYNTH-ROW-ONE wording", "label": 1}, {"text": "SYNTH-ROW-TWO café", "label": 0}]
STAGED_LINES = '{"text": "SYNTH-ROW-ONE wording", "label": 1}\n{"text": "SYNTH-ROW-TWO café", "label": 0}\n'


def test_stage_writes_exact_json_lines_and_returns_count(ssh: Ssh, work: Path) -> None:
    assert remote.stage(STAGE_ROWS, "data/synth.jsonl") == 2
    assert (work / "voice" / "staging" / "synth.jsonl").read_text(encoding="utf-8") == STAGED_LINES


def test_stage_leaves_no_temp_file_beside_staged_file(ssh: Ssh, work: Path) -> None:
    remote.stage(STAGE_ROWS, "data/synth.jsonl")
    assert sorted(p.name for p in (work / "voice" / "staging").iterdir()) == ["synth.jsonl"]


def test_stage_pushes_staged_file_to_name_under_scratch(ssh: Ssh, work: Path) -> None:
    remote.stage(STAGE_ROWS, "data/synth.jsonl")
    assert ssh.calls[0][0] == ["ssh", "gnomon", f"{PREFIX} && mkdir -p -- data && cat > data/synth.jsonl"]
    assert ssh.sent == STAGED_LINES.encode("utf-8")


@pytest.mark.parametrize("name", ["", "/abs/x.jsonl", "../x.jsonl", "a/../b.jsonl", "a//b.jsonl", "a/"])
def test_stage_rejects_bad_name_before_writing(ssh: Ssh, work: Path, name: str) -> None:
    with pytest.raises(Fail):
        remote.stage(STAGE_ROWS, name)
    assert ssh.calls == []
    assert not (work / "voice").exists()


def test_stage_failure_keeps_existing_staged_file_and_removes_temp(ssh: Ssh, work: Path) -> None:
    staging = work / "voice" / "staging"
    staging.mkdir(parents=True)
    (staging / "synth.jsonl").write_bytes(b"old\n")

    def failing_rows() -> Iterator[dict[str, object]]:
        yield STAGE_ROWS[0]
        raise RuntimeError("synthetic write failure")

    with pytest.raises(RuntimeError, match="synthetic write failure"):
        remote.stage(failing_rows(), "data/synth.jsonl")
    assert (staging / "synth.jsonl").read_bytes() == b"old\n"
    assert sorted(p.name for p in staging.iterdir()) == ["synth.jsonl"]
    assert ssh.calls == []
