"""find: print vanilla entries as TOML stubs, ready to paste into dialog/."""

import argparse
import re
import sys

from ffrd.game import Fail, entries, vanilla, vanilla_hash
from ffrd.progress import recorded

BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
ST_CHAPTER = re.compile(r"ST_C(\d+)_")
SEGMENT_CHAPTER = re.compile(r"C(\d+)")


def toml_string(s: str) -> str:
    out = []
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def stub(ns: str, key: str, text: str) -> str:
    bare_or_quoted = key if BARE_KEY.fullmatch(key) else toml_string(key)
    return f"[{toml_string(ns)}.{bare_or_quoted}]\nvanilla = {toml_string(vanilla_hash(text))}\ntext = {toml_string(text)}\n\n"


def chapter(ns: str) -> int | None:
    if m := ST_CHAPTER.match(ns):
        return int(m.group(1))
    for segment in ns.split("_")[1:]:
        if m := SEGMENT_CHAPTER.fullmatch(segment):
            return int(m.group(1))
    return None


def cmd_find(args: argparse.Namespace) -> None:
    limit = recorded()
    if limit is None:
        raise Fail("no progress recorded; run `ffrd progress <chapter>`")
    en = vanilla()
    ns_re = re.compile(args.ns) if args.ns else None
    text_re = re.compile(args.pattern, re.IGNORECASE) if args.pattern else None
    shown = 0
    withheld = 0
    for ns, key, text in entries(en):
        if text == "":
            continue
        ch = chapter(ns)
        if ch is None or ch > limit:
            withheld += 1
            continue
        if ns_re and not ns_re.search(ns):
            continue
        if text_re and not (text_re.search(text) or text_re.search(key)):
            continue
        print(stub(ns, key, text), end="")
        shown += 1
    print(f"# {shown} shown, {withheld} withheld", file=sys.stderr)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    f = subparsers.add_parser("find", help="print vanilla entries as TOML stubs, ready to paste into dialog/")
    f.add_argument("pattern", nargs="?", help="regex matched against text or key (case-insensitive)")
    f.add_argument("-n", "--ns", help="regex the namespace must match, e.g. '^ST_C01_023_'")
    f.set_defaults(fn=cmd_find)
