"""report: count a lens run's outcomes per chapter. Prints counts only, never speaker ids, keys or line text."""

import argparse
import json
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ffrd import scenes as scene_file
from ffrd.game import Fail, natural, repo_root
from ffrd.pipeline.lens import load_lens
from ffrd.pipeline.scene_record import SCENE_KEEP_REASONS, SCENES, SceneRecord, read_scenes
from ffrd.pipeline.stages import KEEP_REASONS
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
Key = tuple[str, str]  # (namespace, key)


@dataclass(frozen=True)
class Record:
    chapter: int | None
    speaker: str | None
    reasons: frozenset[str]
    outcome: str
    gate: str | None
    keep_reason: str | None
    plan: str | None = None


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


def _reasons(obj: dict[str, object], where: str, accepted: frozenset[str]) -> frozenset[str]:
    value = _require(obj, "reasons", where)
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise Fail(f"{where}: field reasons must be a list of strings")
    for reason in value:
        if reason not in accepted:
            raise Fail(f"{where}: field reasons has an id that is not a triage or scene pattern id of the lens")
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


def _keep_reason(obj: dict[str, object], where: str) -> str | None:
    if "keep_reason" not in obj:
        return None
    value = obj["keep_reason"]
    if value is None:
        return None
    if isinstance(value, str) and value in KEEP_REASONS:
        return value
    raise Fail(f"{where}: field keep_reason is not a listed keep reason or null")


def _plan(obj: dict[str, object], where: str) -> str | None:
    if "plan" not in obj:
        return None
    value = obj["plan"]
    if value is None or isinstance(value, str):
        return value
    raise Fail(f"{where}: field plan must be a string or null")


def _parse(text: str, where: str, accepted: frozenset[str]) -> Record:
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
        reasons=_reasons(obj, where, accepted),
        outcome=outcome,
        gate=_gate(obj, outcome, where),
        keep_reason=_keep_reason(obj, where),
        plan=_plan(obj, where),
    )


def _records(path: Path, accepted: frozenset[str]) -> list[Record]:
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
                records.append(_parse(text, where, accepted))
        except OSError as e:
            raise Fail(f"cannot read outcomes {path}: {e.strerror}") from e
    return records


def _keys(path: Path) -> set[Key]:
    # Runs after _records has validated every line, so each line holds a string namespace and key.
    try:
        with path.open("rb") as handle:
            return {(obj["namespace"], obj["key"]) for obj in (json.loads(raw) for raw in handle)}
    except OSError as e:
        raise Fail(f"cannot read outcomes {path}: {e.strerror}") from e


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


def _numbered(speakers: Counter[str | None], numbers: dict[str, int]) -> list[tuple[int, int]]:
    return sorted((numbers[s], n) for s, n in speakers.items() if s is not None)


def _scene_lines(records: list[Record], scenes: list[SceneRecord], numbers: dict[str, int], scene_ids: Sequence[str]) -> list[str]:
    planned = [s for s in scenes if s.plan is not None]
    kept = Counter(s.keep_reason for s in scenes if s.keep_reason is not None)
    gates: Counter[str] = Counter()
    for scene in scenes:
        gates.update(scene.gate_failures)
    changed = [s.changed for s in planned]
    median = statistics.median(changed) if changed else 0
    median_text = str(int(median)) if float(median).is_integer() else f"{median:.1f}"

    lines = [f"  scene eligible: {sum(s.eligible for s in scenes)}"]
    lines += [f"  scene flagged {pattern}: {sum(pattern in s.patterns for s in scenes)}" for pattern in scene_ids]
    lines.append(f"  scene plans: {len(planned)}")
    lines += [f"  scene kept {reason}: {kept[reason]}" for reason in SCENE_KEEP_REASONS if kept[reason]]
    lines.append(f"  scene planned lines: {sum(s.planned for s in scenes)} (required {sum(s.required for s in scenes)})")
    lines.append(f"  scene changed lines: {sum(s.changed for s in scenes)}")
    lines.append(f"  scene replanned: {sum(s.replanned for s in scenes)}")
    lines.append(f"  scene reverted lines: {sum(s.reverted for s in scenes)}")
    lines += [f"  scene gate {gate}: {gates[gate]}" for gate in sorted(gates) if gates[gate]]
    lines.append(f"  scene changed per scene: max {max(changed, default=0)}, median {median_text}")
    speakers = Counter(r.speaker for r in records if r.plan is not None and r.outcome == "changed")
    lines += [f"  scene character {number}: {n}" for number, n in _numbered(speakers, numbers)]
    return lines


def _block(
    label: str,
    records: list[Record],
    numbers: dict[str, int],
    triage_ids: list[str],
    scenes: list[SceneRecord] | None,
    scene_ids: Sequence[str],
) -> list[str]:
    counts = Counter(r.outcome for r in records)
    summary = ", ".join(f"{counts[outcome]} {text}" for outcome, text in OUTCOME_LABELS.items())
    lines = [f"{label}: {summary}"]
    changed = [r for r in records if r.outcome == "changed"]

    reasons = Counter(reason for r in changed for reason in r.reasons)
    lines += [f"  reason {rid}: {reasons[rid]}" for rid in triage_ids if reasons[rid]]

    kept = Counter(r.keep_reason for r in records if r.keep_reason is not None)
    lines += [f"  kept {reason}: {kept[reason]}" for reason in KEEP_REASONS if kept[reason]]

    speakers = Counter(r.speaker for r in changed)
    lines += [f"  character {number}: {n}" for number, n in _numbered(speakers, numbers)]
    if speakers[None]:
        lines.append(f"  no speaker: {speakers[None]}")

    gates = Counter(r.gate for r in records if r.outcome == "gate_failed" and r.gate is not None)
    lines += [f"  gate {gate}: {gates[gate]}" for gate in sorted(gates)]
    if scenes is not None:
        lines += _scene_lines(records, scenes, numbers, scene_ids)
    return lines


def render(
    records: list[Record],
    triage_ids: list[str],
    *,
    scenes: Sequence[SceneRecord] | None = None,
    scene_ids: Sequence[str] = (),
) -> list[str]:
    found: list[SceneRecord] = [] if scenes is None else list(scenes)
    speakers = sorted({r.speaker for r in records if r.speaker is not None}, key=lambda s: (natural(s), s))
    numbers = {speaker: number for number, speaker in enumerate(speakers, 1)}
    chapters = sorted({r.chapter for r in records if r.chapter is not None} | {s.chapter for s in found if s.chapter is not None})

    def block(label: str, outcomes: list[Record], chapter_scenes: list[SceneRecord]) -> list[str]:
        return _block(label, outcomes, numbers, triage_ids, None if scenes is None else chapter_scenes, scene_ids)

    lines: list[str] = []
    for chapter in chapters:
        lines += block(
            f"chapter {chapter}",
            [r for r in records if r.chapter == chapter],
            [s for s in found if s.chapter == chapter],
        )
    unattributed = [r for r in records if r.chapter is None]
    unattributed_scenes = [s for s in found if s.chapter is None]
    if unattributed or unattributed_scenes:
        lines += block("unattributed", unattributed, unattributed_scenes)
    lines += block("total", records, found)
    return lines


def _scene_line(table: list[scene_file.SceneLine], keys: set[Key]) -> str:
    scenes = {line.scene for line in table}
    table_keys = {(line.namespace, line.key) for line in table}
    covered = {line.scene for line in table if (line.namespace, line.key) in keys}
    family = {namespace for namespace, key in keys if (namespace, key) not in table_keys}
    return f"scenes: {len(covered)} of {len(scenes)} from the scene table, {len(family)} outside it"


def _check_scene_patterns(path: Path, scenes: list[SceneRecord], scene_ids: list[str]) -> None:
    # read_scenes keeps one record per line, so a record's index plus one is its line number.
    for lineno, scene in enumerate(scenes, 1):
        if any(pattern not in scene_ids for pattern in scene.patterns):
            raise Fail(f"{path}:{lineno}: field patterns has an id that is not a scene pattern id of the lens")


def cmd_report(args: argparse.Namespace) -> None:
    lens_dir = work_dir(args.lens)
    lens = load_lens(repo_root() / "lenses" / f"{args.lens}.toml")
    triage_ids = [q.id for q in lens.triage]
    scene_ids = [] if lens.scene is None else [p.id for p in lens.scene.patterns]
    records = _records(lens_dir / OUTCOMES, frozenset(triage_ids + scene_ids))
    keys = _keys(lens_dir / OUTCOMES)
    arcs, with_changes = _arcs(lens_dir / ARCS)
    recall = _recall(lens_dir / RECALL, triage_ids)
    scenes_path = lens_dir / SCENES
    scenes = read_scenes(scenes_path) if scenes_path.exists() else None
    if scenes is not None:
        _check_scene_patterns(scenes_path, scenes, scene_ids)
    table = scene_file.load()
    for line in render(records, triage_ids, scenes=scenes, scene_ids=scene_ids):
        print(line)
    print(_scene_line(table, keys))
    print(f"arcs: {arcs}, {with_changes} with changes")
    for rid in triage_ids:
        disagreements, confirmed = recall[rid]
        print(f"recall {rid}: {disagreements} disagreements, {confirmed} confirmed")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("report", help="count a lens run's outcomes per chapter, reason, character and gate")
    p.add_argument("--lens", required=True, help="name of the lens run to count, read from lenses/<name>.toml and work/<name>/")
    p.set_defaults(fn=cmd_report)
