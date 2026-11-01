"""build: apply dialog/**/*.toml edits to the vanilla text and pack the mod into build/.

The game's text lives in FFRS/Content/Localization/Game/<lang>/Game.locres
inside the legacy .pak. The mod packs FFRS-Unstraightened_P.pak, plus a
.utoc/.ucas pair the loader requires (copies of the game's empty global
container). Three pak shapes, chosen by flag:

- default (target): two files. FFRS/Content/Localization/Unstraightened/en/
  Unstraightened.locres holds only the edited entries, each under its
  namespace, key and vanilla source-string hash. FFRS/Config/UserGame.ini
  lists the Unstraightened folder ahead of the Game folder in the game's
  localization paths. The game was seen to honour this shape.
- --full: FFRS/Content/Localization/Game/en/Game.locres, holding the whole
  English table with the edits applied. It replaces the game's table, and
  the game was seen to honour it.
- --partial: FFRS/Content/Localization/Game/en/Unstraightened.locres, holding
  only the edited entries. The game was seen to ignore a second en/*.locres
  for an entry Game.locres already holds, so this shape does not change those
  entries.

--ue4ss writes no pak. It writes build/UnstraightenedText/, a UE4SS mod folder:
the repo's ue4ss/UnstraightenedText/Scripts/main.lua, an empty enabled.txt and
edits.txt, which lists each edit for the mod to register as en polyglot text.
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
PAK_SHAPES = ("target", "full", "partial")
TARGET_LOCRES = Path("FFRS/Content/Localization/Unstraightened/en/Unstraightened.locres")
USER_GAME_INI = Path("FFRS/Config/UserGame.ini")
USER_GAME_TEXT = (
    "[Internationalization]\n"
    "-LocalizationPaths=%GAMEDIR%Content/Localization/Game\n"
    "+LocalizationPaths=%GAMEDIR%Content/Localization/Unstraightened\n"
    "+LocalizationPaths=%GAMEDIR%Content/Localization/Game\n"
)

UE4SS_NAME = "UnstraightenedText"
UE4SS_SRC = Path("ue4ss") / UE4SS_NAME / "Scripts/main.lua"
EDIT_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\r": "\\r", "\n": "\\n"})

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


def pak_files(shape: str, en: LocresFile, overrides: dict[tuple[str, str], str]) -> dict[Path, bytes]:
    """Pak-relative path -> bytes for each file the shape packs. The full shape applies the overrides to en in place."""
    if shape == "target":
        return {TARGET_LOCRES: partial_table(en, overrides).to_binary(), USER_GAME_INI: USER_GAME_TEXT.encode("utf-8")}
    if shape == "partial":
        return {LOCRES / "en" / "Unstraightened.locres": partial_table(en, overrides).to_binary()}
    if shape == "full":
        for (ns, key), text in overrides.items():
            en[ns][key].translation = text
        return {LOCRES / "en" / "Game.locres": en.to_binary()}
    raise ValueError(f"unknown shape {shape!r}")


def pack(files: dict[Path, bytes], out: Path, name: str) -> Path:
    """Stage files under out/stage, pack them with repak into out/<name>.pak, and return that path."""
    stage = out / "stage"
    for rel, data in files.items():
        path = stage / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    pak = out / f"{name}.pak"
    subprocess.run(
        [REPAK, "pack", "-q", "--version", "V11", "--compression", "Zlib", "-m", "../../../", str(stage), str(pak)],
        check=True,
    )
    return pak


def edits_text(overrides: dict[tuple[str, str], str]) -> str:
    """edits.txt for the UE4SS text mod: one namespace<TAB>key<TAB>text line per edit, sorted by namespace and key.

    Backslash, tab, CR and LF are escaped in each field as \\\\, \\t, \\r and \\n.
    """
    return "".join(
        f"{ns.translate(EDIT_ESCAPES)}\t{key.translate(EDIT_ESCAPES)}\t{text.translate(EDIT_ESCAPES)}\n"
        for (ns, key), text in sorted(overrides.items())
    )


def write_ue4ss(out: Path, root: Path, overrides: dict[tuple[str, str], str]) -> None:
    """Write the UE4SS text mod folder under out: the repo's main.lua, an empty enabled.txt and edits.txt."""
    mod = out / UE4SS_NAME
    (mod / "Scripts").mkdir(parents=True)
    shutil.copyfile(root / UE4SS_SRC, mod / "Scripts/main.lua")
    (mod / "enabled.txt").write_bytes(b"")
    (mod / "edits.txt").write_bytes(edits_text(overrides).encode("utf-8"))


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

    out = root / "build"
    if out.exists():
        shutil.rmtree(out)
    if args.shape == "ue4ss":
        write_ue4ss(out, root, overrides)
        print(f"{len(overrides)} lines overridden -> build/{UE4SS_NAME}/")
        return

    files = pak_files(args.shape, en, overrides)
    pak = pack(files, out, MOD_NAME)

    locres = next(path for path in files if path.suffix == ".locres")
    check = read_locres(out / "stage" / locres)
    bad = [f"{ns},{key}" for (ns, key), text in overrides.items() if check[ns][key].translation != text]
    if bad:
        pak.unlink()
        raise Fail(f"built locres lost overrides: {bad[:10]}")

    for ext in ("utoc", "ucas"):
        shutil.copyfile(paks_dir() / f"global.{ext}", out / f"{MOD_NAME}.{ext}")
    print(f"{len(overrides)} lines overridden -> build/{MOD_NAME}.{{pak,utoc,ucas}}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    build = subparsers.add_parser(
        "build",
        help="apply dialog/**/*.toml edits to vanilla text and pack the mod into build/",
        description=(
            "Pack the mod into build/. Shapes: the default (target) packs the edited entries as "
            "FFRS/Content/Localization/Unstraightened/en/Unstraightened.locres plus FFRS/Config/UserGame.ini, "
            "which lists that folder ahead of Game; --full packs the whole English table as en/Game.locres; "
            "--partial packs the edited entries as en/Unstraightened.locres in the Game folder; "
            "--ue4ss writes the UE4SS text mod folder build/UnstraightenedText instead of a pak."
        ),
    )
    shape = build.add_mutually_exclusive_group()
    shape.add_argument(
        "--full",
        dest="shape",
        action="store_const",
        const="full",
        default="target",
        help="pack the whole English table with the edits applied as en/Game.locres, replacing the game's table",
    )
    shape.add_argument(
        "--partial",
        dest="shape",
        action="store_const",
        const="partial",
        default="target",
        help="pack only the edited entries as en/Unstraightened.locres; the game was seen to ignore it for entries Game.locres holds",
    )
    shape.add_argument(
        "--ue4ss",
        dest="shape",
        action="store_const",
        const="ue4ss",
        default="target",
        help="write the UE4SS text mod folder build/UnstraightenedText with the edits in edits.txt, instead of a pak",
    )
    build.set_defaults(fn=cmd_build)
