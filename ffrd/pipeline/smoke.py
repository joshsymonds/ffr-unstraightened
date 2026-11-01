"""smoke: run one in-scope scene through every pipeline stage under work/smoke/, print the DL38 prefix report and each required stage's result count, and fail when a required stage produced no result. Prints keys, labels and counts, never scene text."""

import argparse
import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ffrd.game import Fail
from ffrd.pipeline import arcs, bible, coherence, stages
from ffrd.pipeline import jev as jev_client
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.run import Outcome, run
from ffrd.pipeline.scope import in_scope, scoped
from ffrd.pipeline.stages import Ask, Classifier, Scene
from ffrd.pipeline.store import Result, Store, work_dir

# Made-up lens: its questions and criteria hold no game text. Screen flags the first two spoken lines with a reason a rewrite can fix.
SMOKE = Lens(
    name="smoke",
    triage=(
        TriageQuestion(id="smoke-fixable", question="Could a reworded version of this line fix it while keeping its meaning?"),
        TriageQuestion(id="smoke-second", question="Does this line name a place?"),
    ),
    screen_criteria=("Flag the first two lines of the scene that have a speaker, with the reason smoke-fixable.",),
    intent_rule="Say in one sentence what the rewritten line must do for the player in this scene.",
    judge_rubric="The candidate keeps the meaning, the plot facts and the speaker's character of the target line, and changes no more than the problem needs.",
)

CLASSIFIER = "classifier"
SELECTION = "selection"
REQUIRED_STAGES: tuple[str, ...] = (
    bible.STAGE,
    bible.SETUP_STAGE,
    "screen",
    "triage",
    "rescreen",
    "intent",
    "draft",
    "render",
    CLASSIFIER,
    "judge",
    "cross_judge",
    coherence.STAGE,
    arcs.STAGE,
    SELECTION,
)

OUTSIDE_FALLBACK = "outside fallback"
HARD_EXCLUDED = "hard excluded"
# The DL38 fallback families, as scope.py matches them.
_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ST_C<n>_", re.compile(r"ST_C[0-9]+_")),
    ("ST_SubEv", re.compile(r"ST_SubEv")),
    ("ST_ChrEv", re.compile(r"ST_ChrEv")),
    ("ST_FEV", re.compile(r"ST_FEV")),
    ("ST_LegendEV", re.compile(r"ST_LegendEV")),
    ("ST_GimEv", re.compile(r"ST_GimEv")),
    ("ST_BtlEv", re.compile(r"ST_BtlEv")),
    ("ST_EV", re.compile(r"ST_EV")),
    ("ST_DialogChoice", re.compile(r"ST_DialogChoice(?:_|\Z)")),
    ("ST_MainSummary", re.compile(r"ST_MainSummary(?:_|\Z)")),
    ("ST_UnitDescription", re.compile(r"ST_UnitDescription(?:_|\Z)")),
)
PREFIX_LABELS: tuple[str, ...] = (*(label for label, _ in _FAMILIES), OUTSIDE_FALLBACK, HARD_EXCLUDED)


class _CountingStore(Store):
    """The run's store, counting each stage once its result is returned, whether computed now or reused from an earlier run."""

    def __init__(self, root: Path, counts: Counter[str]) -> None:
        super().__init__(root)
        self._counts = counts

    def run(self, stage: str, inputs: Any, lens: str | None, call: Callable[[], Any]) -> Result:
        result = super().run(stage, inputs, lens, call)
        self._counts[result.stage] += 1
        return result


class _CountingClassifier:
    """The bound classifier, counting each score it returns."""

    def __init__(self, inner: Classifier, counts: Counter[str]) -> None:
        self._inner = inner
        self._counts = counts
        self.threshold: float = inner.threshold

    def score(self, text: str) -> float:
        value = self._inner.score(text)
        self._counts[CLASSIFIER] += 1
        return value


def _prefix_label(namespace: str) -> str:
    if not in_scope(namespace, scene_table=True):
        return HARD_EXCLUDED
    for label, pattern in _FAMILIES:
        if pattern.match(namespace) is not None:
            return label
    return OUTSIDE_FALLBACK


def _prefix_report(scenes: Sequence[Scene]) -> None:
    distinct = {(line.namespace, line.key) for scene in scenes for line in scene.lines}
    counts = Counter(_prefix_label(namespace) for namespace, _ in distinct)
    for label in PREFIX_LABELS:
        print(f"prefix {label}: {counts[label]}")


def _pick(scenes: Sequence[Scene], key: str | None) -> Scene:
    if key is not None:
        for scene in scenes:
            if scene.key == key:
                return scene
        raise Fail(f"no in-scope scene has the key {key!r}")
    for scene in scenes:
        if sum(1 for line in scene.lines if line.speaker) >= 2:
            return scene
    raise Fail("no in-scope scene has two lines with a speaker")


def _selected(outcomes: Sequence[Outcome], scene: Scene) -> int:
    if len(outcomes) == len(scene.lines) and all(outcome.outcome != "unfinished" for outcome in outcomes):
        return len(outcomes)
    return 0


def _batch() -> Ask:
    return stages.Batch().ask


def cmd_smoke(args: argparse.Namespace) -> None:
    bound = stages.bindings()
    scenes = bound.scenes.scenes()
    _prefix_report(scenes)
    picked = _pick(scoped(scenes, scene_table=bound.scenes.scene_table), args.scene)
    root = work_dir(SMOKE.name)
    counts: Counter[str] = Counter()
    outcomes = run(
        root,
        SMOKE,
        _CountingStore(root, counts),
        [picked],
        classifier=_CountingClassifier(bound.classifier, counts),
        renderer=bound.renderer,
        jev=jev_client.triage,
        run_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        batch=_batch,
        scene_table=bound.scenes.scene_table,
        out=print,
    )
    counts[SELECTION] = _selected(outcomes, picked)
    missing: list[str] = []
    for stage in REQUIRED_STAGES:
        print(f"stage {stage}: {counts[stage]}")
        if counts[stage] == 0:
            missing.append(stage)
    if missing:
        raise Fail(f"smoke: no result from {', '.join(missing)}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "smoke",
        help="run one scene through every stage under work/smoke/ and fail when a stage produced no result",
    )
    p.add_argument("--scene", help="key of the in-scope scene to run; by default the first scene with two lines that have a speaker")
    p.set_defaults(fn=cmd_smoke)
