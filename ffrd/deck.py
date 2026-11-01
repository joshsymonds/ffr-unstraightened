"""Steam Deck transport: copy the built mod into the Deck's Paks/~mods and remove it again, over ssh."""

import os
import shlex
import subprocess
from pathlib import Path
from typing import IO

from ffrd.game import Fail

DEFAULT_HOST = "deck"
DEFAULT_PAKS = "~/.local/share/Steam/steamapps/common/FINAL FANTASY RESONANCE/FFRS/Content/Paks"


def host() -> str:
    return os.environ.get("FFR_DECK_HOST", DEFAULT_HOST)


def paks() -> str:
    return os.environ.get("FFR_DECK_PAKS", DEFAULT_PAKS)


def target() -> str:
    return f"{host()}:{paks()}/~mods"


def _remote(path: str) -> str:
    # Only a leading ~/ is left unquoted, so the remote shell expands it to the Deck's home.
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _ssh(command: str, stdin: IO[bytes] | int) -> None:
    try:
        result = subprocess.run(["ssh", host(), command], stdin=stdin, check=False)
    except FileNotFoundError as e:
        raise Fail(f"ssh not found on PATH; cannot reach {host()}") from e
    if result.returncode != 0:
        raise Fail(f"ssh to {host()} exited {result.returncode}")


def install(files: list[Path]) -> None:
    dest = _remote(f"{paks()}/~mods")
    for f in files:
        with f.open("rb") as data:
            _ssh(f"mkdir -p {dest} && cat > {dest}/{shlex.quote(f.name)}", data)


def uninstall(names: list[str]) -> None:
    dest = _remote(f"{paks()}/~mods")
    _ssh("rm -f " + " ".join(f"{dest}/{shlex.quote(name)}" for name in names), subprocess.DEVNULL)
