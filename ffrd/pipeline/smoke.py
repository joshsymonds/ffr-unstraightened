"""smoke: run one in-scope scene through every pipeline stage under work/smoke/, call each data-conditional stage the run left without a result once, print the DL38 prefix report, each required stage's result count and the scene stage counts, and fail when a required stage produced no result. Prints opaque scene labels, prefix and stage labels and counts, never scene keys or text."""

import argparse
import re
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ffrd.game import Fail
from ffrd.pipeline import arcs, bible, claude, coherence, scene_pass, stages
from ffrd.pipeline import jev as jev_client
from ffrd.pipeline.lens import Lens, ScenePattern, SceneSection, TriageQuestion
from ffrd.pipeline.run import Outcome, _chapters, opaque_label, run
from ffrd.pipeline.scope import in_scope, scoped
from ffrd.pipeline.stages import Ask, Classifier, Scene
from ffrd.pipeline.store import Result, Store, work_dir

# Made-up lens: its questions, criteria and scene text hold no game text. Screen flags the first two spoken lines with a reason a rewrite can fix; the scene pass looks for one made-up pattern.
SMOKE = Lens(
    name="smoke",
    triage=(
        TriageQuestion(id="smoke-fixable", question="Could a reworded version of this line fix it while keeping its meaning?"),
        TriageQuestion(id="smoke-second", question="Does this line name a place?"),
    ),
    screen_criteria=("Flag the first two lines of the scene that have a speaker, with the reason smoke-fixable.",),
    intent_rule="Say in one sentence what the rewritten line must do for the player in this scene.",
    judge_rubric="The candidate keeps the meaning, the plot facts and the speaker's character of the target line, and changes no more than the problem needs.",
    scene=SceneSection(
        patterns=(ScenePattern(id="smoke-scene-hit", question="Does the scene carry a made-up smoke pattern on some of its lines?"),),
        criteria=("Name the smoke-scene-hit pattern and the lines that carry it.",),
        plan="Keep the scene for a listed reason, or plan at most four line rewrites that remove the pattern.",
        rubric="The candidate keeps the meaning of the target line and the scene around it.",
    ),
)

CLASSIFIER = "classifier"
SELECTION = "selection"
EXERCISED = "(exercised)"
# Stages whose run reaches depends on model answers, so smoke calls one itself when the run left it at zero.
EXERCISABLE: tuple[str, ...] = ("rescreen", CLASSIFIER, "judge", "cross_judge", coherence.STAGE, arcs.STAGE)
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
# Printed after the required stages. Only the screen is required, and only when the picked scene came from the scene table.
SCENE_STAGES: tuple[str, ...] = (scene_pass.SCREEN_STAGE, scene_pass.PLAN_STAGE, scene_pass.JUDGE_STAGE)

# Guards the shared counts and returned lists: stage calls run concurrently, and each update is a read-modify-write.
_COUNTS_LOCK = threading.Lock()

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
    """The run's store, counting each stage once its result is returned, whether computed now or reused from an earlier run, and keeping each returned result's inputs and value."""

    def __init__(self, root: Path, counts: Counter[str]) -> None:
        super().__init__(root)
        self._counts = counts
        self._returned: dict[str, list[tuple[Any, Any]]] = {}

    def run(self, stage: str, inputs: Any, lens: str | None, call: Callable[[], Any]) -> Result:
        result = super().run(stage, inputs, lens, call)
        with _COUNTS_LOCK:
            self._counts[result.stage] += 1
            self._returned.setdefault(result.stage, []).append((inputs, result.value))
        return result

    def returned(self, stage: str) -> list[tuple[Any, Any]]:
        """The (inputs, value) pairs of the stage's returned results, in the order the run returned them."""
        return self._returned.get(stage, [])


class _CountingClassifier:
    """The bound classifier, counting each score it returns."""

    def __init__(self, inner: Classifier, counts: Counter[str]) -> None:
        self._inner = inner
        self._counts = counts
        self.threshold: float = inner.threshold

    def score(self, text: str) -> float:
        value = self._inner.score(text)
        with _COUNTS_LOCK:
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
        raise Fail(f"no in-scope scene has the key {opaque_label(key)}")
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


def _bibles(root: Path, scene: Scene, ask: Ask) -> list[dict[str, Any]]:
    # A plain store: the run already stored these bibles, so reading them back counts no stage a second time.
    built = bible.build(Store(root), _chapters([scene]), lambda parts, schema: ask(claude.OPUS, parts, schema))
    if not isinstance(built, list):
        raise Fail("story bible unfinished; rerun ffrd smoke")
    return built


def _rewrites(store: _CountingStore) -> dict[tuple[str, str], str]:
    """The first draft of each drafted line, from the first draft result the run returned."""
    returned = store.returned("draft")
    if not returned:
        return {}
    _, entries = returned[0]
    return {(entry["namespace"], entry["key"]): entry["drafts"][0] for entry in entries}


def _intent(store: _CountingStore, ref: tuple[str, str]) -> tuple[dict[str, Any], str] | None:
    """The inputs and value of the intent the run returned for the line, if it returned one."""
    for inputs, intent in store.returned("intent"):
        if (inputs["line"]["namespace"], inputs["line"]["key"]) == ref:
            return inputs, intent["text"]
    return None


def _exercise(store: _CountingStore, classifier: _CountingClassifier, counts: Counter[str], scene: Scene) -> set[str]:
    """Calls each exercisable stage the run left at zero once, from the inputs and values the run returned. Changes no outcome; returns the stages a call produced a result for."""
    pending = {stage for stage in EXERCISABLE if counts[stage] == 0}
    if not pending:
        return set()
    ask = _batch()
    bibles = _bibles(store.root, scene, ask)
    speakers = {line.speaker for line in scene.lines}
    if counts["rescreen"] == 0:
        stages.rescreen(store, SMOKE, scene, SMOKE.triage[0], arcs.cast(bibles, speakers), ask)
    rewrites = _rewrites(store)
    if rewrites:
        ref = next(iter(rewrites))
        text = rewrites[ref]
        if counts[CLASSIFIER] == 0:
            classifier.score(text)
        intended = _intent(store, ref)
        if intended is not None:
            inputs, intent = intended
            line = next(line for line in scene.lines if (line.namespace, line.key) == ref)
            if counts["judge"] == 0:
                stages.judge(store, SMOKE, scene, line, text, inputs["callbacks"], inputs["reasons"], intent, inputs["context"], ask)
            if counts["cross_judge"] == 0:
                related = [other for other in scene.lines if (other.namespace, other.key) != ref]
                stages.cross_judge(
                    store, SMOKE, line, text, inputs["callbacks"], related, inputs["reasons"], intent, inputs["context"], ask
                )
        if counts[coherence.STAGE] == 0:
            scene_arcs = arcs.context(arcs.consolidate(bibles), bibles, speakers, None)["arcs"]
            coherence.check(store, SMOKE, scene, rewrites, scene_arcs, ask)
        if counts[arcs.STAGE] == 0:
            arcs.judge(store, SMOKE, arcs.consolidate(bibles), [scene], rewrites, ask)
    return {stage for stage in pending if counts[stage] > 0}


def cmd_smoke(args: argparse.Namespace) -> None:
    bound = stages.bindings()
    scenes = bound.scenes.scenes()
    _prefix_report(scenes)
    picked = _pick(scoped(scenes, scene_table=bound.scenes.scene_table), args.scene)
    # scoped() rebuilds each scene without its table flag, which the run needs to decide the scene pass.
    picked = replace(picked, table=next(scene.table for scene in scenes if scene.key == picked.key))
    root = work_dir(SMOKE.name)
    counts: Counter[str] = Counter()
    store = _CountingStore(root, counts)
    classifier = _CountingClassifier(bound.classifier, counts)
    outcomes = run(
        root,
        SMOKE,
        store,
        [picked],
        classifier=classifier,
        renderer=bound.renderer,
        jev=jev_client.triage,
        run_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        batch=_batch,
        scene_table=bound.scenes.scene_table,
        out=print,
    )
    counts[SELECTION] = _selected(outcomes, picked)
    exercised = _exercise(store, classifier, counts, picked)
    missing: list[str] = []
    for stage in REQUIRED_STAGES:
        mark = f" {EXERCISED}" if stage in exercised else ""
        print(f"stage {stage}: {counts[stage]}{mark}")
        if counts[stage] == 0:
            missing.append(stage)
    for stage in SCENE_STAGES:
        print(f"stage {stage}: {counts[stage]}")
    if picked.table and counts[scene_pass.SCREEN_STAGE] == 0:
        missing.append(scene_pass.SCREEN_STAGE)
    if missing:
        raise Fail(f"smoke: no result from {', '.join(missing)}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "smoke",
        help="run one scene through every stage under work/smoke/ and fail when a stage produced no result",
    )
    p.add_argument("--scene", help="key of the in-scope scene to run; by default the first scene with two lines that have a speaker")
    p.set_defaults(fn=cmd_smoke)
