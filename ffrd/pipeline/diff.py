"""diff: show dialog/<lens> rewrites beside their vanilla English, up to recorded progress."""

import argparse
import sys
import tomllib
from pathlib import Path

from ffrd.find import BARE_KEY, chapter, toml_string
from ffrd.game import Fail, natural, repo_root, vanilla, vanilla_hash
from ffrd.progress import recorded


def lens_dir(root: Path, lens: str) -> Path:
    if not lens or lens in {".", ".."} or "/" in lens or "\\" in lens:
        raise Fail(f"lens name {lens!r} must be one plain path segment")
    return root / "dialog" / lens


def cmd_diff(args: argparse.Namespace) -> None:
    limit = recorded()
    if limit is None:
        raise Fail("no progress recorded; run `ffrd progress <chapter>`")
    root = repo_root()
    rewrites = lens_dir(root, args.lens)
    if not rewrites.is_dir():
        raise Fail(f"no dialog/{args.lens}/ directory")
    en = vanilla()
    shown: list[tuple[str, str, str, str]] = []
    later = 0
    chapterless = 0
    errors: list[str] = []
    for toml_path in sorted(rewrites.rglob("*.toml")):
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
            ch = chapter(ns)
            for key, edit in keys.items():
                where = f"{rel}: {ns},{key}"
                if not isinstance(edit, dict):
                    errors.append(f"{where} must be a table with vanilla and text")
                    continue
                missing = [field for field in ("vanilla", "text") if not isinstance(edit.get(field), str)]
                if missing:
                    errors.append(f'{where} needs a string "{missing[0]}"')
                    continue
                if ch is None:
                    chapterless += 1
                    continue
                if ch > limit:
                    later += 1
                    continue
                entry = en[ns][key] if en[ns] else None
                if entry is None:
                    errors.append(f"{where} is not in the game's text")
                elif edit["vanilla"] != vanilla_hash(entry.translation):
                    errors.append(f"{where} changed in the game")
                else:
                    shown.append((ns, key, entry.translation, edit["text"]))
    if errors:
        raise Fail("\n".join(errors))
    shown.sort(key=lambda row: (natural(row[0]), natural(row[1])))
    for ns, key, english, text in shown:
        bare_or_quoted = key if BARE_KEY.fullmatch(key) else toml_string(key)
        print(f"[{toml_string(ns)}.{bare_or_quoted}]")
        print(f"- {toml_string(english)}")
        print(f"+ {toml_string(text)}")
        print()
    print(f"# {len(shown)} shown, {later} withheld, {chapterless} without a chapter", file=sys.stderr)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    d = subparsers.add_parser("diff", help="show dialog/<lens> rewrites beside their vanilla English, up to recorded progress")
    d.add_argument("--lens", required=True, help="lens whose dialog/<lens>/ rewrites to show")
    d.set_defaults(fn=cmd_diff)
