"""GPU host transport: ssh to gnomon, sync the voice tools into a scratch dir there, move files and run commands in it."""

import contextlib
import hashlib
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import IO, Any, Self

from ffrd import deck
from ffrd.game import Fail, repo_root
from ffrd.pipeline.store import work_dir

DEFAULT_HOST = "gnomon"
DEFAULT_SCRATCH = "~/.local/share/ffr-unstraightened/voice"
# Each detached job gets a directory named from a digest of its argv, under this one.
JOBS = "jobs"
# Runs from the scratch dir: the cuda profile of the devenv in tools/voice, then argv from the scratch root.
DEVENV = "env -C tools/voice devenv shell --profile cuda --"
# Clears the synced tools/voice but keeps the GPU host's devenv state, which is never synced.
CLEAR_VOICE = "mkdir -p tools/voice && find tools/voice -mindepth 1 -maxdepth 1 ! -name .devenv -exec rm -rf -- {} +"
EXCLUDED_PARTS = frozenset({"__pycache__", ".devenv"})
WAIT_SECONDS = 30
# Each poll of a remote job is one short ssh; a poll reports whether the job finished with one of these statuses.
POLL_SECONDS = 5
POLL_FINISHED = 0
POLL_RUNNING = 3
POLL_VANISHED = 4
TUNNEL_TIMEOUT = 30.0


def host() -> str:
    return os.environ.get("FFR_VOICE_HOST", DEFAULT_HOST)


def scratch() -> str:
    return os.environ.get("FFR_VOICE_SCRATCH", DEFAULT_SCRATCH)


def env(argv: Sequence[str]) -> str:
    return f"{DEVENV} env -C ../.. {shlex.join(argv)}"


def _prefix() -> str:
    return f"umask 077 && mkdir -p {deck.remote(f'{scratch()}/logs')} && cd {deck.remote(scratch())}"


def _ssh(command: str, **streams: Any) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["ssh", host(), command], check=False, **streams)
    except FileNotFoundError as e:
        raise Fail(f"ssh not found on PATH; cannot reach {host()}") from e


def _require_ok(code: int, what: str) -> None:
    if code != 0:
        raise Fail(f"{host()} {what} exited {code}")


def _check_name(name: str) -> None:
    if any(part in ("", "..") for part in name.split("/")):
        raise Fail(f"not a relative path inside the scratch dir: {name}")


def shell(command: str) -> None:
    _require_ok(_ssh(f"{_prefix()} && {command}", stdin=subprocess.DEVNULL).returncode, "command")


def _job_dir(argv: Sequence[str]) -> str:
    digest = hashlib.sha256(json.dumps(list(argv)).encode("utf-8")).hexdigest()[:16]
    return f"{JOBS}/{digest}"


def _write(data: bytes) -> None:
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    sys.stdout.flush()


def _emit_complete_lines(buffered: bytes) -> bytes:
    complete, newline, rest = buffered.rpartition(b"\n")
    if newline:
        _write(complete + newline)
    return rest


def run(argv: Sequence[str]) -> None:
    # The job runs detached on the host, so it outlives this process. Stdout goes to the job's log and the exit
    # status to its exit file. A rerun while the job is alive reattaches; otherwise the job starts afresh. Stderr
    # goes to the remote log.
    job = _job_dir(argv)
    out = f"{job}/out.log"
    exit_file = f"{job}/exit"
    pid = f"{job}/pid"
    inner = (
        f"{env(['env', 'PYTHONUNBUFFERED=1', *argv])} > {out} 2>> logs/remote.log; "
        f"echo $? > {exit_file}.tmp && mv {exit_file}.tmp {exit_file}"
    )
    alive = f"[ ! -f {exit_file} ] && [ -f {pid} ] && kill -0 -- -$(cat {pid}) 2>/dev/null"
    start = (
        f"{_prefix()} && mkdir -p {job} && {{ if {alive}; then :; else "
        f"rm -f {exit_file} {exit_file}.tmp && : > {out} && "
        f"{{ setsid nohup sh -c {shlex.quote(inner)} > /dev/null 2>&1 < /dev/null & echo $! > {pid}; }}; fi; }}"
    )
    _require_ok(_ssh(start, stdin=subprocess.DEVNULL).returncode, "command")
    offset = 0
    pending = b""
    while True:
        poll = (
            f"{_prefix()} && {{ if [ -f {exit_file} ]; then s={POLL_FINISHED}; "
            f"elif kill -0 -- -$(cat {pid}) 2>/dev/null; then s={POLL_RUNNING}; "
            # The job may write its exit file and end between the two checks above, so look again before giving up.
            f"elif [ -f {exit_file} ]; then s={POLL_FINISHED}; else s={POLL_VANISHED}; fi; "
            f"tail -c +{offset + 1} {out}; exit $s; }}"
        )
        result = _ssh(poll, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        if result.returncode == POLL_VANISHED:
            raise Fail(f"{host()} job ended without an exit status")
        if result.returncode not in (POLL_FINISHED, POLL_RUNNING):
            _require_ok(result.returncode, "command")
        offset += len(result.stdout)
        pending = _emit_complete_lines(pending + result.stdout)
        if result.returncode == POLL_FINISHED:
            break
        time.sleep(POLL_SECONDS)
    if pending:
        _write(pending)
    _require_ok(_ssh(f"{_prefix()} && exit $(cat {exit_file})", stdin=subprocess.DEVNULL).returncode, "command")


def sync() -> None:
    root = repo_root()
    base = root / "tools" / "voice"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(base.rglob("*")):
            if path.is_file() and EXCLUDED_PARTS.isdisjoint(path.relative_to(base).parts):
                tar.add(path, arcname=path.relative_to(root).as_posix())
    _require_ok(_ssh(f"{_prefix()} && {CLEAR_VOICE} && tar -x -f -", input=buf.getvalue()).returncode, "sync")


def push(local: Path, name: str) -> None:
    _check_name(name)
    parent = PurePosixPath(name).parent
    with local.open("rb") as data:
        command = f"{_prefix()} && mkdir -p -- {shlex.quote(str(parent))} && cat > {shlex.quote(name)}"
        _require_ok(_ssh(command, stdin=data).returncode, "push")


def stage(rows: Iterable[Mapping[str, Any]], name: str) -> int:
    _check_name(name)
    staging = work_dir("voice") / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / PurePosixPath(name).name
    fd, tmp = tempfile.mkstemp(dir=staging, prefix=f".{path.name}.", suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    push(path, name)
    return count


@contextlib.contextmanager
def tunnel(remote_port: int) -> Iterator[int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        local = probe.getsockname()[1]
    try:
        proc = subprocess.Popen(
            [
                "ssh",
                "-N",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"127.0.0.1:{local}:127.0.0.1:{remote_port}",
                host(),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise Fail(f"ssh not found on PATH; cannot reach {host()}") from e
    try:
        _await_tunnel(proc, local)
        yield local
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _await_tunnel(proc: subprocess.Popen[bytes], local: int) -> None:
    deadline = time.monotonic() + TUNNEL_TIMEOUT
    while True:
        code = proc.poll()
        if code is not None:
            raise Fail(f"ssh tunnel to {host()} exited {code}")
        try:
            with socket.create_connection(("127.0.0.1", local), timeout=1):
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise Fail(f"ssh tunnel to {host()} was not ready within {TUNNEL_TIMEOUT:g} s")
        time.sleep(0.1)


class Worker:
    def __init__(self, argv: Sequence[str]) -> None:
        self._command = f"{_prefix()} && {env(argv)} 2>> logs/worker.log"
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> Self:
        try:
            self._proc = subprocess.Popen(["ssh", host(), self._command], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except FileNotFoundError as e:
            raise Fail(f"ssh not found on PATH; cannot reach {host()}") from e
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.close()
        try:
            proc.wait(timeout=WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()

    def _pipes(self) -> tuple[subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise RuntimeError("Worker used outside its with block")
        return proc, proc.stdin, proc.stdout

    def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        proc, stdin, stdout = self._pipes()
        line = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        try:
            stdin.write(line)
            stdin.flush()
            raw = stdout.readline()
        except BrokenPipeError:
            raw = b""
        if not raw:
            raise Fail(f"remote worker exited {proc.wait()}")
        try:
            reply = json.loads(raw)
        except ValueError as e:
            raise Fail("remote worker sent a malformed reply") from e
        if not isinstance(reply, dict):
            raise Fail("remote worker sent a malformed reply")
        return reply
