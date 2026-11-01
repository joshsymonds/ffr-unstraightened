"""build: apply dialog/**/*.po to the vanilla text and pack the mod into build/.

The game's text lives in FFRS/Content/Localization/Game/<lang>/Game.locres
inside the legacy .pak. A mod overrides it by shipping a pak with a whole
replacement Game.locres, plus a .utoc/.ucas pair the loader requires (copies
of the game's empty global container).
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import polib
from pylocres import LocresFile

from ffrd.game import LOCRES, MOD_NAME, Fail, build_id, paks_dir, read_locres, repo_root, vanilla, vanilla_dir

REPAK = "repak"

PLACEHOLDER = re.compile(r"\{[^}]*\}")
STYLE_OPEN = re.compile(r"<text\b[^>]*>")


def load_overrides(root: Path, en: LocresFile) -> tuple[dict[tuple[str, str], str], list[str], list[str]]:
    """Map (namespace, key) -> new text from dialog/**/*.po, validated against vanilla."""
    overrides: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for po_path in sorted((root / "dialog").rglob("*.po")):
        rel = po_path.relative_to(root)
        for e in polib.pofile(str(po_path), wrapwidth=0):
            if e.obsolete or "fuzzy" in e.flags or not e.msgstr:
                continue
            where = f"{rel}:{e.linenum}"
            if not e.msgctxt or "," not in e.msgctxt:
                errors.append(f'{where}: msgctxt must be "<namespace>,<key>"')
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


def cmd_build(_args: argparse.Namespace) -> None:
    root = repo_root()
    stamp = vanilla_dir() / "BUILDID"
    if not stamp.exists():
        raise Fail("no vanilla text yet; run `ffrd extract`")
    if stamp.read_text().strip() != build_id():
        raise Fail(f"the game updated to build {build_id()}; run `ffrd extract` and rebuild")
    en = vanilla()
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


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    subparsers.add_parser("build", help="apply dialog/*.po to vanilla text and pack the mod into build/").set_defaults(fn=cmd_build)
