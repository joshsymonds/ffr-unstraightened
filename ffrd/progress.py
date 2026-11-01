"""progress: record the chapter you are on, or print the recorded one."""

import argparse
import os
import re
from pathlib import Path

from ffrd.game import Fail

DIGITS = re.compile(r"[0-9]+")


def progress_file() -> Path:
    if env := os.environ.get("FFR_PROGRESS"):
        return Path(env)
    if xdg := os.environ.get("XDG_STATE_HOME"):
        return Path(xdg) / "ffrd" / "progress"
    return Path.home() / ".local/state/ffrd/progress"


def _chapter(text: str) -> int | None:
    if not DIGITS.fullmatch(text):
        return None
    try:
        n = int(text)
    except ValueError:
        return None
    return n if n > 0 else None


def recorded() -> int | None:
    path = progress_file()
    try:
        n = _chapter(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Fail(f"cannot read progress from {path}: {e.strerror}") from e
    except UnicodeDecodeError:
        n = None
    if n is None:
        raise Fail(f"progress file {path} does not hold a positive integer")
    return n


def record(chapter: int) -> None:
    path = progress_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{chapter}\n", encoding="utf-8")
    except OSError as e:
        raise Fail(f"cannot write progress to {path}: {e.strerror}") from e


def cmd_progress(args: argparse.Namespace) -> None:
    if args.chapter is None:
        n = recorded()
        if n is None:
            raise Fail("no progress recorded; run `ffrd progress <chapter>`")
    else:
        n = _chapter(args.chapter)
        if n is None:
            raise Fail(f"chapter must be a positive integer, got {args.chapter!r}")
        record(n)
    print(f"chapter {n}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("progress", help="record the chapter you are on, or print the recorded one")
    p.add_argument("chapter", nargs="?", help="chapter to record; omit to print the recorded chapter")
    p.set_defaults(fn=cmd_progress)
