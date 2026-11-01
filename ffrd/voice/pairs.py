"""pairs: the deterministic held-out split of scenes, the exclusion of held-out lines whose English text recurs in
training, the paraphrase run that writes the pair files, and the counts-only report `ffrd voice pairs --check` prints.

The split holds out a tenth of the scenes by SHA-256 order of their ids. A held-out line is excluded when its normalized
English text occurs in a training scene. `run` asks claude for one paraphrase per line of each scene under each of four
slots: one call per scene and slot, eight at a time, under the account cswap picks. A saved result is reused, so a rerun
makes no call for it. Each call is logged to CALLS, and the pair files are written from every saved result. `run` and the
check print counts only: never speaker ids, keys, line text or paraphrases.
"""

import argparse
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ffrd import scenes
from ffrd.game import Fail, entries, vanilla, vanilla_hash
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.store import Store, key, work_dir
from ffrd.scenes import SceneLine

HELDOUT_PERCENT = 10
CALLS = "calls.jsonl"
HAIKU = "haiku"
SLOTS: tuple[str, ...] = (claude.OPUS, claude.OPUS, claude.SONNET, HAIKU)
WORKERS = 8
STAGE = "pairs"
TRAIN = "train.jsonl"
HELDOUT = "heldout.jsonl"
PAIR_FIELDS = frozenset({"scene", "namespace", "key", "speaker", "slot", "model", "original", "paraphrase"})
PROMPT = (
    "Paraphrase every line of the scene in the input in plain contemporary English. Keep the meaning of each line and who "
    "says it. Do not reuse the wording of the original. Give one paraphrase per id."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "paraphrase": {"type": "string"}},
                "required": ["id", "paraphrase"],
            },
        }
    },
    "required": ["lines"],
}
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


@dataclass(frozen=True)
class Pair:
    scene: str
    namespace: str
    key: str
    speaker: str | None
    slot: int
    model: str
    original: str
    paraphrase: str


class _Failed(Exception):
    """A call whose answer is unusable. The run counts it failed and saves nothing for it."""


class _CallLog:
    """Appends one row per call to CALLS, one writer at a time, and counts this run's calls by the account that answered."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.by_account: Counter[int] = Counter()

    def record(self, scene: str, slot: int, model: str, account: int, outcome: str) -> None:
        row = {"scene": scene, "slot": slot, "model": model, "account": account, "outcome": outcome}
        with self.lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as e:
                raise Fail(f"cannot record call in {self.path}: {e.strerror}") from e
            self.by_account[account] += 1


def _scene_order(lines: Sequence[SceneLine]) -> list[str]:
    return list(dict.fromkeys(line.scene for line in lines))


def _grouped(lines: Sequence[SceneLine]) -> dict[str, list[SceneLine]]:
    grouped: dict[str, list[SceneLine]] = {}
    for line in lines:
        grouped.setdefault(line.scene, []).append(line)
    return grouped


def _inputs(scene: str, slot: int, group: Sequence[SceneLine], texts: Mapping[tuple[str, str], str]) -> dict[str, Any]:
    return {
        "scene": scene,
        "slot": slot,
        "model": SLOTS[slot],
        "lines": [[line.namespace, line.key, vanilla_hash(_text(line, texts)), line.speaker] for line in group],
    }


def _paraphrases(output: Mapping[str, Any], count: int) -> list[str] | None:
    rows = output.get("lines")
    if not isinstance(rows, list):
        return None
    by_id: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or not isinstance(row.get("paraphrase"), str):
            return None
        by_id[row["id"]] = row["paraphrase"]
    if len(rows) != count or set(by_id) != set(range(count)):
        return None
    paraphrases = [by_id[n] for n in range(count)]
    return None if any(not text.strip() for text in paraphrases) else paraphrases


def _generate(
    scene: str, slot: int, group: Sequence[SceneLine], texts: Mapping[tuple[str, str], str], log: _CallLog
) -> dict[str, list[str]]:
    model = SLOTS[slot]
    payload = {"lines": [{"id": n, "speaker": line.speaker, "text": _text(line, texts)} for n, line in enumerate(group)]}
    used, outcome = claude.ask(accounts.choose().number, model, [PROMPT, json.dumps(payload, ensure_ascii=False)], SCHEMA)
    paraphrases = _paraphrases(outcome, len(group)) if isinstance(outcome, dict) else None
    if paraphrases is not None:
        log.record(scene, slot, model, used, "ok")
        return {"paraphrases": paraphrases}
    status = "unfinished" if isinstance(outcome, claude.Unfinished) else "rejected"
    log.record(scene, slot, model, used, status)
    raise _Failed(status)


def _write(path: Path, rows: Sequence[Pair]) -> None:
    text = "".join(json.dumps(asdict(row), ensure_ascii=False) + "\n" for row in rows)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise Fail(f"cannot write pairs {path}: {e.strerror}") from e


def run(
    lines: Sequence[SceneLine],
    texts: Mapping[tuple[str, str], str],
    root: Path,
    *,
    limit: int | None = None,
    workers: int = WORKERS,
) -> list[str]:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Fail(f"cannot create {root}: {e.strerror}") from e
    store = Store(root)
    log = _CallLog(root / CALLS)
    grouped = _grouped(lines)
    resumed = 0
    pending: list[tuple[str, int]] = []
    for scene in _scene_order(lines)[:limit]:
        for slot in range(len(SLOTS)):
            if store.read(key(STAGE, _inputs(scene, slot, grouped[scene], texts), PROMPT)) is None:
                pending.append((scene, slot))
            else:
                resumed += 1
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                store.run,
                STAGE,
                _inputs(scene, slot, grouped[scene], texts),
                PROMPT,
                lambda scene=scene, slot=slot: _generate(scene, slot, grouped[scene], texts, log),
            )
            for scene, slot in pending
        ]
        try:
            for future in as_completed(futures):
                try:
                    future.result()
                except _Failed:
                    failed += 1
        except BaseException:
            pool.shutdown(cancel_futures=True)
            raise
    parts = split(lines)
    drop = excluded(lines, texts, parts)
    train: list[Pair] = []
    heldout: list[Pair] = []
    for scene in _scene_order(lines):
        group = grouped[scene]
        for slot in range(len(SLOTS)):
            saved = store.read(key(STAGE, _inputs(scene, slot, group, texts), PROMPT))
            if saved is None:
                continue
            for n, line in enumerate(group):
                pair = Pair(
                    scene=scene,
                    namespace=line.namespace,
                    key=line.key,
                    speaker=line.speaker,
                    slot=slot,
                    model=SLOTS[slot],
                    original=_text(line, texts),
                    paraphrase=saved.value["paraphrases"][n],
                )
                if scene in parts.train:
                    train.append(pair)
                elif (line.namespace, line.key) not in drop:
                    heldout.append(pair)
    _write(root / TRAIN, train)
    _write(root / HELDOUT, heldout)
    report = [
        f"calls {sum(log.by_account.values())}",
        f"resumed {resumed}",
        f"failed {failed}",
        f"train pairs {len(train)}",
        f"heldout pairs {len(heldout)}",
    ]
    return report + [f"account {account} calls {log.by_account[account]}" for account in sorted(log.by_account)]


def _pair(row: Any) -> Pair:
    if not isinstance(row, dict) or set(row) != PAIR_FIELDS:
        raise ValueError("not a pair row")
    texts = [row["scene"], row["namespace"], row["key"], row["model"], row["original"], row["paraphrase"]]
    speaker = row["speaker"]
    if not all(isinstance(text, str) for text in texts) or type(row["slot"]) is not int:
        raise ValueError("bad field type")
    if speaker is not None and not isinstance(speaker, str):
        raise ValueError("bad field type")
    return Pair(
        scene=row["scene"],
        namespace=row["namespace"],
        key=row["key"],
        speaker=speaker,
        slot=row["slot"],
        model=row["model"],
        original=row["original"],
        paraphrase=row["paraphrase"],
    )


def read(name: str, root: Path) -> list[Pair]:
    path = root / name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise Fail(f"no pairs at {path}; run `ffrd voice pairs`") from e
    except UnicodeDecodeError as e:
        raise Fail(f"{path}: is not valid UTF-8") from e
    except OSError as e:
        raise Fail(f"cannot read pairs {path}: {e.strerror}") from e
    found: list[Pair] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        try:
            found.append(_pair(json.loads(raw)))
        except ValueError as e:
            raise Fail(f"{path}:{lineno}: is not a pair row") from e
    return found


def _positive(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text!r}")
    return n


def cmd_pairs(args: argparse.Namespace) -> None:
    lines = scenes.load()
    texts = english()
    root = work_dir("voice")
    report = check(lines, texts, root) if args.check else run(lines, texts, root, limit=args.limit)
    print("\n".join(report))


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("pairs", help="write four paraphrases per story line to work/voice/, or print split and call counts")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="print the split and the call counts, without calling claude")
    mode.add_argument("--limit", type=_positive, metavar="N", help="paraphrase only the first N scenes")
    p.set_defaults(fn=cmd_pairs)
