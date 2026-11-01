"""The `ffrd voice` command group: each module in SUBCOMMANDS registers one subcommand under `voice`."""

import argparse
from types import ModuleType

from ffrd.voice import pairs

SUBCOMMANDS: tuple[ModuleType, ...] = (pairs,)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("voice", help="build the voice models' training data and evaluate them")
    sub = p.add_subparsers(required=True)
    for module in SUBCOMMANDS:
        module.register(sub)
