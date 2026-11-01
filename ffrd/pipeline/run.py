"""run: take a lens's scenes through the pipeline, then write its outcome records to work/<lens>/ and its changed lines to dialog/<lens>/. Progress prints scene keys and counts, and writes keys, counts and edits, never vanilla text."""

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ffrd.find import BARE_KEY, toml_string
from ffrd.game import Fail, natural, repo_root, vanilla_hash
from ffrd.pipeline import arcs, bible, claude, coherence, stages
from ffrd.pipeline import jev as jev_client
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Candidate, Judgment, Kept, gate_line
from ffrd.pipeline.lens import Lens, load_lens
from ffrd.pipeline.report import OUTCOME_LABELS, OUTCOMES, RECALL
from ffrd.pipeline.scope import scoped
from ffrd.pipeline.select import choose
from ffrd.pipeline.stages import Ask, Classifier, Renderer, Scene, SceneLine, Triage
from ffrd.pipeline.store import Store, work_dir

CHAPTER_FILE = re.compile(r"chapter-\d{2,}\.toml")
UNATTRIBUTED_FILE = "unattributed.toml"
RECHECK_AT = 0.5

OutcomeKind = Literal["changed", "kept_for_plot", "gate_failed", "unfinished", "untouched"]
WriterName = Literal["voice", "opus"]


@dataclass(frozen=True)
class Outcome:
    namespace: str
    key: str
    chapter: int | None
    speaker: str | None
    reasons: tuple[str, ...]
    outcome: OutcomeKind
    gate: str | None
    writer: WriterName | None


@dataclass(frozen=True)
class Change:
    namespace: str
    key: str
    chapter: int | None
    vanilla: str
    text: str
    lens: str
    reason: str
    writer: WriterName
    run: str


def _mkdir(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Fail(f"cannot create {directory}: {e.strerror}") from e


def _write_atomic(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise Fail(f"cannot write {path}: {e.strerror}") from e


def _outcome_line(o: Outcome) -> str:
    record = {
        "namespace": o.namespace,
        "key": o.key,
        "chapter": o.chapter,
        "speaker": o.speaker,
        "reasons": list(o.reasons),
        "outcome": o.outcome,
        "gate": o.gate,
        "writer": o.writer,
    }
    return json.dumps(record, ensure_ascii=False) + "\n"


def write_outcomes(path: Path, outcomes: Sequence[Outcome]) -> None:
    _write_atomic(path, "".join(_outcome_line(o) for o in outcomes))


def _table(c: Change) -> str:
    key = c.key if BARE_KEY.fullmatch(c.key) else toml_string(c.key)
    return (
        f"[{toml_string(c.namespace)}.{key}]\n"
        f"vanilla = {toml_string(c.vanilla)}\n"
        f"text = {toml_string(c.text)}\n"
        f"lens = {toml_string(c.lens)}\n"
        f"reason = {toml_string(c.reason)}\n"
        f"writer = {toml_string(c.writer)}\n"
        'verdict = "passed"\n'
        f"run = {toml_string(c.run)}\n\n"
    )


def _file_name(chapter: int | None) -> str:
    return UNATTRIBUTED_FILE if chapter is None else f"chapter-{chapter:02d}.toml"


def _is_dialog_file(name: str) -> bool:
    return CHAPTER_FILE.fullmatch(name) is not None or name == UNATTRIBUTED_FILE


def _order(c: Change) -> tuple[list[int | str], str, list[int | str], str]:
    return (natural(c.namespace), c.namespace, natural(c.key), c.key)


def write_dialog(directory: Path, changes: Sequence[Change]) -> None:
    seen: set[tuple[str, str]] = set()
    for c in changes:
        if vanilla_hash(c.text) == c.vanilla:
            raise Fail(f"{c.namespace} {c.key}: text is unchanged from vanilla, nothing to write")
        if (c.namespace, c.key) in seen:
            raise Fail(f"{c.namespace} {c.key}: more than one change for this line")
        seen.add((c.namespace, c.key))

    files: dict[str, list[Change]] = {}
    for c in sorted(changes, key=_order):
        files.setdefault(_file_name(c.chapter), []).append(c)

    _mkdir(directory)
    for name, group in files.items():
        _write_atomic(directory / name, "".join(_table(c) for c in group))

    try:
        entries = list(directory.iterdir())
    except OSError as e:
        raise Fail(f"cannot list {directory}: {e.strerror}") from e
    for path in entries:
        if path.name in files or not _is_dialog_file(path.name) or not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as e:
            raise Fail(f"cannot remove stale {path}: {e.strerror}") from e


@dataclass(frozen=True)
class _Context:
    lens: Lens
    store: Store
    classifier: Classifier
    renderer: Renderer
    run_id: str
    jev: Triage | None
    bibles: Sequence[Mapping[str, Any]]
    arcs: Sequence[Mapping[str, Any]]
    lines: Mapping[tuple[str, str], SceneLine]


@dataclass
class _Recall:
    disagreements: Counter[str] = field(default_factory=Counter)
    confirmed: Counter[str] = field(default_factory=Counter)


def _chapters(scenes: Sequence[Scene]) -> list[tuple[int | None, list[bible.Line]]]:
    by_chapter: dict[int, dict[tuple[str, str], bible.Line]] = {}
    unplaced: dict[str, dict[tuple[str, str], bible.Line]] = {}
    for scene in scenes:
        for line in scene.lines:
            bible_line = bible.Line(line.namespace, line.key, line.speaker or "", line.english)
            seen = unplaced.setdefault(line.namespace, {}) if line.chapter is None else by_chapter.setdefault(line.chapter, {})
            seen.setdefault((line.namespace, line.key), bible_line)
    numbered: list[tuple[int | None, list[bible.Line]]] = [(number, list(seen.values())) for number, seen in sorted(by_chapter.items())]
    loose = [(None, list(unplaced[namespace].values())) for namespace in sorted(unplaced, key=lambda ns: (natural(ns), ns))]
    return numbered + loose


def _speakers(scene: Scene) -> set[str | None]:
    return {line.speaker for line in scene.lines}


def _first_lines(scenes: Sequence[Scene]) -> dict[tuple[str, str], SceneLine]:
    first: dict[tuple[str, str], SceneLine] = {}
    for scene in scenes:
        for line in scene.lines:
            first.setdefault((line.namespace, line.key), line)
    return first


def _outcome(
    line: SceneLine, reasons: tuple[str, ...], kind: OutcomeKind, gate: str | None = None, writer: WriterName | None = None
) -> Outcome:
    return Outcome(line.namespace, line.key, line.chapter, line.speaker, reasons, kind, gate, writer)


def _every_line(scene: Scene, kind: OutcomeKind) -> list[Outcome]:
    return [_outcome(line, (), kind) for line in scene.lines]


def _counts(outcomes: Sequence[Outcome]) -> str:
    counts = Counter(o.outcome for o in outcomes)
    return ", ".join(f"{counts[kind]} {label}" for kind, label in OUTCOME_LABELS.items())


def _related(ctx: _Context, line: SceneLine, callbacks: Sequence[Mapping[str, Any]]) -> list[SceneLine]:
    refs: list[tuple[str, str]] = []
    for callback in callbacks:
        refs.append((callback["line"]["namespace"], callback["line"]["key"]))
        refs.extend((ref["namespace"], ref["key"]) for ref in callback["depends"])
    related: list[SceneLine] = []
    for ref in dict.fromkeys(refs):
        found = ctx.lines.get(ref)
        if found is not None and ref != (line.namespace, line.key):
            related.append(found)
    return related


def _candidates(
    ctx: _Context,
    line: SceneLine,
    drafted: tuple[str, ...] | Unfinished | Rejected,
    origin: dict[str, WriterName],
) -> Sequence[Candidate] | Unfinished:
    if isinstance(drafted, Unfinished):
        return drafted
    texts = () if isinstance(drafted, Rejected) else drafted
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for text in texts:
        voiced = stages.render(ctx.store, ctx.renderer, line.speaker, text)
        options: tuple[tuple[WriterName, str], ...] = (("opus", text), ("voice", voiced))
        for writer, option in options:
            if option == line.english or option in seen:
                continue
            seen.add(option)
            origin.setdefault(option, writer)
            candidates.append(Candidate(line.english, line.japanese, option))
    return candidates


def _judge(
    ctx: _Context,
    scene: Scene,
    line: SceneLine,
    callbacks: Sequence[Mapping[str, Any]],
    candidate: Candidate,
    reasons: tuple[str, ...],
    intent: str,
    context: Mapping[str, Any],
    ask: Ask,
) -> Judgment | Unfinished | Rejected:
    verdict = stages.judge(ctx.store, ctx.lens, scene, line, candidate.text, callbacks, reasons, intent, context, ask)
    if not isinstance(verdict, Judgment) or not callbacks:
        return verdict
    if not (verdict.meaning and verdict.plot and verdict.minimal_change and verdict.callback and verdict.fixed):
        return verdict
    consistent = stages.cross_judge(
        ctx.store, ctx.lens, line, candidate.text, callbacks, _related(ctx, line, callbacks), reasons, intent, context, ask
    )
    if isinstance(consistent, Unfinished | Rejected):
        return consistent
    return verdict if consistent else replace(verdict, callback=False)


def _drafted_for(
    answer: dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected, line: SceneLine
) -> tuple[str, ...] | Unfinished | Rejected:
    if isinstance(answer, dict):
        return answer[(line.namespace, line.key)]
    return answer


def _first_drafts(
    ctx: _Context, scene: Scene, targets: Sequence[stages.Target], ask: Ask
) -> dict[tuple[str, str], tuple[str, ...] | Unfinished | Rejected]:
    if not targets:
        return {}
    answer = stages.draft(ctx.store, ctx.lens, scene, targets, None, ask)
    return {(target.line.namespace, target.line.key): _drafted_for(answer, target.line) for target in targets}


def _intend(
    ctx: _Context,
    scene: Scene,
    line: SceneLine,
    reasons: tuple[str, ...],
    ask: Ask,
) -> stages.Target | Outcome:
    callbacks = tuple(bible.touching(ctx.bibles, line.namespace, line.key))
    context = arcs.context(ctx.arcs, ctx.bibles, _speakers(scene), line.speaker)
    intended = stages.intent(ctx.store, ctx.lens, scene, line, reasons, callbacks, context, ask)
    if not isinstance(intended, str):
        return _outcome(line, reasons, "unfinished")
    return stages.Target(line, intended, callbacks, context)


def _line(
    ctx: _Context,
    scene: Scene,
    target: stages.Target,
    reasons: tuple[str, ...],
    first: tuple[str, ...] | Unfinished | Rejected,
    ask: Ask,
) -> tuple[Outcome, Change | None]:
    line = target.line
    origin: dict[str, WriterName] = {}

    def write(retry: str | None) -> Sequence[Candidate] | Unfinished:
        if retry is None:
            return _candidates(ctx, line, first, origin)
        return _candidates(ctx, line, _drafted_for(stages.draft(ctx.store, ctx.lens, scene, [target], retry, ask), line), origin)

    def judge_candidate(candidate: Candidate) -> Judgment | Unfinished | Rejected:
        return _judge(ctx, scene, line, target.callbacks, candidate, reasons, target.intent, target.context, ask)

    verdict = gate_line(write, classify=ctx.classifier.score, threshold=ctx.classifier.threshold, judge=judge_candidate)
    if isinstance(verdict, Kept):
        if verdict.gate == "plot":
            return _outcome(line, reasons, "kept_for_plot", gate="plot"), None
        return _outcome(line, reasons, "gate_failed", gate=verdict.gate), None
    if isinstance(verdict, Unfinished | RateLimited):
        return _outcome(line, reasons, "unfinished"), None
    best = choose(verdict)
    if best is None:
        raise RuntimeError("gate_line passed a line without a candidate")
    text = best.candidate.text
    writer = origin[text]
    change = Change(
        namespace=line.namespace,
        key=line.key,
        chapter=line.chapter,
        vanilla=vanilla_hash(line.english),
        text=text,
        lens=ctx.lens.name,
        reason=reasons[0],
        writer=writer,
        run=ctx.run_id,
    )
    return _outcome(line, reasons, "changed", writer=writer), change


def _join(
    lens: Lens,
    flagged: Mapping[tuple[str, str], tuple[str, ...]],
    rechecked: Mapping[tuple[str, str], tuple[str, ...]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    joined = dict(flagged)
    for ref, reasons in rechecked.items():
        ids = {*joined.get(ref, ()), *reasons}
        joined[ref] = tuple(question.id for question in lens.triage if question.id in ids)
    return joined


def _coherent(ctx: _Context, scene: Scene, outcomes: list[Outcome], changes: list[Change], ask: Ask) -> tuple[list[Outcome], list[Change]]:
    if len(changes) < 2:
        return outcomes, changes
    rewrites = {(change.namespace, change.key): change.text for change in changes}
    scene_arcs = arcs.context(ctx.arcs, ctx.bibles, _speakers(scene), None)["arcs"]
    verdict = coherence.check(ctx.store, ctx.lens, scene, rewrites, scene_arcs, ask)
    if isinstance(verdict, Unfinished | Rejected):
        return [replace(o, outcome="unfinished", writer=None) if (o.namespace, o.key) in rewrites else o for o in outcomes], []
    return (
        [replace(o, outcome="gate_failed", gate="scene", writer=None) if (o.namespace, o.key) in verdict else o for o in outcomes],
        [change for change in changes if (change.namespace, change.key) not in verdict],
    )


def _scene(ctx: _Context, scene: Scene, ask: Ask) -> tuple[list[Outcome], list[Change], _Recall]:
    cast = arcs.cast(ctx.bibles, _speakers(scene))
    flagged = stages.screen(ctx.store, ctx.lens, scene, cast, ask)
    if not isinstance(flagged, dict):
        return _every_line(scene, "unfinished"), [], _Recall()
    recall = _Recall()
    if ctx.jev is not None:
        probabilities = stages.triage(ctx.store, ctx.lens, scene, ctx.jev)
        if not isinstance(probabilities, dict):
            return _every_line(scene, "unfinished"), [], _Recall()
        for question in ctx.lens.triage:
            if probabilities[question.id] < RECHECK_AT or any(question.id in reasons for reasons in flagged.values()):
                continue
            recall.disagreements[question.id] += 1
            rechecked = stages.rescreen(ctx.store, ctx.lens, scene, question, cast, ask)
            if not isinstance(rechecked, dict):
                return _every_line(scene, "unfinished"), [], _Recall()
            if rechecked:
                recall.confirmed[question.id] += 1
            flagged = _join(ctx.lens, flagged, rechecked)
    planned: dict[tuple[str, str], stages.Target | Outcome] = {}
    for line in scene.lines:
        reasons = flagged.get((line.namespace, line.key))
        if reasons is not None:
            planned[(line.namespace, line.key)] = _intend(ctx, scene, line, reasons, ask)
    targets = [plan for plan in planned.values() if isinstance(plan, stages.Target)]
    first = _first_drafts(ctx, scene, targets, ask)
    outcomes: list[Outcome] = []
    changes: list[Change] = []
    for line in scene.lines:
        ref = (line.namespace, line.key)
        reasons = flagged.get(ref)
        if reasons is None:
            outcomes.append(_outcome(line, (), "untouched"))
            continue
        plan = planned[ref]
        if isinstance(plan, Outcome):
            outcomes.append(plan)
            continue
        outcome, change = _line(ctx, scene, plan, reasons, first[ref], ask)
        outcomes.append(outcome)
        if change is not None:
            changes.append(change)
    outcomes, changes = _coherent(ctx, scene, outcomes, changes, ask)
    return outcomes, changes, recall


def _write_recall(path: Path, lens: Lens, recall: _Recall) -> None:
    record = {
        question.id: {"disagreements": recall.disagreements[question.id], "confirmed": recall.confirmed[question.id]}
        for question in lens.triage
    }
    _write_atomic(path, json.dumps(record, ensure_ascii=False) + "\n")


def run(
    root: Path,
    lens: Lens,
    store: Store,
    scenes: Sequence[Scene],
    *,
    classifier: Classifier,
    renderer: Renderer,
    jev: Triage | None,
    run_id: str,
    batch: Callable[[], Ask],
    scene_table: bool,
    out: Callable[[str], None] = print,
) -> list[Outcome]:
    scenes = scoped(scenes, scene_table=scene_table)
    bible_ask = batch()
    built = bible.build(store, _chapters(scenes), lambda parts, schema: bible_ask(claude.OPUS, parts, schema))
    if not isinstance(built, list):
        out("bible: unfinished")
        raise Fail("story bible unfinished; rerun ffrd run")
    ctx = _Context(
        lens=lens,
        store=store,
        classifier=classifier,
        renderer=renderer,
        run_id=run_id,
        jev=jev,
        bibles=built,
        arcs=arcs.consolidate(built),
        lines=_first_lines(scenes),
    )
    outcomes: list[Outcome] = []
    changes: list[Change] = []
    recall = _Recall()
    for scene in scenes:
        scene_outcomes, scene_changes, scene_recall = _scene(ctx, scene, batch())
        outcomes += scene_outcomes
        changes += scene_changes
        recall.disagreements.update(scene_recall.disagreements)
        recall.confirmed.update(scene_recall.confirmed)
        out(f"scene {scene.key}: {_counts(scene_outcomes)}")
    out(f"total: {_counts(outcomes)}")
    write_outcomes(store.root / OUTCOMES, outcomes)
    _write_recall(store.root / RECALL, lens, recall)
    write_dialog(root / "dialog" / lens.name, changes)
    return outcomes


def _batch() -> Ask:
    return stages.Batch().ask


def cmd_run(args: argparse.Namespace) -> None:
    root = repo_root()
    lens = load_lens(root / "lenses" / f"{args.lens}.toml")
    directory = work_dir(args.lens)
    bound = stages.bindings()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run(
        root,
        lens,
        Store(directory),
        bound.scenes.scenes(),
        classifier=bound.classifier,
        renderer=bound.renderer,
        jev=None if args.no_jev else jev_client.triage,
        run_id=run_id,
        batch=_batch,
        scene_table=bound.scenes.scene_table,
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "run",
        help="run a lens's scenes through the screen, the Jev recall check, drafts, gates and selection, then write its outcomes and dialog",
    )
    p.add_argument("--lens", required=True, help="name of the lens to run, read from lenses/<name>.toml and work/<name>/")
    p.add_argument("--no-jev", action="store_true", help="skip the Jev recall check")
    p.set_defaults(fn=cmd_run)
