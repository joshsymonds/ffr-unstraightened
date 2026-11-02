"""run: take a lens's scenes through the pipeline, then write its outcome records to work/<lens>/ and its changed lines to dialog/<lens>/. Progress prints opaque scene labels and counts, and writes keys, counts and edits, never vanilla text."""

import argparse
import hashlib
import json
import os
import re
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ffrd.find import BARE_KEY, toml_string
from ffrd.game import Fail, natural, repo_root, vanilla_hash
from ffrd.pipeline import arcs, bible, claude, coherence, scene_pass, stages
from ffrd.pipeline import jev as jev_client
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Candidate, Judgment, Kept, check, gate_line, gate_mechanical
from ffrd.pipeline.lens import Lens, SceneSection, load_lens
from ffrd.pipeline.report import ARCS, OUTCOME_LABELS, OUTCOMES, RECALL, Record, render
from ffrd.pipeline.scope import scoped
from ffrd.pipeline.select import choose, distance
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
    keep_reason: str | None = None
    plan: str | None = None


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
        "keep_reason": o.keep_reason,
        "plan": o.plan,
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
    for text in texts:
        options: tuple[tuple[WriterName, str], ...] = (("opus", text), ("voice", voiced[text]))
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


def _neighbourhood(ctx: _Context, scene: Scene, line: SceneLine) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
    """The bible callbacks that touch a line and the arc context of its scene's speakers."""
    return tuple(bible.touching(ctx.bibles, line.namespace, line.key)), arcs.context(ctx.arcs, ctx.bibles, _speakers(scene), line.speaker)


def _intend(
    ctx: _Context,
    scene: Scene,
    line: SceneLine,
    reasons: tuple[str, ...],
    ask: Ask,
) -> stages.Target | Outcome:
    callbacks, context = _neighbourhood(ctx, scene, line)
    intended = stages.intent(ctx.store, ctx.lens, scene, line, reasons, callbacks, context, ask)
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
            produced = _candidates(ctx, line, _drafted_for(stages.draft(ctx.store, ctx.lens, scene, [target], retry, ask), line), origin)
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
        vanilla=vanilla_hash(line.english),
        text=text,
        lens=ctx.lens.name,
        reasons=reasons,
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


def _with_plans(refs: frozenset[tuple[str, str]], changes: Sequence[Change]) -> frozenset[tuple[str, str]]:
    """Widens refs to every changed line that shares a plan with one of them, so a plan reverts or stays unfinished as a whole."""
    plans = {change.plan for change in changes if change.plan is not None and (change.namespace, change.key) in refs}
    return refs | {(change.namespace, change.key) for change in changes if change.plan in plans}


def _coherent(ctx: _Context, scene: Scene, outcomes: list[Outcome], changes: list[Change], ask: Ask) -> tuple[list[Outcome], list[Change]]:
    if len(changes) < 2:
        return outcomes, changes
    rewrites = {(change.namespace, change.key): change.text for change in changes}
    scene_arcs = arcs.context(ctx.arcs, ctx.bibles, _speakers(scene), None)["arcs"]
    verdict = coherence.check(ctx.store, ctx.lens, scene, rewrites, scene_arcs, ask)
    if isinstance(verdict, Unfinished | Rejected):
        return [replace(o, outcome="unfinished", writer=None) if (o.namespace, o.key) in rewrites else o for o in outcomes], []
    reverted = _with_plans(verdict, changes)
    return (
        [replace(o, outcome="gate_failed", gate="scene", writer=None) if (o.namespace, o.key) in reverted else o for o in outcomes],
        [change for change in changes if (change.namespace, change.key) not in reverted],
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


def _arc_judged(
    ctx: _Context, scenes: Sequence[Scene], outcomes: list[Outcome], changes: list[Change], ask: Ask
) -> tuple[list[Outcome], list[Change]]:
    rewrites = {(change.namespace, change.key): change.text for change in changes}
    verdicts = arcs.judge(ctx.store, ctx.lens, ctx.arcs, scenes, rewrites, ask)
    planned = arcs.Verdicts(_with_plans(verdicts.reverted, changes), _with_plans(verdicts.unfinished, changes))
    dropped = planned.reverted | planned.unfinished
    return (
        [_arc_outcome(outcome, planned) for outcome in outcomes],
        [change for change in changes if (change.namespace, change.key) not in dropped],
    )


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
    target: stages.Target,
    first: tuple[str, ...] | Unfinished | Rejected,
    ask: Ask,
) -> tuple[Candidate, WriterName] | Kept | Unfinished | RateLimited:
    """Gates one planned line with the mechanical gates only, then returns its passing candidate nearest the English line with that candidate's writer."""
    line = target.line
    origin: dict[str, WriterName] = {}
    scores: dict[str, float] = {}

    def classify(text: str) -> float:
        if text not in scores:
            scores[text] = ctx.classifier.score(text)
        return scores[text]

    def write(retry: str | None) -> Sequence[Candidate] | Unfinished:
        drafted = first if retry is None else _drafted_for(stages.draft(ctx.store, ctx.lens, scene, [target], retry, ask), line)
        if isinstance(drafted, Rejected):
            return Unfinished(drafted.detail)
        return _candidates(ctx, line, drafted, origin)

    verdict = gate_mechanical(write, classify=classify, threshold=ctx.classifier.threshold)
    if isinstance(verdict, Kept | Unfinished | RateLimited):
        return verdict
    best = min(verdict, key=lambda candidate: (distance(candidate.text, line.english), candidate.text))
    return best, origin[best.text]


def _scene_pass(
    ctx: _Context,
    scene: Scene,
    section: SceneSection,
    cast: Sequence[Mapping[str, Any]],
    flagged: Mapping[tuple[str, str], tuple[str, ...]],
    outcomes: list[Outcome],
    ask: Ask,
) -> tuple[list[Outcome], list[Change]]:
    """Screens a scene for its patterns, plans line rewrites the line pass did not flag, and gates the planned lines mechanically. Returns the outcomes and only the scene pass's own changes. A failed required line blocks every scene-pass change of the scene; a failed optional line is dropped."""

    def unfinished() -> tuple[list[Outcome], list[Change]]:
        return [
            outcome if (outcome.namespace, outcome.key) in flagged else replace(outcome, outcome="unfinished") for outcome in outcomes
        ], []

    hits = scene_pass.screen(ctx.store, ctx.lens, scene, cast, ask)
    if not isinstance(hits, dict):
        return unfinished()
    if not hits:
        return outcomes, []
    plan = scene_pass.plan(ctx.store, ctx.lens, scene, cast, hits, frozenset(flagged), (), ask)
    if not isinstance(plan, scene_pass.Plan):
        return unfinished()
    if plan.keep_reason is not None:
        return outcomes, []
    lines = {(line.namespace, line.key): line for line in scene.lines}
    reasons = tuple(pattern.id for pattern in section.patterns if pattern.id in hits)
    planned: list[tuple[scene_pass.PlannedLine, stages.Target]] = []
    for entry in plan.lines:
        line = lines[(entry.namespace, entry.key)]
        callbacks, context = _neighbourhood(ctx, scene, line)
        planned.append((entry, stages.Target(line, entry.intent, callbacks, context)))
    first = _first_drafts(ctx, scene, [target for _, target in planned], ask)

    def gate(item: tuple[scene_pass.PlannedLine, stages.Target]) -> tuple[Candidate, WriterName] | Kept | Unfinished | RateLimited:
        _, target = item
        return _scene_gate(ctx, scene, target, first[(target.line.namespace, target.line.key)], ask)

    gated = claude.fan_out(gate, planned)
    settled: list[tuple[scene_pass.PlannedLine, stages.Target, tuple[Candidate, WriterName] | Kept]] = []
    for (entry, target), result in zip(planned, gated, strict=True):
        if isinstance(result, Unfinished | RateLimited):
            return unfinished()
        settled.append((entry, target, result))
    if any(isinstance(result, Kept) and entry.required for entry, _, result in settled):
        return outcomes, []
    shipped: dict[tuple[str, str], Outcome] = {}
    changes: list[Change] = []
    for _, target, result in settled:
        if isinstance(result, Kept):
            continue
        candidate, writer = result
        line = target.line
        shipped[(line.namespace, line.key)] = replace(_outcome(line, reasons, "changed", writer=writer), plan=plan.id)
        changes.append(
            Change(
                namespace=line.namespace,
                key=line.key,
                chapter=line.chapter,
                vanilla=vanilla_hash(line.english),
                text=candidate.text,
                lens=ctx.lens.name,
                reasons=reasons,
                writer=writer,
                run=ctx.run_id,
                plan=plan.id,
            )
        )
    return [shipped.get((outcome.namespace, outcome.key), outcome) for outcome in outcomes], changes


def _from_table(ctx: _Context, scene: Scene) -> bool:
    """True when every line of the scene came from the game's scene table, the only scenes the scene pass takes."""
    return all((line.namespace, line.key) in ctx.table_lines for line in scene.lines)


def _scene(ctx: _Context, scene: Scene, ask: Ask) -> tuple[list[Outcome], list[Change], _Recall]:
    cast = arcs.cast(ctx.bibles, _speakers(scene))

    def screen() -> dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected:
        return stages.screen(ctx.store, ctx.lens, scene, cast, ask)

    def triage() -> dict[str, float] | RateLimited | Unfinished | Rejected | None:
        if ctx.jev is None:
            return None
        return stages.triage(ctx.store, ctx.lens, scene, ctx.jev)

    flagged, probabilities = _both(screen, triage)
    if not isinstance(flagged, dict):
        return _every_line(scene, "unfinished"), [], _Recall()
    recall = _Recall()
    if ctx.jev is not None:
        if not isinstance(probabilities, dict):
            return _every_line(scene, "unfinished"), [], _Recall()
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
                return _every_line(scene, "unfinished"), [], _Recall()
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
        return _line(ctx, scene, plan, reasons, first[ref], ask)

    gated = claude.fan_out(gate, scene.lines)
    outcomes = [outcome for outcome, _ in gated]
    changes = [change for _, change in gated if change is not None]
    section = ctx.lens.scene
    if section is not None and _from_table(ctx, scene):
        outcomes, scene_changes = _scene_pass(ctx, scene, section, cast, flagged, outcomes, ask)
        changes += scene_changes
    outcomes, changes = _coherent(ctx, scene, outcomes, changes, ask)
    return outcomes, changes, recall


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


def _write_provisional(root: Path, lens: Lens, outcomes: Sequence[Outcome]) -> None:
    """Writes the counts of Chapter 1's outcomes in the report format, before the arc judge."""
    records = [_record(o) for o in outcomes if o.chapter == 1]
    lines = render(records, [question.id for question in lens.triage])
    _write_atomic(root / "reports" / f"{lens.name}-c01.txt", "".join(f"{line}\n" for line in lines))


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
    through: int | None = None,
    out: Callable[[str], None] = print,
) -> list[Outcome]:
    if through is not None and through < 1:
        raise Fail("through must be at least 1")
    table_lines = frozenset((line.namespace, line.key) for scene in scenes if scene.table for line in scene.lines)
    scenes = scoped(scenes, scene_table=scene_table)
    if through is not None:
        scenes = _limited(scenes, through)
    scenes = sorted(scenes, key=_chapter_order)
    bible_ask = batch()
    built = bible.build(store, _chapters(scenes), lambda parts, schema: bible_ask(claude.OPUS, parts, schema))
    if not isinstance(built, list):
        out(f"bible: unfinished ({built.detail})")
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
        table_lines=table_lines,
    )
    progress = threading.Lock()
    waiting = sum(_holds_chapter_one(scene) for scene in scenes)
    chapter_one: list[Outcome] = []

    def scene_pass(item: tuple[int, Scene, Ask]) -> tuple[list[Outcome], list[Change], _Recall]:
        nonlocal waiting
        position, scene, ask = item
        scene_outcomes, scene_changes, scene_recall = _scene(ctx, scene, ask)
        with progress:
            out(f"scene {position}/{len(scenes)} {opaque_label(scene.key)}: {_counts(scene_outcomes)}")
            if _holds_chapter_one(scene):
                chapter_one.extend(scene_outcomes)
                waiting -= 1
                if waiting == 0:
                    _write_provisional(root, lens, chapter_one)
        return scene_outcomes, scene_changes, scene_recall

    scene_results = claude.fan_out(scene_pass, [(position, scene, batch()) for position, scene in enumerate(scenes, start=1)])
    outcomes: list[Outcome] = []
    changes: list[Change] = []
    recall = _Recall()
    for scene_outcomes, scene_changes, scene_recall in scene_results:
        outcomes += scene_outcomes
        changes += scene_changes
        recall.disagreements.update(scene_recall.disagreements)
        recall.confirmed.update(scene_recall.confirmed)
    outcomes, changes = _arc_judged(ctx, scenes, outcomes, changes, batch())
    _check_changes(changes)
    out(f"total: {_counts(outcomes)}")
    write_outcomes(store.root / OUTCOMES, outcomes)
    _write_arcs(store.root / ARCS, ctx.arcs, scenes, changes)
    _write_recall(store.root / RECALL, lens, recall)
    write_dialog(root / "dialog" / lens.name, changes)
    return outcomes


def _batch() -> Ask:
    return stages.Batch().ask


def cmd_run(args: argparse.Namespace) -> None:
    claude.set_concurrency(args.concurrency)
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
        "--through",
        type=int,
        default=None,
        metavar="N",
        help="run only lines of chapter N or earlier, leaving out unplaced lines (default all chapters)",
    )
    p.set_defaults(fn=cmd_run)
