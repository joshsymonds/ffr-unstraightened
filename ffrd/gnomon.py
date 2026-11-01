"""Runs the UE4SS probe mod on a Steam machine over ssh: install it, launch FFR, read its log, quit."""

import os
import re
import shlex
import tempfile
import time
from pathlib import Path

from ffrd import deck
from ffrd.game import APP_ID, Fail

DEFAULT_HOST = "gnomon"
DEFAULT_GAME = "~/.local/share/Steam/steamapps/common/FINAL FANTASY RESONANCE"
GAME_PATTERN = "FINAL FANTASY RESONANC[E]"
EXE_PATTERN = "FFRS-Win64-Shippin[g]"
LAUNCH_ATTEMPTS = 4
EXE_TIMEOUT = 120
STALL_WINDOW = 90
DONE_TIMEOUT = 300
QUIT_TIMEOUT = 60
POLL_INTERVAL = 5
BUILDID = re.compile(r'"buildid"\s+"([^"]+)"')


def host() -> str:
    return os.environ.get("FFR_PROBE_HOST", DEFAULT_HOST)


def game() -> str:
    return os.environ.get("FFR_PROBE_GAME", DEFAULT_GAME)


def paks() -> str:
    return f"{game()}/FFRS/Content/Paks"


def ue4ss() -> str:
    return f"{game()}/FFRS/Binaries/Win64/ue4ss"


def _matches(pattern: str) -> bool:
    status = deck.ssh_status(host(), f"pgrep -f '{pattern}'")
    if status not in (0, 1):
        raise Fail(f"pgrep on {host()} exited {status}")
    return status == 0


def running() -> bool:
    return _matches(GAME_PATTERN)


def build_id() -> str:
    manifest = f"{game()}/../../appmanifest_{APP_ID}.acf"
    text = deck.ssh(host(), f"cat {deck.remote(manifest)}").decode("utf-8", errors="replace")
    match = BUILDID.search(text)
    if match is None:
        raise Fail(f"no buildid in appmanifest_{APP_ID}.acf on {host()}")
    return match.group(1)


def fetch(path: str) -> bytes:
    return deck.ssh(host(), f"cat {deck.remote(path)}")


def run_mod(mod: Path, files: dict[str, bytes], log: str) -> list[str]:
    if mod.name in ("", ".."):
        raise Fail(f"mod folder {mod} has no name")
    if running():
        raise Fail(f"FINAL FANTASY RESONANCE is running on {host()}; refusing to start the probe")
    dest = f"{ue4ss()}/Mods/{mod.name}"
    try:
        _install(dest, mod, files)
        for attempt in range(LAUNCH_ATTEMPTS):
            if attempt:
                _quit()
            lines = _launch(f"{dest}/{log}")
            if lines is not None:
                return lines
        raise Fail(f"FFR stalled on {host()} in all {LAUNCH_ATTEMPTS} launches")
    finally:
        try:
            _quit()
        finally:
            deck.ssh(host(), f"rm -rf {deck.remote(dest)}")


def _install(dest: str, mod: Path, files: dict[str, bytes]) -> None:
    deck.ssh(host(), f"rm -rf {deck.remote(dest)}")
    for src in sorted(mod.rglob("*")):
        if src.is_file():
            _write(f"{dest}/{src.relative_to(mod).as_posix()}", src.read_bytes())
    _write(f"{dest}/enabled.txt", b"")
    for name, data in files.items():
        _write(f"{dest}/{name}", data)


def _write(path: str, data: bytes) -> None:
    parent, name = path.rsplit("/", 1)
    with tempfile.TemporaryFile() as stdin:
        stdin.write(data)
        stdin.seek(0)
        deck.ssh(
            host(),
            f"mkdir -p {deck.remote(parent)} && cat > {deck.remote(parent)}/{shlex.quote(name)}",
            stdin,
        )


def _launch(log: str) -> list[str] | None:
    # A log left by an earlier launch could already end in done, so it goes before every launch.
    deck.ssh(host(), f"rm -f {deck.remote(log)}")
    deck.ssh(host(), f"timeout 30 steam steam://rungameid/{APP_ID}")
    _wait_for_exe()
    started = time.monotonic()
    while True:
        lines = _log_lines(log)
        if lines and lines[-1] == "done":
            return lines
        if not _matches(EXE_PATTERN):
            raise Fail(f"FFR exited on {host()} before the mod log reached done")
        elapsed = time.monotonic() - started
        if not lines and elapsed >= STALL_WINDOW:
            return None
        if elapsed >= DONE_TIMEOUT:
            raise Fail(f"no done line from the mod on {host()} within {DONE_TIMEOUT}s")
        time.sleep(POLL_INTERVAL)


def _wait_for_exe() -> None:
    deadline = time.monotonic() + EXE_TIMEOUT
    while not _matches(EXE_PATTERN):
        if time.monotonic() >= deadline:
            raise Fail(f"FFR did not start on {host()} within {EXE_TIMEOUT}s")
        time.sleep(POLL_INTERVAL)


def _log_lines(log: str) -> list[str]:
    data = deck.ssh(host(), f"cat {deck.remote(log)} 2>/dev/null || true")
    text = data.decode("utf-8", errors="replace").replace("\r", "")
    return [line for line in text.split("\n") if line]


def _quit() -> None:
    # pkill exits 1 when no exe matched; the running() wait below is the real check.
    deck.ssh_status(host(), f"pkill -KILL -f '{EXE_PATTERN}'")
    deadline = time.monotonic() + QUIT_TIMEOUT
    while running():
        if time.monotonic() >= deadline:
            raise Fail(f"FFR still running on {host()} {QUIT_TIMEOUT}s after the kill")
        time.sleep(POLL_INTERVAL)
