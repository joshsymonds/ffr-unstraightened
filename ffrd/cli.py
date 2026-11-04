"""ffrd command line: builds the parser from the command modules and dispatches to them."""

import argparse
import sys
from types import ModuleType

from ffrd import build, extract, find, install, leakcheck, probe, progress, scenes, shot
from ffrd.game import Fail
from ffrd.pipeline import diff, report, run, smoke
from ffrd.voice import cli as voice

COMMANDS: tuple[ModuleType, ...] = (
    extract,
    progress,
    find,
    build,
    install,
    leakcheck,
    report,
    diff,
    run,
    scenes,
    voice,
    shot,
    smoke,
    probe,
)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ffrd", description="Build a FINAL FANTASY RESONANCE dialogue mod from dialog/**/*.toml edits.")
    sub = p.add_subparsers(required=True)
    for module in COMMANDS:
        module.register(sub)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except Fail as e:
        print(f"ffrd: {e}", file=sys.stderr)
        sys.exit(1)
