import io
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.voice import remote

# The default scratch dir, prepared the way every remote command prepares it.
PREFIX = "umask 077 && mkdir -p ~/.local/share/ffr-unstraightened/voice/logs && cd ~/.local/share/ffr-unstraightened/voice"
NIX = "nix develop path:./tools/voice#cuda --command"


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
    def __init__(self, reply: bytes = b"", returncode: int = 0, hang: bool = False) -> None:
        self.stdin = FakePipe()
        self.stdout = FakePipe(reply)
        self.returncode = returncode
        self.hang = hang
        self.killed = False
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.hang and not self.killed and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class FakePopen:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
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


def test_env_wraps_shell_quoted_argv_in_nix_develop() -> None:
    assert remote.env(["python3", "-m", "pytest", "a b"]) == f"{NIX} python3 -m pytest 'a b'"


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
    assert ssh.calls[0][0][2] == f"{PREFIX} && {NIX} python3 x.py 2>> logs/remote.log"


def test_sync_tar_holds_only_tools_voice_files_without_pycache(ssh: Ssh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    voice = tmp_path / "tools" / "voice"
    (voice / "pkg").mkdir(parents=True)
    (voice / "__pycache__").mkdir()
    (voice / "flake.nix").write_text("{}\n")
    (voice / "pkg" / "a.py").write_text("x = 1\n")
    (voice / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    (tmp_path / "ffrd").mkdir()
    (tmp_path / "ffrd" / "b.py").write_text("y = 2\n")
    monkeypatch.setattr(remote, "repo_root", lambda: tmp_path)

    remote.sync()

    args, kwargs = ssh.calls[0]
    assert args == ["ssh", "gnomon", f"{PREFIX} && rm -rf -- tools/voice && tar -x -f -"]
    with tarfile.open(fileobj=io.BytesIO(kwargs["input"])) as tar:
        names = sorted(tar.getnames())
    assert names == ["tools/voice/flake.nix", "tools/voice/pkg/a.py"]


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
        f"{PREFIX} && {NIX} python3 serve.py 2>> logs/worker.log",
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
