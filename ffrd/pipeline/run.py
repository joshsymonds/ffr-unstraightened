"""run: take a lens's scenes through the pipeline, then write its outcome records to work/<lens>/ and its changed lines to dialog/<lens>/. Progress prints opaque scene labels and counts, and writes keys, counts and edits, never vanilla text."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ffrd.find import BARE_KEY, toml_string
from ffrd.game import Fail, natural, repo_root, vanilla_hash
from ffrd.pipeline import accounts, arcs, bible, claude, coherence, ledger, queering, scene_pass, stages
from ffrd.pipeline import jev as jev_client
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Candidate, Judgment, Kept, check, gate_line, gate_mechanical
from ffrd.pipeline.lens import Lens, SceneSection, load_lens
from ffrd.pipeline.report import ARCS, OUTCOME_LABELS, OUTCOMES, RECALL, SELECTION, USAGE, Record, render
from ffrd.pipeline.scene_record import SCENES, SceneRecord, write_scenes
from ffrd.pipeline.scope import scoped
from ffrd.pipeline.select import choose, distance
from ffrd.pipeline.stages import Ask, Classifier, Renderer, Scene, SceneLine, Triage
from ffrd.pipeline.store import Result, Store, work_dir

CHAPTER_FILE = re.compile(r"chapter-\d{2,}\.toml")
UNATTRIBUTED_FILE = "unattributed.toml"
RECHECK_AT = 0.5
REDO_NOTE_MAX = 200
MARKS = "marks.jsonl"

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
    keep_reason: str | None = None
    plan: str | None = None
    redo: bool = False
    bucket: queering.Selection | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Change:
    namespace: str
    key: str
    chapter: int | None
    vanilla: str
    text: str
    lens: str
    reasons: tuple[str, ...]
    writer: WriterName
    run: str
    plan: str | None = None
    base: str | None = None


def _mkdir(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise Fail(f"cannot create {directory}: {e.strerror}") from e


@contextlib.contextmanager
def _run_lock(directory: Path, lens: str) -> Iterator[None]:
    _mkdir(directory)
    path = directory / ".run.lock"
    try:
        handle = path.open("a")
    except OSError as e:
        raise Fail(f"cannot open {path}: {e.strerror}") from e
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Fail(f"another ffrd run holds work/{lens}/; stop it first") from e
        except OSError as e:
            raise Fail(f"cannot lock {path}: {e.strerror}") from e
        yield


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
        "keep_reason": o.keep_reason,
        "plan": o.plan,
        "redo": o.redo,
    }
    if o.bucket is not None:
        record["bucket"] = o.bucket
    return json.dumps(record, ensure_ascii=False) + "\n"


def write_outcomes(path: Path, outcomes: Sequence[Outcome]) -> None:
    _write_atomic(path, "".join(_outcome_line(o) for o in outcomes))


def _write_marks(path: Path, run_id: str, marks: Sequence[Outcome]) -> None:
    lines = [
        {
            "run": run_id,
            "namespace": o.namespace,
            "key": o.key,
            "speaker": o.speaker,
            "chapter": o.chapter,
            "questions": list(o.reasons),
            "candidates": list(o.candidates),
        }
        for o in marks
    ]
    _write_atomic(path, "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))


def _table(c: Change) -> str:
    key = c.key if BARE_KEY.fullmatch(c.key) else toml_string(c.key)
    base = "" if c.base is None else f"base = {toml_string(c.base)}\n"
    return (
        f"[{toml_string(c.namespace)}.{key}]\n"
        f"vanilla = {toml_string(c.vanilla)}\n"
        f"{base}"
        f"text = {toml_string(c.text)}\n"
        f"lens = {toml_string(c.lens)}\n"
        f"reason = [{', '.join(toml_string(reason) for reason in c.reasons)}]\n"
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


def opaque_label(text: str) -> str:
    """The first 8 hex of the sha256 of text, which names a scene key or a namespace/key line in progress and Fail output."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _check_changes(changes: Sequence[Change]) -> None:
    seen: set[tuple[str, str]] = set()
    for c in changes:
        line = opaque_label(f"{c.namespace}/{c.key}")
        if vanilla_hash(c.text) == c.vanilla:
            raise Fail(f"line {line}: text is unchanged from vanilla, nothing to write")
        if (c.namespace, c.key) in seen:
            raise Fail(f"line {line}: more than one change for this line")
        seen.add((c.namespace, c.key))


def write_dialog(directory: Path, changes: Sequence[Change]) -> None:
    _check_changes(changes)
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
class _Marking:
    """The queering cast of a run, read from the whole game: the core cast and antagonists that stay vanilla, and the flirt."""

    core: frozenset[str]
    antagonists: frozenset[str]
    flirt: str | None


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
    table_lines: frozenset[tuple[str, str]]
    redo: Mapping[tuple[str, str], str]
    vanilla_hashes: Mapping[tuple[str, str], str]
    base_hashes: Mapping[tuple[str, str], str]
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]]
    marking: _Marking | None = None


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


def _limited(scenes: Sequence[Scene], limit: int) -> list[Scene]:
    kept: list[Scene] = []
    for scene in scenes:
        lines = tuple(line for line in scene.lines if line.chapter is not None and line.chapter <= limit)
        if lines:
            kept.append(Scene(key=scene.key, lines=lines))
    return kept


def _chapter_order(scene: Scene) -> tuple[bool, int]:
    """Orders a scene by the smallest chapter of its lines, with chapterless scenes last."""
    chapters = [line.chapter for line in scene.lines if line.chapter is not None]
    return not chapters, min(chapters, default=0)


def _holds_chapter_one(scene: Scene) -> bool:
    return any(line.chapter == 1 for line in scene.lines)


def _speakers(scene: Scene) -> set[str | None]:
    return {line.speaker for line in scene.lines}


def _first_lines(scenes: Sequence[Scene]) -> dict[tuple[str, str], SceneLine]:
    first: dict[tuple[str, str], SceneLine] = {}
    for scene in scenes:
        for line in scene.lines:
            first.setdefault((line.namespace, line.key), line)
    return first


def _outcome(
    line: SceneLine,
    reasons: tuple[str, ...],
    kind: OutcomeKind,
    gate: str | None = None,
    writer: WriterName | None = None,
    keep_reason: str | None = None,
) -> Outcome:
    return Outcome(line.namespace, line.key, line.chapter, line.speaker, reasons, kind, gate, writer, keep_reason)


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
    unique = list(dict.fromkeys(texts))
    voiced = dict(zip(unique, claude.fan_out(lambda text: stages.render(ctx.store, ctx.renderer, line.speaker, text), unique), strict=True))
    candidates: list[Candidate] = []
    seen: set[str] = set()
    game = ctx.vanilla_hashes[(line.namespace, line.key)]
    for text in texts:
        options: tuple[tuple[WriterName, str], ...] = (("opus", text), ("voice", voiced[text]))
        for writer, option in options:
            if option == line.english or vanilla_hash(option) == game or option in seen:
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
    ctx: _Context, scene: Scene, targets: Sequence[stages.Target], ask: Ask, note: str | None = None
) -> dict[tuple[str, str], tuple[str, ...] | Unfinished | Rejected]:
    if not targets:
        return {}
    answer = stages.draft(ctx.store, ctx.lens, scene, targets, None, ask, note)
    return {(target.line.namespace, target.line.key): _drafted_for(answer, target.line) for target in targets}


def _neighbourhood(ctx: _Context, scene: Scene, line: SceneLine) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
    """The bible callbacks that touch a line and the arc context of its scene's speakers, which names the scene's base rewrites when the lens has a base."""
    if ctx.lens.base is None:
        context = arcs.context(ctx.arcs, ctx.bibles, _speakers(scene), line.speaker)
    else:
        context = arcs.line_context(ctx.arcs, ctx.bibles, _speakers(scene), line)
    rewrites = [
        {"namespace": scene_line.namespace, "key": scene_line.key, **ctx.base_rewrites[(scene_line.namespace, scene_line.key)]}
        for scene_line in scene.lines
        if (scene_line.namespace, scene_line.key) in ctx.base_rewrites
    ]
    if rewrites:
        context = {**context, "base_rewrites": rewrites}
    return tuple(bible.touching(ctx.bibles, line.namespace, line.key)), context


def _intend(
    ctx: _Context,
    scene: Scene,
    line: SceneLine,
    reasons: tuple[str, ...],
    ask: Ask,
    bucket: str | None = None,
) -> stages.Target | Outcome:
    callbacks, context = _neighbourhood(ctx, scene, line)
    intended = stages.intent(ctx.store, ctx.lens, scene, line, reasons, callbacks, context, ask, bucket=bucket)
    if not isinstance(intended, stages.Intent):
        return _outcome(line, reasons, "unfinished")
    if intended.keep:
        kind: OutcomeKind = "kept_for_plot" if intended.keep_reason == "plot" else "untouched"
        return _outcome(line, reasons, kind, keep_reason=intended.keep_reason)
    return stages.Target(line, intended.text, callbacks, context)


def _line(
    ctx: _Context,
    scene: Scene,
    target: stages.Target,
    reasons: tuple[str, ...],
    first: tuple[str, ...] | Unfinished | Rejected,
    ask: Ask,
    note: str | None = None,
) -> tuple[Outcome, Change | None]:
    line = target.line
    origin: dict[str, WriterName] = {}
    scores: dict[str, float] = {}
    judged: dict[str, Judgment | Unfinished | Rejected] = {}

    def classify(text: str) -> float:
        if text not in scores:
            scores[text] = ctx.classifier.score(text)
        return scores[text]

    def judge_candidate(candidate: Candidate) -> Judgment | Unfinished | Rejected:
        if candidate.text not in judged:
            judged[candidate.text] = _judge(ctx, scene, line, target.callbacks, candidate, reasons, target.intent, target.context, ask)
        return judged[candidate.text]

    def check_round(candidates: Sequence[Candidate]) -> None:
        # The round's candidates are checked at once and fill the memos above, so gate_line reads each result instead of asking again.
        claude.fan_out(
            lambda candidate: check(candidate, classify=classify, threshold=ctx.classifier.threshold, judge=judge_candidate), candidates
        )

    def write(retry: str | None) -> Sequence[Candidate] | Unfinished:
        if retry is None:
            produced = _candidates(ctx, line, first, origin)
        else:
            produced = _candidates(
                ctx, line, _drafted_for(stages.draft(ctx.store, ctx.lens, scene, [target], retry, ask, note), line), origin
            )
        if not isinstance(produced, Unfinished):
            check_round(produced)
        return produced

    verdict = gate_line(write, classify=classify, threshold=ctx.classifier.threshold, judge=judge_candidate)
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
        vanilla=ctx.vanilla_hashes[(line.namespace, line.key)],
        text=text,
        lens=ctx.lens.name,
        reasons=reasons,
        writer=writer,
        run=ctx.run_id,
        base=ctx.base_hashes.get((line.namespace, line.key)),
    )
    return _outcome(line, reasons, "changed", writer=writer), change


def _redraft(
    ctx: _Context, scene: Scene, target: stages.Target, reasons: tuple[str, ...], note: str, ask: Ask
) -> tuple[Outcome, Change | None]:
    """Redrafts a line the redo list names, from its note, and gates the redraft like any line-pass line. A redraft that does not pass is gate_failed under redo, so the first rewrite never ships in its place."""
    line = target.line
    first = _first_drafts(ctx, scene, [target], ask, note)[(line.namespace, line.key)]
    outcome, change = _line(ctx, scene, target, reasons, first, ask, note)
    if outcome.outcome == "changed":
        return outcome, change
    if outcome.outcome == "unfinished":
        return outcome, None
    return _outcome(line, reasons, "gate_failed", gate="redo"), None


def _gated(
    ctx: _Context, scene: Scene, target: stages.Target, reasons: tuple[str, ...], first: tuple[str, ...] | Unfinished | Rejected, ask: Ask
) -> tuple[Outcome, Change | None]:
    """Gates one drafted target through the line pass, then redrafts it under its note when the redo list names it."""
    outcome, change = _line(ctx, scene, target, reasons, first, ask)
    line = target.line
    note = ctx.redo.get((line.namespace, line.key))
    if note is None or outcome.outcome != "changed":
        return outcome, change
    return _redraft(ctx, scene, target, reasons, note, ask)


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


def _with_plans(refs: frozenset[tuple[str, str]], changes: Sequence[Change]) -> frozenset[tuple[str, str]]:
    """Widens refs to every changed line that shares a plan with one of them, so a plan reverts or stays unfinished as a whole."""
    plans = {change.plan for change in changes if change.plan is not None and (change.namespace, change.key) in refs}
    return refs | {(change.namespace, change.key) for change in changes if change.plan in plans}


def _scene_arcs(ctx: _Context, scene: Scene) -> Sequence[Mapping[str, Any]]:
    if ctx.lens.base is None:
        return arcs.context(ctx.arcs, ctx.bibles, _speakers(scene), None)["arcs"]
    return arcs.scene_arcs(ctx.arcs, scene)


def _coherent(ctx: _Context, scene: Scene, outcomes: list[Outcome], changes: list[Change], ask: Ask) -> tuple[list[Outcome], list[Change]]:
    if len(changes) < 2:
        return outcomes, changes
    rewrites = {(change.namespace, change.key): change.text for change in changes}
    verdict = coherence.check(ctx.store, ctx.lens, scene, rewrites, _scene_arcs(ctx, scene), ask, base_rewrites=ctx.base_rewrites)
    if isinstance(verdict, Unfinished | Rejected):
        return [replace(o, outcome="unfinished", writer=None) if (o.namespace, o.key) in rewrites else o for o in outcomes], []
    return (
        [replace(o, outcome="gate_failed", gate="scene", writer=None) if (o.namespace, o.key) in verdict else o for o in outcomes],
        [change for change in changes if (change.namespace, change.key) not in verdict],
    )


def _arc_outcome(outcome: Outcome, verdicts: arcs.Verdicts) -> Outcome:
    ref = (outcome.namespace, outcome.key)
    if outcome.outcome != "changed":
        return outcome
    if ref in verdicts.reverted:
        return replace(outcome, outcome="gate_failed", gate="arc", writer=None)
    if ref in verdicts.unfinished:
        return replace(outcome, outcome="unfinished", writer=None)
    return outcome


def _unified(ctx: _Context, outcomes: Sequence[Outcome], changes: Sequence[Change]) -> tuple[list[Outcome], list[Change]]:
    """Gives every changed line that shares one English text the text and writer of the earliest listed one in story order, or of the earliest one when none is listed. Each line keeps its own key, plan and outcome, and only the rewrite and writer are copied."""
    position = {(outcome.namespace, outcome.key): index for index, outcome in enumerate(outcomes) if outcome.outcome == "changed"}
    english = {(change.namespace, change.key): ctx.lines[(change.namespace, change.key)].english for change in changes}
    earliest: dict[str, Change] = {}
    for change in sorted(changes, key=lambda c: ((c.namespace, c.key) not in ctx.redo, position[(c.namespace, c.key)])):
        earliest.setdefault(english[(change.namespace, change.key)], change)
    sources = {ref: earliest[text] for ref, text in english.items()}
    unified_changes = [
        replace(change, text=sources[(change.namespace, change.key)].text, writer=sources[(change.namespace, change.key)].writer)
        for change in changes
    ]
    unified_outcomes = [
        replace(outcome, writer=sources[(outcome.namespace, outcome.key)].writer) if outcome.outcome == "changed" else outcome
        for outcome in outcomes
    ]
    return unified_outcomes, unified_changes


def _arc_judged(
    ctx: _Context, scenes: Sequence[Scene], outcomes: list[Outcome], changes: list[Change], ask: Ask
) -> tuple[list[Outcome], list[Change]]:
    """Judges the line-pass changes against the arcs, then judges the line-pass changes that survived with the scene-pass changes under the scene rubric, when the lens has a scene section and a scene-pass change exists. Second-pass reverts widen to whole plans."""
    line_pass = [change for change in changes if change.plan is None]
    verdicts = arcs.judge(
        ctx.store, ctx.lens, ctx.arcs, scenes, {(c.namespace, c.key): c.text for c in line_pass}, ask, base_rewrites=ctx.base_rewrites
    )
    dropped = verdicts.reverted | verdicts.unfinished
    outcomes = [_arc_outcome(outcome, verdicts) for outcome in outcomes]
    scene_pass = [change for change in changes if change.plan is not None]
    if ctx.lens.scene is not None and scene_pass:
        kept = [change for change in line_pass if (change.namespace, change.key) not in dropped]
        scene_refs = frozenset((change.namespace, change.key) for change in scene_pass)
        rewrites = {(change.namespace, change.key): change.text for change in [*kept, *scene_pass]}
        second = arcs.judge_scene(ctx.store, ctx.lens, ctx.arcs, scenes, rewrites, scene_refs, ask, base_rewrites=ctx.base_rewrites)
        widened = arcs.Verdicts(_with_plans(second.reverted, scene_pass), _with_plans(second.unfinished, scene_pass))
        outcomes = [_arc_outcome(outcome, widened) for outcome in outcomes]
        dropped = dropped | widened.reverted | widened.unfinished
    return outcomes, [change for change in changes if (change.namespace, change.key) not in dropped]


def _write_arcs(path: Path, consolidated: Sequence[Mapping[str, Any]], scenes: Sequence[Scene], changes: Sequence[Change]) -> None:
    rewrites = {(change.namespace, change.key): change.text for change in changes}
    record = {"arcs": len(consolidated), "with_changes": arcs.with_changes(consolidated, scenes, rewrites)}
    _write_atomic(path, json.dumps(record, ensure_ascii=False) + "\n")


def _both[A, B](first: Callable[[], A], second: Callable[[], B]) -> tuple[A, B]:
    """Runs two calls at once and returns both answers, each with its own type."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(first)
        right = pool.submit(second)
        return left.result(), right.result()


def _scene_gate(
    ctx: _Context,
    scene: Scene,
    entry: scene_pass.PlannedLine,
    target: stages.Target,
    first: tuple[str, ...] | Unfinished | Rejected,
    ask: Ask,
    note: str | None = None,
) -> tuple[Candidate, WriterName] | Kept | Unfinished | RateLimited:
    """Gates one planned line with the mechanical gates, drops the candidates that keep its carrier, then returns the passing candidate nearest the English line with that candidate's writer. A line left with none fails the scene gate. A redraft's retry carries its note."""
    line = target.line
    origin: dict[str, WriterName] = {}
    scores: dict[str, float] = {}

    def classify(text: str) -> float:
        if text not in scores:
            scores[text] = ctx.classifier.score(text)
        return scores[text]

    def write(retry: str | None) -> Sequence[Candidate] | Unfinished:
        drafted = first if retry is None else _drafted_for(stages.draft(ctx.store, ctx.lens, scene, [target], retry, ask, note), line)
        if isinstance(drafted, Rejected):
            return Unfinished(drafted.detail)
        return _candidates(ctx, line, drafted, origin)

    verdict = gate_mechanical(write, classify=classify, threshold=ctx.classifier.threshold)
    if isinstance(verdict, Kept | Unfinished | RateLimited):
        return verdict
    free = [candidate for candidate in verdict if not scene_pass.keeps_carrier(entry, candidate.text)]
    if not free:
        return Kept("scene")
    best = min(free, key=lambda candidate: (distance(candidate.text, line.english), candidate.text))
    return best, origin[best.text]


@dataclass(frozen=True)
class _Round:
    """One scene-pass plan after its gates and, when no required line failed the gates, the scene judge. Failed lines are gate failures and judge reverts. Redo_failed names the failed lines whose redraft under a reviewer note failed."""

    plan: scene_pass.Plan
    picks: dict[tuple[str, str], tuple[Candidate, WriterName]]
    failed: frozenset[tuple[str, str]]
    required_failed: bool
    gates: Counter[str] = field(default_factory=Counter)
    reverts: int = 0
    redo_failed: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class _PassRecord:
    """A scene's record data from the scene pass: its screen's pattern ids in lens order, the final plan, whether a re-plan ran, and the gate failures and judge reverts across the plans made."""

    patterns: tuple[str, ...] = ()
    plan: scene_pass.Plan | None = None
    replanned: bool = False
    gate_failures: Counter[str] = field(default_factory=Counter)
    judge_reverts: int = 0


def _gate_plan(
    ctx: _Context, scene: Scene, plan: scene_pass.Plan, ask: Ask
) -> dict[tuple[str, str], tuple[Candidate, WriterName] | Kept] | Unfinished | RateLimited:
    lines = {(line.namespace, line.key): line for line in scene.lines}
    planned: list[tuple[scene_pass.PlannedLine, stages.Target]] = []
    for entry in plan.lines:
        line = lines[(entry.namespace, entry.key)]
        callbacks, context = _neighbourhood(ctx, scene, line)
        planned.append((entry, stages.Target(line, entry.intent, callbacks, context)))
    first = _first_drafts(ctx, scene, [target for _, target in planned], ask)

    def gate(item: tuple[scene_pass.PlannedLine, stages.Target]) -> tuple[Candidate, WriterName] | Kept | Unfinished | RateLimited:
        entry, target = item
        ref = (target.line.namespace, target.line.key)
        normal = _scene_gate(ctx, scene, entry, target, first[ref], ask)
        note = ctx.redo.get(ref)
        if note is None or isinstance(normal, Kept | Unfinished | RateLimited):
            return normal
        redrafted = _first_drafts(ctx, scene, [target], ask, note)[ref]
        redraft = _scene_gate(ctx, scene, entry, target, redrafted, ask, note)
        return Kept("redo") if isinstance(redraft, Kept) else redraft

    gated = claude.fan_out(gate, planned)
    settled: dict[tuple[str, str], tuple[Candidate, WriterName] | Kept] = {}
    for (entry, _), result in zip(planned, gated, strict=True):
        if isinstance(result, Unfinished | RateLimited):
            return result
        settled[(entry.namespace, entry.key)] = result
    return settled


def _settled(
    ctx: _Context,
    scene: Scene,
    cast: Sequence[Mapping[str, Any]],
    hits: Mapping[str, Sequence[tuple[str, str]]],
    flagged: Mapping[tuple[str, str], tuple[str, ...]],
    excluded: frozenset[tuple[str, str]],
    line_rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
) -> _Round | Unfinished | Rejected | RateLimited:
    """Plans once, gates the planned lines, and runs the scene judge over the passing lines with the surviving line-pass rewrites as context, unless a required line failed its gates. The judge reverts only passing lines it names."""
    plan = scene_pass.plan(ctx.store, ctx.lens, scene, cast, hits, frozenset(flagged), excluded, ask)
    if not isinstance(plan, scene_pass.Plan):
        return plan
    if plan.keep_reason is not None:
        return _Round(plan, {}, frozenset(), False)
    gated = _gate_plan(ctx, scene, plan, ask)
    if not isinstance(gated, dict):
        return gated
    required = {(entry.namespace, entry.key) for entry in plan.lines if entry.required}
    picks = {ref: result for ref, result in gated.items() if not isinstance(result, Kept)}
    failed = {ref for ref, result in gated.items() if isinstance(result, Kept)}
    redo_failed = {ref for ref, result in gated.items() if isinstance(result, Kept) and result.gate == "redo"}
    gates = Counter(result.gate for result in gated.values() if isinstance(result, Kept))
    reverts = 0
    if picks and not failed & required:
        rewrites = {**line_rewrites, **{ref: candidate.text for ref, (candidate, _) in picks.items()}}
        verdict = scene_pass.judge(ctx.store, ctx.lens, scene, rewrites, _scene_arcs(ctx, scene), ask)
        if not isinstance(verdict, frozenset):
            return verdict
        reverted = frozenset(ref for ref in verdict if ref in picks)
        picks = {ref: pick for ref, pick in picks.items() if ref not in reverted}
        failed |= reverted
        redo_failed |= {ref for ref in reverted if ref in ctx.redo}
        reverts = len(reverted)
    return _Round(plan, picks, frozenset(failed), bool(required & failed), gates, reverts, frozenset(redo_failed))


def _redone(
    outcomes: Sequence[Outcome], lines: Mapping[tuple[str, str], SceneLine], reasons: tuple[str, ...], failed: frozenset[tuple[str, str]]
) -> list[Outcome]:
    """Makes each listed line whose redraft failed a gate_failed under redo, with the scene pass's reasons and no plan. Every other outcome is kept."""
    return [
        _outcome(lines[(outcome.namespace, outcome.key)], reasons, "gate_failed", gate="redo")
        if (outcome.namespace, outcome.key) in failed
        else outcome
        for outcome in outcomes
    ]


def _scene_pass(
    ctx: _Context,
    scene: Scene,
    section: SceneSection,
    cast: Sequence[Mapping[str, Any]],
    flagged: Mapping[tuple[str, str], tuple[str, ...]],
    outcomes: list[Outcome],
    line_rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
) -> tuple[list[Outcome], list[Change], _PassRecord]:
    """Screens a scene for its patterns, plans line rewrites the line pass did not flag, gates them mechanically and judges the passing ones as a plan, with the surviving line-pass rewrites as context. Returns the outcomes and only the scene pass's own changes. A failed required line, by gate or judge, triggers one re-plan that excludes every failed line of the first plan; a failed required line of the re-plan blocks every scene-pass change. A failed optional line is dropped. A listed line whose redraft failed in any round made is gate_failed under redo."""
    lines = {(line.namespace, line.key): line for line in scene.lines}

    def unfinished(
        hit_ids: tuple[str, ...] = (), redone: frozenset[tuple[str, str]] = frozenset()
    ) -> tuple[list[Outcome], list[Change], _PassRecord]:
        kept = [outcome if (outcome.namespace, outcome.key) in flagged else replace(outcome, outcome="unfinished") for outcome in outcomes]
        return _redone(kept, lines, hit_ids, redone), [], _PassRecord(hit_ids)

    hits = scene_pass.screen(ctx.store, ctx.lens, scene, cast, ask)
    if not isinstance(hits, dict):
        return unfinished()
    if not hits:
        return outcomes, [], _PassRecord()
    reasons = tuple(pattern.id for pattern in section.patterns if pattern.id in hits)
    first = _settled(ctx, scene, cast, hits, flagged, frozenset(), line_rewrites, ask)
    if not isinstance(first, _Round):
        return unfinished(reasons)
    if first.plan.keep_reason is not None:
        return outcomes, [], _PassRecord(reasons, first.plan)
    chosen = first
    gates, reverts = first.gates, first.reverts
    redone = first.redo_failed
    if first.required_failed:
        second = _settled(ctx, scene, cast, hits, flagged, first.failed, line_rewrites, ask)
        if not isinstance(second, _Round):
            return unfinished(reasons, redone)
        gates, reverts = gates + second.gates, reverts + second.reverts
        redone = redone | second.redo_failed
        if second.required_failed:
            return _redone(outcomes, lines, reasons, redone), [], _PassRecord(reasons, second.plan, True, gates, reverts)
        chosen = second
    shipped: dict[tuple[str, str], Outcome] = {}
    changes: list[Change] = []
    for ref, (candidate, writer) in chosen.picks.items():
        line = lines[ref]
        shipped[ref] = replace(_outcome(line, reasons, "changed", writer=writer), plan=chosen.plan.id)
        changes.append(
            Change(
                namespace=line.namespace,
                key=line.key,
                chapter=line.chapter,
                vanilla=ctx.vanilla_hashes[(line.namespace, line.key)],
                text=candidate.text,
                lens=ctx.lens.name,
                reasons=reasons,
                writer=writer,
                run=ctx.run_id,
                plan=chosen.plan.id,
                base=ctx.base_hashes.get((line.namespace, line.key)),
            )
        )
    passed = _PassRecord(reasons, chosen.plan, first.required_failed, gates, reverts)
    return (
        _redone([shipped.get((outcome.namespace, outcome.key), outcome) for outcome in outcomes], lines, reasons, redone),
        changes,
        passed,
    )


def _from_table(ctx: _Context, scene: Scene) -> bool:
    """True when every line of the scene came from the game's scene table, the only scenes the scene pass takes."""
    return all((line.namespace, line.key) in ctx.table_lines for line in scene.lines)


def _marking(bibles: Sequence[Mapping[str, Any]], scenes: Sequence[Scene]) -> _Marking:
    """The queering cast from the whole game: each speaker's distinct in-scope lines, the bible's antagonists and its flirt."""
    counts = Counter(line.speaker for line in _first_lines(scenes).values() if line.speaker is not None)
    return _Marking(queering.core_cast(bibles, counts), queering.antagonists(bibles), queering.flirt(bibles))


def _marked(ctx: _Context, scene: Scene, marking: _Marking, ask: Ask) -> tuple[list[Outcome], list[Change], _Recall, _PassRecord]:
    """Phase 1 of queering: one screen on the lens's model over the scene's markable lines. Each marked line is recorded untouched with its reasons, and every other line untouched. Buckets are set per round once every scene is screened (_bucketed). A scene with no markable line makes no call."""
    eligible = {
        (line.namespace, line.key)
        for line in scene.lines
        if queering.markable(line.speaker, marking.core, marking.antagonists, marking.flirt)
    }
    if not eligible:
        return _every_line(scene, "untouched"), [], _Recall(), _PassRecord()
    cast = [{**entry, "flirt": True} if entry["speaker"] == marking.flirt else entry for entry in arcs.scene_cast(ctx.bibles, scene)]
    marked = stages.screen(ctx.store, ctx.lens, scene, cast, ask, model=ctx.lens.screen_model, eligible=eligible)
    if not isinstance(marked, dict):
        return _every_line(scene, "unfinished"), [], _Recall(), _PassRecord()
    outcomes = [_outcome(line, marked.get((line.namespace, line.key), ()), "untouched") for line in scene.lines]
    return outcomes, [], _Recall(), _PassRecord()


def _candidate_pass(
    ctx: _Context, marking: _Marking, scene: Scene, scene_outcomes: Sequence[Outcome], ask: Ask
) -> tuple[list[Outcome], bool]:
    """Phase 1b of queering, once every scene is screened: one candidate call over the scene's named lines outside the core cast, the antagonists and the flirt that carry no romance mark and that the screen finished. Each listed line takes its candidate marks, and a scene with no such line makes no call. Returns the scene's outcomes and whether its call finished. A call that does not finish marks none of the scene's lines."""
    listed = {
        (line.namespace, line.key)
        for line, outcome in zip(scene.lines, scene_outcomes, strict=True)
        if line.speaker
        and queering.selectable(line.speaker, marking.core, marking.antagonists, marking.flirt)
        and queering.ROMANCE not in outcome.reasons
        and outcome.outcome != "unfinished"
    }
    if not listed:
        return list(scene_outcomes), True
    answer = stages.candidates(ctx.store, scene, listed, ask)
    if not isinstance(answer, dict):
        return list(scene_outcomes), False
    return [
        replace(outcome, candidates=answer.get((outcome.namespace, outcome.key), ()))
        if (outcome.namespace, outcome.key) in listed
        else outcome
        for outcome in scene_outcomes
    ], True


def _unit(outcome: Outcome) -> str:
    return queering.unit(outcome.namespace, outcome.key, outcome.speaker)


def _trans_candidate(outcome: Outcome, trans: frozenset[str]) -> bool:
    """A trans pick's line that carries the trans-candidate mark. Only these lines take the trans bucket."""
    return outcome.speaker in trans and stages.TRANS_CANDIDATE in outcome.candidates


def _bucket(outcome: Outcome, marking: _Marking, queer: frozenset[str], trans: frozenset[str]) -> queering.Selection:
    """A line's bucket in one round. A trans pick's trans-candidate lines are trans. The flirt keeps the redirect split, a romance line of a queer pick is queer, and every other marked line is untouched."""
    if _trans_candidate(outcome, trans):
        return "trans"
    if marking.flirt is not None and outcome.speaker == marking.flirt:
        return "redirect" if queering.redirected(outcome.namespace, outcome.key) else "not-redirected"
    if queering.ROMANCE in outcome.reasons and _unit(outcome) in queer:
        return "queer"
    return "untouched"


def _bucketed(marking: _Marking, outcomes: Sequence[Outcome], queer: frozenset[str], trans: frozenset[str]) -> list[Outcome]:
    """Buckets the lines of one scene for a round. A marked line, screen or candidate, takes its bucket, and a trans pick's trans-candidate line takes the trans bucket. Any other unmarked line keeps no bucket."""
    return [
        replace(outcome, bucket=_bucket(outcome, marking, queer, trans)) if outcome.reasons or outcome.candidates else outcome
        for outcome in outcomes
    ]


def _romance_pool(marking: _Marking, marks: Sequence[Outcome]) -> set[str]:
    """The units with a romance-marked line that the queer side may pick. Gender-roles marks never make a unit part of the pool."""
    return {
        _unit(outcome)
        for outcome in marks
        if queering.ROMANCE in outcome.reasons and queering.selectable(outcome.speaker, marking.core, marking.antagonists, marking.flirt)
    }


def _trans_pool(marking: _Marking, marks: Sequence[Outcome]) -> set[str]:
    """The named speakers outside the core cast, the antagonists and the flirt with a trans-candidate-marked line, which the trans side may pick. A speakerless marked line is never a trans unit."""
    return {
        outcome.speaker
        for outcome in marks
        if outcome.speaker
        and stages.TRANS_CANDIDATE in outcome.candidates
        and queering.selectable(outcome.speaker, marking.core, marking.antagonists, marking.flirt)
    }


def _eligible(marking: _Marking, scenes: Sequence[Scene]) -> frozenset[str]:
    """The distinct speakers of the run's scenes outside the core cast, the antagonists and the flirt. They are counted as the eligible characters."""
    return frozenset(
        line.speaker
        for scene in scenes
        for line in scene.lines
        if line.speaker and queering.selectable(line.speaker, marking.core, marking.antagonists, marking.flirt)
    )


def _characters(marks: Sequence[Outcome]) -> int:
    return len({outcome.speaker for outcome in marks if outcome.speaker})


def _scene(ctx: _Context, scene: Scene, ask: Ask) -> tuple[list[Outcome], list[Change], _Recall, _PassRecord]:
    if ctx.marking is not None:
        return _marked(ctx, scene, ctx.marking, ask)
    cast = arcs.cast(ctx.bibles, _speakers(scene))

    def screen() -> dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected:
        return stages.screen(ctx.store, ctx.lens, scene, cast, ask, model=ctx.lens.screen_model)

    def triage() -> dict[str, float] | RateLimited | Unfinished | Rejected | None:
        if ctx.jev is None:
            return None
        return stages.triage(ctx.store, ctx.lens, scene, ctx.jev)

    flagged, probabilities = _both(screen, triage)
    if not isinstance(flagged, dict):
        return _every_line(scene, "unfinished"), [], _Recall(), _PassRecord()
    recall = _Recall()
    if ctx.jev is not None:
        if not isinstance(probabilities, dict):
            return _every_line(scene, "unfinished"), [], _Recall(), _PassRecord()
        # A disagreement is decided from the screen's flags only. A recheck adds just its own triage id, so it never decides another id.
        questions = [
            question
            for question in ctx.lens.triage
            if probabilities[question.id] >= RECHECK_AT and not any(question.id in reasons for reasons in flagged.values())
        ]
        rechecks = claude.fan_out(lambda question: stages.rescreen(ctx.store, ctx.lens, scene, question, cast, ask), questions)
        for question, rechecked in zip(questions, rechecks, strict=True):
            recall.disagreements[question.id] += 1
            if not isinstance(rechecked, dict):
                return _every_line(scene, "unfinished"), [], _Recall(), _PassRecord()
            if rechecked:
                recall.confirmed[question.id] += 1
            flagged = _join(ctx.lens, flagged, rechecked)
    flagged_lines = [line for line in scene.lines if (line.namespace, line.key) in flagged]
    intended = claude.fan_out(lambda line: _intend(ctx, scene, line, flagged[(line.namespace, line.key)], ask), flagged_lines)
    planned = {(line.namespace, line.key): plan for line, plan in zip(flagged_lines, intended, strict=True)}
    targets = [plan for plan in planned.values() if isinstance(plan, stages.Target)]
    first = _first_drafts(ctx, scene, targets, ask)

    def gate(line: SceneLine) -> tuple[Outcome, Change | None]:
        ref = (line.namespace, line.key)
        reasons = flagged.get(ref)
        if reasons is None:
            return _outcome(line, (), "untouched"), None
        plan = planned[ref]
        if isinstance(plan, Outcome):
            return plan, None
        return _gated(ctx, scene, plan, reasons, first[ref], ask)

    gated = claude.fan_out(gate, scene.lines)
    outcomes = [outcome for outcome, _ in gated]
    changes = [change for _, change in gated if change is not None]
    outcomes, changes = _coherent(ctx, scene, outcomes, changes, ask)
    section = ctx.lens.scene
    passed = _PassRecord()
    if section is not None and _from_table(ctx, scene):
        line_rewrites = {(change.namespace, change.key): change.text for change in changes}
        outcomes, scene_changes, passed = _scene_pass(ctx, scene, section, cast, flagged, outcomes, line_rewrites, ask)
        changes += scene_changes
    return outcomes, changes, recall, passed


def _trans_line(
    ctx: _Context,
    scene: Scene,
    line: SceneLine,
    marked: Outcome,
    answer: stages.PickChange | Unfinished | Rejected | None,
    ask: Ask,
) -> tuple[Outcome, Change | None]:
    """One line of a trans pick. The line the pick's answer names gates as a line-pass line with the answer's intent and drafts. A failed answer leaves the line unfinished, and any other line stays unchanged."""
    if isinstance(answer, Unfinished | Rejected):
        return _outcome(line, marked.reasons, "unfinished"), None
    if answer is None or (answer.namespace, answer.key) != (line.namespace, line.key):
        return _outcome(line, marked.reasons, "untouched"), None
    callbacks, context = _neighbourhood(ctx, scene, line)
    return _gated(ctx, scene, stages.Target(line, answer.intent, callbacks, context), marked.reasons, answer.drafts, ask)


def _trans_picked(
    ctx: _Context,
    scenes: Sequence[tuple[Scene, Ask]],
    picked: Mapping[int, Sequence[tuple[SceneLine, Outcome]]],
    ask: Ask,
) -> dict[tuple[int, str, str], tuple[Outcome, Change | None]]:
    """One trans pick. Its lines across the run's scenes go to one pick draft call, which changes at most one of them. The chosen line gates once, in its earliest scene, and its later occurrences take that outcome with no change of their own. Every other line stays untouched with the trans bucket, and a failed call leaves each line unfinished with it."""
    # Every scene keeps its place as context, but each ref is named once across the call, since its one answer covers every occurrence.
    named: set[tuple[str, str]] = set()
    groups: list[tuple[Scene, list[SceneLine]]] = []
    for index, members in picked.items():
        fresh: list[SceneLine] = []
        for line, _ in members:
            if (line.namespace, line.key) not in named:
                named.add((line.namespace, line.key))
                fresh.append(line)
        groups.append((scenes[index][0], fresh))
    answer = stages.pick_draft(ctx.store, ctx.lens, "trans", groups, ask)
    results: dict[tuple[int, str, str], tuple[Outcome, Change | None]] = {}
    gated: dict[tuple[str, str], tuple[Outcome, Change | None]] = {}
    for index, members in picked.items():
        scene = scenes[index][0]
        for line, marked in members:
            ref = (line.namespace, line.key)
            if ref in gated:
                first, _ = gated[ref]
                results[(index, *ref)] = (replace(first, chapter=line.chapter, reasons=marked.reasons), None)
                continue
            outcome, change = _trans_line(ctx, scene, line, marked, answer, ask)
            gated[ref] = (replace(outcome, bucket=marked.bucket), change)
            results[(index, *ref)] = gated[ref]
    return results


def _trans_gated(
    ctx: _Context, scenes: Sequence[tuple[Scene, Ask]], outcomes: Sequence[list[Outcome]], batch: Callable[[], Ask]
) -> dict[tuple[int, str, str], tuple[Outcome, Change | None]]:
    """Gates every line of the round's trans picks, one pick draft call per pick across its scenes."""
    picked: dict[str | None, dict[int, list[tuple[SceneLine, Outcome]]]] = {}
    for index, scene_outcomes in enumerate(outcomes):
        for line, marked in zip(scenes[index][0].lines, scene_outcomes, strict=True):
            if marked.bucket == "trans":
                picked.setdefault(line.speaker, {}).setdefault(index, []).append((line, marked))
    units = [(by_scene, batch()) for by_scene in picked.values()]
    gated = claude.fan_out(lambda unit: _trans_picked(ctx, scenes, unit[0], unit[1]), units)
    return {ref: result for results in gated for ref, result in results.items()}


def _queered(
    ctx: _Context, scenes: Sequence[tuple[Scene, Ask]], outcomes: Sequence[list[Outcome]], batch: Callable[[], Ask]
) -> list[tuple[list[Outcome], list[Change]]]:
    """Phase 2 of queering, once every scene's screen has marked its lines and the round has bucketed them. Each line in a queer or redirect bucket is intended with its bucket. Each speaker's targets are drafted in one call across their scenes, and each speakerless target per scene. Each trans pick's lines are drafted in one call per pick. Every target then gates as a line-pass line, and each scene settles its coherence. Every outcome of a marked line keeps its bucket. Returns each scene's outcomes and changes in run order."""
    intents = [
        (index, scene, ask, line, marked)
        for index, ((scene, ask), scene_outcomes) in enumerate(zip(scenes, outcomes, strict=True))
        for line, marked in zip(scene.lines, scene_outcomes, strict=True)
        if marked.bucket in queering.REWRITTEN
    ]

    def intend(item: tuple[int, Scene, Ask, SceneLine, Outcome]) -> stages.Target | Outcome:
        _, scene, ask, line, marked = item
        return _intend(ctx, scene, line, marked.reasons, ask, marked.bucket)

    intended = claude.fan_out(intend, intents)
    targets: dict[str | None, dict[int, list[stages.Target]]] = {}
    for (index, *_), plan in zip(intents, intended, strict=True):
        if isinstance(plan, stages.Target):
            targets.setdefault(plan.line.speaker, {}).setdefault(index, []).append(plan)
    units: list[tuple[str | None, dict[int, list[stages.Target]], Ask]] = []
    for speaker, by_scene in targets.items():
        if speaker is None:
            units.extend((None, {index: group}, batch()) for index, group in by_scene.items())
        else:
            units.append((speaker, by_scene, batch()))

    def draft(
        unit: tuple[str | None, dict[int, list[stages.Target]], Ask],
    ) -> dict[tuple[int, str, str], tuple[str, ...] | Unfinished | Rejected]:
        speaker, by_scene, ask = unit
        if speaker is None:
            index, group = next(iter(by_scene.items()))
            return {
                (index, namespace, key): drafted for (namespace, key), drafted in _first_drafts(ctx, scenes[index][0], group, ask).items()
            }
        answer = stages.draft_speaker(ctx.store, ctx.lens, [(scenes[index][0], group) for index, group in by_scene.items()], ask)
        return {
            (index, target.line.namespace, target.line.key): _drafted_for(answer, target.line)
            for index, group in by_scene.items()
            for target in group
        }

    firsts: dict[tuple[int, str, str], tuple[str, ...] | Unfinished | Rejected] = {}
    for drafted in claude.fan_out(draft, units):
        firsts.update(drafted)

    def gate(item: tuple[int, Scene, Ask, SceneLine, Outcome, stages.Target | Outcome]) -> tuple[Outcome, Change | None]:
        index, scene, ask, line, marked, plan = item
        if isinstance(plan, Outcome):
            outcome, change = plan, None
        else:
            outcome, change = _gated(ctx, scene, plan, marked.reasons, firsts[(index, line.namespace, line.key)], ask)
        return replace(outcome, bucket=marked.bucket), change

    gated = dict(
        zip(
            [(index, line.namespace, line.key) for index, _, _, line, _ in intents],
            claude.fan_out(gate, [(*intent, plan) for intent, plan in zip(intents, intended, strict=True)]),
            strict=True,
        )
    )

    gated.update(_trans_gated(ctx, scenes, outcomes, batch))

    def settle(index: int) -> tuple[list[Outcome], list[Change]]:
        scene, ask = scenes[index]
        settled: list[Outcome] = []
        changes: list[Change] = []
        for line, marked in zip(scene.lines, outcomes[index], strict=True):
            result = gated.get((index, line.namespace, line.key))
            if result is None:
                settled.append(marked)
                continue
            settled.append(result[0])
            if result[1] is not None:
                changes.append(result[1])
        return _coherent(ctx, scene, settled, changes, ask)

    return claude.fan_out(settle, list(range(len(scenes))))


@dataclass(frozen=True)
class _Screened:
    """A scene after phase 1: its marked outcomes and scene-pass record, with its run position and the ask its phase-two calls use."""

    position: int
    scene: Scene
    ask: Ask
    outcomes: tuple[Outcome, ...]
    passed: _PassRecord


@dataclass(frozen=True)
class _Refill:
    """The refill loop's last round: its outcomes and changes, each side's (tried, passed) rounds in order, and each side's picks with a changed line that survived it."""

    outcomes: list[Outcome]
    changes: list[Change]
    queer_rounds: tuple[tuple[int, int], ...]
    queer_passing: frozenset[str]
    trans_rounds: tuple[tuple[int, int], ...]
    trans_passing: frozenset[str]


def _surviving(outcomes: Sequence[Outcome], bucket: queering.Selection) -> frozenset[str]:
    """The units with a changed line in the bucket that survived every judge of the round."""
    return frozenset(_unit(outcome) for outcome in outcomes if outcome.outcome == "changed" and outcome.bucket == bucket)


def _shared_fate(outcomes: Sequence[Outcome], changes: Sequence[Change]) -> list[Outcome]:
    """A line has one change, though a repeated line can be marked in more than one scene. When that change did not survive its scene's coherence, each other changed occurrence of the line takes the outcome of the occurrence that lost it."""
    kept = {(change.namespace, change.key) for change in changes}
    lost: dict[tuple[str, str], Outcome] = {}
    for outcome in outcomes:
        if outcome.outcome != "changed":
            lost.setdefault((outcome.namespace, outcome.key), outcome)
    shared: list[Outcome] = []
    for outcome in outcomes:
        ref = (outcome.namespace, outcome.key)
        if outcome.outcome == "changed" and ref not in kept and ref in lost:
            source = lost[ref]
            outcome = replace(outcome, outcome=source.outcome, gate=source.gate, writer=None)
        shared.append(outcome)
    return shared


def _settle(
    ctx: _Context, scenes: Sequence[Scene], outcomes: Sequence[Outcome], changes: Sequence[Change], batch: Callable[[], Ask]
) -> tuple[list[Outcome], list[Change]]:
    """Gives each repeated line one fate, unifies the changed lines that share one English text, then arc-judges the changes."""
    unified_outcomes, unified_changes = _unified(ctx, _shared_fate(outcomes, changes), changes)
    return _arc_judged(ctx, scenes, unified_outcomes, unified_changes, batch())


def _round(
    ctx: _Context,
    root: Path,
    marking: _Marking,
    screened: Sequence[_Screened],
    queer: frozenset[str],
    trans: frozenset[str],
    batch: Callable[[], Ask],
) -> tuple[list[Outcome], list[Change]]:
    """One round of the refill loop over the cumulative picks of both sides: buckets the lines, runs phase two, writes the chapter one report from this round, then settles it."""
    bucketed = [_bucketed(marking, entry.outcomes, queer, trans) for entry in screened]
    phase_two = _queered(ctx, [(entry.scene, entry.ask) for entry in screened], bucketed, batch)
    done = [
        (entry.position, entry.scene, entry.passed, scene_outcomes)
        for entry, (scene_outcomes, _) in zip(screened, phase_two, strict=True)
        if _holds_chapter_one(entry.scene)
    ]
    if done:
        _write_provisional(root, ctx, done)
    outcomes = [outcome for scene_outcomes, _ in phase_two for outcome in scene_outcomes]
    changes = [change for _, scene_changes in phase_two for change in scene_changes]
    return _settle(ctx, [entry.scene for entry in screened], outcomes, changes, batch)


def _draw(order: Sequence[str], start: int, taken: frozenset[str], count: int) -> tuple[list[str], int]:
    """Draws up to count units from the order, from position start on, skipping each unit the other side has drawn. Returns the units drawn and the position after the last unit read."""
    drawn: list[str] = []
    position = start
    while position < len(order) and len(drawn) < count:
        unit = order[position]
        position += 1
        if unit not in taken:
            drawn.append(unit)
    return drawn, position


def _spent(passing: frozenset[str], target: int, read: int, order: Sequence[str]) -> bool:
    """A side is spent once its surviving picks meet its target or its order has been read to the end."""
    return len(passing) >= target or read >= len(order)


def _refill(
    ctx: _Context,
    root: Path,
    marking: _Marking,
    screened: Sequence[_Screened],
    queer_order: Sequence[str],
    queer_target: int,
    trans_order: Sequence[str],
    trans_target: int,
    batch: Callable[[], Ask],
) -> _Refill:
    """The refill loop over the two seeded orders. Each round draws the queer side's shortfall, then the trans side's, and neither side draws a unit the other has drawn. The round plays the cumulative picks through phase two, unification and both arc judges, and counts each side's picks with a surviving changed line. It stops once both sides have met their targets or read their orders to the end."""
    queer_drawn: list[str] = []
    trans_drawn: list[str] = []
    queer_read = trans_read = 0
    queer_passing: frozenset[str] = frozenset()
    trans_passing: frozenset[str] = frozenset()
    queer_rounds: list[tuple[int, int]] = []
    trans_rounds: list[tuple[int, int]] = []
    while True:
        new_queer, queer_read = _draw(queer_order, queer_read, frozenset(trans_drawn), queer_target - len(queer_passing))
        queer_drawn += new_queer
        new_trans, trans_read = _draw(trans_order, trans_read, frozenset(queer_drawn), trans_target - len(trans_passing))
        trans_drawn += new_trans
        outcomes, changes = _round(ctx, root, marking, screened, frozenset(queer_drawn), frozenset(trans_drawn), batch)
        queer_passing = _surviving(outcomes, "queer")
        trans_passing = _surviving(outcomes, "trans")
        if new_queer:
            queer_rounds.append((len(new_queer), len(queer_passing.intersection(new_queer))))
        if new_trans:
            trans_rounds.append((len(new_trans), len(trans_passing.intersection(new_trans))))
        if _spent(queer_passing, queer_target, queer_read, queer_order) and _spent(trans_passing, trans_target, trans_read, trans_order):
            return _Refill(outcomes, changes, tuple(queer_rounds), queer_passing, tuple(trans_rounds), trans_passing)


def _side(share: int, pool: int, denominator: int, target: int, rounds: Sequence[tuple[int, int]], achieved: int) -> dict[str, object]:
    """One side of the queering selection: its share, its pool, the denominator its target is a share of, the target, each round's (tried, passed), the picks achieved, and whether it fell short of its target."""
    return {
        "share": share,
        "pool": pool,
        "denominator": denominator,
        "target": target,
        "rounds": [{"tried": tried, "passed": passed} for tried, passed in rounds],
        "achieved": achieved,
        "exhausted": achieved < target,
    }


def _write_selection(
    path: Path, lens: Lens, marks: Sequence[Outcome], eligible: int, pool: int, trans_pool: int, unfinished: int, refill: _Refill
) -> None:
    """Writes the queering selection beside the outcomes: the romance and gender-roles marks, the candidate marks and the scenes whose candidate call did not finish, the eligible characters, and the romance and trans sides. The romance side's share is of the romance pool, and the trans side's is of all eligible characters."""
    romance = [outcome for outcome in marks if queering.ROMANCE in outcome.reasons]
    gender = [outcome for outcome in marks if queering.GENDER_ROLES in outcome.reasons]
    insertable = [outcome for outcome in marks if stages.INSERTABLE in outcome.candidates]
    trans_candidate = [outcome for outcome in marks if stages.TRANS_CANDIDATE in outcome.candidates]
    record = {
        "romance": {
            "lines": len(romance),
            "characters": _characters(romance),
            "speakerless": sum(1 for outcome in romance if not outcome.speaker),
        },
        "gender_roles": {"lines": len(gender), "characters": _characters(gender)},
        "eligible_characters": eligible,
        "candidates": {
            "insertable": {"lines": len(insertable), "characters": _characters(insertable)},
            "trans_candidate": {"lines": len(trans_candidate), "characters": _characters(trans_candidate)},
            "unfinished_scenes": unfinished,
        },
        "sides": {
            "romance": _side(
                lens.romance_share,
                pool,
                pool,
                queering.target(lens.romance_share, pool),
                refill.queer_rounds,
                len(refill.queer_passing),
            ),
            "trans": _side(
                lens.trans_share,
                trans_pool,
                eligible,
                queering.target(lens.trans_share, eligible),
                refill.trans_rounds,
                len(refill.trans_passing),
            ),
        },
    }
    _write_atomic(path, json.dumps(record, ensure_ascii=False) + "\n")


def _record(outcome: Outcome) -> Record:
    return Record(
        chapter=outcome.chapter,
        speaker=outcome.speaker,
        reasons=frozenset(outcome.reasons),
        outcome=outcome.outcome,
        gate=outcome.gate,
        keep_reason=outcome.keep_reason,
        plan=outcome.plan,
    )


def _write_provisional(root: Path, ctx: _Context, done: Sequence[tuple[int, Scene, _PassRecord, Sequence[Outcome]]]) -> None:
    """Writes the counts of Chapter 1 in the report format, before the arc judge. The scene lines cover the scenes holding a Chapter 1 line, in run order."""
    ordered = sorted(done, key=lambda entry: entry[0])
    scenes = [scene for _, scene, _, _ in ordered]
    outcomes = [outcome for _, _, _, scene_outcomes in ordered for outcome in scene_outcomes]
    records = [_record(o) for o in outcomes if o.chapter == 1]
    scene_records = _scene_records(ctx, scenes, [data for _, _, data, _ in ordered], outcomes)
    scene_ids = [] if ctx.lens.scene is None else [pattern.id for pattern in ctx.lens.scene.patterns]
    lines = render(records, [question.id for question in ctx.lens.triage], scenes=scene_records, scene_ids=scene_ids)
    _write_atomic(root / "reports" / f"{ctx.lens.name}-c01.txt", "".join(f"{line}\n" for line in lines))


def _scene_chapter(scene: Scene) -> int | None:
    return min((line.chapter for line in scene.lines if line.chapter is not None), default=None)


def _scene_records(ctx: _Context, scenes: Sequence[Scene], passes: Sequence[_PassRecord], outcomes: Sequence[Outcome]) -> list[SceneRecord]:
    """One record per scene in run order. A plan's changed and reverted counts are the final outcomes that name its id."""
    final: Counter[tuple[str | None, str]] = Counter((o.plan, o.outcome) for o in outcomes if o.plan is not None)
    records: list[SceneRecord] = []
    for scene, data in zip(scenes, passes, strict=True):
        plan = data.plan
        lines = () if plan is None else plan.lines
        plan_id = None if plan is None else plan.id
        records.append(
            SceneRecord(
                scene=scene.key,
                chapter=_scene_chapter(scene),
                eligible=ctx.lens.scene is not None and _from_table(ctx, scene),
                patterns=data.patterns,
                plan=plan_id,
                keep_reason=None if plan is None else plan.keep_reason,
                planned=len(lines),
                required=sum(line.required for line in lines),
                changed=final[(plan_id, "changed")],
                reverted=data.judge_reverts + final[(plan_id, "gate_failed")],
                replanned=int(data.replanned),
                gate_failures=dict(data.gate_failures),
            )
        )
    return records


def _write_recall(path: Path, lens: Lens, recall: _Recall) -> None:
    record = {
        question.id: {"disagreements": recall.disagreements[question.id], "confirmed": recall.confirmed[question.id]}
        for question in lens.triage
    }
    _write_atomic(path, json.dumps(record, ensure_ascii=False) + "\n")


def _base(directory: Path, base: str, scenes: Sequence[Scene]) -> dict[tuple[str, str], str]:
    """Reads the base lens's records under dialog/<base>/ for the lines of the passed scenes. Each such record must carry string vanilla and text, and its vanilla hash must match the game line. Returns each checked line's base text."""
    if not directory.is_dir():
        raise Fail(f"dialog/{base}/: directory is missing")
    lines = _first_lines(scenes)
    texts: dict[tuple[str, str], str] = {}
    for path in sorted(directory.rglob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise Fail(f"{path}: cannot read: {e.strerror}") from e
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise Fail(f"{path}: cannot read as UTF-8 TOML: {e}") from e
        for namespace, keys in data.items():
            if not isinstance(keys, dict):
                raise Fail(f"{path}: {namespace}: must be a table of records")
            for key, record in keys.items():
                if (namespace, key) not in lines:
                    continue
                ref = f"{namespace}/{key}"
                if not isinstance(record, dict) or not isinstance(record.get("vanilla"), str) or not isinstance(record.get("text"), str):
                    raise Fail(f"{path}: {ref}: record needs string vanilla and text")
                if record["vanilla"] != vanilla_hash(lines[(namespace, key)].english):
                    raise Fail(f"{path}: {ref}: changed in the game")
                texts[(namespace, key)] = record["text"]
    return texts


def _base_rewrites(root: Path, base: str, texts: Mapping[tuple[str, str], str]) -> dict[tuple[str, str], dict[str, Any]]:
    """The lens and reasons of each base-rewritten line, from the base lens's latest ledger entry. The entry must hold the hash of the base text, or the base lens has not run over that text."""
    latest = ledger.latest(ledger.read(root, base))
    rewrites: dict[tuple[str, str], dict[str, Any]] = {}
    for ref, text in texts.items():
        entry = latest.get(ref)
        if entry is None or entry.text_hash != vanilla_hash(text):
            line = opaque_label(f"{ref[0]}/{ref[1]}")
            raise Fail(f"ledger/{base}.jsonl: line {line} has no entry for its base rewrite; run ffrd run --lens {base} first")
        rewrites[ref] = {"lens": base, "reasons": list(entry.reasons)}
    return rewrites


def _chained(scenes: Sequence[Scene], texts: Mapping[tuple[str, str], str]) -> list[Scene]:
    """Starts each line that has a base text from that text, the English every stage then reads."""
    return [
        replace(
            scene,
            lines=tuple(
                replace(line, english=texts[(line.namespace, line.key)]) if (line.namespace, line.key) in texts else line
                for line in scene.lines
            ),
        )
        for scene in scenes
    ]


def _redo(path: Path, scenes: Sequence[Scene]) -> dict[tuple[str, str], str]:
    """Reads the lens's redo list, where a missing file is an empty list. Each key must name a line of the in-scope scenes, and each note must be a non-empty string of at most REDO_NOTE_MAX characters."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Fail(f"{path}: cannot read: {e.strerror}") from e
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise Fail(f"{path}: cannot read as UTF-8 TOML: {e}") from e
    in_scope = {(line.namespace, line.key) for scene in scenes for line in scene.lines}
    redo: dict[tuple[str, str], str] = {}
    for namespace, keys in data.items():
        if not isinstance(keys, dict):
            raise Fail(f"{path}: {namespace}: must be a table of notes")
        for key, note in keys.items():
            ref = f"{namespace}/{key}"
            if (namespace, key) not in in_scope:
                raise Fail(f"{path}: {ref}: is not an in-scope line")
            if not isinstance(note, str):
                raise Fail(f"{path}: {ref}: note must be a string")
            if not note.strip():
                raise Fail(f"{path}: {ref}: note is empty")
            if len(note) > REDO_NOTE_MAX:
                raise Fail(f"{path}: {ref}: note is longer than {REDO_NOTE_MAX} characters")
            redo[(namespace, key)] = note
    return redo


class _Metered(Store):
    """The run's store, appending each model call's usage reading to the run's usage file under the stage whose computation made the call."""

    def __init__(self, inner: Store, run_id: str) -> None:
        super().__init__(inner.root)
        self._inner = inner
        self._run_id = run_id
        self._lock = threading.Lock()

    def run(self, stage: str, inputs: Any, lens: str | None, call: Callable[[], Any]) -> Result:
        def metered() -> Any:
            with claude.usage_sink(lambda reading: self._append(stage, reading)):
                return call()

        return self._inner.run(stage, inputs, lens, metered)

    def _append(self, stage: str, reading: claude.Reading) -> None:
        entry = {
            "run": self._run_id,
            "stage": stage,
            "model": reading.model,
            "input_tokens": reading.input_tokens,
            "output_tokens": reading.output_tokens,
            "cache_read_tokens": reading.cache_read_tokens,
            "cache_creation_tokens": reading.cache_creation_tokens,
            "duration_ms": reading.duration_ms,
            "cost_usd": reading.cost_usd,
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        path = self.root / USAGE
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                raise Fail(f"cannot write usage {path}: {e.strerror}") from e


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
    through: int | None = None,
    out: Callable[[str], None] = print,
) -> list[Outcome]:
    if through is not None and through < 1:
        raise Fail("through must be at least 1")
    store = _Metered(store, run_id)
    table_lines = frozenset((line.namespace, line.key) for scene in scenes if scene.table for line in scene.lines)
    vanilla_hashes = {ref: vanilla_hash(line.english) for ref, line in _first_lines(scenes).items()}
    base_texts = {} if lens.base is None else _base(root / "dialog" / lens.base, lens.base, scenes)
    base_rewrites = {} if lens.base is None else _base_rewrites(root, lens.base, base_texts)
    game_scenes = scoped(scenes, scene_table=scene_table)
    scenes = _chained(game_scenes, base_texts)
    redo = _redo(root / "lenses" / f"{lens.name}.redo.toml", scenes)
    ledger.read(root, lens.name)
    in_scope = sorted(scenes, key=_chapter_order)
    scenes = in_scope if through is None else _limited(in_scope, through)
    selecting = lens.selection == "queering"
    if lens.base is None:
        bible_ask = batch()
        built = bible.build(
            store, _chapters(in_scope if selecting else scenes), lambda parts, schema: bible_ask(claude.OPUS, parts, schema)
        )
        if not isinstance(built, list):
            out(f"bible: unfinished ({built.detail})")
            raise Fail("story bible unfinished; rerun ffrd run")
    else:
        built = bible.stored(Store(store.root.parent / lens.base), _chapters(sorted(game_scenes, key=_chapter_order)))
        if not isinstance(built, list):
            out(f"bible: {built.detail}")
            raise Fail(f"work/{lens.base}/: {built.detail}; run ffrd run --lens {lens.base} first")
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
        table_lines=table_lines,
        redo=redo,
        vanilla_hashes=vanilla_hashes,
        base_hashes={ref: vanilla_hash(text) for ref, text in base_texts.items()},
        base_rewrites=base_rewrites,
        marking=_marking(built, in_scope) if selecting else None,
    )
    progress = threading.Lock()
    waiting = sum(_holds_chapter_one(scene) for scene in scenes)
    chapter_one: list[tuple[int, Scene, _PassRecord, list[Outcome]]] = []

    def scene_pass(item: tuple[int, Scene, Ask]) -> tuple[list[Outcome], list[Change], _Recall, _PassRecord]:
        nonlocal waiting
        position, scene, ask = item
        scene_outcomes, scene_changes, scene_recall, passed = _scene(ctx, scene, ask)
        with progress:
            out(f"scene {position}/{len(scenes)} {opaque_label(scene.key)}: {_counts(scene_outcomes)}")
            if not selecting and _holds_chapter_one(scene):
                chapter_one.append((position, scene, passed, scene_outcomes))
                waiting -= 1
                if waiting == 0:
                    _write_provisional(root, ctx, chapter_one)
        return scene_outcomes, scene_changes, scene_recall, passed

    scene_items = [(position, scene, batch()) for position, scene in enumerate(scenes, start=1)]
    scene_results = claude.fan_out(scene_pass, scene_items)
    outcomes: list[Outcome] = []
    changes: list[Change] = []
    recall = _Recall()
    for scene_outcomes, scene_changes, scene_recall, _ in scene_results:
        outcomes += scene_outcomes
        changes += scene_changes
        recall.disagreements.update(scene_recall.disagreements)
        recall.confirmed.update(scene_recall.confirmed)
    passes = [passed for _, _, _, passed in scene_results]
    marks: list[Outcome] = []
    if ctx.marking is None:
        outcomes, changes = _settle(ctx, scenes, outcomes, changes, batch)
    else:
        marking = ctx.marking
        candidated = claude.fan_out(
            lambda item: _candidate_pass(ctx, marking, *item),
            [(scene, scene_outcomes, ask) for (_, scene, ask), (scene_outcomes, _, _, _) in zip(scene_items, scene_results, strict=True)],
        )
        unfinished = sum(not finished for _, finished in candidated)
        screened = [
            _Screened(position, scene, ask, tuple(scene_outcomes), passed)
            for (position, scene, ask), (scene_outcomes, _), (_, _, _, passed) in zip(scene_items, candidated, scene_results, strict=True)
        ]
        marks = [outcome for entry in screened for outcome in entry.outcomes if outcome.reasons or outcome.candidates]
        pool = _romance_pool(marking, marks)
        trans_pool = _trans_pool(marking, marks)
        eligible = _eligible(marking, scenes)
        refill = _refill(
            ctx,
            root,
            marking,
            screened,
            queering.order(pool, queering.QUEER_SALT),
            queering.target(lens.romance_share, len(pool)),
            queering.order(trans_pool, queering.TRANS_SALT),
            queering.target(lens.trans_share, len(eligible)),
            batch,
        )
        outcomes, changes = refill.outcomes, refill.changes
        _write_selection(store.root / SELECTION, lens, marks, len(eligible), len(pool), len(trans_pool), unfinished, refill)
    outcomes = [replace(o, redo=(o.namespace, o.key) in ctx.redo) for o in outcomes]
    _check_changes(changes)
    out(f"total: {_counts(outcomes)}")
    write_outcomes(store.root / OUTCOMES, outcomes)
    if selecting:
        _write_marks(store.root / MARKS, run_id, marks)
    write_scenes(store.root / SCENES, _scene_records(ctx, scenes, passes, outcomes))
    _write_arcs(store.root / ARCS, ctx.arcs, scenes, changes)
    _write_recall(store.root / RECALL, lens, recall)
    write_dialog(root / "dialog" / lens.name, changes)
    ledger.append(root, lens.name, [_ledger_entry(lens, run_id, c) for c in sorted(changes, key=_order)])
    return outcomes


def _ledger_entry(lens: Lens, run_id: str, c: Change) -> ledger.Entry:
    return ledger.Entry(
        run=run_id,
        lens=lens.name,
        base=lens.base,
        namespace=c.namespace,
        key=c.key,
        vanilla_hash=c.vanilla,
        base_hash=c.base,
        text_hash=vanilla_hash(c.text),
        reasons=c.reasons,
        writer=c.writer,
        plan=c.plan,
    )


def _batch() -> Ask:
    return stages.Batch().ask


def cmd_run(args: argparse.Namespace) -> None:
    claude.set_concurrency(args.concurrency)
    if args.account is not None and args.account not in {row.number for row in accounts.read()}:
        raise Fail(f"cswap account {args.account} is not a usable account")
    accounts.pin(args.account)
    root = repo_root()
    lens = load_lens(root / "lenses" / f"{args.lens}.toml")
    directory = work_dir(args.lens)
    bound = stages.bindings()
    scenes = bound.scenes.scenes()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with _run_lock(directory, args.lens):
        run(
            root,
            lens,
            Store(directory),
            scenes,
            classifier=bound.classifier,
            renderer=bound.renderer,
            jev=None if args.no_jev else jev_client.triage,
            run_id=run_id,
            batch=_batch,
            scene_table=bound.scenes.scene_table,
            through=args.through,
        )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "run",
        help="run a lens's scenes through the screen, the Jev recall check, drafts, gates and selection, then write its outcomes and dialog",
    )
    p.add_argument("--lens", required=True, help="name of the lens to run, read from lenses/<name>.toml and work/<name>/")
    p.add_argument("--no-jev", action="store_true", help="skip the Jev recall check")
    p.add_argument(
        "--concurrency",
        type=int,
        default=claude.DEFAULT_CONCURRENCY,
        metavar="N",
        help="Claude calls in flight at once across the bible, the scenes and the arc judges (default 8, at least 1)",
    )
    p.add_argument(
        "--account",
        type=int,
        default=None,
        metavar="N",
        help="pin every Claude call of the run to cswap account N, skipping the weekly-use filter for it (default none)",
    )
    p.add_argument(
        "--through",
        type=int,
        default=None,
        metavar="N",
        help="run only lines of chapter N or earlier, leaving out unplaced lines (default all chapters)",
    )
    p.set_defaults(fn=cmd_run)
