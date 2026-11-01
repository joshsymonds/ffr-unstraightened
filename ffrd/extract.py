"""extract: pull the installed game's text into vanilla/."""

import argparse
import shutil
import subprocess

import polib

from ffrd.game import LOCRES, build_id, entries, natural, paks_dir, po_entry, vanilla, vanilla_dir

REPAK = "repak"


def cmd_extract(_args: argparse.Namespace) -> None:
    out = vanilla_dir()
    if out.exists():
        shutil.rmtree(out)
    (out / "pak").mkdir(parents=True)
    # Base pak first, then patch paks in mount order so later ones win.
    paks = sorted(paks_dir().glob("FFRS-Windows*.pak"), key=lambda p: natural(p.stem))
    for pak in paks:
        listing = subprocess.run([REPAK, "list", str(pak)], check=True, capture_output=True, text=True).stdout
        if f"{LOCRES}/" not in listing:
            continue
        subprocess.run([REPAK, "unpack", "-q", "-f", "-o", str(out / "pak"), "-i", str(LOCRES), str(pak)], check=True)
        print(f"extracted text from {pak.name}")
    bid = build_id()
    (out / "BUILDID").write_text(bid + "\n")

    en, ja = vanilla(), vanilla("ja")
    ref = polib.POFile(wrapwidth=0)
    ref.metadata = {"Content-Type": "text/plain; charset=UTF-8", "Language": "en", "X-Game-Build": bid}
    count = 0
    for ns, key, text in entries(en):
        if text == "":
            continue
        j = ja[ns][key] if ja[ns] else None
        ref.append(po_entry(ns, key, text, j.translation if j else None))
        count += 1
    ref.save(str(out / "en.po"))
    print(f"build {bid}: {count} English entries -> vanilla/en.po")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    subparsers.add_parser("extract", help="pull the installed game's text into vanilla/").set_defaults(fn=cmd_extract)
