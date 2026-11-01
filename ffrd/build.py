"""build: apply dialog/**/*.toml edits to the vanilla text and pack the mod into build/.

The game's text lives in FFRS/Content/Localization/Game/<lang>/Game.locres
inside the legacy .pak. The mod ships its own English locres in the same
folder, plus a .utoc/.ucas pair the loader requires (copies of the game's
empty global container). Two shapes:

- default: en/Game.locres, holding the whole English table with the edits
  applied. It replaces the game's table, and the game was seen to honour it.
- --partial: en/Unstraightened.locres, holding only the edited entries, each
  under its namespace, key and vanilla source-string hash. The game was seen
  to ignore a second en/*.locres for an entry Game.locres already holds, so
  this shape does not change those entries.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from pylocres import Entry, LocresFile, Namespace

from ffrd.game import LOCRES, MOD_NAME, Fail, build_id, paks_dir, read_locres, repo_root, vanilla, vanilla_dir, vanilla_hash

REPAK = "repak"

PLACEHOLDER = re.compile(r"\{[^}]*\}")
STYLE_OPEN = re.compile(r"<text\b[^>]*>")


def load_overrides(root: Path, en: LocresFile) -> tuple[dict[tuple[str, str], str], list[str], list[str]]:
    """Map (namespace, key) -> new text from dialog/**/*.toml, validated against the game's English text.

    Messages name the file and namespace,key but never the vanilla line.
    """
    overrides: dict[tuple[str, str], str] = {}
    origin: dict[tuple[str, str], Path] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for toml_path in sorted((root / "dialog").rglob("*.toml")):
        rel = toml_path.relative_to(root)
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            errors.append(f"{rel}: cannot read as TOML: {e}")
            continue
        for ns, keys in data.items():
            if not isinstance(keys, dict):
                errors.append(f"{rel}: {ns} must be a table of edits")
                continue
            for key, edit in keys.items():
                where = f"{rel}: {ns},{key}"
                if (ns, key) in origin:
                    errors.append(f"{where} is also edited in {origin[(ns, key)]}")
                    continue
                origin[(ns, key)] = rel
                if not isinstance(edit, dict):
                    errors.append(f"{where} must be a table with vanilla and text")
                    continue
                missing = [field for field in ("vanilla", "text") if not isinstance(edit.get(field), str)]
                if missing:
                    errors.append(f'{where} needs a string "{missing[0]}"')
                    continue
                expected, text = edit["vanilla"], edit["text"]
                entry = en[ns][key] if en[ns] else None
                if entry is None:
                    errors.append(f"{where} is not in the game's text")
                    continue
                if expected != vanilla_hash(entry.translation):
                    errors.append(f"{where} changed in the game")
                    continue
                if vanilla_hash(text) == expected:
                    errors.append(f"{where} is unedited; text matches vanilla")
                    continue
                added = set(PLACEHOLDER.findall(text)) - set(PLACEHOLDER.findall(entry.translation))
                if added:
                    errors.append(f"{where} uses placeholders the game won't fill: {sorted(added)}")
                dropped = set(PLACEHOLDER.findall(entry.translation)) - set(PLACEHOLDER.findall(text))
                if dropped:
                    warnings.append(f"{where} drops placeholders {sorted(dropped)}")
                if len(STYLE_OPEN.findall(text)) != text.count("</>"):
                    errors.append(f"{where} has unbalanced <text ...>...</> tags")
                overrides[(ns, key)] = text
    return overrides, errors, warnings


def partial_table(en: LocresFile, overrides: dict[tuple[str, str], str]) -> LocresFile:
    """A table holding only the edited entries, each keeping its vanilla namespace, key and source-string hash."""
    table = LocresFile()
    table.version = en.version
    for (ns, key), text in overrides.items():
        if ns not in table:
            table.add(Namespace(ns))
        table[ns].add(Entry(key, text, en[ns][key].hash))
    return table


def cmd_build(args: argparse.Namespace) -> None:
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

    if args.partial:
        table, name = partial_table(en, overrides), "Unstraightened.locres"
    else:
        for (ns, key), text in overrides.items():
            en[ns][key].translation = text
        table, name = en, "Game.locres"

    out = root / "build"
    if out.exists():
        shutil.rmtree(out)
    stage = out / "stage"
    locres_path = stage / LOCRES / "en" / name
    locres_path.parent.mkdir(parents=True)
    locres_path.write_bytes(table.to_binary())

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
    build = subparsers.add_parser("build", help="apply dialog/**/*.toml edits to vanilla text and pack the mod into build/")
    build.add_argument(
        "--partial",
        action="store_true",
        help="pack only the edited entries as en/Unstraightened.locres; the game was seen to ignore it for entries Game.locres holds",
    )
    build.set_defaults(fn=cmd_build)
