"""pairs: the deterministic held-out split of scenes, the exclusion of held-out lines whose English text recurs in
training, and the counts-only report `ffrd voice pairs --check` prints.

The split holds out a tenth of the scenes by SHA-256 order of their ids. A held-out line is excluded when its normalized
English text occurs in a training scene. The report prints counts only: never speaker ids, keys or line text.
"""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ffrd.game import Fail, entries, vanilla
from ffrd.scenes import SceneLine

HELDOUT_PERCENT = 10
CALLS = "calls.jsonl"
TOKEN = re.compile(r"\w+")


def normalize(text: str) -> str:
    return " ".join(TOKEN.findall(unicodedata.normalize("NFKC", text).casefold()))


@dataclass(frozen=True)
class Split:
    train: frozenset[str]
    heldout: frozenset[str]


def split(lines: Sequence[SceneLine]) -> Split:
    scenes = sorted({line.scene for line in lines}, key=lambda scene: hashlib.sha256(scene.encode("utf-8")).hexdigest())
    count = round(len(scenes) * HELDOUT_PERCENT / 100)
    return Split(train=frozenset(scenes[count:]), heldout=frozenset(scenes[:count]))


def english() -> dict[tuple[str, str], str]:
    return {(namespace, key): text for namespace, key, text in entries(vanilla("en"))}


def _text(line: SceneLine, texts: Mapping[tuple[str, str], str]) -> str:
    try:
        return texts[(line.namespace, line.key)]
    except KeyError as e:
        raise Fail(f"no English text for line {line.namespace}/{line.key}") from e


def excluded(lines: Sequence[SceneLine], texts: Mapping[tuple[str, str], str], parts: Split) -> frozenset[tuple[str, str]]:
    trained = {normalize(_text(line, texts)) for line in lines if line.scene in parts.train}
    return frozenset(
        (line.namespace, line.key) for line in lines if line.scene in parts.heldout and normalize(_text(line, texts)) in trained
    )


def _calls(path: Path) -> Counter[int]:
    counts: Counter[int] = Counter()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return counts
    except UnicodeDecodeError as e:
        raise Fail(f"{path}: is not valid UTF-8") from e
    except OSError as e:
        raise Fail(f"cannot read calls {path}: {e.strerror}") from e
    for lineno, raw in enumerate(text.splitlines(), 1):
        where = f"{path}:{lineno}"
        try:
            obj: Any = json.loads(raw)
        except ValueError as e:
            raise Fail(f"{where}: is not valid JSON") from e
        if not isinstance(obj, dict):
            raise Fail(f"{where}: is not a JSON object")
        account = obj.get("account")
        if isinstance(account, bool) or not isinstance(account, int):
            raise Fail(f"{where}: field account must be an integer")
        counts[account] += 1
    return counts


def check(lines: Sequence[SceneLine], texts: Mapping[tuple[str, str], str], root: Path) -> list[str]:
    parts = split(lines)
    drop = excluded(lines, texts, parts)
    train = [line for line in lines if line.scene in parts.train]
    heldout = [line for line in lines if line.scene in parts.heldout]
    kept = [line for line in heldout if (line.namespace, line.key) not in drop]
    report = [
        f"scenes {len(parts.train) + len(parts.heldout)}",
        f"train scenes {len(parts.train)}",
        f"heldout scenes {len(parts.heldout)}",
        f"train lines {len(train)}",
        f"heldout lines {len(kept)}",
        f"excluded lines {len(heldout) - len(kept)}",
    ]
    calls = _calls(root / CALLS)
    report += [f"account {account} calls {calls[account]}" for account in sorted(calls)]
    return report
