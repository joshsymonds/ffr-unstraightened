"""run: take a lens's scenes through the pipeline, then write its outcome records to work/<lens>/ and its changed lines to dialog/<lens>/. Progress prints scene keys and counts, and writes keys, counts and edits, never vanilla text."""

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ffrd.find import BARE_KEY, toml_string
from ffrd.game import Fail, natural, repo_root, vanilla_hash
from ffrd.pipeline import bible, claude, stages
from ffrd.pipeline import jev as jev_client
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Candidate, Judgment, Kept, gate_line
from ffrd.pipeline.lens import Lens, load_lens
from ffrd.pipeline.report import OUTCOME_LABELS, OUTCOMES
from ffrd.pipeline.scope import scoped
from ffrd.pipeline.select import choose
from ffrd.pipeline.stages import Ask, Classifier, Renderer, Scene, SceneLine, Triage
from ffrd.pipeline.store import Store, work_dir
from ffrd.pipeline.tune import load_threshold

CHAPTER_FILE = re.compile(r"chapter-\d{2,}\.toml")
UNATTRIBUTED_FILE = "unattributed.toml"

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
    threshold: float | None
    run_id: str
    jev: Triage
    bibles: Sequence[Mapping[str, Any]]
    lines: Mapping[tuple[str, str], SceneLine]


def _chapters(scenes: Sequence[Scene]) -> list[tuple[int, list[bible.Line]]]:
    by_chapter: dict[int, dict[tuple[str, str], bible.Line]] = {}
    for scene in scenes:
        for line in scene.lines:
            if line.chapter is not None:
                seen = by_chapter.setdefault(line.chapter, {})
                seen.setdefault((line.namespace, line.key), bible.Line(line.namespace, line.key, line.speaker or "", line.english))
    return [(number, list(seen.values())) for number, seen in sorted(by_chapter.items())]


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
    scene: Scene,
    line: SceneLine,
    intended: str,
    callbacks: Sequence[Mapping[str, Any]],
    retry: str | None,
    ask: Ask,
    origin: dict[str, WriterName],
) -> Sequence[Candidate] | Unfinished:
    drafted = stages.draft(ctx.store, ctx.lens, scene, line, intended, callbacks, retry, ask)
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
    ask: Ask,
) -> Judgment | Unfinished | Rejected:
    verdict = stages.judge(ctx.store, ctx.lens, scene, line, candidate.text, callbacks, ask)
    if not isinstance(verdict, Judgment) or not callbacks:
        return verdict
    if not (verdict.meaning and verdict.plot and verdict.minimal_change and verdict.callback):
        return verdict
    consistent = stages.cross_judge(ctx.store, ctx.lens, line, candidate.text, callbacks, _related(ctx, line, callbacks), ask)
    if isinstance(consistent, Unfinished | Rejected):
        return consistent
    return verdict if consistent else replace(verdict, callback=False)


def _line(
    ctx: _Context,
    scene: Scene,
    line: SceneLine,
    reasons: tuple[str, ...],
    ask: Ask,
) -> tuple[Outcome, Change | None]:
    callbacks = bible.touching(ctx.bibles, line.namespace, line.key)
    intended = stages.intent(ctx.store, ctx.lens, scene, line, reasons, callbacks, ask)
    if not isinstance(intended, str):
        return _outcome(line, reasons, "unfinished"), None
    origin: dict[str, WriterName] = {}

    def write(retry: str | None) -> Sequence[Candidate] | Unfinished:
        return _candidates(ctx, scene, line, intended, callbacks, retry, ask, origin)

    def judge_candidate(candidate: Candidate) -> Judgment | Unfinished | Rejected:
        return _judge(ctx, scene, line, callbacks, candidate, ask)

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


def _scene(ctx: _Context, scene: Scene, ask: Ask) -> tuple[list[Outcome], list[Change]]:
    if ctx.threshold is not None:
        answer = stages.triage(ctx.store, ctx.lens, scene, ctx.jev)
        if not isinstance(answer, dict):
            return _every_line(scene, "unfinished"), []
        if max(answer[question.id] for question in ctx.lens.triage) < ctx.threshold:
            return _every_line(scene, "untouched"), []
    flagged = stages.screen(ctx.store, ctx.lens, scene, ask)
    if not isinstance(flagged, dict):
        return _every_line(scene, "unfinished"), []
    outcomes: list[Outcome] = []
    changes: list[Change] = []
    for line in scene.lines:
        reasons = flagged.get((line.namespace, line.key))
        if reasons is None:
            outcomes.append(_outcome(line, (), "untouched"))
            continue
        outcome, change = _line(ctx, scene, line, reasons, ask)
        outcomes.append(outcome)
        if change is not None:
            changes.append(change)
    return outcomes, changes


def run(
    root: Path,
    lens: Lens,
    store: Store,
    scenes: Sequence[Scene],
    *,
    classifier: Classifier,
    renderer: Renderer,
    threshold: float | None,
    run_id: str,
    jev: Triage,
    batch: Callable[[], Ask],
    out: Callable[[str], None] = print,
) -> list[Outcome]:
    scenes = scoped(scenes)
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
        threshold=threshold,
        run_id=run_id,
        jev=jev,
        bibles=built,
        lines=_first_lines(scenes),
    )
    outcomes: list[Outcome] = []
    changes: list[Change] = []
    for scene in scenes:
        scene_outcomes, scene_changes = _scene(ctx, scene, batch())
        outcomes += scene_outcomes
        changes += scene_changes
        out(f"scene {scene.key}: {_counts(scene_outcomes)}")
    out(f"total: {_counts(outcomes)}")
    write_outcomes(store.root / OUTCOMES, outcomes)
    write_dialog(root / "dialog" / lens.name, changes)
    return outcomes


def _batch() -> Ask:
    return stages.Batch().ask


def cmd_run(args: argparse.Namespace) -> None:
    root = repo_root()
    lens = load_lens(root / "lenses" / f"{args.lens}.toml")
    directory = work_dir(args.lens)
    threshold = None if args.no_triage else load_threshold(directory)
    bound = stages.bindings()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run(
        root,
        lens,
        Store(directory),
        bound.scenes.scenes(),
        classifier=bound.classifier,
        renderer=bound.renderer,
        threshold=threshold,
        run_id=run_id,
        jev=jev_client.triage,
        batch=_batch,
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "run", help="run a lens's scenes through triage, screen, drafts, gates and selection, then write its outcomes and dialog"
    )
    p.add_argument("--lens", required=True, help="name of the lens to run, read from lenses/<name>.toml and work/<name>/")
    p.add_argument("--no-triage", action="store_true", help="screen every scene without the Jev triage threshold")
    p.set_defaults(fn=cmd_run)
