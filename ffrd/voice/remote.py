"""GPU host transport: ssh to gnomon, sync the voice tools into a scratch dir there, move files and run commands in it."""

import contextlib
import io
import json
import os
import shlex
import socket
import subprocess
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
# Runs from the scratch dir: the cuda profile of the devenv in tools/voice, then argv from the scratch root.
DEVENV = "env -C tools/voice devenv shell --profile cuda --"
# Clears the synced tools/voice but keeps the GPU host's devenv state, which is never synced.
CLEAR_VOICE = "mkdir -p tools/voice && find tools/voice -mindepth 1 -maxdepth 1 ! -name .devenv -exec rm -rf -- {} +"
EXCLUDED_PARTS = frozenset({"__pycache__", ".devenv"})
WAIT_SECONDS = 30
TUNNEL_TIMEOUT = 30.0


def host() -> str:
    return os.environ.get("FFR_VOICE_HOST", DEFAULT_HOST)


def scratch() -> str:
    return os.environ.get("FFR_VOICE_SCRATCH", DEFAULT_SCRATCH)


def env(argv: Sequence[str]) -> str:
    return f"{DEVENV} env -C ../.. {shlex.join(argv)}"


def _prefix() -> str:
    return f"umask 077 && mkdir -p {deck.remote(f'{scratch()}/logs')} && cd {deck.remote(scratch())}"


def _ssh(command: str, **streams: Any) -> int:
    try:
        result = subprocess.run(["ssh", host(), command], check=False, **streams)
    except FileNotFoundError as e:
        raise Fail(f"ssh not found on PATH; cannot reach {host()}") from e
    return result.returncode


def _require_ok(code: int, what: str) -> None:
    if code != 0:
        raise Fail(f"{host()} {what} exited {code}")


def _check_name(name: str) -> None:
    if any(part in ("", "..") for part in name.split("/")):
        raise Fail(f"not a relative path inside the scratch dir: {name}")


def shell(command: str) -> None:
    _require_ok(_ssh(f"{_prefix()} && {command}", stdin=subprocess.DEVNULL), "command")


def run(argv: Sequence[str]) -> None:
    shell(f"{env(argv)} 2>> logs/remote.log")


def sync() -> None:
    root = repo_root()
    base = root / "tools" / "voice"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(base.rglob("*")):
            if path.is_file() and EXCLUDED_PARTS.isdisjoint(path.relative_to(base).parts):
                tar.add(path, arcname=path.relative_to(root).as_posix())
    _require_ok(_ssh(f"{_prefix()} && {CLEAR_VOICE} && tar -x -f -", input=buf.getvalue()), "sync")


def push(local: Path, name: str) -> None:
    _check_name(name)
    parent = PurePosixPath(name).parent
    with local.open("rb") as data:
        command = f"{_prefix()} && mkdir -p -- {shlex.quote(str(parent))} && cat > {shlex.quote(name)}"
        _require_ok(_ssh(command, stdin=data), "push")


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


def pull(name: str, local: Path) -> None:
    _check_name(name)
    local.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=local.parent, prefix=f".{local.name}.")
    try:
        with os.fdopen(fd, "wb") as out:
            code = _ssh(f"{_prefix()} && cat -- {shlex.quote(name)}", stdout=out)
        _require_ok(code, "pull")
        os.replace(tmp, local)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def tunnel(remote_port: int) -> Iterator[int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        local = probe.getsockname()[1]
    try:
        proc = subprocess.Popen(
            ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{local}:127.0.0.1:{remote_port}", host()],
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
