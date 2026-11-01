"""Steam Deck transport: ssh to a host, copy the built mod into its Paks/~mods and remove it again."""

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


def remote(path: str) -> str:
    # Only a leading ~/ is left unquoted, so the remote shell expands it to the remote home.
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def ssh(host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
    try:
        result = subprocess.run(["ssh", host, command], stdin=stdin, stdout=subprocess.PIPE, check=False)
    except FileNotFoundError as e:
        raise Fail(f"ssh not found on PATH; cannot reach {host}") from e
    if result.returncode != 0:
        raise Fail(f"ssh to {host} exited {result.returncode}")
    return result.stdout


def ssh_status(host: str, command: str) -> int:
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as e:
        raise Fail(f"ssh not found on PATH; cannot reach {host}") from e
    return result.returncode


def put(host: str, dest: str, files: list[Path]) -> None:
    remote_dest = remote(dest)
    for f in files:
        with f.open("rb") as data:
            ssh(host, f"mkdir -p {remote_dest} && cat > {remote_dest}/{shlex.quote(f.name)}", data)


def delete(host: str, dest: str, names: list[str]) -> None:
    remote_dest = remote(dest)
    ssh(host, "rm -f " + " ".join(f"{remote_dest}/{shlex.quote(name)}" for name in names))


def install(files: list[Path]) -> None:
    put(host(), f"{paks()}/~mods", files)


def uninstall(names: list[str]) -> None:
    delete(host(), f"{paks()}/~mods", names)
