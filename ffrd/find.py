"""find: print vanilla entries as .po, ready to paste into dialog/."""

import argparse
import re
import sys

import polib

from ffrd.game import entries, po_entry, vanilla


def cmd_find(args: argparse.Namespace) -> None:
    en, ja = vanilla(), vanilla("ja")
    ns_re = re.compile(args.ns) if args.ns else None
    text_re = re.compile(args.pattern, re.IGNORECASE) if args.pattern else None
    out = polib.POFile(wrapwidth=0)
    for ns, key, text in entries(en):
        if ns_re and not ns_re.search(ns):
            continue
        if text_re and not (text_re.search(text) or text_re.search(key)):
            continue
        if text == "":
            continue
        j = ja[ns][key] if ja[ns] else None
        out.append(po_entry(ns, key, text, j.translation if j else None))
    for e in out:
        print(e)
    print(f"# {len(out)} entries", file=sys.stderr)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    f = subparsers.add_parser("find", help="print vanilla entries as .po, ready to paste into dialog/")
    f.add_argument("pattern", nargs="?", help="regex matched against text or key (case-insensitive)")
    f.add_argument("-n", "--ns", help="regex the namespace must match, e.g. '^ST_C01_023_'")
    f.set_defaults(fn=cmd_find)
