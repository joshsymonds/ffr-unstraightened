"""report: count a lens run's outcomes per chapter. Prints counts only, never speaker ids, keys or line text."""

import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ffrd import scenes as scene_file
from ffrd.game import Fail, natural, repo_root
from ffrd.pipeline.lens import load_lens
from ffrd.pipeline.queering import SELECTIONS
from ffrd.pipeline.scene_record import SCENE_KEEP_REASONS, SCENES, SceneRecord, read_scenes
from ffrd.pipeline.stages import KEEP_REASONS
from ffrd.pipeline.store import work_dir

OUTCOMES = "outcomes.jsonl"
SELECTION = "selection.json"
ARCS = "arcs.json"
RECALL = "recall.json"
USAGE = "usage.jsonl"
RECALL_FIELDS = frozenset({"disagreements", "confirmed"})
OUTCOME_LABELS = {
    "changed": "changed",
    "kept_for_plot": "kept for plot",
    "gate_failed": "gate failed",
    "unfinished": "unfinished",
    "untouched": "untouched",
}
GATE = re.compile(r"[a-z0-9]+([-_][a-z0-9]+)*")
QUEERING_REWRITES = ("queer", "trans", "redirect")
FLIRT_BUCKETS = frozenset({"redirect", "not-redirected"})
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
    redo: bool = False
    bucket: str | None = None


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


def _redo(obj: dict[str, object], where: str) -> bool:
    if "redo" not in obj:
        return False
    value = obj["redo"]
    if isinstance(value, bool):
        return value
    raise Fail(f"{where}: field redo must be true or false")


def _bucket(obj: dict[str, object], where: str) -> str | None:
    if "bucket" not in obj:
        return None
    value = obj["bucket"]
    if value is None:
        return None
    if isinstance(value, str) and value in SELECTIONS:
        return value
    raise Fail(f"{where}: field bucket is not a queering bucket or null")


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
        redo=_redo(obj, where),
        bucket=_bucket(obj, where),
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


@dataclass(frozen=True)
class Usage:
    run: str
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float


def _cost(obj: dict[str, object], where: str) -> float:
    value = _require(obj, "cost_usd", where)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise Fail(f"{where}: field cost_usd must be a non-negative number")
    return float(value)


def _usage_entry(text: str, where: str) -> Usage:
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise Fail(f"{where}: is not valid JSON") from e
    if not isinstance(obj, dict):
        raise Fail(f"{where}: is not a JSON object")
    _text(obj, "run", where)
    _text(obj, "stage", where)
    _text(obj, "model", where)
    _count(obj, "duration_ms", where)
    return Usage(
        run=obj["run"],
        stage=obj["stage"],
        model=obj["model"],
        input_tokens=_count(obj, "input_tokens", where),
        output_tokens=_count(obj, "output_tokens", where),
        cache_read_tokens=_count(obj, "cache_read_tokens", where),
        cache_creation_tokens=_count(obj, "cache_creation_tokens", where),
        cost_usd=_cost(obj, where),
    )


def _usage(path: Path) -> list[Usage]:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise Fail(f"cannot read usage {path}: {e.strerror}") from e
    entries: list[Usage] = []
    with handle:
        try:
            for lineno, raw in enumerate(handle, 1):
                where = f"{path}:{lineno}"
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as e:
                    raise Fail(f"{where}: is not valid UTF-8") from e
                entries.append(_usage_entry(text, where))
        except OSError as e:
            raise Fail(f"cannot read usage {path}: {e.strerror}") from e
    return entries


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


@dataclass(frozen=True)
class Round:
    tried: int
    passed: int


@dataclass(frozen=True)
class Side:
    share: int
    pool: int
    target: int
    rounds: tuple[Round, ...]
    achieved: int
    exhausted: bool


@dataclass(frozen=True)
class Selection:
    romance_lines: int
    romance_characters: int
    romance_speakerless: int
    gender_lines: int
    gender_characters: int
    eligible: int
    queer: Side
    trans: Side


def _selection_field(obj: dict[str, object], name: str, prefix: str, path: Path) -> object:
    if name not in obj:
        raise Fail(f"{path}: field {prefix}{name} is missing")
    return obj[name]


def _selection_count(obj: dict[str, object], name: str, prefix: str, path: Path) -> int:
    value = _selection_field(obj, name, prefix, path)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Fail(f"{path}: field {prefix}{name} must be a non-negative integer")
    return value


def _selection_object(obj: dict[str, object], name: str, prefix: str, path: Path) -> dict[str, object]:
    value = _selection_field(obj, name, prefix, path)
    if not isinstance(value, dict):
        raise Fail(f"{path}: field {prefix}{name} must be an object")
    return value


def _round(entry: object, field: str, path: Path) -> Round:
    if not isinstance(entry, dict):
        raise Fail(f"{path}: field {field} must be an object")
    return Round(
        tried=_selection_count(entry, "tried", f"{field}.", path),
        passed=_selection_count(entry, "passed", f"{field}.", path),
    )


def _side(obj: dict[str, object], name: str, path: Path) -> Side:
    side = _selection_object(obj, name, "", path)
    prefix = f"{name}."
    rounds = _selection_field(side, "rounds", prefix, path)
    if not isinstance(rounds, list):
        raise Fail(f"{path}: field {prefix}rounds must be a list")
    exhausted = _selection_field(side, "exhausted", prefix, path)
    if not isinstance(exhausted, bool):
        raise Fail(f"{path}: field {prefix}exhausted must be true or false")
    return Side(
        share=_selection_count(side, "share", prefix, path),
        pool=_selection_count(side, "pool", prefix, path),
        target=_selection_count(side, "target", prefix, path),
        rounds=tuple(_round(entry, f"{prefix}rounds.{index}", path) for index, entry in enumerate(rounds)),
        achieved=_selection_count(side, "achieved", prefix, path),
        exhausted=exhausted,
    )


def _selection(path: Path) -> Selection:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise Fail(f"no selection at {path}; run the lens first") from e
    except OSError as e:
        raise Fail(f"cannot read selection {path}: {e.strerror}") from e
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise Fail(f"{path}: is not valid JSON") from e
    if not isinstance(obj, dict):
        raise Fail(f"{path}: is not a JSON object")
    romance = _selection_object(obj, "romance", "", path)
    gender = _selection_object(obj, "gender_roles", "", path)
    return Selection(
        romance_lines=_selection_count(romance, "lines", "romance.", path),
        romance_characters=_selection_count(romance, "characters", "romance.", path),
        romance_speakerless=_selection_count(romance, "speakerless", "romance.", path),
        gender_lines=_selection_count(gender, "lines", "gender_roles.", path),
        gender_characters=_selection_count(gender, "characters", "gender_roles.", path),
        eligible=_selection_count(obj, "eligible_characters", "", path),
        queer=_side(obj, "queer", path),
        trans=_side(obj, "trans", path),
    )


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


def _redo_line(records: list[Record]) -> str:
    listed = [r for r in records if r.redo]
    changed = sum(r.outcome == "changed" for r in listed)
    gate_failed = sum(r.outcome == "gate_failed" for r in listed)
    return f"redo: {len(listed)} listed, {changed} changed, {gate_failed} gate failed"


def _side_lines(label: str, side: Side) -> list[str]:
    exact = side.share * side.pool / 100
    lines = [f"queering {label} target: {side.target} ({side.share}% of {side.pool} = {exact:.2f})"]
    lines += [f"queering {label} round {n}: tried {r.tried}, passed {r.passed}" for n, r in enumerate(side.rounds, 1)]
    exhausted = ", pool exhausted" if side.exhausted else ""
    lines.append(f"queering {label} achieved: {side.achieved} of {side.target}{exhausted}")
    return lines


def _queering_lines(records: list[Record], selection: Selection) -> list[str]:
    flirt = sum(r.bucket in FLIRT_BUCKETS for r in records)
    redirect = sum(r.bucket == "redirect" for r in records)
    changed = Counter(r.bucket for r in records if r.outcome == "changed")
    gate_failed = Counter(r.bucket for r in records if r.outcome == "gate_failed")
    romance = (
        f"queering romance: {selection.romance_lines} lines, {selection.romance_characters} characters, "
        f"{selection.romance_speakerless} speakerless"
    )
    lines = [romance, f"queering gender roles: {selection.gender_lines} lines, {selection.gender_characters} characters"]
    lines.append(f"queering eligible characters: {selection.eligible}")
    lines += _side_lines("queer", selection.queer)
    lines += _side_lines("trans", selection.trans)
    lines.append(f"queering flirt marked: {flirt}, redirect half: {redirect}")
    lines += [f"queering changed {bucket}: {changed[bucket]}" for bucket in QUEERING_REWRITES]
    lines += [f"queering gate failed {bucket}: {gate_failed[bucket]}" for bucket in QUEERING_REWRITES]
    return lines


def _usage_totals(entries: list[Usage]) -> str:
    prompt = sum(e.input_tokens + e.cache_read_tokens + e.cache_creation_tokens for e in entries)
    return (
        f"{len(entries)} calls, {sum(e.input_tokens for e in entries)} input, {sum(e.output_tokens for e in entries)} output, "
        f"{sum(e.cache_read_tokens for e in entries)} cache read, {sum(e.cache_creation_tokens for e in entries)} cache write, "
        f"{round(prompt / len(entries))} prompt tokens per call, ${sum(e.cost_usd for e in entries):.2f}"
    )


def _usage_lines(entries: list[Usage]) -> list[str]:
    if not entries:
        return []
    latest = max(e.run for e in entries)
    current = [e for e in entries if e.run == latest]
    groups: defaultdict[tuple[str, str], list[Usage]] = defaultdict(list)
    for e in current:
        groups[(e.stage, e.model)].append(e)
    lines = [f"usage run {latest}"]
    lines += [f"usage {stage} {model}: {_usage_totals(groups[(stage, model)])}" for stage, model in sorted(groups)]
    lines.append(f"usage total: {_usage_totals(current)}")
    return lines


def cmd_report(args: argparse.Namespace) -> None:
    lens_dir = work_dir(args.lens)
    lens = load_lens(repo_root() / "lenses" / f"{args.lens}.toml")
    triage_ids = [q.id for q in lens.triage]
    scene_ids = [] if lens.scene is None else [p.id for p in lens.scene.patterns]
    records = _records(lens_dir / OUTCOMES, frozenset(triage_ids + scene_ids))
    keys = _keys(lens_dir / OUTCOMES)
    arcs, with_changes = _arcs(lens_dir / ARCS)
    recall = _recall(lens_dir / RECALL, triage_ids)
    selection = _selection(lens_dir / SELECTION) if lens.selection == "queering" else None
    usage = _usage(lens_dir / USAGE)
    scenes_path = lens_dir / SCENES
    scenes = read_scenes(scenes_path) if scenes_path.exists() else None
    if scenes is not None:
        _check_scene_patterns(scenes_path, scenes, scene_ids)
    table = scene_file.load()
    for line in render(records, triage_ids, scenes=scenes, scene_ids=scene_ids):
        print(line)
    print(_scene_line(table, keys))
    print(f"arcs: {arcs}, {with_changes} with changes")
    print(_redo_line(records))
    if selection is not None:
        for line in _queering_lines(records, selection):
            print(line)
    for rid in triage_ids:
        disagreements, confirmed = recall[rid]
        print(f"recall {rid}: {disagreements} disagreements, {confirmed} confirmed")
    for line in _usage_lines(usage):
        print(line)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("report", help="count a lens run's outcomes per chapter, reason, character and gate")
    p.add_argument("--lens", required=True, help="name of the lens run to count, read from lenses/<name>.toml and work/<name>/")
    p.set_defaults(fn=cmd_report)
