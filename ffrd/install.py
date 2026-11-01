"""install and uninstall: copy the built mod into the game's Paks/~mods, and remove it again."""

import argparse
import shutil

from ffrd.game import MOD_NAME, Fail, paks_dir, repo_root

EXTS = ("pak", "utoc", "ucas")


def cmd_install(_args: argparse.Namespace) -> None:
    root = repo_root()
    files = [root / "build" / f"{MOD_NAME}.{ext}" for ext in EXTS]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise Fail(f"missing {missing}; run `ffrd build`")
    dest = paks_dir() / "~mods"
    dest.mkdir(exist_ok=True)
    for f in files:
        shutil.copyfile(f, dest / f.name)
    print(f"installed to {dest}")


def cmd_uninstall(_args: argparse.Namespace) -> None:
    dest = paks_dir() / "~mods"
    for ext in EXTS:
        (dest / f"{MOD_NAME}.{ext}").unlink(missing_ok=True)
    print(f"removed {MOD_NAME} from {dest}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    subparsers.add_parser("install", help="copy the built mod into the game's Paks/~mods").set_defaults(fn=cmd_install)
    subparsers.add_parser("uninstall", help="remove the mod from Paks/~mods").set_defaults(fn=cmd_uninstall)
