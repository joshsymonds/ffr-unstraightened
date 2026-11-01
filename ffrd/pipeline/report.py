"""report: count a lens run's outcomes per chapter. Prints counts only, never speaker ids, keys or line text."""

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ffrd.game import Fail, natural, repo_root
from ffrd.pipeline.lens import load_lens
from ffrd.pipeline.store import work_dir

OUTCOMES = "outcomes.jsonl"
ARCS = "arcs.json"
RECALL = "recall.json"
RECALL_FIELDS = frozenset({"disagreements", "confirmed"})
OUTCOME_LABELS = {
    "changed": "changed",
    "kept_for_plot": "kept for plot",
    "gate_failed": "gate failed",
    "unfinished": "unfinished",
    "untouched": "untouched",
}
GATE = re.compile(r"[a-z0-9]+([-_][a-z0-9]+)*")


@dataclass(frozen=True)
class Record:
    chapter: int | None
    speaker: str | None
    reasons: frozenset[str]
    outcome: str
    gate: str | None


def _require(obj: dict[str, object], name: str, where: str) -> object:
    if name not in obj:
        raise Fail(f"{where}: field {name} is missing")
    return obj[name]


def _text(obj: dict[str, object], name: str, where: str) -> None:
    if not isinstance(_require(obj, name, where), str):
        raise Fail(f"{where}: field {name} must be a string")


def _chapter(obj: dict[str, object], where: str) -> int | None:
    value = _require(obj, "chapter", where)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Fail(f"{where}: field chapter must be a positive integer or null")
    return value


def _speaker(obj: dict[str, object], where: str) -> str | None:
    value = _require(obj, "speaker", where)
    if value is None or isinstance(value, str):
        return value
    raise Fail(f"{where}: field speaker must be a string or null")


def _reasons(obj: dict[str, object], where: str, triage: frozenset[str]) -> frozenset[str]:
    value = _require(obj, "reasons", where)
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise Fail(f"{where}: field reasons must be a list of strings")
    for reason in value:
        if reason not in triage:
            raise Fail(f"{where}: field reasons has an id that is not a triage id of the lens")
    return frozenset(value)


def _outcome(obj: dict[str, object], where: str) -> str:
    value = _require(obj, "outcome", where)
    if not isinstance(value, str) or value not in OUTCOME_LABELS:
        raise Fail(f"{where}: field outcome is not a known outcome")
    return value


def _gate(obj: dict[str, object], outcome: str, where: str) -> str | None:
    value = _require(obj, "gate", where)
    if value is None:
        if outcome == "gate_failed":
            raise Fail(f"{where}: field gate must name the failing gate")
        return None
    if not isinstance(value, str) or not GATE.fullmatch(value):
        raise Fail(f"{where}: field gate must be a gate name or null")
    return value


def _parse(text: str, where: str, triage: frozenset[str]) -> Record:
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise Fail(f"{where}: is not valid JSON") from e
    if not isinstance(obj, dict):
        raise Fail(f"{where}: is not a JSON object")
    _text(obj, "namespace", where)
    _text(obj, "key", where)
    outcome = _outcome(obj, where)
    return Record(
        chapter=_chapter(obj, where),
        speaker=_speaker(obj, where),
        reasons=_reasons(obj, where, triage),
        outcome=outcome,
        gate=_gate(obj, outcome, where),
    )


def _records(path: Path, triage: frozenset[str]) -> list[Record]:
    try:
        handle = path.open("rb")
    except FileNotFoundError as e:
        raise Fail(f"no outcomes at {path}; run the lens first") from e
    except OSError as e:
        raise Fail(f"cannot read outcomes {path}: {e.strerror}") from e
    records: list[Record] = []
    with handle:
        try:
            for lineno, raw in enumerate(handle, 1):
                where = f"{path}:{lineno}"
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as e:
                    raise Fail(f"{where}: is not valid UTF-8") from e
                records.append(_parse(text, where, triage))
        except OSError as e:
            raise Fail(f"cannot read outcomes {path}: {e.strerror}") from e
    return records


def _count(obj: dict[str, object], name: str, where: str) -> int:
    value = _require(obj, name, where)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Fail(f"{where}: field {name} must be a non-negative integer")
    return value


def _arcs(path: Path) -> tuple[int, int]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise Fail(f"no arcs at {path}; run the lens first") from e
    except OSError as e:
        raise Fail(f"cannot read arcs {path}: {e.strerror}") from e
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise Fail(f"{path}: is not valid JSON") from e
    if not isinstance(obj, dict):
        raise Fail(f"{path}: is not a JSON object")
    arcs = _count(obj, "arcs", str(path))
    with_changes = _count(obj, "with_changes", str(path))
    if with_changes > arcs:
        raise Fail(f"{path}: field with_changes must not exceed arcs")
    return arcs, with_changes


def _pair(entry: object, where: str) -> tuple[int, int]:
    if not isinstance(entry, dict):
        raise Fail(f"{where} is not an object")
    if not RECALL_FIELDS.issuperset(entry):
        raise Fail(f"{where}: has a field that is not disagreements or confirmed")
    disagreements = _count(entry, "disagreements", where)
    confirmed = _count(entry, "confirmed", where)
    if confirmed > disagreements:
        raise Fail(f"{where}: field confirmed must not exceed disagreements")
    return disagreements, confirmed


def _recall(path: Path, triage_ids: list[str]) -> dict[str, tuple[int, int]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise Fail(f"no recall at {path}; run the lens first") from e
    except OSError as e:
        raise Fail(f"cannot read recall {path}: {e.strerror}") from e
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise Fail(f"{path}: is not valid JSON") from e
    if not isinstance(obj, dict):
        raise Fail(f"{path}: is not a JSON object")
    if any(key not in triage_ids for key in obj):
        raise Fail(f"{path}: has a key that is not a triage id of the lens")
    recall: dict[str, tuple[int, int]] = {}
    for rid in triage_ids:
        if rid not in obj:
            raise Fail(f"{path}: triage id {rid} is missing")
        recall[rid] = _pair(obj[rid], f"{path}: triage id {rid}")
    return recall


def _block(label: str, records: list[Record], numbers: dict[str, int], triage_ids: list[str]) -> list[str]:
    counts = Counter(r.outcome for r in records)
    summary = ", ".join(f"{counts[outcome]} {text}" for outcome, text in OUTCOME_LABELS.items())
    lines = [f"{label}: {summary}"]
    changed = [r for r in records if r.outcome == "changed"]

    reasons = Counter(reason for r in changed for reason in r.reasons)
    lines += [f"  reason {rid}: {reasons[rid]}" for rid in triage_ids if reasons[rid]]

    speakers = Counter(r.speaker for r in changed)
    characters = sorted((numbers[s], n) for s, n in speakers.items() if s is not None)
    lines += [f"  character {number}: {n}" for number, n in characters]
    if speakers[None]:
        lines.append(f"  no speaker: {speakers[None]}")

    gates = Counter(r.gate for r in records if r.outcome == "gate_failed" and r.gate is not None)
    lines += [f"  gate {gate}: {gates[gate]}" for gate in sorted(gates)]
    return lines


def render(records: list[Record], triage_ids: list[str]) -> list[str]:
    speakers = sorted({r.speaker for r in records if r.speaker is not None}, key=lambda s: (natural(s), s))
    numbers = {speaker: number for number, speaker in enumerate(speakers, 1)}
    chapters = sorted({r.chapter for r in records if r.chapter is not None})

    lines: list[str] = []
    for chapter in chapters:
        lines += _block(f"chapter {chapter}", [r for r in records if r.chapter == chapter], numbers, triage_ids)
    unattributed = [r for r in records if r.chapter is None]
    if unattributed:
        lines += _block("unattributed", unattributed, numbers, triage_ids)
    lines += _block("total", records, numbers, triage_ids)
    return lines


def cmd_report(args: argparse.Namespace) -> None:
    lens_dir = work_dir(args.lens)
    lens = load_lens(repo_root() / "lenses" / f"{args.lens}.toml")
    triage_ids = [q.id for q in lens.triage]
    records = _records(lens_dir / OUTCOMES, frozenset(triage_ids))
    arcs, with_changes = _arcs(lens_dir / ARCS)
    recall = _recall(lens_dir / RECALL, triage_ids)
    for line in render(records, triage_ids):
        print(line)
    print(f"arcs: {arcs}, {with_changes} with changes")
    for rid in triage_ids:
        disagreements, confirmed = recall[rid]
        print(f"recall {rid}: {disagreements} disagreements, {confirmed} confirmed")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("report", help="count a lens run's outcomes per chapter, reason, character and gate")
    p.add_argument("--lens", required=True, help="name of the lens run to count, read from lenses/<name>.toml and work/<name>/")
    p.set_defaults(fn=cmd_report)
