"""Append-only change ledger: one JSON entry per line in ledger/<lens>.jsonl, holding hashes and ids only."""

import itertools
import json
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from ffrd.game import Fail


@dataclass(frozen=True)
class Entry:
    run: str
    lens: str
    base: str | None
    namespace: str
    key: str
    vanilla_hash: str
    base_hash: str | None
    text_hash: str
    reasons: tuple[str, ...]
    writer: str
    plan: str | None


FIELD_NAMES = frozenset(f.name for f in fields(Entry))
REQUIRED_TEXT = ("run", "lens", "namespace", "key", "vanilla_hash", "text_hash", "writer")
OPTIONAL_TEXT = ("base", "base_hash", "plan")


def _name(lens: str) -> str:
    if not lens or lens in {".", ".."} or "/" in lens or "\\" in lens:
        raise Fail(f"lens name {lens!r} must be one plain path segment")
    return f"ledger/{lens}.jsonl"


def path(root: Path, lens: str) -> Path:
    return root / _name(lens)


def _encode(entry: Entry) -> str:
    return json.dumps(asdict(entry), ensure_ascii=False)


def _well_typed(obj: dict[str, Any]) -> bool:
    return (
        all(isinstance(obj[k], str) for k in REQUIRED_TEXT)
        and all(obj[k] is None or isinstance(obj[k], str) for k in OPTIONAL_TEXT)
        and isinstance(obj["reasons"], list)
        and all(isinstance(r, str) for r in obj["reasons"])
    )


def _decode(raw: bytes, name: str, number: int) -> Entry:
    bad = Fail(f"{name}:{number}: not a ledger entry")
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text)
    except ValueError as e:
        raise bad from e
    if not isinstance(obj, dict) or set(obj) != FIELD_NAMES or not _well_typed(obj):
        raise bad
    entry = Entry(**{**obj, "reasons": tuple(obj["reasons"])})
    if _encode(entry) != text:
        raise bad
    return entry


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def _committed_versions(root: Path, name: str) -> list[bytes]:
    if _git(root, "rev-parse", "--verify", "-q", "HEAD").returncode:
        return []
    log = _git(root, "log", "--reverse", "--format=%H", "--", name)
    if log.returncode:
        raise Fail(f"cannot read the git history of {name}: {log.stderr.decode(errors='replace').strip()}")
    versions: list[bytes] = []
    for commit in log.stdout.decode().split():
        spec = f"{commit}:./{name}"
        if _git(root, "cat-file", "-e", spec).returncode:
            versions.append(b"")
            continue
        blob = _git(root, "cat-file", "blob", spec)
        if blob.returncode:
            raise Fail(f"cannot read {name} at {commit}: {blob.stderr.decode(errors='replace').strip()}")
        versions.append(blob.stdout)
    return versions


def _working(file: Path) -> bytes:
    try:
        return file.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as e:
        raise Fail(f"cannot read {file}: {e.strerror}") from e


def read(root: Path, lens: str) -> list[Entry]:
    name = _name(lens)
    data = _working(root / name)
    chain = [*_committed_versions(root, name), data]
    if any(not newer.startswith(older) for older, newer in itertools.pairwise(chain)):
        raise Fail(f"{name}: an existing line was changed or removed; the ledger is append-only")
    if not data:
        return []
    if not data.endswith(b"\n"):
        unterminated = data.count(b"\n") + 1
        raise Fail(f"{name}:{unterminated}: not a ledger entry")
    lines = data[:-1].split(b"\n")
    return [_decode(raw, name, number) for number, raw in enumerate(lines, start=1)]


def latest(entries: Iterable[Entry]) -> dict[tuple[str, str], Entry]:
    return {(e.namespace, e.key): e for e in entries}


def _append(file: Path, text: str) -> None:
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise Fail(f"cannot append to {file}: {e.strerror}") from e


def append(root: Path, lens: str, entries: Sequence[Entry]) -> list[Entry]:
    current = latest(read(root, lens))
    appended: list[Entry] = []
    for entry in entries:
        prior = current.get((entry.namespace, entry.key))
        if prior is not None and replace(prior, run="") == replace(entry, run=""):
            continue
        appended.append(entry)
        current[(entry.namespace, entry.key)] = entry
    if appended:
        _append(root / _name(lens), "".join(_encode(e) + "\n" for e in appended))
    return appended
