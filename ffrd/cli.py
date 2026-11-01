"""ffrd command line: builds the parser from the command modules and dispatches to them."""

import argparse
import sys
from types import ModuleType

from ffrd import install
from ffrd.game import Fail

COMMANDS: tuple[ModuleType, ...] = (install,)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ffrd", description="Build a FINAL FANTASY RESONANCE dialogue mod from .po overrides.")
    sub = p.add_subparsers(required=True)
    for module in COMMANDS:
        module.register(sub)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except Fail as e:
        print(f"ffrd: {e}", file=sys.stderr)
        sys.exit(1)
