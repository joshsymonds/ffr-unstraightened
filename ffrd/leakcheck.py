"""leak-check: report vanilla game text found anywhere in the repository's history."""

import argparse
import subprocess
from collections import defaultdict
from pathlib import Path

import polib

from ffrd.game import LOCRES, Fail, entries, read_locres, repo_root, vanilla_dir

# Vanilla strings shorter than this are ignored: short labels occur by chance in code and prose.
MIN_LEN = 20

Label = tuple[str, str, str]  # (language, namespace, key)
Hit = tuple[int, int, Label]  # (start, end, label) of a match inside a text


def tables() -> list[Path]:
    found = sorted((vanilla_dir() / "pak" / LOCRES).glob("*/Game.locres"))
    if not found:
        raise Fail(f"no vanilla text under {vanilla_dir() / 'pak' / LOCRES}; run `ffrd extract`")
    return found


def build_index(paths: list[Path]) -> dict[str, list[tuple[str, Label]]]:
    """Map each string's first MIN_LEN characters to (needle, label), for verbatim and .po-escaped forms."""
    index: defaultdict[str, list[tuple[str, Label]]] = defaultdict(list)
    for path in paths:
        lang = path.parent.name
        for ns, key, text in entries(read_locres(path)):
            if len(text) < MIN_LEN:
                continue
            for needle in dict.fromkeys((text, polib.escape(text))):
                index[needle[:MIN_LEN]].append((needle, (lang, ns, key)))
    return index


def matches(text: str, index: dict[str, list[tuple[str, Label]]]) -> list[Hit]:
    found: list[Hit] = []
    for i in range(len(text) - MIN_LEN + 1):
        for needle, label in index.get(text[i : i + MIN_LEN], ()):
            if text.startswith(needle, i):
                found.append((i, i + len(needle), label))
    return found


def redact(path: str, hits: list[Hit]) -> str:
    """The path with every matched span masked, so a path never prints vanilla text."""
    chars = list(path)
    for start, end, _ in hits:
        for i in range(start, end):
            chars[i] = "*"
    return "".join(chars)


def report(location: str, hits: list[Hit]) -> list[str]:
    labels = dict.fromkeys(label for _, _, label in hits)
    return [f"{location} {lang} {ns},{key}" for lang, ns, key in labels]


def git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as e:
        raise Fail(f"git {args[0]} failed: {e.stderr.decode(errors='replace').strip()}") from e


class Objects:
    """Reads git objects through one `git cat-file --batch` process."""

    def __init__(self, root: Path) -> None:
        self._proc = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def __enter__(self) -> "Objects":
        return self

    def __exit__(self, *_: object) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.close()
        self._proc.wait()

    def read(self, sha: str) -> bytes:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(f"{sha}\n".encode())
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().split()
        if len(header) != 3:
            raise Fail(f"git cat-file gave no object for {sha}")
        content = self._proc.stdout.read(int(header[2]))
        self._proc.stdout.read(1)
        return content


def scan(root: Path, index: dict[str, list[tuple[str, Label]]]) -> list[str]:
    lines: list[str] = []
    shown: dict[str, str] = {}  # tree path -> the path as printed, after its first match
    seen_blobs: set[str] = set()
    commits = git(root, "rev-list", "--reverse", "HEAD").decode().split()
    with Objects(root) as objects:
        for commit in commits:
            message = objects.read(commit).partition(b"\n\n")[2]
            lines += report(f"{commit}:message", matches(message.decode(errors="replace"), index))
            for entry in git(root, "ls-tree", "-r", "-z", commit).split(b"\0"):
                if not entry:
                    continue
                meta, _, raw_path = entry.partition(b"\t")
                _, kind, sha = meta.decode().split()
                path = raw_path.decode(errors="replace")
                if path not in shown:
                    hits = matches(path, index)
                    shown[path] = redact(path, hits)
                    lines += report(f"{commit}:{shown[path]}", hits)
                if kind == "blob" and sha not in seen_blobs:
                    seen_blobs.add(sha)
                    text = objects.read(sha).decode(errors="replace")
                    lines += report(f"{commit}:{shown[path]}", matches(text, index))
    return lines


def cmd_leak_check(args: argparse.Namespace) -> None:
    index = build_index(tables())
    lines = scan(repo_root(), index)
    if not lines:
        print(0)
        return
    for line in lines:
        print(line)
    raise Fail(f"{len(lines)} vanilla strings in history")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    c = subparsers.add_parser(
        "leak-check",
        help="fail if vanilla game text is in any commit's paths, files or messages",
        description=f"Scan every commit reachable from HEAD for vanilla game text. Ignores vanilla strings shorter than {MIN_LEN} characters. "
        "Prints a location and table key for each match, never the matched text.",
    )
    c.set_defaults(fn=cmd_leak_check)
