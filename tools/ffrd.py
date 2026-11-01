"""ffrd: build a FINAL FANTASY RESONANCE dialogue mod from .po overrides.

The game's text lives in FFRS/Content/Localization/Game/<lang>/Game.locres
inside the legacy .pak. A mod overrides it by shipping a pak with a whole
replacement Game.locres, plus a .utoc/.ucas pair the loader requires (copies
of the game's empty global container).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import polib
from pylocres import LocresFile

REPAK = "@repak@"
MOD_NAME = "FFRS-Dialog_P"
APP_ID = "3259780"
LOCRES = Path("FFRS/Content/Localization/Game")
DEFAULT_GAME = Path.home() / ".local/share/Steam/steamapps/common/FINAL FANTASY RESONANCE"

PLACEHOLDER = re.compile(r"\{[^}]*\}")
STYLE_OPEN = re.compile(r"<text\b[^>]*>")


class Fail(Exception):
    pass


def repo_root():
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "flake.nix").exists() and (d / "dialog").is_dir():
            return d
    raise Fail("run ffrd inside the ffr-dialog repository")


def game_dir():
    g = Path(os.environ.get("FFR_GAME", DEFAULT_GAME))
    if not (g / "FFRS/Content/Paks").is_dir():
        raise Fail(f"no game install at {g} (set FFR_GAME)")
    return g


def paks_dir():
    return game_dir() / "FFRS/Content/Paks"


def build_id():
    manifest = game_dir().parent.parent / f"appmanifest_{APP_ID}.acf"
    m = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text())
    if not m:
        raise Fail(f"no buildid in {manifest}")
    return m.group(1)


def natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def read_locres(path):
    f = LocresFile()
    f.read(str(path))
    return f


def vanilla(root, lang="en"):
    path = root / "vanilla/pak" / LOCRES / lang / "Game.locres"
    if not path.exists():
        raise Fail("no vanilla text yet; run `ffrd extract`")
    return read_locres(path)


def entries(locres):
    """(namespace, key, text) in scene order."""
    for ns in sorted(locres, key=lambda n: natural(n.name)):
        for key in sorted(ns.entrys, key=natural):
            yield ns.name, key, ns[key].translation


def po_entry(ns, key, text, ja):
    e = polib.POEntry(msgctxt=f"{ns},{key}", msgid=text, msgstr="")
    if ja is not None and ja != text:
        e.comment = f"ja: {ja}"
    return e


def cmd_extract(args):
    root = repo_root()
    out = root / "vanilla"
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

    en, ja = vanilla(root), vanilla(root, "ja")
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


def cmd_find(args):
    root = repo_root()
    en, ja = vanilla(root), vanilla(root, "ja")
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


def load_overrides(root, en):
    """Map (namespace, key) -> new text from dialog/**/*.po, validated against vanilla."""
    overrides, errors, warnings = {}, [], []
    for po_path in sorted((root / "dialog").rglob("*.po")):
        rel = po_path.relative_to(root)
        for e in polib.pofile(str(po_path), wrapwidth=0):
            if e.obsolete or "fuzzy" in e.flags or not e.msgstr:
                continue
            where = f"{rel}:{e.linenum}"
            if not e.msgctxt or "," not in e.msgctxt:
                errors.append(f"{where}: msgctxt must be \"<namespace>,<key>\"")
                continue
            ns, key = e.msgctxt.split(",", 1)
            entry = en[ns][key] if en[ns] else None
            if entry is None:
                errors.append(f"{where}: {e.msgctxt} is not in the game's text")
                continue
            if e.msgid != entry.translation:
                errors.append(f"{where}: {e.msgctxt} changed in the game; vanilla is now {entry.translation!r}")
                continue
            if (ns, key) in overrides:
                errors.append(f"{where}: {e.msgctxt} is overridden twice")
                continue
            added = set(PLACEHOLDER.findall(e.msgstr)) - set(PLACEHOLDER.findall(e.msgid))
            if added:
                errors.append(f"{where}: {e.msgctxt} uses placeholders the game won't fill: {sorted(added)}")
            dropped = set(PLACEHOLDER.findall(e.msgid)) - set(PLACEHOLDER.findall(e.msgstr))
            if dropped:
                warnings.append(f"{where}: {e.msgctxt} drops placeholders {sorted(dropped)}")
            if len(STYLE_OPEN.findall(e.msgstr)) != e.msgstr.count("</>"):
                errors.append(f"{where}: {e.msgctxt} has unbalanced <text ...>...</> tags")
            overrides[(ns, key)] = e.msgstr
    return overrides, errors, warnings


def cmd_build(args):
    root = repo_root()
    stamp = root / "vanilla/BUILDID"
    if not stamp.exists():
        raise Fail("no vanilla text yet; run `ffrd extract`")
    if stamp.read_text().strip() != build_id():
        raise Fail(f"the game updated to build {build_id()}; run `ffrd extract` and rebuild")
    en = vanilla(root)
    overrides, errors, warnings = load_overrides(root, en)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if errors:
        raise Fail("\n".join(errors))

    for (ns, key), text in overrides.items():
        en[ns][key].translation = text

    out = root / "build"
    if out.exists():
        shutil.rmtree(out)
    stage = out / "stage"
    locres_path = stage / LOCRES / "en/Game.locres"
    locres_path.parent.mkdir(parents=True)
    locres_path.write_bytes(en.to_binary())

    check = read_locres(locres_path)
    bad = [f"{ns},{key}" for (ns, key), text in overrides.items() if check[ns][key].translation != text]
    if bad:
        raise Fail(f"built locres lost overrides: {bad[:10]}")

    pak = out / f"{MOD_NAME}.pak"
    subprocess.run(
        [REPAK, "pack", "-q", "--version", "V11", "--compression", "Zlib", "-m", "../../../", str(stage), str(pak)],
        check=True,
    )
    for ext in ("utoc", "ucas"):
        shutil.copyfile(paks_dir() / f"global.{ext}", out / f"{MOD_NAME}.{ext}")
    print(f"{len(overrides)} lines overridden -> build/{MOD_NAME}.{{pak,utoc,ucas}}")


def cmd_install(args):
    root = repo_root()
    files = [root / "build" / f"{MOD_NAME}.{ext}" for ext in ("pak", "utoc", "ucas")]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise Fail(f"missing {missing}; run `ffrd build`")
    dest = paks_dir() / "~mods"
    dest.mkdir(exist_ok=True)
    for f in files:
        shutil.copyfile(f, dest / f.name)
    print(f"installed to {dest}")


def cmd_uninstall(args):
    dest = paks_dir() / "~mods"
    for ext in ("pak", "utoc", "ucas"):
        (dest / f"{MOD_NAME}.{ext}").unlink(missing_ok=True)
    print(f"removed {MOD_NAME} from {dest}")


def main():
    p = argparse.ArgumentParser(prog="ffrd", description="Build a FINAL FANTASY RESONANCE dialogue mod from .po overrides.")
    sub = p.add_subparsers(required=True)
    sub.add_parser("extract", help="pull the installed game's text into vanilla/").set_defaults(fn=cmd_extract)
    f = sub.add_parser("find", help="print vanilla entries as .po, ready to paste into dialog/")
    f.add_argument("pattern", nargs="?", help="regex matched against text or key (case-insensitive)")
    f.add_argument("-n", "--ns", help="regex the namespace must match, e.g. '^ST_C01_023_'")
    f.set_defaults(fn=cmd_find)
    sub.add_parser("build", help="apply dialog/*.po to vanilla text and pack the mod into build/").set_defaults(fn=cmd_build)
    sub.add_parser("install", help="copy the built mod into the game's Paks/~mods").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the mod from Paks/~mods").set_defaults(fn=cmd_uninstall)
    args = p.parse_args()
    try:
        args.fn(args)
    except Fail as e:
        print(f"ffrd: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
