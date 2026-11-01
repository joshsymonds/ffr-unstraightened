"""install and uninstall: copy the built mod into the game's Paks/~mods, and remove it again."""

import argparse
import shutil

from ffrd import deck
from ffrd.game import MOD_NAME, Fail, paks_dir, repo_root

EXTS = ("pak", "utoc", "ucas")
DECK_HELP = "copy to the Steam Deck over ssh instead of the local game: host from FFR_DECK_HOST (default deck), Paks from FFR_DECK_PAKS"
DECK_UNINSTALL_HELP = (
    "remove from the Steam Deck over ssh instead of the local game: host from FFR_DECK_HOST (default deck), Paks from FFR_DECK_PAKS"
)


def cmd_install(args: argparse.Namespace) -> None:
    root = repo_root()
    files = [root / "build" / f"{MOD_NAME}.{ext}" for ext in EXTS]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise Fail(f"missing {missing}; run `ffrd build`")
    if args.deck:
        deck.install(files)
        print(f"installed to {deck.target()}")
        return
    dest = paks_dir() / "~mods"
    dest.mkdir(exist_ok=True)
    for f in files:
        shutil.copyfile(f, dest / f.name)
    print(f"installed to {dest}")


def cmd_uninstall(args: argparse.Namespace) -> None:
    names = [f"{MOD_NAME}.{ext}" for ext in EXTS]
    if args.deck:
        deck.uninstall(names)
        print(f"removed {MOD_NAME} from {deck.target()}")
        return
    dest = paks_dir() / "~mods"
    for name in names:
        (dest / name).unlink(missing_ok=True)
    print(f"removed {MOD_NAME} from {dest}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    install = subparsers.add_parser("install", help="copy the built mod into the game's Paks/~mods")
    install.add_argument("--deck", action="store_true", help=DECK_HELP)
    install.set_defaults(fn=cmd_install)
    uninstall = subparsers.add_parser("uninstall", help="remove the mod from Paks/~mods")
    uninstall.add_argument("--deck", action="store_true", help=DECK_UNINSTALL_HELP)
    uninstall.set_defaults(fn=cmd_uninstall)
