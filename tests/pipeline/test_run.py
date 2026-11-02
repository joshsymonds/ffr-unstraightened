import argparse
import json
import re
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest

from ffrd.game import Fail, vanilla_hash
from ffrd.pipeline import arcs, bible, coherence, queering, report, scene_pass, scene_record, stages
from ffrd.pipeline import claude as claude_module
from ffrd.pipeline import jev as jev_module
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, ScenePattern, SceneSection, TriageQuestion
from ffrd.pipeline.run import (
    Change,
    Outcome,
    _arc_judged,
    _coherent,
    _Context,
    _record,
    cmd_run,
    register,
    run,
    write_dialog,
    write_outcomes,
)
from ffrd.pipeline.stages import (
    CROSS_SCHEMA,
    DRAFT_SCHEMA,
    INTENT_SCHEMA,
    JUDGE_SCHEMA,
    RESCREEN_SCHEMA,
    SCREEN_SCHEMA,
    Ask,
    Bindings,
    Scene,
    SceneLine,
)
from ffrd.pipeline.store import Store

LENS = "made-up-lens"
RUN = "made-up-run-id"
TRIAGE_ORDER = ["made-up-first-id", "made-up-second-id"]
TRIAGE = frozenset(TRIAGE_ORDER)
OUTCOME_KEYS = {"namespace", "key", "chapter", "speaker", "reasons", "outcome", "gate", "writer", "keep_reason", "plan", "redo"}
FIELD_ORDER = ["vanilla", "text", "lens", "reason", "writer", "verdict", "run"]
PLAN_ID = "made-up-plan-one"

# Made-up lines. Not game text.
EN_ONE = "Made-up English one. Not game text."
EN_TWO = "Made-up English two. Not game text."
TEXT_ONE = "Made-up rewrite one. Not game text."
TEXT_TWO = "Made-up rewrite two. Not game text."
TEXT_TRICKY = 'Made-up "quoted" \\ line.\nSecond\tline. Made-up ünïcode. Not game text.'
EN_TRICKY = "Made-up English tricky. Not game text."

OUTCOMES = [
    Outcome("made-up-scene-a", "1", 1, "made-up-speaker-2", ("made-up-first-id",), "changed", None, "voice"),
    Outcome("made-up-scene-a", "2", 1, "made-up-speaker-10", ("made-up-first-id", "made-up-second-id"), "changed", None, "opus"),
    Outcome("made-up-scene-a", "3", 1, "made-up-speaker-2", ("made-up-second-id",), "kept_for_plot", None, None),
    Outcome("made-up-scene-a", "4", 1, "made-up-speaker-2", ("made-up-first-id",), "gate_failed", "made-up-gate", "voice"),
    Outcome("made-up-scene-b", "5", None, None, (), "untouched", None, None),
    Outcome("made-up-scene-b", "6", 2, "made-up-speaker-10", ("made-up-second-id",), "unfinished", None, None),
]

EXPECTED_SUMMARY = """\
chapter 1: 2 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 0 untouched
  reason made-up-first-id: 2
  reason made-up-second-id: 1
  character 1: 1
  character 2: 1
  gate made-up-gate: 1
chapter 2: 0 changed, 0 kept for plot, 0 gate failed, 1 unfinished, 0 untouched
unattributed: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched
total: 2 changed, 1 kept for plot, 1 gate failed, 1 unfinished, 1 untouched
  reason made-up-first-id: 2
  reason made-up-second-id: 1
  character 1: 1
  character 2: 1
  gate made-up-gate: 1
"""


def _change(
    namespace: str = "made-up-scene-a",
    key: str = "1",
    chapter: int | None = 1,
    vanilla: str | None = None,
    text: str = TEXT_ONE,
    reasons: tuple[str, ...] = ("made-up-first-id",),
    writer: Literal["voice", "opus"] = "voice",
    plan: str | None = None,
) -> Change:
    # Made-up change. Not game text.
    return Change(
        namespace=namespace,
        key=key,
        chapter=chapter,
        vanilla=vanilla_hash(EN_ONE) if vanilla is None else vanilla,
        text=text,
        lens=LENS,
        reasons=reasons,
        writer=writer,
        run=RUN,
        plan=plan,
    )


def _dialog_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_outcomes_parse_through_report_records_and_render_counts(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    write_outcomes(path, OUTCOMES)
    records = report._records(path, TRIAGE)
    assert [r.outcome for r in records] == [o.outcome for o in OUTCOMES]
    assert "\n".join(report.render(records, TRIAGE_ORDER)) + "\n" == EXPECTED_SUMMARY


def test_each_outcome_object_has_exactly_the_dl31_keys_and_no_text(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    write_outcomes(path, OUTCOMES)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(OUTCOMES)
    for line, outcome in zip(lines, OUTCOMES, strict=True):
        obj = json.loads(line)
        assert set(obj) == OUTCOME_KEYS
        assert obj["key"] == outcome.key
        assert obj["reasons"] == list(outcome.reasons)
        assert obj["writer"] == outcome.writer
        assert obj["gate"] == outcome.gate


def test_outcome_records_carry_keep_reason_only_for_a_line_the_intent_kept(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    kept = Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "untouched", None, None, keep_reason="earned")
    write_outcomes(path, [kept, OUTCOMES[0]])
    kept_record, other_record = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert kept_record["keep_reason"] == "earned"
    assert other_record["keep_reason"] is None


def test_outcome_with_a_plan_id_writes_it_and_a_line_pass_outcome_writes_null(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    planned = Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID)
    write_outcomes(path, [planned, OUTCOMES[0]])
    planned_record, line_pass_record = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert planned_record["plan"] == PLAN_ID
    assert line_pass_record["plan"] is None


def test_outcome_plan_reaches_the_report_record_and_the_run_record(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    planned = Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID)
    write_outcomes(path, [planned, OUTCOMES[0]])
    planned_record, line_pass_record = report._records(path, TRIAGE)
    assert planned_record.plan == PLAN_ID
    assert line_pass_record.plan is None
    assert _record(planned).plan == PLAN_ID


def test_outcomes_are_written_in_the_given_order_and_no_temp_file_remains(tmp_path: Path) -> None:
    parent = tmp_path / "work" / LENS
    path = parent / "outcomes.jsonl"
    write_outcomes(path, list(reversed(OUTCOMES)))
    keys = [json.loads(line)["key"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert keys == [o.key for o in reversed(OUTCOMES)]
    assert _dialog_files(parent) == ["outcomes.jsonl"]


def test_outcomes_replace_an_earlier_file(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    write_outcomes(path, OUTCOMES)
    write_outcomes(path, OUTCOMES[:1])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_outcomes_oserror_fails_naming_the_path_and_reason(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    path = blocker / "work" / LENS / "outcomes.jsonl"
    with pytest.raises(Fail) as raised:
        write_outcomes(path, OUTCOMES)
    assert str(path) in str(raised.value)
    assert "Not a directory" in str(raised.value)


def test_dialog_entries_round_trip_through_tomllib(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(
        directory,
        [
            _change(namespace="made-up-scene-a", key="2", reasons=("made-up-second-id",), writer="opus"),
            _change(namespace="made-up-scene-b", key='made-up "quoted" key', chapter=12, vanilla=vanilla_hash(EN_TRICKY), text=TEXT_TRICKY),
        ],
    )
    first = tomllib.loads((directory / "chapter-01.toml").read_text(encoding="utf-8"))
    assert list(first["made-up-scene-a"]["2"]) == FIELD_ORDER
    assert first["made-up-scene-a"]["2"] == {
        "vanilla": vanilla_hash(EN_ONE),
        "text": TEXT_ONE,
        "lens": LENS,
        "reason": ["made-up-second-id"],
        "writer": "opus",
        "verdict": "passed",
        "run": RUN,
    }
    twelve = tomllib.loads((directory / "chapter-12.toml").read_text(encoding="utf-8"))
    assert list(twelve["made-up-scene-b"]['made-up "quoted" key']) == FIELD_ORDER
    assert twelve["made-up-scene-b"]['made-up "quoted" key'] == {
        "vanilla": vanilla_hash(EN_TRICKY),
        "text": TEXT_TRICKY,
        "lens": LENS,
        "reason": ["made-up-first-id"],
        "writer": "voice",
        "verdict": "passed",
        "run": RUN,
    }


def test_dialog_never_contains_the_vanilla_line(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(directory, [_change(vanilla=vanilla_hash(EN_ONE), text=TEXT_ONE)])
    content = (directory / "chapter-01.toml").read_text(encoding="utf-8")
    assert EN_ONE not in content
    assert TEXT_ONE in content


def test_change_plan_leaves_the_dialog_toml_byte_identical(tmp_path: Path) -> None:
    without_plan = tmp_path / "without"
    with_plan = tmp_path / "with"
    write_dialog(without_plan, [_change()])
    write_dialog(with_plan, [_change(plan=PLAN_ID)])
    assert (with_plan / "chapter-01.toml").read_bytes() == (without_plan / "chapter-01.toml").read_bytes()
    assert "plan" not in (with_plan / "chapter-01.toml").read_text(encoding="utf-8")


def test_chapter_and_unattributed_placement_and_table_order(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(
        directory,
        [
            _change(namespace="made-up-scene-10", key="2"),
            _change(namespace="made-up-scene-2", key="10"),
            _change(namespace="made-up-scene-2", key="2"),
            _change(namespace="made-up-scene-3", key='made-up "quoted" key', chapter=12),
            _change(namespace="made-up-scene-4", key="1", chapter=None, reasons=("made-up-second-id",), writer="opus"),
        ],
    )
    assert _dialog_files(directory) == ["chapter-01.toml", "chapter-12.toml", "unattributed.toml"]
    first = tomllib.loads((directory / "chapter-01.toml").read_text(encoding="utf-8"))
    assert list(first) == ["made-up-scene-2", "made-up-scene-10"]
    assert list(first["made-up-scene-2"]) == ["2", "10"]
    twelve = tomllib.loads((directory / "chapter-12.toml").read_text(encoding="utf-8"))
    assert list(twelve) == ["made-up-scene-3"]
    unattributed = tomllib.loads((directory / "unattributed.toml").read_text(encoding="utf-8"))
    assert list(unattributed) == ["made-up-scene-4"]
    assert unattributed["made-up-scene-4"]["1"]["writer"] == "opus"


def test_creates_the_directory_even_with_no_changes(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(directory, [])
    assert directory.is_dir()
    assert _dialog_files(directory) == []


def test_stale_chapter_and_unattributed_files_are_removed_and_others_kept(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    directory.mkdir(parents=True)
    # Made-up stale files. Not game text.
    (directory / "chapter-01.toml").write_text("# Made-up old chapter one. Not game text.\n")
    (directory / "chapter-03.toml").write_text("# Made-up old chapter three. Not game text.\n")
    (directory / "unattributed.toml").write_text("# Made-up old unattributed. Not game text.\n")
    (directory / "chapter-3.toml").write_text("# Made-up other shape. Not game text.\n")
    (directory / "notes.toml").write_text("# Made-up notes. Not game text.\n")
    (directory / "README.md").write_text("Made-up readme. Not game text.\n")

    write_dialog(directory, [_change(chapter=1)])

    assert _dialog_files(directory) == ["README.md", "chapter-01.toml", "chapter-3.toml", "notes.toml"]
    assert (directory / "chapter-3.toml").read_text() == "# Made-up other shape. Not game text.\n"
    assert (directory / "notes.toml").read_text() == "# Made-up notes. Not game text.\n"
    assert (directory / "README.md").read_text() == "Made-up readme. Not game text.\n"
    assert TEXT_ONE in (directory / "chapter-01.toml").read_text(encoding="utf-8")


def test_unedited_change_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    directory.mkdir(parents=True)
    # Made-up stale file. Not game text.
    (directory / "chapter-01.toml").write_text("# Made-up old chapter one. Not game text.\n")
    changes = [
        _change(namespace="made-up-scene-a", key="1", chapter=1),
        _change(namespace="made-up-scene-z", key="9", chapter=2, vanilla=vanilla_hash(EN_TWO), text=EN_TWO),
    ]
    with pytest.raises(Fail) as raised:
        write_dialog(directory, changes)
    message = str(raised.value)
    assert message == "line ec54f750: text is unchanged from vanilla, nothing to write"
    assert EN_TWO not in message
    assert _dialog_files(directory) == ["chapter-01.toml"]
    assert (directory / "chapter-01.toml").read_text() == "# Made-up old chapter one. Not game text.\n"


def test_unedited_change_with_no_directory_creates_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    with pytest.raises(Fail):
        write_dialog(directory, [_change(vanilla=vanilla_hash(EN_ONE), text=EN_ONE)])
    assert not directory.exists()


def test_duplicate_namespace_and_key_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    with pytest.raises(Fail) as raised:
        write_dialog(directory, [_change(key="7"), _change(key="7", text=TEXT_TWO)])
    assert str(raised.value) == "line 4ba5f09c: more than one change for this line"
    assert not directory.exists()


def test_dialog_oserror_fails_naming_the_path_and_reason(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    directory = blocker / "dialog" / LENS
    with pytest.raises(Fail) as raised:
        write_dialog(directory, [_change()])
    assert str(directory) in str(raised.value)
    assert "Not a directory" in str(raised.value)


# Made-up scenes, lens and answers. Not game text.
ALPHA = "made-up-first-id"
BETA = "made-up-second-id"
NS_A = "ST_C01_001_01"
NS_B = "ST_C02_001_01"
NS_C = "ST_SubEv_004_01"
NS_OUT = "ST_Menu_001"
EN_A1 = "Made-up line one of scene a. Not game text."
EN_A2 = "Made-up line two of scene a. Not game text."
EN_A3 = "Made-up line three of scene a. Not game text."
EN_A4 = "Made-up line four of scene a. Not game text."
EN_OUT = "Made-up menu line, out of scope. Not game text."
EN_B1 = "Made-up line one of scene b. Not game text."
EN_B2 = "Made-up line two of scene b. Not game text."
EN_C1 = "Made-up unattributed line. Not game text."
ENGLISH = (EN_A1, EN_A2, EN_A3, EN_A4, EN_OUT, EN_B1, EN_B2, EN_C1)
OPUS_A1 = "Made-up opus rewrite one. Not game text."
VOICE_A1 = "Made-up voiced rewrite one. Not game text."
OPUS_A2 = "Made-up opus rewrite two. Not game text."
OPUS_A3 = "Made-up opus rewrite three. Not game text."
OPUS_B1 = "Made-up opus rewrite of b one. Not game text."
OPUS_B2 = "Made-up opus rewrite of b two. Not game text."
OPUS_C1 = "Made-up opus unattributed. Not game text."
JEV_HIGH = {ALPHA: 0.9, BETA: 0.9}
COUNT_LINE = re.compile(
    r"(?:scene \d+/\d+ [0-9a-f]{8}|total): \d+ changed, \d+ kept for plot, \d+ gate failed, \d+ unfinished, \d+ untouched"
)
LENS_TOML = """\
[[triage]]
id = "made-up-first-id"
question = "Made-up question one. Not game text."

[[triage]]
id = "made-up-second-id"
question = "Made-up question two. Not game text."

[screen]
criteria = ["Made-up criterion. Not game text."]

[intent]
rule = "Made-up intent rule. Not game text."

[judge]
rubric = "Made-up judge rubric. Not game text."
"""
LENS_OBJ = Lens(
    name=LENS,
    triage=(TriageQuestion(ALPHA, "Made-up question one. Not game text."), TriageQuestion(BETA, "Made-up question two. Not game text.")),
    screen_criteria=("Made-up criterion. Not game text.",),
    intent_rule="Made-up intent rule. Not game text.",
    judge_rubric="Made-up judge rubric. Not game text.",
)
ALL_PASS = {"meaning": True, "plot": True, "minimal_change": True, "callback": True, "fixed": True, "restores_japanese": True}
CHAPTER_ONE_BIBLE: dict[str, Any] = {
    "characters": [],
    "facts": [],
    "callbacks": [
        {
            "line": {"namespace": NS_A, "key": "1"},
            "depends": [{"namespace": NS_A, "key": "3"}],
            "meaning": "Made-up callback meaning. Not game text.",
        }
    ],
    "arcs": [],
}
EMPTY_BIBLE: dict[str, Any] = {"characters": [], "facts": [], "callbacks": [], "arcs": []}


def _line(namespace: str, key: str, speaker: str | None, english: str, chapter: int | None) -> SceneLine:
    return SceneLine(namespace, key, speaker, english, f"Made-up Japanese {key}. Not game text.", chapter)


SCENE_A = Scene(
    "made-up-scene-a",
    (
        _line(NS_A, "1", "made-up-speaker-a", EN_A1, 1),
        _line(NS_A, "2", None, EN_A2, 1),
        _line(NS_A, "3", "made-up-speaker-a", EN_A3, 1),
        _line(NS_OUT, "1", None, EN_OUT, None),
        _line(NS_A, "4", None, EN_A4, 1),
    ),
)
SCENE_B = Scene("made-up-scene-b", (_line(NS_B, "1", "made-up-speaker-b", EN_B1, 2), _line(NS_B, "2", None, EN_B2, 2)))
SCENE_C = Scene("made-up-scene-c", (_line(NS_C, "1", None, EN_C1, None),))
SCENES = (SCENE_A, SCENE_B, SCENE_C)

EXPECTED_OUTCOMES = [
    Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
    Outcome(NS_A, "2", 1, None, (BETA,), "gate_failed", "minimal_change", None),
    Outcome(NS_A, "3", 1, "made-up-speaker-a", (ALPHA,), "kept_for_plot", "plot", None),
    Outcome(NS_A, "4", 1, None, (), "untouched", None, None),
    Outcome(NS_B, "1", 2, "made-up-speaker-b", (ALPHA,), "changed", None, "opus"),
    Outcome(NS_B, "2", 2, None, (BETA,), "unfinished", None, None),
    Outcome(NS_C, "1", None, None, (ALPHA,), "changed", None, "opus"),
]
EXPECTED_PRINTED = [
    "scene 1/3 54193dbc: 1 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched",
    "scene 2/3 e4beb400: 1 changed, 0 kept for plot, 0 gate failed, 1 unfinished, 0 untouched",
    "scene 3/3 0f594d36: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched",
    "total: 3 changed, 1 kept for plot, 1 gate failed, 1 unfinished, 1 untouched",
]


def _judgment(**changed: bool) -> dict[str, bool]:
    return {**ALL_PASS, **changed}


def _part(parts: Sequence[str], prefix: str) -> str:
    return next(part for part in parts if part.startswith(prefix)).removeprefix(prefix)


def _is_scene_judge(parts: Sequence[str]) -> bool:
    """True for a coherence call judged under the lens scene rubric, the plan-level scene judge."""
    return f"Judge rubric:\n{SCENE_LENS.stage_text('scene')}" in parts


def _is_scene_arc(parts: Sequence[str]) -> bool:
    """True for an arc judge call under the scene rubric, the second arc pass."""
    return any(part.startswith("Scene rubric:\n") for part in parts)


def _target(parts: Sequence[str]) -> tuple[str, str]:
    ref = json.loads(_part(parts, "Target line:\n"))
    return ref["namespace"], ref["key"]


def _draft_refs(parts: Sequence[str]) -> list[tuple[str, str]]:
    """The lines a draft call is about: a speaker draft names them under its scenes, a scene draft under its targets."""
    if any(part.startswith("Scenes:\n") for part in parts):
        scenes = json.loads(_part(parts, "Scenes:\n"))
        return [(target["namespace"], target["key"]) for scene in scenes for target in scene["targets"]]
    return [(target["namespace"], target["key"]) for target in json.loads(_part(parts, "Targets:\n"))]


def _line_targets(parts: Sequence[str]) -> list[tuple[tuple[str, str], dict[str, Any]]]:
    """Each line a line-stage prompt is about, with the arc context that prompt gives it."""
    if any(part.startswith("Targets:\n") for part in parts):
        return [((target["namespace"], target["key"]), target["context"]) for target in json.loads(_part(parts, "Targets:\n"))]
    return [(_target(parts), _context_of(parts))]


def _related_refs(parts: Sequence[str]) -> list[tuple[str, str]]:
    return [(ref["namespace"], ref["key"]) for ref in json.loads(_part(parts, "Related lines:\n"))]


class FakeClaude:
    """Stands in for the Claude batches. Answers each stage from the target line or candidate in its prompt. Not game text."""

    def __init__(self) -> None:
        self.batches = 0
        self.calls: list[tuple[str, list[str], Mapping[str, Any]]] = []
        self.bible: dict[int, dict[str, Any] | Unfinished | Rejected] = {1: CHAPTER_ONE_BIBLE}
        self.unplaced: dict[str, dict[str, Any]] = {}
        self.screen_failure: Unfinished | None = None
        # Screens of the scene holding the line get this transport failure, or raise, and nothing else changes. Not game text.
        self.screen_failure_refs: dict[tuple[str, str], Unfinished] = {}
        self.raising: set[tuple[str, str]] = set()
        # Every screen waits here before it answers, so the scenes start together. Not game text.
        self.screen_gate: threading.Barrier | None = None
        self.flags: dict[tuple[str, str], list[str]] = {}
        self.rescreen_flags: dict[tuple[str, str], list[str]] = {}
        self.rescreen_failures: dict[str, Unfinished | Rejected] = {}
        self.intents: dict[tuple[str, str], str | Unfinished] = {}
        # Lines whose intent answer keeps them unchanged, with the keep reason it names. Not game text.
        self.keeps: dict[tuple[str, str], str] = {}
        self.drafts: dict[tuple[str, str], list[str] | Unfinished | Rejected] = {}
        # Answers for draft calls that carry a reviewer note, by the one line they name. Not game text.
        self.redrafts: dict[tuple[str, str], list[str] | Unfinished | Rejected] = {}
        self.judgments: dict[str, dict[str, bool]] = {}
        self.judge_failures: dict[str, Unfinished | Rejected] = {}
        self.consistent: dict[str, bool] = {}
        self.cross_failures: dict[str, Unfinished | Rejected] = {}
        # Answers for coherence calls in order, first pass then recheck; an empty queue answers coherent. Not game text.
        self.coherence: list[dict[str, Any] | Unfinished | Rejected] = []
        # Answers for arc judge calls in order, first pass then recheck; an empty queue answers holds with no reverts. Not game text.
        self.arc: list[dict[str, Any] | Unfinished | Rejected] = []
        # Answers for arc judge calls under the scene rubric, the second pass, in order; an empty queue answers holds with no reverts. Not game text.
        self.scene_arc: list[dict[str, Any] | Unfinished | Rejected] = []
        # Answers for the scene pass's screen and plan calls. The re-plan, the plan call that names excluded lines, has its own answer. Not game text.
        self.scene_screen: dict[str, Any] | Unfinished | Rejected = {"patterns": []}
        self.scene_plan: dict[str, Any] | Unfinished | Rejected = {"keep": True, "keep_reason": "plot", "lines": []}
        self.scene_replan: dict[str, Any] | Unfinished | Rejected = {"keep": True, "keep_reason": "plot", "lines": []}
        # Answers for the scene judge's coherence calls in order, first pass then recheck; an empty queue answers coherent. Not game text.
        self.scene_judge: list[dict[str, Any] | Unfinished | Rejected] = []

    def start_batch(self) -> Ask:
        self.batches += 1
        return self.ask

    def ask(self, model: str, parts: Sequence[str], schema: Mapping[str, Any]) -> dict[str, Any] | Unfinished | Rejected:
        self.calls.append((model, list(parts), schema))
        if schema is bible.SCHEMA:
            if any(part.startswith("Unplaced lines:\n") for part in parts):
                namespace = json.loads(_part(parts, "Unplaced lines:\n"))[0]["namespace"]
                return self.unplaced.get(namespace, EMPTY_BIBLE)
            number = int(_part(parts, "Chapter ").split()[0])
            return self.bible.get(number, EMPTY_BIBLE)
        if schema is bible.SETUP_SCHEMA:
            return {"setups": []}
        if schema is SCREEN_SCHEMA:
            return self._screen(parts)
        if schema is RESCREEN_SCHEMA:
            return self._rescreen(parts)
        if schema is INTENT_SCHEMA:
            ref = _target(parts)
            answer = self.intents.get(ref, "Made-up intent. Not game text.")
            if isinstance(answer, Unfinished):
                return answer
            return {"intent": answer, "keep": ref in self.keeps, "keep_reason": self.keeps.get(ref, "none")}
        if schema is DRAFT_SCHEMA:
            refs = _draft_refs(parts)
            answers = self.redrafts if any(part.startswith("Reviewer note:\n") for part in parts) else self.drafts
            for ref in refs:
                answer = answers[ref]
                if isinstance(answer, Unfinished | Rejected):
                    return answer
            return {"drafts": [{"namespace": namespace, "key": key, "drafts": answers[(namespace, key)]} for namespace, key in refs]}
        if schema is JUDGE_SCHEMA:
            candidate = _part(parts, "Candidate:\n")
            if candidate in self.judge_failures:
                return self.judge_failures[candidate]
            return dict(self.judgments.get(candidate, ALL_PASS))
        if schema is CROSS_SCHEMA:
            candidate = _part(parts, "Candidate:\n")
            if candidate in self.cross_failures:
                return self.cross_failures[candidate]
            return {"consistent": self.consistent.get(candidate, True)}
        if schema is coherence.SCHEMA:
            if _is_scene_judge(parts):
                return self.scene_judge.pop(0) if self.scene_judge else {"coherent": True, "revert": []}
            return self.coherence.pop(0) if self.coherence else {"coherent": True, "revert": []}
        if schema is arcs.SCHEMA:
            if _is_scene_arc(parts):
                return self.scene_arc.pop(0) if self.scene_arc else {"holds": True, "revert": []}
            return self.arc.pop(0) if self.arc else {"holds": True, "revert": []}
        if schema is scene_pass.SCREEN_SCHEMA:
            return self.scene_screen
        if schema is scene_pass.PLAN_SCHEMA:
            if json.loads(_part(parts, "Excluded lines:\n")):
                return self.scene_replan
            return self.scene_plan
        raise AssertionError("unexpected schema")

    def _screen(self, parts: Sequence[str]) -> dict[str, Any] | Unfinished:
        if self.screen_gate is not None:
            self.screen_gate.wait()
        if self.screen_failure is not None:
            return self.screen_failure
        lines = json.loads(_part(parts, "Scene lines:\n"))
        for line in lines:
            ref = (line["namespace"], line["key"])
            if ref in self.screen_failure_refs:
                return self.screen_failure_refs[ref]
            if ref in self.raising:
                raise RuntimeError("Made-up scene failure. Not game text.")
        flagged = [
            {"namespace": line["namespace"], "key": line["key"], "reasons": self.flags[(line["namespace"], line["key"])]}
            for line in lines
            if (line["namespace"], line["key"]) in self.flags
        ]
        return {"flagged": flagged}

    def _rescreen(self, parts: Sequence[str]) -> dict[str, Any] | Unfinished | Rejected:
        question = json.loads(_part(parts, "Triage question:\n"))["id"]
        if question in self.rescreen_failures:
            return self.rescreen_failures[question]
        lines = json.loads(_part(parts, "Scene lines:\n"))
        flagged = [
            {"namespace": line["namespace"], "key": line["key"]}
            for line in lines
            if question in self.rescreen_flags.get((line["namespace"], line["key"]), [])
        ]
        return {"flagged": flagged}


class FakeJev:
    """Stands in for the Jev triage call, with one answer for every scene. Not game text."""

    def __init__(self, answer: dict[str, float] | Unfinished) -> None:
        self.answer = answer
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, scene: str, questions: Sequence[TriageQuestion]) -> dict[str, float] | Unfinished:
        self.calls.append((scene, [question.id for question in questions]))
        return self.answer


class FakeRenderer:
    """Stands in for the voice render. Maps a draft to its voiced text, or returns it unchanged. Not game text."""

    def __init__(self, voiced: Mapping[str, str]) -> None:
        self.voiced = dict(voiced)
        self.calls: list[tuple[str | None, str]] = []

    def render(self, speaker: str | None, text: str) -> str:
        self.calls.append((speaker, text))
        return self.voiced.get(text, text)


class FakeClassifier:
    """Scores every text as game-like and records each text it scored. Not game text."""

    threshold = 0.5

    def __init__(self) -> None:
        self.scored: list[str] = []

    def score(self, text: str) -> float:
        self.scored.append(text)
        return 1.0


class FakeSource:
    def __init__(self, scenes: Sequence[Scene], *, scene_table: bool) -> None:
        self._scenes = list(scenes)
        self.scene_table = scene_table

    def scenes(self) -> Sequence[Scene]:
        return self._scenes


def _claude() -> FakeClaude:
    claude = FakeClaude()
    claude.flags = {
        (NS_A, "1"): [ALPHA],
        (NS_A, "2"): [BETA],
        (NS_A, "3"): [ALPHA],
        (NS_B, "1"): [ALPHA],
        (NS_B, "2"): [BETA],
        (NS_C, "1"): [ALPHA],
    }
    claude.drafts = {
        (NS_A, "1"): [OPUS_A1],
        (NS_A, "2"): [OPUS_A2],
        (NS_A, "3"): [OPUS_A3],
        (NS_B, "1"): [OPUS_B1],
        (NS_C, "1"): [OPUS_C1],
    }
    # Scene B's second line fails its intent, so it stays out of the scene's shared draft call.
    claude.intents = {(NS_B, "2"): Unfinished("Made-up intent failure. Not game text.")}
    # OPUS_A1 loses on ranking (restores_japanese False), so the voiced text ships.
    claude.judgments = {
        OPUS_A1: _judgment(restores_japanese=False),
        OPUS_A2: _judgment(minimal_change=False),
        OPUS_A3: _judgment(plot=False),
    }
    # The redrafts of line 1 of scenes a and b pass every gate unless a test says otherwise. Not game text.
    claude.redrafts = {(NS_A, "1"): [REDRAFT_A1], (NS_B, "1"): [REDRAFT_B1]}
    return claude


def _run(
    tmp_path: Path,
    claude: FakeClaude,
    printed: list[str],
    *,
    jev: FakeJev | None,
    renderer: FakeRenderer | None = None,
    scenes: Sequence[Scene] = SCENES,
    classifier: FakeClassifier | None = None,
    scene_table: bool = False,
    batch: Callable[[], Ask] | None = None,
    through: int | None = None,
    lens: Lens = LENS_OBJ,
) -> list[Outcome]:
    return run(
        tmp_path / "repo",
        lens,
        Store(tmp_path / "work" / LENS),
        scenes,
        classifier=FakeClassifier() if classifier is None else classifier,
        renderer=FakeRenderer({OPUS_A1: VOICE_A1}) if renderer is None else renderer,
        run_id=RUN,
        jev=jev,
        batch=claude.start_batch if batch is None else batch,
        scene_table=scene_table,
        through=through,
        out=printed.append,
    )


def _all_prompts(claude: FakeClaude, jev: FakeJev) -> list[str]:
    return [part for _, parts, _ in claude.calls for part in parts] + [scene for scene, _ in jev.calls]


def test_each_outcome_kind_and_writer_is_recorded(tmp_path: Path) -> None:
    outcomes = _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH))
    assert outcomes == EXPECTED_OUTCOMES


def test_out_of_scope_line_has_no_record_and_reaches_no_prompt(tmp_path: Path) -> None:
    claude = _claude()
    jev = FakeJev(JEV_HIGH)
    _run(tmp_path, claude, [], jev=jev)
    prompts = _all_prompts(claude, jev)
    assert not any(NS_OUT in text or EN_OUT in text for text in prompts)
    assert NS_OUT not in (tmp_path / "work" / LENS / report.OUTCOMES).read_text(encoding="utf-8")


def test_one_batch_for_the_bible_one_per_scene_and_one_for_the_arc_judge(tmp_path: Path) -> None:
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert claude.batches == 2 + len(SCENES)


def test_one_setup_call_per_bible_chapter(tmp_path: Path) -> None:
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    forward = [parts[-1].split("\n", 1)[0] for _, parts, schema in claude.calls if schema is bible.SCHEMA]
    setup = [parts[-1].split("\n", 1)[0] for _, parts, schema in claude.calls if schema is bible.SETUP_SCHEMA]
    assert forward
    assert sorted(setup) == sorted(forward)


def _recall(tmp_path: Path) -> dict[str, dict[str, int]]:
    return json.loads((tmp_path / "work" / LENS / report.RECALL).read_text(encoding="utf-8"))


NO_RECALL = {ALPHA: {"disagreements": 0, "confirmed": 0}, BETA: {"disagreements": 0, "confirmed": 0}}
ALPHA_FLAGGED_ONLY = {(NS_A, "1"): [ALPHA], (NS_A, "3"): [ALPHA], (NS_B, "1"): [ALPHA], (NS_C, "1"): [ALPHA]}


def test_jev_off_never_calls_jev_and_screens_every_scene(tmp_path: Path) -> None:
    claude = _claude()
    outcomes = _run(tmp_path, claude, [], jev=None)
    assert sum(1 for _, _, schema in claude.calls if schema is SCREEN_SCHEMA) == len(SCENES)
    assert not any(schema is RESCREEN_SCHEMA for _, _, schema in claude.calls)
    assert outcomes == EXPECTED_OUTCOMES
    assert _recall(tmp_path) == NO_RECALL


def test_jev_below_half_makes_no_recheck_and_the_screen_flags_stand(tmp_path: Path) -> None:
    claude = _claude()
    outcomes = _run(tmp_path, claude, [], jev=FakeJev({ALPHA: 0.2, BETA: 0.4}))
    assert outcomes == EXPECTED_OUTCOMES
    assert not any(schema is RESCREEN_SCHEMA for _, _, schema in claude.calls)
    assert _recall(tmp_path) == NO_RECALL


def test_jev_at_half_for_a_flagged_id_makes_no_recheck(tmp_path: Path) -> None:
    claude = _claude()
    outcomes = _run(tmp_path, claude, [], jev=FakeJev({ALPHA: 0.5, BETA: 0.1}))
    assert outcomes == EXPECTED_OUTCOMES
    assert not any(schema is RESCREEN_SCHEMA for _, _, schema in claude.calls)
    assert _recall(tmp_path) == NO_RECALL


def test_jev_at_half_for_an_unflagged_id_rechecks_that_id_alone_and_processes_the_flagged_line(tmp_path: Path) -> None:
    claude = _claude()
    claude.flags = dict(ALPHA_FLAGGED_ONLY)
    claude.intents = {}
    claude.drafts[(NS_B, "2")] = [OPUS_B2]
    claude.rescreen_flags = {(NS_B, "2"): [BETA]}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    rechecks = [(model, parts) for model, parts, schema in claude.calls if schema is RESCREEN_SCHEMA]
    assert [model for model, _ in rechecks] == [claude_module.OPUS] * len(SCENES)
    assert [json.loads(_part(parts, "Triage question:\n"))["id"] for _, parts in rechecks] == [BETA] * len(SCENES)
    assert [o for o in outcomes if (o.namespace, o.key) == (NS_B, "2")] == [Outcome(NS_B, "2", 2, None, (BETA,), "changed", None, "opus")]
    assert _recall(tmp_path)[BETA] == {"disagreements": len(SCENES), "confirmed": 1}
    assert _recall(tmp_path)[ALPHA] == {"disagreements": 0, "confirmed": 0}


def test_recheck_flags_join_the_screen_reasons_in_lens_order(tmp_path: Path) -> None:
    claude = _claude()
    claude.flags = {(NS_B, "2"): [BETA]}
    claude.intents = {}
    claude.drafts[(NS_B, "2")] = [OPUS_B2]
    claude.rescreen_flags = {(NS_B, "2"): [ALPHA]}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert [o for o in outcomes if (o.namespace, o.key) == (NS_B, "2")] == [
        Outcome(NS_B, "2", 2, None, (ALPHA, BETA), "changed", None, "opus")
    ]


def test_a_changed_line_with_two_reasons_writes_both_ids_in_lens_order(tmp_path: Path) -> None:
    claude = _claude()
    claude.flags = {(NS_B, "2"): [BETA]}
    claude.intents = {}
    claude.drafts[(NS_B, "2")] = [OPUS_B2]
    claude.rescreen_flags = {(NS_B, "2"): [ALPHA]}
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    chapter_two = tomllib.loads((tmp_path / "repo" / "dialog" / LENS / "chapter-02.toml").read_text(encoding="utf-8"))
    assert list(chapter_two[NS_B]["2"]) == FIELD_ORDER
    assert chapter_two[NS_B]["2"]["reason"] == [ALPHA, BETA]


def test_recall_counts_are_exact_and_parse_through_report(tmp_path: Path) -> None:
    claude = _claude()
    claude.flags = dict(ALPHA_FLAGGED_ONLY)
    claude.rescreen_flags = {(NS_B, "2"): [BETA], (NS_A, "2"): [BETA]}
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert _recall(tmp_path) == {ALPHA: {"disagreements": 0, "confirmed": 0}, BETA: {"disagreements": 3, "confirmed": 2}}
    assert report._recall(tmp_path / "work" / LENS / report.RECALL, list(TRIAGE_ORDER)) == {ALPHA: (0, 0), BETA: (3, 2)}


def test_recheck_failure_leaves_the_scene_unfinished_and_counts_nothing(tmp_path: Path) -> None:
    claude = _claude()
    claude.flags = {}
    claude.rescreen_failures = {BETA: Unfinished("Made-up re-screen failure. Not game text.")}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert [o.outcome for o in outcomes] == ["unfinished"] * 7
    assert all(o.reasons == () for o in outcomes)
    assert _recall(tmp_path) == NO_RECALL


def test_jev_failure_leaves_every_line_unfinished_and_makes_no_recheck(tmp_path: Path) -> None:
    claude = _claude()
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(Unfinished("Made-up triage failure. Not game text.")))
    assert [o.outcome for o in outcomes] == ["unfinished"] * 7
    assert all(o.reasons == () for o in outcomes)
    assert not any(schema is RESCREEN_SCHEMA for _, _, schema in claude.calls)
    assert _recall(tmp_path) == NO_RECALL


def test_screen_failure_leaves_every_line_unfinished(tmp_path: Path) -> None:
    claude = _claude()
    claude.screen_failure = Unfinished("Made-up screen failure. Not game text.")
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert [o.outcome for o in outcomes] == ["unfinished"] * 7
    assert all(o.reasons == () for o in outcomes)


def test_intent_failure_leaves_that_line_unfinished_and_drafts_nothing(tmp_path: Path) -> None:
    claude = _claude()
    claude.intents[(NS_A, "1")] = Unfinished("Made-up intent failure. Not game text.")
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "unfinished", None, None)
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]
    assert not any((NS_A, "1") in _draft_refs(parts) for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA)


def test_a_kept_line_is_untouched_with_its_reasons_and_never_drafted_or_judged(tmp_path: Path) -> None:
    claude = _claude()
    claude.keeps = {(NS_A, "1"): "earned"}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "untouched", None, None, keep_reason="earned")
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]
    assert not any((NS_A, "1") in _draft_refs(parts) for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA)
    judged = [ref for ref, _ in _judged(claude, JUDGE_SCHEMA) + _judged(claude, CROSS_SCHEMA)]
    assert (NS_A, "1") not in judged


def test_a_line_kept_for_plot_is_kept_for_plot_with_its_reason_and_never_drafted_or_judged(tmp_path: Path) -> None:
    claude = _claude()
    claude.keeps = {(NS_A, "1"): "plot"}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "kept_for_plot", None, None, keep_reason="plot")
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]
    assert not any((NS_A, "1") in _draft_refs(parts) for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA)
    judged = [ref for ref, _ in _judged(claude, JUDGE_SCHEMA) + _judged(claude, CROSS_SCHEMA)]
    assert (NS_A, "1") not in judged


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up bible failure. Not game text."), Rejected("Made-up bible rejection. Not game text.")],
)
def test_bible_failure_stops_the_run_before_any_scene(tmp_path: Path, failure: Unfinished | Rejected) -> None:
    claude = _claude()
    claude.bible[1] = failure
    printed: list[str] = []
    with pytest.raises(Fail, match="story bible unfinished; rerun ffrd run"):
        _run(tmp_path, claude, printed, jev=FakeJev(JEV_HIGH))
    assert printed == [f"bible: unfinished ({failure.detail})"]
    assert all(schema is bible.SCHEMA for _, _, schema in claude.calls)
    assert claude.batches == 1
    assert not (tmp_path / "work" / LENS / report.OUTCOMES).exists()


# Made-up redo notes and redrafts. Not game text.
REDO_NOTE = "Made-up redo note. Not game text."
REDO_NOTE_TWO = "Made-up second redo note. Not game text."
REDRAFT_A1 = "Made-up redraft one. Not game text."
REDRAFT_B1 = "Made-up redraft of b one. Not game text."
DRAFT_STAGES = frozenset({"intent", "draft", "render", "judge", "cross_judge"})


def _redo_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "repo" / "lenses" / f"{LENS}.redo.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_redo_file_marks_no_outcome_and_makes_the_same_batches(tmp_path: Path) -> None:
    claude = _claude()
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert [o.redo for o in outcomes] == [False] * len(EXPECTED_OUTCOMES)
    assert claude.batches == 2 + len(SCENES)


def test_listed_line_outcome_is_redo_and_every_other_outcome_is_not(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"1" = "{REDO_NOTE}"\n\n[{NS_B}]\n1 = "{REDO_NOTE}"\n')
    outcomes = _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH))
    assert {(o.namespace, o.key) for o in outcomes if o.redo} == {(NS_A, "1"), (NS_B, "1")}
    # Line 1 of scene a ships its redraft, which has no voiced version, so its writer is opus.
    expected = [replace(o, writer="opus") if (o.namespace, o.key) == (NS_A, "1") else o for o in EXPECTED_OUTCOMES]
    assert [replace(o, redo=False) for o in outcomes] == expected
    records = [json.loads(line) for line in (tmp_path / "work" / LENS / report.OUTCOMES).read_text(encoding="utf-8").splitlines()]
    assert {(r["namespace"], r["key"]) for r in records if r["redo"] is True} == {(NS_A, "1"), (NS_B, "1")}
    assert all(r["redo"] is False for r in records if (r["namespace"], r["key"]) not in {(NS_A, "1"), (NS_B, "1")})


def test_note_of_exactly_200_characters_is_accepted(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n1 = "{"x" * 200}"\n')
    outcomes = _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH))
    assert {(o.namespace, o.key) for o in outcomes if o.redo} == {(NS_A, "1")}


def test_listed_key_beyond_through_is_accepted_and_the_listed_line_in_range_is_redo(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n1 = "{REDO_NOTE}"\n[{NS_B}]\n1 = "{REDO_NOTE}"\n')
    outcomes = _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH), through=1)
    assert NS_B not in {o.namespace for o in outcomes}
    assert {(o.namespace, o.key) for o in outcomes if o.redo} == {(NS_A, "1")}


@pytest.mark.parametrize(
    ("text", "names"),
    [
        pytest.param(f'{NS_A} = "{REDO_NOTE}"\n', [NS_A], id="namespace-value-not-a-table"),
        pytest.param(f"[{NS_A}]\n1 = 5\n", [f"{NS_A}/1"], id="note-not-a-string"),
        pytest.param(f'[{NS_A}]\n1 = ""\n', [f"{NS_A}/1"], id="note-empty"),
        pytest.param(f'[{NS_A}]\n1 = "   "\n', [f"{NS_A}/1"], id="note-only-whitespace"),
        pytest.param(f'[{NS_A}]\n1 = "{"x" * 201}"\n', [f"{NS_A}/1"], id="note-over-200-characters"),
        pytest.param(f'[{NS_OUT}]\n1 = "{REDO_NOTE}"\n', [f"{NS_OUT}/1"], id="key-out-of-scope"),
        pytest.param(f'[{NS_A}]\n9 = "{REDO_NOTE}"\n', [f"{NS_A}/9"], id="key-not-a-line"),
    ],
)
def test_invalid_redo_file_fails_naming_the_file_and_key_before_any_claude_call(tmp_path: Path, text: str, names: list[str]) -> None:
    path = _redo_file(tmp_path, text)
    claude = _claude()
    with pytest.raises(Fail) as raised:
        _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    message = str(raised.value)
    assert message.startswith(str(path))
    assert all(name in message for name in names)
    assert claude.calls == []
    assert claude.batches == 0


def test_redo_file_that_is_not_toml_fails_naming_the_file_before_any_claude_call(tmp_path: Path) -> None:
    path = _redo_file(tmp_path, f"[{NS_A}\n")
    claude = _claude()
    with pytest.raises(Fail, match=re.escape(str(path))):
        _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert claude.calls == []
    assert claude.batches == 0


def test_redo_path_that_cannot_be_read_fails_naming_the_file_and_strerror(tmp_path: Path) -> None:
    path = tmp_path / "repo" / "lenses" / f"{LENS}.redo.toml"
    path.mkdir(parents=True)
    claude = _claude()
    with pytest.raises(Fail) as raised:
        _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    message = str(raised.value)
    assert str(path) in message
    assert "Is a directory" in message
    assert claude.calls == []
    assert claude.batches == 0


BASE = "made-up-base-lens"
BASE_A1 = "Made-up base rewrite of line one of scene a. Not game text."
BASE_A2 = "Made-up base rewrite of line two of scene a. Not game text."
BASE_FIELD_ORDER = ["vanilla", "base", "text", "lens", "reason", "writer", "verdict", "run"]
EN_STALE = "Made-up English the game no longer has. Not game text."


def _base_record(namespace: str, key: str, english: str, text: str) -> str:
    # A base record as the base lens wrote it, for a line whose game English is english. Not game text.
    return f'[{namespace}.{key}]\nvanilla = "{vanilla_hash(english)}"\ntext = "{text}"\n'


def _base_file(tmp_path: Path, text: str, name: str = "chapter-01.toml") -> Path:
    path = tmp_path / "repo" / "dialog" / BASE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_base_rewrite_is_the_starting_english_of_every_prompt(tmp_path: Path) -> None:
    _base_file(tmp_path, _base_record(NS_A, "1", EN_A1, BASE_A1))
    claude = _claude()
    jev = FakeJev(JEV_HIGH)
    outcomes = _run(tmp_path, claude, [], jev=jev, lens=replace(LENS_OBJ, base=BASE))
    prompts = _all_prompts(claude, jev)
    assert not any(EN_A1 in text for text in prompts)
    assert any(BASE_A1 in text for text in prompts)
    assert outcomes == EXPECTED_OUTCOMES


def test_changed_line_started_from_a_base_rewrite_records_the_game_hash_and_the_base_hash(tmp_path: Path) -> None:
    _base_file(tmp_path, _base_record(NS_A, "1", EN_A1, BASE_A1))
    _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    dialog = tmp_path / "repo" / "dialog" / LENS
    chapter_one = tomllib.loads((dialog / "chapter-01.toml").read_text(encoding="utf-8"))
    assert list(chapter_one[NS_A]["1"]) == BASE_FIELD_ORDER
    assert chapter_one[NS_A]["1"] == {
        "vanilla": vanilla_hash(EN_A1),
        "base": vanilla_hash(BASE_A1),
        "text": VOICE_A1,
        "lens": LENS,
        "reason": [ALPHA],
        "writer": "voice",
        "verdict": "passed",
        "run": RUN,
    }
    chapter_two = tomllib.loads((dialog / "chapter-02.toml").read_text(encoding="utf-8"))
    assert "base" not in chapter_two[NS_B]["1"]
    assert chapter_two[NS_B]["1"]["vanilla"] == vanilla_hash(EN_B1)


def test_base_record_whose_vanilla_differs_from_the_game_line_fails_naming_the_key_before_any_claude_call(tmp_path: Path) -> None:
    path = _base_file(tmp_path, _base_record(NS_A, "1", EN_STALE, BASE_A1))
    claude = _claude()
    with pytest.raises(Fail) as raised:
        _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    assert str(raised.value) == f"{path}: {NS_A}/1: changed in the game"
    assert claude.calls == []
    assert claude.batches == 0


@pytest.mark.parametrize(
    ("text", "name"),
    [
        pytest.param(f'[{NS_A}.1]\ntext = "{BASE_A1}"\n', f"{NS_A}/1", id="record-lacks-vanilla"),
        pytest.param(f'[{NS_A}.1]\nvanilla = "{vanilla_hash(EN_A1)}"\n', f"{NS_A}/1", id="record-lacks-text"),
        pytest.param(f'[{NS_A}.1]\nvanilla = 5\ntext = "{BASE_A1}"\n', f"{NS_A}/1", id="vanilla-not-a-string"),
        pytest.param(f'[{NS_A}.1]\nvanilla = "{vanilla_hash(EN_A1)}"\ntext = 5\n', f"{NS_A}/1", id="text-not-a-string"),
        pytest.param(f'[{NS_A}]\n1 = "{BASE_A1}"\n', f"{NS_A}/1", id="record-not-a-table"),
        pytest.param(f'{NS_A} = "{BASE_A1}"\n', NS_A, id="namespace-not-a-table"),
    ],
)
def test_malformed_base_record_fails_naming_the_file_and_key_before_any_claude_call(tmp_path: Path, text: str, name: str) -> None:
    path = _base_file(tmp_path, text)
    claude = _claude()
    with pytest.raises(Fail) as raised:
        _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    assert str(raised.value).startswith(f"{path}: {name}: ")
    assert claude.calls == []
    assert claude.batches == 0


def test_base_record_for_a_key_outside_the_passed_scenes_is_neither_checked_nor_used(tmp_path: Path) -> None:
    _base_file(tmp_path, _base_record(NS_A, "1", EN_A1, BASE_A1) + f'[{NS_A}.9]\nvanilla = "stale"\n')
    outcomes = _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    assert outcomes == EXPECTED_OUTCOMES


def test_missing_base_directory_fails_naming_it_before_any_claude_call(tmp_path: Path) -> None:
    claude = _claude()
    with pytest.raises(Fail) as raised:
        _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    assert str(raised.value) == f"dialog/{BASE}/: directory is missing"
    assert claude.calls == []
    assert claude.batches == 0


def test_base_file_that_is_not_toml_fails_naming_the_file_before_any_claude_call(tmp_path: Path) -> None:
    path = _base_file(tmp_path, f"[{NS_A}\n")
    claude = _claude()
    with pytest.raises(Fail, match=re.escape(str(path))):
        _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    assert claude.calls == []
    assert claude.batches == 0


def test_change_with_a_base_writes_base_directly_after_vanilla(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(directory, [replace(_change(), base=vanilla_hash(BASE_A1))])
    first = tomllib.loads((directory / "chapter-01.toml").read_text(encoding="utf-8"))
    assert list(first["made-up-scene-a"]["1"]) == BASE_FIELD_ORDER
    assert first["made-up-scene-a"]["1"]["base"] == vanilla_hash(BASE_A1)


def test_scene_pass_change_started_from_a_base_rewrite_records_the_game_hash_and_the_base_hash(tmp_path: Path) -> None:
    _base_file(tmp_path, _base_record(NS_A, "2", EN_A2, BASE_A2))
    claude = _scene_claude()
    # The plan quotes the carrier from the base text, the English the scene pass now reads.
    claude.scene_plan = {
        "keep": False,
        "keep_reason": "none",
        "lines": [{"namespace": NS_A, "key": "2", "intent": "Made-up scene intent. Not game text.", "carrier": BASE_A2, "required": True}],
    }
    outcomes = _scene_run(tmp_path, claude, lens=replace(SCENE_LENS, base=BASE))
    assert _outcome_of(outcomes, (NS_A, "2")).outcome == "changed"
    chapter_one = tomllib.loads((tmp_path / "repo" / "dialog" / LENS / "chapter-01.toml").read_text(encoding="utf-8"))
    assert chapter_one[NS_A]["2"]["vanilla"] == vanilla_hash(EN_A2)
    assert chapter_one[NS_A]["2"]["base"] == vanilla_hash(BASE_A2)


def test_base_rewrite_draft_equal_to_the_game_line_is_never_judged_or_written_and_the_run_completes(tmp_path: Path) -> None:
    _base_file(tmp_path, _base_record(NS_A, "1", EN_A1, BASE_A1))
    claude = _claude()
    claude.drafts[(NS_A, "1")] = [EN_A1, OPUS_A1]
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    assert outcomes[0] == EXPECTED_OUTCOMES[0]
    assert EN_A1 not in [candidate for _, candidate in _judged(claude, JUDGE_SCHEMA)]
    chapter_one = tomllib.loads((tmp_path / "repo" / "dialog" / LENS / "chapter-01.toml").read_text(encoding="utf-8"))
    assert chapter_one[NS_A]["1"]["text"] == VOICE_A1


def test_base_rewrite_whose_only_draft_is_the_game_line_completes_the_run_and_writes_no_game_line(tmp_path: Path) -> None:
    _base_file(tmp_path, _base_record(NS_A, "1", EN_A1, BASE_A1))
    claude = _claude()
    claude.drafts[(NS_A, "1")] = [EN_A1]
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), lens=replace(LENS_OBJ, base=BASE))
    assert EN_A1 not in [candidate for _, candidate in _judged(claude, JUDGE_SCHEMA)]
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "writer", None)
    dialog = tmp_path / "repo" / "dialog" / LENS
    assert not any(EN_A1 in path.read_text(encoding="utf-8") for path in dialog.glob("*.toml"))


def _note_drafts(claude: FakeClaude) -> list[tuple[list[tuple[str, str]], str]]:
    """Each draft call that carries a reviewer note, with the lines it names and its note."""
    return [
        (_draft_refs(parts), _part(parts, "Reviewer note:\n"))
        for _, parts, schema in claude.calls
        if schema is DRAFT_SCHEMA and any(part.startswith("Reviewer note:\n") for part in parts)
    ]


def _stored(work: Path) -> dict[str, str]:
    """Each stored result of the run's draft stages, by result key, with its stage."""
    stored: dict[str, str] = {}
    for path in (work / "results").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["stage"] in DRAFT_STAGES:
            stored[path.stem] = record["stage"]
    return stored


def _dialog_texts(tmp_path: Path) -> dict[tuple[str, str], str]:
    texts: dict[tuple[str, str], str] = {}
    for path in (tmp_path / "repo" / "dialog" / LENS).iterdir():
        for namespace, table in tomllib.loads(path.read_text(encoding="utf-8")).items():
            texts.update({(namespace, key): entry["text"] for key, entry in table.items()})
    return texts


# A later line that shares scene a's line 1 English. Not game text.
LATE_DUPLICATE = Scene("made-up-scene-duplicate", (_line(NS_B, "1", "made-up-speaker-b", EN_A1, 2),))
EARLY_DUPLICATE = Scene("made-up-scene-first", (_line(NS_A, "1", "made-up-speaker-a", EN_A1, 1),))


def test_listed_line_is_redrafted_with_its_note_and_the_redraft_ships(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"1" = "{REDO_NOTE}"\n')
    claude = _claude()
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert _note_drafts(claude) == [([(NS_A, "1")], REDO_NOTE)]
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "opus", redo=True)
    assert _dialog_texts(tmp_path)[(NS_A, "1")] == REDRAFT_A1


def test_every_draft_stage_result_of_the_run_without_the_redo_file_is_also_stored_with_it(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    listed = tmp_path / "listed"
    _run(plain, _claude(), [], jev=FakeJev(JEV_HIGH))
    _redo_file(listed, f'[{NS_A}]\n"1" = "{REDO_NOTE}"\n')
    _run(listed, _claude(), [], jev=FakeJev(JEV_HIGH))
    plain_stored = _stored(plain / "work" / LENS)
    assert set(plain_stored.values()) == DRAFT_STAGES
    assert set(plain_stored.items()) <= set(_stored(listed / "work" / LENS).items())


def test_failed_redraft_is_gate_failed_under_redo_and_ships_no_first_rewrite(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"1" = "{REDO_NOTE}"\n')
    claude = _claude()
    claude.judgments[REDRAFT_A1] = _judgment(plot=False)
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "redo", None, redo=True)
    assert (NS_A, "1") not in _dialog_texts(tmp_path)
    retries = [
        parts
        for _, parts, _ in claude.calls
        if any(part.startswith("Reviewer note:\n") for part in parts) and _draft_refs(parts) == [(NS_A, "1")]
    ]
    assert any(part == "The previous candidates failed the plot gate. Write candidates that pass it." for part in retries[-1])
    assert _note_drafts(claude) == [([(NS_A, "1")], REDO_NOTE), ([(NS_A, "1")], REDO_NOTE)]


def test_unfinished_redraft_is_unfinished_and_ships_no_first_rewrite(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"1" = "{REDO_NOTE}"\n')
    claude = _claude()
    claude.redrafts[(NS_A, "1")] = Unfinished("Made-up redraft failure. Not game text.")
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "unfinished", None, None, redo=True)
    assert (NS_A, "1") not in _dialog_texts(tmp_path)


def test_listed_line_the_run_would_not_change_makes_no_note_call(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"2" = "{REDO_NOTE}"\n3 = "{REDO_NOTE}"\n4 = "{REDO_NOTE}"\n')
    claude = _claude()
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert _note_drafts(claude) == []
    assert {(o.namespace, o.key) for o in outcomes if o.redo} == {(NS_A, "2"), (NS_A, "3"), (NS_A, "4")}
    assert [replace(o, redo=False) for o in outcomes] == EXPECTED_OUTCOMES


def test_changed_note_asks_a_new_draft_on_the_same_store(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"1" = "{REDO_NOTE}"\n')
    _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH))
    _redo_file(tmp_path, f'[{NS_A}]\n"1" = "{REDO_NOTE_TWO}"\n')
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert _note_drafts(claude) == [([(NS_A, "1")], REDO_NOTE_TWO)]
    assert [_draft_refs(parts) for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA] == [[(NS_A, "1")]]


def test_listed_later_duplicate_takes_its_redraft_as_its_groups_text(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_B}]\n"1" = "{REDO_NOTE}"\n')
    claude = _claude()
    claude.redrafts[(NS_B, "1")] = [REDRAFT_A1]
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), scenes=(EARLY_DUPLICATE, LATE_DUPLICATE))
    assert outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "opus"),
        Outcome(NS_B, "1", 2, "made-up-speaker-b", (ALPHA,), "changed", None, "opus", redo=True),
    ]
    assert _dialog_texts(tmp_path) == {(NS_A, "1"): REDRAFT_A1, (NS_B, "1"): REDRAFT_A1}


def test_cross_judge_runs_for_a_callback_line_with_its_related_lines(tmp_path: Path) -> None:
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    cross = [(_target(parts), _related_refs(parts)) for _, parts, schema in claude.calls if schema is CROSS_SCHEMA]
    assert cross
    assert {target for target, _ in cross} == {(NS_A, "1")}
    assert all(related == [(NS_A, "3")] for _, related in cross)


def test_false_cross_judge_verdict_blocks_the_change(tmp_path: Path) -> None:
    claude = _claude()
    claude.consistent = {OPUS_A1: False, VOICE_A1: False}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "callback", None)
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]


def test_writer_retry_is_told_the_failing_gate_once(tmp_path: Path) -> None:
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    drafts_a2 = _drafts_for(claude, (NS_A, "2"))
    assert len(drafts_a2) == 2
    assert not any(part.startswith("The previous candidates failed") for part in drafts_a2[0])
    assert any(part.startswith("The previous candidates failed the minimal_change gate") for part in drafts_a2[1])
    assert len(_drafts_for(claude, (NS_A, "1"))) == 1


def test_draft_failure_leaves_every_flagged_line_of_the_scene_unfinished(tmp_path: Path) -> None:
    claude = _claude()
    claude.intents = {}
    claude.drafts[(NS_B, "2")] = Unfinished("Made-up draft failure. Not game text.")
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), scenes=(SCENE_B,))
    assert [o.outcome for o in outcomes] == ["unfinished", "unfinished"]
    assert len([parts for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA]) == 1
    assert not any(schema in (JUDGE_SCHEMA, CROSS_SCHEMA) for _, _, schema in claude.calls)


def test_scene_with_two_flagged_lines_drafts_both_in_one_call(tmp_path: Path) -> None:
    claude = _claude()
    claude.intents = {}
    claude.drafts[(NS_B, "2")] = [OPUS_B2]
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), scenes=(SCENE_B,))
    draft_calls = [parts for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA]
    assert [_draft_refs(parts) for parts in draft_calls] == [[(NS_B, "1"), (NS_B, "2")]]
    assert outcomes == [
        Outcome(NS_B, "1", 2, "made-up-speaker-b", (ALPHA,), "changed", None, "opus"),
        Outcome(NS_B, "2", 2, None, (BETA,), "changed", None, "opus"),
    ]


def test_writer_retry_names_only_its_line_and_the_gate_it_failed(tmp_path: Path) -> None:
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    retries = [
        parts
        for _, parts, schema in claude.calls
        if schema is DRAFT_SCHEMA and any(part.startswith("The previous candidates failed") for part in parts)
    ]
    # The scene's lines are gated concurrently, so the retries are told apart by their line, not by call order.
    assert sorted(_draft_refs(parts) for parts in retries) == [[(NS_A, "2")], [(NS_A, "3")]]
    by_line = {_draft_refs(parts)[0]: parts for parts in retries}
    assert any(
        part == "The previous candidates failed the minimal_change gate. Write candidates that pass it." for part in by_line[(NS_A, "2")]
    )
    assert any(part == "The previous candidates failed the plot gate. Write candidates that pass it." for part in by_line[(NS_A, "3")])
    for parts in retries:
        scene_refs = {(line["namespace"], line["key"]) for line in json.loads(_part(parts, "Scene lines:\n"))}
        assert {(NS_A, "1"), (NS_A, "2"), (NS_A, "3"), (NS_A, "4")} <= scene_refs


def _drafts_for(claude: FakeClaude, ref: tuple[str, str]) -> list[list[str]]:
    return [parts for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA and ref in _draft_refs(parts)]


def _judged(claude: FakeClaude, schema: Mapping[str, Any]) -> list[tuple[tuple[str, str], str]]:
    return [(_target(parts), _part(parts, "Candidate:\n")) for _, parts, called in claude.calls if called is schema]


def _dialog_entries(tmp_path: Path) -> set[tuple[str, str]]:
    entries: set[tuple[str, str]] = set()
    for path in (tmp_path / "repo" / "dialog" / LENS).iterdir():
        for namespace, table in tomllib.loads(path.read_text(encoding="utf-8")).items():
            entries.update((namespace, key) for key in table)
    return entries


def test_scene_judge_unfinished_leaves_the_line_unfinished_with_no_dialog_entry(tmp_path: Path) -> None:
    claude = _claude()
    failure = Unfinished("Made-up judge failure. Not game text.")
    claude.judge_failures = {OPUS_A1: failure, VOICE_A1: failure}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "unfinished", None, None)
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]
    assert len(_drafts_for(claude, (NS_A, "1"))) == 1
    assert (NS_A, "1") not in _dialog_entries(tmp_path)


def test_scene_judge_rejecting_every_candidate_fails_the_judge_gate_after_one_retry(tmp_path: Path) -> None:
    claude = _claude()
    rejection = Rejected("Made-up judge rejection. Not game text.")
    claude.judge_failures = {OPUS_A1: rejection, VOICE_A1: rejection}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "judge", None)
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]
    assert len(_drafts_for(claude, (NS_A, "1"))) == 2
    assert (NS_A, "1") not in _dialog_entries(tmp_path)


def test_cross_judge_unfinished_leaves_the_callback_line_unfinished_with_no_dialog_entry(tmp_path: Path) -> None:
    claude = _claude()
    failure = Unfinished("Made-up cross-judge failure. Not game text.")
    claude.cross_failures = {OPUS_A1: failure, VOICE_A1: failure}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "unfinished", None, None)
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]
    assert (NS_A, "1") not in _dialog_entries(tmp_path)


def test_cross_judge_rejecting_every_candidate_fails_the_judge_gate(tmp_path: Path) -> None:
    claude = _claude()
    rejection = Rejected("Made-up cross-judge rejection. Not game text.")
    claude.cross_failures = {OPUS_A1: rejection, VOICE_A1: rejection}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "judge", None)
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]
    assert (NS_A, "1") not in _dialog_entries(tmp_path)


def test_unfixed_candidate_fails_the_fixed_gate(tmp_path: Path) -> None:
    claude = _claude()
    claude.judgments = {**claude.judgments, OPUS_A1: _judgment(fixed=False), VOICE_A1: _judgment(fixed=False)}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "fixed", None)
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]


def test_unfixed_japanese_restoring_candidate_never_ships_beside_a_fixed_one(tmp_path: Path) -> None:
    claude = _claude()
    claude.judgments = {
        **claude.judgments,
        OPUS_A1: _judgment(fixed=False, restores_japanese=True),
        VOICE_A1: _judgment(restores_japanese=False),
    }
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice")
    assert outcomes[1:] == EXPECTED_OUTCOMES[1:]


def test_both_judges_receive_the_line_reasons_and_intent(tmp_path: Path) -> None:
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    for schema in (JUDGE_SCHEMA, CROSS_SCHEMA):
        prompts = [parts for _, parts, called in claude.calls if called is schema and _target(parts) == (NS_A, "1")]
        assert prompts
        for parts in prompts:
            assert _part(parts, "Line reasons:\n") == '[{"id": "made-up-first-id", "question": "Made-up question one. Not game text."}]'
            assert _part(parts, "Intent:\n") == "Made-up intent. Not game text."


def test_rejected_draft_fails_the_writer_gate_after_one_retry_and_is_never_judged(tmp_path: Path) -> None:
    claude = _claude()
    claude.intents = {}
    claude.drafts[(NS_B, "2")] = Rejected("Made-up draft rejection. Not game text.")
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes == [*EXPECTED_OUTCOMES[:5], Outcome(NS_B, "2", 2, None, (BETA,), "gate_failed", "writer", None), *EXPECTED_OUTCOMES[6:]]
    retried = _drafts_for(claude, (NS_B, "2"))
    assert len(retried) == 2
    assert not any(part.startswith("The previous candidates failed") for part in retried[0])
    assert any(part == "The previous candidates failed the writer gate. Write candidates that pass it." for part in retried[1])
    judged = [ref for ref, _ in _judged(claude, JUDGE_SCHEMA) + _judged(claude, CROSS_SCHEMA)]
    assert (NS_B, "2") not in judged


def test_candidate_equal_to_the_english_is_never_judged_or_written(tmp_path: Path) -> None:
    claude = _claude()
    claude.drafts[(NS_A, "1")] = [EN_A1, OPUS_A1]
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == EXPECTED_OUTCOMES[0]
    assert EN_A1 not in [candidate for _, candidate in _judged(claude, JUDGE_SCHEMA)]
    chapter_one = tomllib.loads((tmp_path / "repo" / "dialog" / LENS / "chapter-01.toml").read_text(encoding="utf-8"))
    assert chapter_one[NS_A]["1"]["text"] == VOICE_A1
    assert chapter_one[NS_A]["1"]["text"] != EN_A1


def test_repeated_candidate_is_judged_once(tmp_path: Path) -> None:
    claude = _claude()
    claude.drafts[(NS_B, "1")] = [OPUS_B1, OPUS_B1]
    classifier = FakeClassifier()
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), classifier=classifier)
    assert [candidate for ref, candidate in _judged(claude, JUDGE_SCHEMA) if ref == (NS_B, "1")] == [OPUS_B1]
    assert classifier.scored.count(OPUS_B1) == 1
    assert outcomes[4] == EXPECTED_OUTCOMES[4]


# Made-up scenes for bible assembly, out of chapter order. Not game text.
EN_A_REPEAT = "Made-up different English for a repeated key. Not game text."
LATE_SCENE = Scene("made-up-scene-late", (_line(NS_B, "1", "made-up-speaker-b", EN_B1, 2),))
EARLY_SCENE = Scene(
    "made-up-scene-early",
    (
        _line(NS_A, "1", "made-up-speaker-a", EN_A1, 1),
        _line(NS_A, "2", None, EN_A2, 1),
        _line(NS_A, "1", "made-up-speaker-a", EN_A_REPEAT, 1),
    ),
)


def test_bible_runs_chapters_ascending_with_first_occurrence_lines_and_empty_speaker(tmp_path: Path) -> None:
    claude = _claude()
    claude.bible = {}
    claude.flags = {}
    _run(tmp_path, claude, [], jev=None, scenes=(LATE_SCENE, EARLY_SCENE))
    bible_calls = [parts for _, parts, schema in claude.calls if schema is bible.SCHEMA]
    assert len(bible_calls) == 2
    assert json.loads(_part(bible_calls[0], "Chapter 1 lines:\n")) == [
        {"namespace": NS_A, "key": "1", "speaker": "made-up-speaker-a", "text": EN_A1},
        {"namespace": NS_A, "key": "2", "speaker": "", "text": EN_A2},
    ]
    assert json.loads(_part(bible_calls[1], "Chapter 2 lines:\n")) == [
        {"namespace": NS_B, "key": "1", "speaker": "made-up-speaker-b", "text": EN_B1},
    ]


# Made-up unplaced lines of two namespaces, the one that sorts later as plain text first in the scene.
# Not game text.
UNPLACED_LATER = "ST_SubEv_10"
UNPLACED_EARLIER = "ST_SubEv_2"
UNPLACED_SCENE = Scene(
    "made-up-scene-unplaced",
    (
        _line(UNPLACED_LATER, "1", None, EN_C1, None),
        _line(UNPLACED_EARLIER, "1", "made-up-speaker-a", EN_OUT, None),
        _line(UNPLACED_LATER, "2", None, EN_B2, None),
    ),
)
UNPLACED_BIBLE: dict[str, Any] = {
    "characters": [],
    "facts": [],
    "callbacks": [
        {
            "line": {"namespace": UNPLACED_EARLIER, "key": "1"},
            "depends": [{"namespace": NS_B, "key": "1"}],
            "meaning": "Made-up callback meaning unplaced. Not game text.",
        }
    ],
    "arcs": [],
}


def test_unplaced_lines_are_one_bible_per_namespace_after_numbered_chapters_in_natural_order(tmp_path: Path) -> None:
    claude = _claude()
    claude.bible = {}
    claude.flags = {}
    claude.unplaced = {UNPLACED_EARLIER: UNPLACED_BIBLE}
    _run(tmp_path, claude, [], jev=None, scenes=(LATE_SCENE, EARLY_SCENE, UNPLACED_SCENE))
    bible_calls = [parts for _, parts, schema in claude.calls if schema is bible.SCHEMA]
    assert len(bible_calls) == 4
    assert any(part.startswith("Chapter 1 lines:\n") for part in bible_calls[0])
    assert any(part.startswith("Chapter 2 lines:\n") for part in bible_calls[1])
    # The two unplaced chunks run concurrently, so they are told apart by their namespace, not by call order.
    unplaced = {json.loads(_part(parts, "Unplaced lines:\n"))[0]["namespace"]: parts for parts in bible_calls[2:]}
    assert set(unplaced) == {UNPLACED_EARLIER, UNPLACED_LATER}
    assert json.loads(_part(unplaced[UNPLACED_EARLIER], "Unplaced lines:\n")) == [
        {"namespace": UNPLACED_EARLIER, "key": "1", "speaker": "made-up-speaker-a", "text": EN_OUT},
    ]
    assert json.loads(_part(unplaced[UNPLACED_LATER], "Unplaced lines:\n")) == [
        {"namespace": UNPLACED_LATER, "key": "1", "speaker": "", "text": EN_C1},
        {"namespace": UNPLACED_LATER, "key": "2", "speaker": "", "text": EN_B2},
    ]
    # Each unplaced chunk reads exactly the two numbered chapter bibles, never the other unplaced chunk's bible.
    for parts in unplaced.values():
        assert [bible_["chapter"] for bible_ in json.loads(_part(parts, "Earlier bibles:\n"))] == [1, 2]
    assert "Made-up callback meaning unplaced. Not game text." not in "\n".join(unplaced[UNPLACED_LATER])


def _assert_printed(printed: Sequence[str]) -> None:
    # A scene's line prints as that scene finishes, so only the total line has a fixed place.
    assert sorted(printed[:-1]) == sorted(EXPECTED_PRINTED[:-1])
    assert printed[-1] == EXPECTED_PRINTED[-1]


def test_printed_lines_are_scene_keys_and_counts_only(tmp_path: Path) -> None:
    printed: list[str] = []
    _run(tmp_path, _claude(), printed, jev=FakeJev(JEV_HIGH))
    _assert_printed(printed)
    assert all(COUNT_LINE.fullmatch(line) for line in printed)
    assert not any(text in line for line in printed for text in (*ENGLISH, OPUS_A1, VOICE_A1, OPUS_B1))


# Made-up scene key holding a sentinel name. Not game text.
SENTINEL = "made-up-scene-Zorbleth"
SENTINEL_SCENE = Scene(SENTINEL, (_line(NS_A, "1", "made-up-speaker-a", EN_A1, 1), _line(NS_A, "3", "made-up-speaker-a", EN_A3, 1)))


def test_scene_lines_name_each_scene_by_position_and_label_and_never_its_key(tmp_path: Path) -> None:
    printed: list[str] = []
    _run(tmp_path, _claude(), printed, jev=FakeJev(JEV_HIGH), scenes=(SENTINEL_SCENE, SCENE_B))
    assert not any("Zorbleth" in line for line in printed)
    scene_lines = sorted(line.split(":")[0] for line in printed if line.startswith("scene "))
    assert scene_lines == ["scene 1/2 2a41d117", "scene 2/2 e4beb400"]
    assert all(COUNT_LINE.fullmatch(line) for line in printed)


def test_run_command_stdout_and_stderr_never_name_the_sentinel_scene_key(
    lens_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bind(monkeypatch, _claude(), FakeJev(JEV_HIGH), scenes=(SENTINEL_SCENE, SCENE_B))
    args = _parse(["run", "--lens", LENS])

    args.fn(args)

    captured = capsys.readouterr()
    assert "Zorbleth" not in captured.out
    assert "Zorbleth" not in captured.err
    scene_lines = sorted(line.split(":")[0] for line in captured.out.splitlines() if line.startswith("scene "))
    assert scene_lines == ["scene 1/2 2a41d117", "scene 2/2 e4beb400"]


def test_outcomes_and_dialog_are_written_and_hold_no_english(tmp_path: Path) -> None:
    _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH))
    outcome_text = (tmp_path / "work" / LENS / report.OUTCOMES).read_text(encoding="utf-8")
    records = [json.loads(line) for line in outcome_text.splitlines()]
    assert [(r["namespace"], r["key"], r["outcome"], r["gate"], r["writer"]) for r in records] == [
        (o.namespace, o.key, o.outcome, o.gate, o.writer) for o in EXPECTED_OUTCOMES
    ]
    dialog = tmp_path / "repo" / "dialog" / LENS
    assert _dialog_files(dialog) == ["chapter-01.toml", "chapter-02.toml", "unattributed.toml"]
    chapter_one = tomllib.loads((dialog / "chapter-01.toml").read_text(encoding="utf-8"))
    assert chapter_one[NS_A]["1"] == {
        "vanilla": vanilla_hash(EN_A1),
        "text": VOICE_A1,
        "lens": LENS,
        "reason": [ALPHA],
        "writer": "voice",
        "verdict": "passed",
        "run": RUN,
    }
    chapter_two = tomllib.loads((dialog / "chapter-02.toml").read_text(encoding="utf-8"))
    assert chapter_two[NS_B]["1"]["text"] == OPUS_B1
    assert chapter_two[NS_B]["1"]["writer"] == "opus"
    unattributed = tomllib.loads((dialog / "unattributed.toml").read_text(encoding="utf-8"))
    assert unattributed[NS_C]["1"]["text"] == OPUS_C1
    written = outcome_text + "".join(p.read_text(encoding="utf-8") for p in dialog.iterdir())
    assert not any(english in written for english in ENGLISH)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_rerun_makes_no_calls_and_writes_identical_files(tmp_path: Path) -> None:
    claude = _claude()
    # Every stage must finish on the first run, or the rerun would ask again for the unfinished line.
    claude.intents = {}
    claude.drafts[(NS_B, "2")] = [OPUS_B2]
    jev = FakeJev(JEV_HIGH)
    renderer = FakeRenderer({OPUS_A1: VOICE_A1})
    _run(tmp_path, claude, [], jev=jev, renderer=renderer)
    before = _snapshot(tmp_path)
    calls, jev_calls, renders = len(claude.calls), len(jev.calls), len(renderer.calls)

    _run(tmp_path, claude, [], jev=jev, renderer=renderer)

    assert len(claude.calls) == calls
    assert len(jev.calls) == jev_calls
    assert len(renderer.calls) == renders
    assert _snapshot(tmp_path) == before


# Made-up command repo and lens file. Not game text.
@pytest.fixture
def lens_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    (root / "lenses").mkdir()
    (root / "lenses" / f"{LENS}.toml").write_text(LENS_TOML)
    monkeypatch.chdir(root)
    return root


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ffrd")
    register(parser.add_subparsers(required=True))
    return parser.parse_args(argv)


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    claude: FakeClaude,
    jev: FakeJev,
    *,
    scenes: Sequence[Scene] = SCENES,
    scene_table: bool = False,
) -> None:
    class Batch:
        def __init__(self) -> None:
            self.ask: Ask = claude.start_batch()

    def bindings() -> Bindings:
        source = FakeSource(scenes, scene_table=scene_table)
        return Bindings(scenes=source, classifier=FakeClassifier(), renderer=FakeRenderer({OPUS_A1: VOICE_A1}))

    monkeypatch.setattr(stages, "bindings", bindings)
    monkeypatch.setattr(jev_module, "triage", jev)
    monkeypatch.setattr(stages, "Batch", Batch)


def test_register_declares_the_run_command_with_required_lens_and_no_jev_flag() -> None:
    args = _parse(["run", "--lens", LENS])
    assert args.fn is cmd_run
    assert args.lens == LENS
    assert args.no_jev is False
    assert _parse(["run", "--lens", LENS, "--no-jev"]).no_jev is True
    with pytest.raises(SystemExit):
        _parse(["run"])


def test_run_command_writes_outcomes_and_dialog_through_the_bound_stages(
    lens_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claude = _claude()
    jev = FakeJev(JEV_HIGH)
    _bind(monkeypatch, claude, jev)
    args = _parse(["run", "--lens", LENS])

    args.fn(args)

    _assert_printed(capsys.readouterr().out.splitlines())
    records = (lens_repo / "work" / LENS / report.OUTCOMES).read_text(encoding="utf-8").splitlines()
    assert len(records) == len(EXPECTED_OUTCOMES)
    dialog = lens_repo / "dialog" / LENS
    assert _dialog_files(dialog) == ["chapter-01.toml", "chapter-02.toml", "unattributed.toml"]
    run_id = tomllib.loads((dialog / "chapter-01.toml").read_text(encoding="utf-8"))[NS_A]["1"]["run"]
    assert re.fullmatch(r"\d{8}T\d{6}Z", run_id)
    assert len(jev.calls) == len(SCENES)


def test_run_command_with_no_jev_never_calls_jev_and_writes_zero_recall(
    lens_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claude = _claude()
    jev = FakeJev(JEV_HIGH)
    _bind(monkeypatch, claude, jev)
    args = _parse(["run", "--lens", LENS, "--no-jev"])

    args.fn(args)

    assert jev.calls == []
    _assert_printed(capsys.readouterr().out.splitlines())
    assert (lens_repo / "work" / LENS / report.OUTCOMES).is_file()
    assert json.loads((lens_repo / "work" / LENS / report.RECALL).read_text(encoding="utf-8")) == NO_RECALL


# Made-up story line outside the fallback allowlist. Not game text.
NS_STORY = "ST_Story_001"
EN_STORY = "Made-up story line. Not game text."
STORY_SCENE = Scene("made-up-scene-story", (_line(NS_STORY, "1", None, EN_STORY, 1),))


@pytest.mark.parametrize(("scene_table", "present"), [(False, False), (True, True)])
def test_run_keeps_a_line_outside_the_fallback_allowlist_only_with_a_scene_table(tmp_path: Path, scene_table: bool, present: bool) -> None:
    claude = _claude()
    claude.bible = {}
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(STORY_SCENE,), scene_table=scene_table)
    assert [(o.namespace, o.key) for o in outcomes] == ([(NS_STORY, "1")] if present else [])


@pytest.mark.parametrize(("scene_table", "present"), [(False, False), (True, True)])
def test_run_command_scopes_by_the_bound_source_scene_table(
    lens_repo: Path, monkeypatch: pytest.MonkeyPatch, scene_table: bool, present: bool
) -> None:
    claude = _claude()
    claude.bible = {}
    _bind(monkeypatch, claude, FakeJev(JEV_HIGH), scenes=(STORY_SCENE,), scene_table=scene_table)
    args = _parse(["run", "--lens", LENS, "--no-jev"])

    args.fn(args)

    outcome_text = (lens_repo / "work" / LENS / report.OUTCOMES).read_text(encoding="utf-8")
    assert (NS_STORY in outcome_text) is present


# Made-up arcs and character notes for the arc context and cast. Not game text.
ARC_A = "made-up-arc-a"
ARC_B = "made-up-arc-b"
NOTES_A = {"speaker": "made-up-speaker-a", "notes": "Made-up notes for speaker a. Not game text.", "gender": "made-up-gender-a"}
NOTES_B = {"speaker": "made-up-speaker-b", "notes": "Made-up notes for speaker b. Not game text.", "gender": "made-up-gender-b"}
ARC_A_ENTRY = {
    "id": ARC_A,
    "characters": ["made-up-speaker-a"],
    "state": "Made-up state of arc a. Not game text.",
    "beats": [{"line": {"namespace": NS_A, "key": "1"}, "kind": "setup", "earned": True}],
}
ARC_B_ENTRY = {
    "id": ARC_B,
    "characters": ["made-up-speaker-b"],
    "state": "Made-up state of arc b. Not game text.",
    "beats": [{"line": {"namespace": NS_B, "key": "1"}, "kind": "setup", "earned": True}],
}
BIBLE_ONE: dict[str, Any] = {**CHAPTER_ONE_BIBLE, "characters": [NOTES_A], "arcs": [ARC_A_ENTRY]}
BIBLE_TWO: dict[str, Any] = {"characters": [NOTES_B], "facts": [], "callbacks": [], "arcs": [ARC_B_ENTRY]}
LINE_SCHEMAS = (INTENT_SCHEMA, DRAFT_SCHEMA, JUDGE_SCHEMA, CROSS_SCHEMA)
CAST_A = {**NOTES_A, "chapter": 1}
CAST_B = {**NOTES_B, "chapter": 2}
CAST_A_NOTES = {"chapter": 1, "gender": "made-up-gender-a", "notes": NOTES_A["notes"]}
CAST_B_NOTES = {"chapter": 2, "gender": "made-up-gender-b", "notes": NOTES_B["notes"]}


def _context_of(parts: Sequence[str]) -> dict[str, Any]:
    return json.loads(_part(parts, "Arc context:\n"))


def test_line_stages_receive_the_arcs_of_the_scene_speakers_and_the_target_speakers_notes(tmp_path: Path) -> None:
    claude = _claude()
    claude.bible = {1: BIBLE_ONE, 2: BIBLE_TWO}
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    expected_arcs = {NS_A: [ARC_A], NS_B: [ARC_B], NS_C: []}
    expected_notes = {(NS_A, "1"): [CAST_A_NOTES], (NS_A, "3"): [CAST_A_NOTES], (NS_B, "1"): [CAST_B_NOTES]}
    line_calls = [(schema, parts) for _, parts, schema in claude.calls if schema in LINE_SCHEMAS]

    covered = {(LINE_SCHEMAS.index(schema), target) for schema, parts in line_calls for target, _ in _line_targets(parts)}
    # Cross-judge runs only on the callback line, so the second scene's line reaches intent, draft and judge alone.
    assert {(index, (NS_A, "1")) for index in range(len(LINE_SCHEMAS))} | {(index, (NS_B, "1")) for index in range(3)} <= covered
    for _, parts in line_calls:
        for target, context in _line_targets(parts):
            assert [arc["id"] for arc in context["arcs"]] == expected_arcs[target[0]]
            assert context["characters"] == expected_notes.get(target, [])


def test_screen_receives_the_scene_cast_entries_of_its_speakers(tmp_path: Path) -> None:
    claude = _claude()
    claude.bible = {1: BIBLE_ONE, 2: BIBLE_TWO}
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    screens = [parts for _, parts, schema in claude.calls if schema is SCREEN_SCHEMA]
    scene_a = next(parts for parts in screens if any(line["namespace"] == NS_A for line in json.loads(_part(parts, "Scene lines:\n"))))
    scene_b = next(parts for parts in screens if any(line["namespace"] == NS_B for line in json.loads(_part(parts, "Scene lines:\n"))))

    assert json.loads(_part(scene_a, "Cast:\n")) == [CAST_A]
    assert json.loads(_part(scene_b, "Cast:\n")) == [CAST_B]


def test_rescreen_receives_the_scene_cast(tmp_path: Path) -> None:
    scene = Scene(
        "made-up-scene-recheck",
        (_line(NS_A, "1", "made-up-speaker-a", EN_A1, 1), _line(NS_A, "3", "made-up-speaker-a", EN_A3, 1)),
    )
    claude = _claude()
    claude.bible = {1: BIBLE_ONE}
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), scenes=(scene,))
    rescreens = [parts for _, parts, schema in claude.calls if schema is RESCREEN_SCHEMA]

    assert rescreens
    for parts in rescreens:
        assert json.loads(_part(parts, "Cast:\n")) == [CAST_A]


# Made-up coherence scene: two flagged lines, both shipping unless the coherence check reverts them. Not game text.
EN_D1 = "Made-up line one of scene d. Not game text."
EN_D2 = "Made-up line two of scene d. Not game text."
OPUS_D1 = "Made-up opus rewrite of d one. Not game text."
OPUS_D2 = "Made-up opus rewrite of d two. Not game text."
D1 = (NS_C, "2")
D2 = (NS_C, "3")
SCENE_D = Scene("made-up-scene-d", (_line(NS_C, "2", None, EN_D1, 3), _line(NS_C, "3", None, EN_D2, 3)))


def _coherence_claude() -> FakeClaude:
    claude = FakeClaude()
    claude.flags = {D1: [ALPHA], D2: [ALPHA]}
    claude.drafts = {D1: [OPUS_D1], D2: [OPUS_D2]}
    return claude


def _verdict(coherent: bool, *refs: tuple[str, str]) -> dict[str, Any]:
    return {"coherent": coherent, "revert": [{"namespace": namespace, "key": key} for namespace, key in refs]}


def _coherence_calls(claude: FakeClaude) -> list[list[str]]:
    return [parts for _, parts, schema in claude.calls if schema is coherence.SCHEMA and not _is_scene_judge(parts)]


def test_scene_with_two_changes_makes_one_coherence_call_and_keeps_both_when_coherent(tmp_path: Path) -> None:
    claude = _coherence_claude()
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(SCENE_D,))
    assert len(_coherence_calls(claude)) == 1
    assert outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "changed", None, "opus"),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "changed", None, "opus"),
    ]
    assert _dialog_entries(tmp_path) == {D1, D2}


def test_line_named_on_both_passes_reverts_to_gate_failed_scene_and_its_sibling_ships(tmp_path: Path) -> None:
    claude = _coherence_claude()
    claude.coherence = [_verdict(False, D1), _verdict(False, D1)]
    printed: list[str] = []
    outcomes = _run(tmp_path, claude, printed, jev=None, scenes=(SCENE_D,))
    recheck = _coherence_calls(claude)[1]
    assert json.loads(_part(recheck, "Proposed reverts:\n")) == [{"namespace": NS_C, "key": "2"}]
    assert outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "gate_failed", "scene", None),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "changed", None, "opus"),
    ]
    assert _dialog_entries(tmp_path) == {D2}
    assert printed == [
        "scene 1/1 d19f5402: 1 changed, 0 kept for plot, 1 gate failed, 0 unfinished, 0 untouched",
        "total: 1 changed, 0 kept for plot, 1 gate failed, 0 unfinished, 0 untouched",
    ]


def test_line_named_only_on_the_first_pass_ships(tmp_path: Path) -> None:
    claude = _coherence_claude()
    claude.coherence = [_verdict(False, D1), _verdict(False)]
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(SCENE_D,))
    assert len(_coherence_calls(claude)) == 2
    assert outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "changed", None, "opus"),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "changed", None, "opus"),
    ]
    assert _dialog_entries(tmp_path) == {D1, D2}


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up coherence failure. Not game text."), Rejected("Made-up coherence rejection. Not game text.")],
)
def test_coherence_failure_leaves_every_changed_line_of_the_scene_unfinished(tmp_path: Path, failure: Unfinished | Rejected) -> None:
    claude = _coherence_claude()
    claude.coherence = [failure]
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(SCENE_D,))
    assert outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "unfinished", None, None),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "unfinished", None, None),
    ]
    assert _dialog_entries(tmp_path) == set()


def test_scene_with_one_change_makes_no_coherence_call(tmp_path: Path) -> None:
    claude = _coherence_claude()
    claude.flags = {D1: [ALPHA]}
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(SCENE_D,))
    assert _coherence_calls(claude) == []
    assert outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "changed", None, "opus"),
        Outcome(NS_C, "3", 3, None, (), "untouched", None, None),
    ]


# Made-up arc scenes: one arc character speaks a changed line in each scene, and a second arc has no lines. Not game text.
ARC_C = "made-up-arc-c"
ARC_C_ENTRY = {"id": ARC_C, "characters": ["made-up-speaker-c"], "state": "Made-up state of arc c. Not game text.", "beats": []}
ARC_BIBLE: dict[str, Any] = {**EMPTY_BIBLE, "arcs": [ARC_A_ENTRY, ARC_C_ENTRY]}
SCENE_X = Scene("made-up-scene-x", (_line(NS_A, "1", "made-up-speaker-a", EN_A1, 1), _line(NS_A, "2", "made-up-speaker-c", EN_A2, 1)))
SCENE_Y = Scene("made-up-scene-y", (_line(NS_B, "1", "made-up-speaker-a", EN_B1, 2),))
ARC_SCENES = (SCENE_X, SCENE_Y)
ARC_REFS = [(NS_A, "1"), (NS_B, "1")]


def _arc_claude() -> FakeClaude:
    claude = FakeClaude()
    claude.bible = {1: ARC_BIBLE}
    claude.flags = {(NS_A, "1"): [ALPHA], (NS_B, "1"): [ALPHA]}
    claude.drafts = {(NS_A, "1"): [OPUS_A1], (NS_B, "1"): [OPUS_B1]}
    return claude


def _arc_verdict(holds: bool, *refs: tuple[str, str]) -> dict[str, Any]:
    return {"holds": holds, "revert": [{"namespace": namespace, "key": key} for namespace, key in refs]}


def _arc_calls(claude: FakeClaude) -> list[list[str]]:
    return [parts for _, parts, schema in claude.calls if schema is arcs.SCHEMA]


def _first_pass_arc_calls(claude: FakeClaude) -> list[list[str]]:
    return [parts for parts in _arc_calls(claude) if not _is_scene_arc(parts)]


def _scene_arc_calls(claude: FakeClaude) -> list[list[str]]:
    return [parts for parts in _arc_calls(claude) if _is_scene_arc(parts)]


def _arc_refs(parts: Sequence[str]) -> list[tuple[str, str]]:
    return [(line["namespace"], line["key"]) for line in json.loads(_part(parts, "Arc lines:\n"))]


def _outcome_of(outcomes: Sequence[Outcome], ref: tuple[str, str]) -> Outcome:
    return next(o for o in outcomes if (o.namespace, o.key) == ref)


def test_arc_judge_runs_once_with_every_changed_line_of_the_arc_in_story_order(tmp_path: Path) -> None:
    claude = _arc_claude()
    _run(tmp_path, claude, [], jev=None, scenes=ARC_SCENES)
    calls = _arc_calls(claude)
    assert len(calls) == 1
    assert _arc_refs(calls[0]) == ARC_REFS


def test_no_arc_and_no_changed_arc_line_makes_no_arc_call(tmp_path: Path) -> None:
    claude = _claude()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert _arc_calls(claude) == []


# Made-up scenes whose lines share one English text, and a made-up table scene for the scene pass. Not game text.
EN_SHARED = "Made-up shared English. Not game text."
SHARED_X = Scene("made-up-scene-x", (_line(NS_A, "1", "made-up-speaker-a", EN_SHARED, 1), _line(NS_A, "2", "made-up-speaker-c", EN_A2, 1)))
SHARED_Y = Scene("made-up-scene-y", (_line(NS_B, "1", "made-up-speaker-a", EN_SHARED, 2),))
SHARED_LATE = Scene("made-up-scene-late-shared", (_line(NS_C, "1", "made-up-speaker-c", EN_SHARED, 3),))
SHARED_ARC_SCENES = (SHARED_X, SHARED_Y)
SHARED_TABLE = Scene(
    "made-up-scene-shared",
    (_line(NS_A, "1", None, EN_SHARED, 1), _line(NS_A, "2", None, EN_SHARED, 1)),
    table=True,
)


def _shared_arc_claude() -> FakeClaude:
    # OPUS_A1 loses to its voiced text, so the first occurrence ships VOICE_A1 with the voice writer. OPUS_B1 ships with the opus writer.
    claude = _arc_claude()
    claude.judgments = {OPUS_A1: _judgment(restores_japanese=False)}
    return claude


def test_identical_english_changed_in_two_scenes_ships_the_earlier_rewrite_and_writer_under_both_keys(tmp_path: Path) -> None:
    outcomes = _run(tmp_path, _shared_arc_claude(), [], jev=None, scenes=SHARED_ARC_SCENES)
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice")
    assert _outcome_of(outcomes, (NS_B, "1")) == Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice")
    tables = _dialog_tables(tmp_path)
    assert tables[(NS_A, "1")]["text"] == VOICE_A1
    assert tables[(NS_A, "1")]["writer"] == "voice"
    assert tables[(NS_B, "1")]["text"] == VOICE_A1
    assert tables[(NS_B, "1")]["writer"] == "voice"
    assert tables[(NS_B, "1")]["reason"] == [ALPHA]


def test_arc_judge_prompt_carries_the_unified_rewrite_under_both_keys(tmp_path: Path) -> None:
    claude = _shared_arc_claude()
    _run(tmp_path, claude, [], jev=None, scenes=SHARED_ARC_SCENES)
    [parts] = _first_pass_arc_calls(claude)
    rewrites = {(line["namespace"], line["key"]): line["rewrite"] for line in json.loads(_part(parts, "Arc lines:\n"))}
    assert rewrites == {(NS_A, "1"): VOICE_A1, (NS_B, "1"): VOICE_A1}


def test_a_third_occurrence_no_stage_changed_stays_untouched_with_no_dialog_entry(tmp_path: Path) -> None:
    outcomes = _run(tmp_path, _shared_arc_claude(), [], jev=None, scenes=(*SHARED_ARC_SCENES, SHARED_LATE))
    assert _outcome_of(outcomes, (NS_C, "1")) == Outcome(NS_C, "1", 3, "made-up-speaker-c", (), "untouched", None, None)
    tables = _dialog_tables(tmp_path)
    assert (NS_C, "1") not in tables
    assert tables[(NS_B, "1")]["text"] == VOICE_A1


def test_scene_pass_change_sharing_english_with_a_line_pass_change_takes_its_rewrite_and_keeps_its_plan(tmp_path: Path) -> None:
    claude = FakeClaude()
    claude.bible = {}
    claude.flags = {(NS_A, "1"): [ALPHA]}
    claude.drafts = {(NS_A, "1"): [OPUS_A1], (NS_A, "2"): [OPUS_S2]}
    claude.scene_screen = {"patterns": [{"id": PATTERN_ONE, "carriers": [{"namespace": NS_A, "key": "2"}]}]}
    claude.scene_plan = {
        "keep": False,
        "keep_reason": "none",
        "lines": [
            {"namespace": NS_A, "key": "2", "intent": "Made-up scene intent. Not game text.", "carrier": EN_SHARED, "required": True}
        ],
    }
    outcomes = _scene_run(tmp_path, claude, scene=SHARED_TABLE)
    plan = scene_pass.plan_id(SHARED_TABLE.key, [(NS_A, "2")])
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, None, (ALPHA,), "changed", None, "opus")
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, (PATTERN_ONE,), "changed", None, "opus", None, plan)
    tables = _dialog_tables(tmp_path)
    assert tables[(NS_A, "2")]["text"] == OPUS_A1
    assert tables[(NS_A, "2")]["writer"] == "opus"


def test_distinct_english_changed_in_two_scenes_keeps_each_own_rewrite_and_writer(tmp_path: Path) -> None:
    _run(tmp_path, _shared_arc_claude(), [], jev=None, scenes=ARC_SCENES)
    tables = _dialog_tables(tmp_path)
    assert tables[(NS_A, "1")]["text"] == VOICE_A1
    assert tables[(NS_A, "1")]["writer"] == "voice"
    assert tables[(NS_B, "1")]["text"] == OPUS_B1
    assert tables[(NS_B, "1")]["writer"] == "opus"


def test_unification_makes_no_claude_call_so_a_shared_english_run_calls_as_often_as_a_distinct_one(tmp_path: Path) -> None:
    shared = _shared_arc_claude()
    _run(tmp_path / "shared", shared, [], jev=None, scenes=SHARED_ARC_SCENES)
    distinct = _shared_arc_claude()
    _run(tmp_path / "distinct", distinct, [], jev=None, scenes=ARC_SCENES)
    assert len(shared.calls) == len(distinct.calls)
    assert len(_arc_calls(shared)) == len(_arc_calls(distinct)) == 1


# Made-up scenes for one key in two scenes: untouched in the first, changed in the last, with another key sharing its English changed between. Not game text.
EN_REPEAT = "Made-up line of the repeated scenes. Not game text."
OPUS_REPEAT_LATE = "Made-up opus rewrite of the late scene. Not game text."
OPUS_REPEAT_MIDDLE = "Made-up opus rewrite of the middle scene. Not game text."
VOICE_REPEAT_MIDDLE = "Made-up voiced rewrite of the middle scene. Not game text."
REPEAT_EARLY = Scene(
    "made-up-scene-repeat-early", (_line(NS_A, "1", "made-up-speaker-a", EN_REPEAT, 1), _line(NS_A, "2", "made-up-speaker-a", EN_A2, 1))
)
REPEAT_MIDDLE = Scene("made-up-scene-repeat-middle", (_line(NS_B, "1", "made-up-speaker-a", EN_REPEAT, 2),))
REPEAT_LATE = Scene("made-up-scene-repeat-late", (_line(NS_A, "1", "made-up-speaker-a", EN_REPEAT, 3),))
REPEAT_SCENES = (REPEAT_EARLY, REPEAT_MIDDLE, REPEAT_LATE)


class _FlagsByScene(FakeClaude):
    """Screens each scene from the flags keyed by the exact set of line refs in that scene, so one key can be flagged in one scene and not in another. Not game text."""

    def __init__(self, flags_by_scene: Mapping[frozenset[tuple[str, str]], Mapping[tuple[str, str], list[str]]]) -> None:
        super().__init__()
        self.flags_by_scene = flags_by_scene

    def _screen(self, parts: Sequence[str]) -> dict[str, Any]:
        lines = json.loads(_part(parts, "Scene lines:\n"))
        flags = self.flags_by_scene.get(frozenset((line["namespace"], line["key"]) for line in lines), {})
        return {"flagged": [{"namespace": namespace, "key": key, "reasons": reasons} for (namespace, key), reasons in flags.items()]}


def _repeat_run(tmp_path: Path) -> list[Outcome]:
    claude = _FlagsByScene(
        {
            frozenset({(NS_B, "1")}): {(NS_B, "1"): [ALPHA]},
            frozenset({(NS_A, "1")}): {(NS_A, "1"): [ALPHA]},
        }
    )
    claude.drafts = {(NS_A, "1"): [OPUS_REPEAT_LATE], (NS_B, "1"): [OPUS_REPEAT_MIDDLE]}
    # The middle line's opus draft loses to its voiced text, so the voice writer ships. The late line's opus draft wins.
    claude.judgments = {OPUS_REPEAT_MIDDLE: _judgment(restores_japanese=False)}
    return _run(
        tmp_path,
        claude,
        [],
        jev=None,
        scenes=REPEAT_SCENES,
        renderer=FakeRenderer({OPUS_REPEAT_MIDDLE: VOICE_REPEAT_MIDDLE}),
    )


def test_untouched_occurrence_of_a_key_changed_in_a_later_scene_keeps_no_writer(tmp_path: Path) -> None:
    outcomes = _repeat_run(tmp_path)
    assert outcomes[:2] == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (), "untouched", None, None),
        Outcome(NS_A, "2", 1, "made-up-speaker-a", (), "untouched", None, None),
    ]


def test_changed_occurrences_of_a_repeated_english_take_the_earliest_changed_rewrite_and_writer(tmp_path: Path) -> None:
    outcomes = _repeat_run(tmp_path)
    assert outcomes[2:] == [
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
        Outcome(NS_A, "1", 3, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
    ]
    tables = _dialog_tables(tmp_path)
    assert tables[(NS_B, "1")]["text"] == VOICE_REPEAT_MIDDLE
    assert tables[(NS_B, "1")]["writer"] == "voice"
    assert tables[(NS_A, "1")]["text"] == VOICE_REPEAT_MIDDLE
    assert tables[(NS_A, "1")]["writer"] == "voice"


def test_line_named_on_both_arc_passes_ends_gate_failed_arc_with_no_dialog_entry(tmp_path: Path) -> None:
    claude = _arc_claude()
    claude.arc = [_arc_verdict(False, (NS_A, "1")), _arc_verdict(False, (NS_A, "1"))]
    printed: list[str] = []
    outcomes = _run(tmp_path, claude, printed, jev=None, scenes=ARC_SCENES)
    assert json.loads(_part(_arc_calls(claude)[1], "Proposed reverts:\n")) == [{"namespace": NS_A, "key": "1"}]
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None)
    assert _outcome_of(outcomes, (NS_B, "1")).outcome == "changed"
    assert _dialog_entries(tmp_path) == {(NS_B, "1")}
    # The two scenes finish in either order, so only the total line has a fixed place.
    assert sorted(printed[:-1]) == [
        "scene 1/2 d68762f0: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched",
        "scene 2/2 c6863ef3: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched",
    ]
    assert printed[-1] == "total: 1 changed, 0 kept for plot, 1 gate failed, 0 unfinished, 1 untouched"


def test_unfinished_arc_judge_leaves_its_changed_lines_unfinished_with_no_dialog_entry(tmp_path: Path) -> None:
    claude = _arc_claude()
    claude.arc = [Unfinished("Made-up arc failure. Not game text.")]
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=ARC_SCENES)
    assert outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "unfinished", None, None),
        Outcome(NS_A, "2", 1, "made-up-speaker-c", (), "untouched", None, None),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "unfinished", None, None),
    ]
    assert _dialog_entries(tmp_path) == set()


def test_arcs_json_counts_the_arcs_and_the_arcs_with_changes_left_after_the_arc_judge(tmp_path: Path) -> None:
    _run(tmp_path, _arc_claude(), [], jev=None, scenes=ARC_SCENES)
    assert json.loads((tmp_path / "work" / LENS / report.ARCS).read_text(encoding="utf-8")) == {"arcs": 2, "with_changes": 1}


def test_arcs_json_and_the_report_line_show_the_arcs_left_with_changes_after_reverts(
    lens_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _arc_claude()
    claude.arc = [_arc_verdict(False, (NS_A, "1"), (NS_B, "1")), _arc_verdict(False, (NS_A, "1"), (NS_B, "1"))]
    run(
        lens_repo,
        LENS_OBJ,
        Store(lens_repo / "work" / LENS),
        ARC_SCENES,
        classifier=FakeClassifier(),
        renderer=FakeRenderer({}),
        run_id=RUN,
        jev=None,
        batch=claude.start_batch,
        scene_table=False,
        out=lambda _: None,
    )
    assert json.loads((lens_repo / "work" / LENS / report.ARCS).read_text(encoding="utf-8")) == {"arcs": 2, "with_changes": 0}
    # Made-up empty scene table for ffrd report. Not game text.
    vanilla = lens_repo / "vanilla"
    vanilla.mkdir()
    (vanilla / "BUILDID").write_text("424242\n")
    (vanilla / "scenes.json").write_text(json.dumps({"build": "424242", "lines": []}), encoding="utf-8")
    monkeypatch.setenv("FFR_VANILLA", str(vanilla))
    report.cmd_report(argparse.Namespace(lens=LENS))
    assert "arcs: 2, 0 with changes" in capsys.readouterr().out.splitlines()


def test_line_changed_in_two_scenes_is_refused_by_run_and_nothing_is_written(tmp_path: Path) -> None:
    # Made-up second scene that carries scene a's first line. Not game text.
    shared = Scene(SENTINEL, (_line(NS_A, "1", "made-up-speaker-a", EN_A1, 1),))
    with pytest.raises(Fail) as raised:
        _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH), scenes=(SCENE_A, shared))
    message = str(raised.value)
    assert message == "line eadc9f7a: more than one change for this line"
    assert "Zorbleth" not in message
    assert NS_A not in message
    assert not any(text in message for text in (EN_A1, VOICE_A1, OPUS_A1))
    work = tmp_path / "work" / LENS
    assert not (work / report.OUTCOMES).exists()
    assert not (work / report.ARCS).exists()
    assert not (work / report.RECALL).exists()
    assert not (tmp_path / "repo" / "dialog" / LENS).exists()


# Made-up unplaced lines in two more namespaces, one spoken by the third arc character. Not game text.
NS_D = "ST_SubEv_005_01"
NS_E = "ST_SubEv_006_01"
EN_E1 = "Made-up unplaced line one. Not game text."
EN_E2 = "Made-up unplaced line two. Not game text."
OPUS_E1 = "Made-up opus rewrite of e one. Not game text."
SCENE_E = Scene("made-up-scene-e", (_line(NS_D, "1", "made-up-speaker-c", EN_E1, None), _line(NS_E, "1", None, EN_E2, None)))
THREE_ARC_BIBLE: dict[str, Any] = {**EMPTY_BIBLE, "arcs": [ARC_A_ENTRY, ARC_B_ENTRY, ARC_C_ENTRY]}
SLOW = 0.1


@pytest.fixture(autouse=True)
def no_live_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    # A test that reaches the real claude.ask without installing its own fake call fails here instead of running cswap.
    def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a test reached the live Claude call")

    monkeypatch.setattr(claude_module, "call", refuse)


@pytest.fixture(autouse=True)
def restore_concurrency() -> Iterator[None]:
    # cmd_run sets the process-wide Claude limit, so every test in this file puts the default back.
    yield
    claude_module.set_concurrency(claude_module.DEFAULT_CONCURRENCY)


def _stage(schema: Mapping[str, Any]) -> str:
    if schema is bible.SCHEMA or schema is bible.SETUP_SCHEMA:
        return "bible"
    if schema is arcs.SCHEMA:
        return "arc"
    return "scene"


class SlowCalls:
    """Stands in for claude.call. Answers from FakeClaude after a delay the prompt picks, and counts the calls in flight. Not game text."""

    def __init__(self, claude: FakeClaude, delay: Callable[[Sequence[str]], float]) -> None:
        self.claude = claude
        self.delay = delay
        self.lock = threading.Lock()
        self.in_flight = 0
        self.highest = 0
        self.peak: dict[str, int] = {}
        self.calls = 0

    def __call__(
        self, _account: int, model: str, parts: Sequence[str], schema: Mapping[str, Any]
    ) -> dict[str, Any] | Unfinished | Rejected:
        stage = _stage(schema)
        with self.lock:
            self.in_flight += 1
            self.calls += 1
            self.highest = max(self.highest, self.in_flight)
            self.peak[stage] = max(self.peak.get(stage, 0), self.in_flight)
        try:
            time.sleep(self.delay(parts))
            return self.claude.ask(model, parts, schema)
        finally:
            with self.lock:
                self.in_flight -= 1


def _delay_by_prompt(text: str, *, named: bool) -> Callable[[Sequence[str]], float]:
    """Delays each prompt that names the English line when named is true, and each prompt that does not when it is false. Not game text."""

    def delay(parts: Sequence[str]) -> float:
        return SLOW if any(text in part for part in parts) == named else 0.0

    return delay


def _through_claude_limit() -> Ask:
    return stages.Batch(choose=lambda: 1, ask=claude_module.ask).ask


# A call that waits this long at a barrier for its partners fails, so a call that runs alone never hangs the suite. Not game text.
MEET_TIMEOUT = 5


class MeetingCalls:
    """Stands in for claude.call. Calls with the given schema wait at the barrier before they answer from FakeClaude. Not game text."""

    def __init__(self, claude: FakeClaude, schema: Mapping[str, Any], meet: threading.Barrier) -> None:
        self.claude = claude
        self.schema = schema
        self.meet = meet

    def __call__(
        self, _account: int, model: str, parts: Sequence[str], schema: Mapping[str, Any]
    ) -> dict[str, Any] | Unfinished | Rejected:
        if schema is self.schema:
            self.meet.wait()
        return self.claude.ask(model, parts, schema)


class MeetingJev(FakeJev):
    """Stands in for the Jev triage call, which waits at the barrier before it answers. Not game text."""

    def __init__(self, answer: dict[str, float] | Unfinished, meet: threading.Barrier) -> None:
        super().__init__(answer)
        self.meet = meet

    def __call__(self, scene: str, questions: Sequence[TriageQuestion]) -> dict[str, float] | Unfinished:
        self.meet.wait()
        return super().__call__(scene, questions)


class MeetingRenderer(FakeRenderer):
    """Stands in for the voice render, which waits at the barrier before it answers. Not game text."""

    def __init__(self, voiced: Mapping[str, str], meet: threading.Barrier) -> None:
        super().__init__(voiced)
        self.meet = meet

    def render(self, speaker: str | None, text: str) -> str:
        self.meet.wait()
        return super().render(speaker, text)


def _three_arc_claude() -> FakeClaude:
    claude = _claude()
    claude.bible = {1: THREE_ARC_BIBLE}
    claude.flags[(NS_D, "1")] = [ALPHA]
    claude.drafts[(NS_D, "1")] = [OPUS_E1]
    return claude


def _scene_order(printed: Sequence[str]) -> list[str]:
    return [line.split()[2].removesuffix(":") for line in printed if line.startswith("scene ")]


def test_in_flight_calls_reach_and_never_exceed_the_limit_in_every_stage_and_beat_the_sequential_sum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _three_arc_claude()
    claude_module.set_concurrency(3)
    slow = SlowCalls(claude, lambda parts: SLOW)
    monkeypatch.setattr(claude_module, "call", slow)
    started = time.monotonic()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), scenes=(*SCENES, SCENE_E), batch=_through_claude_limit)
    elapsed = time.monotonic() - started
    assert slow.highest == 3
    assert slow.peak == {"bible": 3, "scene": 3, "arc": 3}
    # The chapter bibles and their setups run in order, so only the rest overlaps. Sequential runs take the whole sum.
    assert elapsed < slow.calls * SLOW * 0.75


def test_outputs_are_byte_identical_whatever_order_the_scenes_finish_in_and_at_one_call_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = {
        "one-at-a-time": (1, lambda parts: 0.0),
        "scene-a-finishes-last": (8, _delay_by_prompt(EN_A1, named=True)),
        "scene-a-finishes-first": (8, _delay_by_prompt(EN_A1, named=False)),
    }
    snapshots: dict[str, dict[str, bytes]] = {}
    orders: dict[str, list[str]] = {}
    for name, (limit, delay) in plans.items():
        claude = _claude()
        claude_module.set_concurrency(limit)
        monkeypatch.setattr(claude_module, "call", SlowCalls(claude, delay))
        printed: list[str] = []
        _run(tmp_path / name, claude, printed, jev=FakeJev(JEV_HIGH), batch=_through_claude_limit)
        snapshots[name] = _snapshot(tmp_path / name)
        orders[name] = _scene_order(printed)

    assert orders["one-at-a-time"] == ["54193dbc", "e4beb400", "0f594d36"]
    assert orders["scene-a-finishes-last"][-1] == "54193dbc"
    assert orders["scene-a-finishes-first"][0] == "54193dbc"
    assert f"work/{LENS}/{report.OUTCOMES}" in snapshots["one-at-a-time"]
    assert f"repo/dialog/{LENS}/chapter-01.toml" in snapshots["one-at-a-time"]
    assert snapshots["scene-a-finishes-last"] == snapshots["one-at-a-time"]
    assert snapshots["scene-a-finishes-first"] == snapshots["one-at-a-time"]


def test_a_scene_that_raises_keeps_the_other_scenes_results_and_a_rerun_calls_only_for_it(tmp_path: Path) -> None:
    claude = _claude()
    claude.raising = {(NS_B, "1")}
    # All three scenes are screening before the failure, so none is cancelled before it starts.
    claude.screen_gate = threading.Barrier(3, timeout=10)
    with pytest.raises(RuntimeError, match="Made-up scene failure"):
        _run(tmp_path / "resumed", claude, [], jev=FakeJev(JEV_HIGH))

    claude.raising = set()
    claude.screen_gate = None
    before = len(claude.calls)
    _run(tmp_path / "resumed", claude, [], jev=FakeJev(JEV_HIGH))
    rerun = [(parts, schema) for _, parts, schema in claude.calls[before:]]
    assert sum(1 for _, schema in rerun if schema is SCREEN_SCHEMA) == 1
    assert not any(EN_A1 in part or EN_C1 in part for parts, schema in rerun if schema is not arcs.SCHEMA for part in parts)

    _run(tmp_path / "clean", _claude(), [], jev=FakeJev(JEV_HIGH))
    assert _snapshot(tmp_path / "resumed") == _snapshot(tmp_path / "clean")


def test_a_transport_failed_scene_stays_unfinished_while_the_others_change(tmp_path: Path) -> None:
    claude = _claude()
    claude.screen_failure_refs = {(NS_B, "1"): Unfinished("Made-up transport failure. Not game text.")}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes == [
        EXPECTED_OUTCOMES[0],
        EXPECTED_OUTCOMES[1],
        EXPECTED_OUTCOMES[2],
        EXPECTED_OUTCOMES[3],
        Outcome(NS_B, "1", 2, "made-up-speaker-b", (), "unfinished", None, None),
        Outcome(NS_B, "2", 2, None, (), "unfinished", None, None),
        EXPECTED_OUTCOMES[6],
    ]
    assert _dialog_files(tmp_path / "repo" / "dialog" / LENS) == ["chapter-01.toml", "unattributed.toml"]


OPUS_A4 = "Made-up opus rewrite four. Not game text."
VOICE_A2 = "Made-up voiced rewrite two. Not game text."


def test_screen_and_jev_triage_of_a_scene_are_in_flight_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = _claude()
    meet = threading.Barrier(2, timeout=MEET_TIMEOUT)
    claude_module.set_concurrency(8)
    monkeypatch.setattr(claude_module, "call", MeetingCalls(claude, SCREEN_SCHEMA, meet))
    outcomes = _run(tmp_path, claude, [], jev=MeetingJev(JEV_HIGH, meet), scenes=(SCENE_A,), batch=_through_claude_limit)
    assert outcomes == EXPECTED_OUTCOMES[:4]


def test_two_rescreens_of_a_scene_are_in_flight_together_and_join_in_lens_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = _claude()
    claude.flags = {}
    claude.rescreen_flags = {(NS_A, "1"): [ALPHA], (NS_A, "2"): [BETA]}
    meet = threading.Barrier(2, timeout=MEET_TIMEOUT)
    claude_module.set_concurrency(8)
    monkeypatch.setattr(claude_module, "call", MeetingCalls(claude, RESCREEN_SCHEMA, meet))
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), scenes=(SCENE_A,), batch=_through_claude_limit)
    assert _outcome_of(outcomes, (NS_A, "1")).reasons == (ALPHA,)
    assert _outcome_of(outcomes, (NS_A, "2")).reasons == (BETA,)
    assert _recall(tmp_path) == {ALPHA: {"disagreements": 1, "confirmed": 1}, BETA: {"disagreements": 1, "confirmed": 1}}


def test_the_intents_of_a_scenes_flagged_lines_are_in_flight_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = _claude()
    meet = threading.Barrier(3, timeout=MEET_TIMEOUT)
    claude_module.set_concurrency(8)
    monkeypatch.setattr(claude_module, "call", MeetingCalls(claude, INTENT_SCHEMA, meet))
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(SCENE_A,), batch=_through_claude_limit)
    assert outcomes == EXPECTED_OUTCOMES[:4]


def test_the_judges_of_a_scenes_two_flagged_lines_are_in_flight_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = FakeClaude()
    claude.flags = {(NS_A, "2"): [BETA], (NS_A, "4"): [BETA]}
    claude.drafts = {(NS_A, "2"): [OPUS_A2], (NS_A, "4"): [OPUS_A4]}
    meet = threading.Barrier(2, timeout=MEET_TIMEOUT)
    claude_module.set_concurrency(8)
    monkeypatch.setattr(claude_module, "call", MeetingCalls(claude, JUDGE_SCHEMA, meet))
    outcomes = _run(tmp_path, claude, [], jev=None, renderer=FakeRenderer({}), scenes=(SCENE_A,), batch=_through_claude_limit)
    assert _outcome_of(outcomes, (NS_A, "2")).outcome == "changed"
    assert _outcome_of(outcomes, (NS_A, "4")).outcome == "changed"


def test_the_two_candidates_of_one_gating_round_are_judged_in_flight_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = FakeClaude()
    claude.flags = {(NS_A, "2"): [BETA]}
    claude.drafts = {(NS_A, "2"): [OPUS_A2]}
    meet = threading.Barrier(2, timeout=MEET_TIMEOUT)
    claude_module.set_concurrency(8)
    monkeypatch.setattr(claude_module, "call", MeetingCalls(claude, JUDGE_SCHEMA, meet))
    outcomes = _run(
        tmp_path, claude, [], jev=None, renderer=FakeRenderer({OPUS_A2: VOICE_A2}), scenes=(SCENE_A,), batch=_through_claude_limit
    )
    assert _outcome_of(outcomes, (NS_A, "2")).outcome == "changed"


def test_the_drafts_of_one_line_are_rendered_in_flight_together(tmp_path: Path) -> None:
    claude = FakeClaude()
    claude.flags = {(NS_A, "2"): [BETA]}
    claude.drafts = {(NS_A, "2"): [OPUS_A2, OPUS_A3]}
    meet = threading.Barrier(2, timeout=MEET_TIMEOUT)
    claude_module.set_concurrency(8)
    outcomes = _run(tmp_path, claude, [], jev=None, renderer=MeetingRenderer({}, meet), scenes=(SCENE_A,))
    assert _outcome_of(outcomes, (NS_A, "2")).outcome == "changed"


def test_each_candidate_text_is_classified_and_judged_once_per_line_even_when_a_retry_repeats_it(tmp_path: Path) -> None:
    claude = _claude()
    classifier = FakeClassifier()
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH), classifier=classifier)
    judged = _judged(claude, JUDGE_SCHEMA)
    assert judged.count(((NS_A, "2"), OPUS_A2)) == 1
    assert len(judged) == len(set(judged))
    assert len(classifier.scored) == len(set(classifier.scored))


def test_concurrency_option_defaults_to_eight() -> None:
    assert _parse(["run", "--lens", LENS]).concurrency == 8


@pytest.mark.parametrize("value", ["0", "-3"])
def test_concurrency_below_one_is_refused_before_any_work(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    def unexpected() -> Bindings:
        raise AssertionError("the scenes were read before the limit was checked")

    monkeypatch.setattr(stages, "bindings", unexpected)
    args = _parse(["run", "--lens", LENS, "--concurrency", value])
    with pytest.raises(Fail, match="concurrency must be at least 1"):
        args.fn(args)
    assert claude_module.concurrency() == claude_module.DEFAULT_CONCURRENCY


def test_concurrency_option_sets_the_claude_limit_before_the_scenes_are_read(lens_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = _claude()
    _bind(monkeypatch, claude, FakeJev(JEV_HIGH))
    bound = stages.bindings
    limits: list[int] = []

    def read_limit() -> Bindings:
        limits.append(claude_module.concurrency())
        return bound()

    monkeypatch.setattr(stages, "bindings", read_limit)
    args = _parse(["run", "--lens", LENS, "--concurrency", "3"])
    args.fn(args)
    assert limits == [3]
    assert claude_module.concurrency() == 3


def test_readme_documents_the_concurrency_option_in_the_run_row() -> None:
    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if line.startswith("| `ffrd run --lens"))
    assert "[--concurrency N]" in row


# Made-up chapter-order scene: an unplaced line, a chapter-1 line and a chapter-2 line. Not game text.
NS_MIX = "ST_Mix_001"
EN_MIX1 = "Made-up mixed line one, unplaced. Not game text."
EN_MIX2 = "Made-up mixed line two, chapter one. Not game text."
EN_MIX3 = "Made-up mixed line three, chapter two. Not game text."
OPUS_MIX1 = "Made-up opus rewrite of mix one. Not game text."
OPUS_MIX2 = "Made-up opus rewrite of mix two. Not game text."
OPUS_MIX3 = "Made-up opus rewrite of mix three. Not game text."
MIXED = Scene(
    "made-up-scene-mixed",
    (
        _line(NS_MIX, "1", None, EN_MIX1, None),
        _line(NS_MIX, "2", "made-up-speaker-a", EN_MIX2, 1),
        _line(NS_MIX, "3", None, EN_MIX3, 2),
    ),
)
EXPECTED_PROVISIONAL = (
    "chapter 1: 1 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "  gate minimal_change: 1\n"
    "  scene eligible: 0\n"
    "  scene plans: 0\n"
    "  scene planned lines: 0 (required 0)\n"
    "  scene changed lines: 0\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 0, median 0\n"
    "total: 1 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "  gate minimal_change: 1\n"
    "  scene eligible: 0\n"
    "  scene plans: 0\n"
    "  scene planned lines: 0 (required 0)\n"
    "  scene changed lines: 0\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 0, median 0\n"
)
EXPECTED_PROVISIONAL_ARC = (
    "chapter 1: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "  scene eligible: 0\n"
    "  scene plans: 0\n"
    "  scene planned lines: 0 (required 0)\n"
    "  scene changed lines: 0\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 0, median 0\n"
    "total: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "  scene eligible: 0\n"
    "  scene plans: 0\n"
    "  scene planned lines: 0 (required 0)\n"
    "  scene changed lines: 0\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 0, median 0\n"
)
EXPECTED_PROVISIONAL_MIXED = (
    "chapter 1: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "  scene eligible: 0\n"
    "  scene plans: 0\n"
    "  scene planned lines: 0 (required 0)\n"
    "  scene changed lines: 0\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 0, median 0\n"
    "total: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "  scene eligible: 0\n"
    "  scene plans: 0\n"
    "  scene planned lines: 0 (required 0)\n"
    "  scene changed lines: 0\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 0, median 0\n"
)


def _add_mixed(claude: FakeClaude) -> FakeClaude:
    claude.flags.update({(NS_MIX, "1"): [ALPHA], (NS_MIX, "2"): [ALPHA], (NS_MIX, "3"): [ALPHA]})
    claude.drafts.update({(NS_MIX, "1"): [OPUS_MIX1], (NS_MIX, "2"): [OPUS_MIX2], (NS_MIX, "3"): [OPUS_MIX3]})
    return claude


def _c01_path(tmp_path: Path) -> Path:
    return tmp_path / "repo" / "reports" / f"{LENS}-c01.txt"


def test_scenes_run_chapter_one_first_then_ascending_chapters_and_chapterless_last(tmp_path: Path) -> None:
    claude_module.set_concurrency(1)
    printed: list[str] = []
    _run(tmp_path, _add_mixed(_claude()), printed, jev=FakeJev(JEV_HIGH), scenes=(SCENE_C, SCENE_B, MIXED, SCENE_A), scene_table=True)
    assert _scene_order(printed) == ["7a5aa354", "54193dbc", "e4beb400", "0f594d36"]


def test_provisional_file_holds_chapter_one_counts_in_report_format(tmp_path: Path) -> None:
    _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH))
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == EXPECTED_PROVISIONAL


def test_provisional_file_counts_an_intent_kept_line_under_its_keep_reason(tmp_path: Path) -> None:
    claude = _claude()
    claude.keeps = {(NS_A, "1"): "earned"}
    _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    # The kept line is in chapter 1, so its keep reason shows in the chapter block and in the total.
    assert _c01_path(tmp_path).read_text(encoding="utf-8").splitlines().count("  kept earned: 1") == 2


def test_provisional_file_is_written_while_a_later_chapter_scene_still_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude_module.set_concurrency(2)
    claude = _claude()
    probe = _c01_path(tmp_path)
    seen: list[bool] = []
    ask = claude.ask

    def probed(model: str, parts: Sequence[str], schema: Mapping[str, Any]) -> dict[str, Any] | Unfinished | Rejected:
        if schema is SCREEN_SCHEMA and any(line["namespace"] == NS_B for line in json.loads(_part(parts, "Scene lines:\n"))):
            # The chapter-two screen waits for the chapter-one file. A file written only after every scene finishes is not there yet.
            deadline = time.monotonic() + MEET_TIMEOUT
            while not probe.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            seen.append(probe.exists())
        return ask(model, parts, schema)

    monkeypatch.setattr(claude, "ask", probed)
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert seen == [True]
    assert outcomes == EXPECTED_OUTCOMES
    assert probe.read_text(encoding="utf-8") == EXPECTED_PROVISIONAL


def test_provisional_file_ignores_an_arc_revert_of_a_chapter_one_change(tmp_path: Path) -> None:
    claude = _arc_claude()
    claude.arc = [_arc_verdict(False, (NS_A, "1")), _arc_verdict(False, (NS_A, "1"))]
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=ARC_SCENES)
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None)
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == EXPECTED_PROVISIONAL_ARC


def test_provisional_file_counts_only_chapter_one_lines_of_a_mixed_scene(tmp_path: Path) -> None:
    claude = FakeClaude()
    # The default chapter-one bible names lines of other scenes, so this scene gets an empty one.
    claude.bible = {1: EMPTY_BIBLE}
    _run(tmp_path, _add_mixed(claude), [], jev=None, scenes=(MIXED,), scene_table=True)
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == EXPECTED_PROVISIONAL_MIXED


def test_provisional_file_waits_for_every_scene_holding_a_chapter_one_line(tmp_path: Path) -> None:
    _run(tmp_path, _add_mixed(_claude()), [], jev=None, scenes=(SCENE_A, MIXED), scene_table=True)
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == (
        "chapter 1: 2 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched\n"
        f"  reason {ALPHA}: 2\n"
        "  character 1: 2\n"
        "  gate minimal_change: 1\n"
        "  scene eligible: 0\n"
        "  scene plans: 0\n"
        "  scene planned lines: 0 (required 0)\n"
        "  scene changed lines: 0\n"
        "  scene replanned: 0\n"
        "  scene reverted lines: 0\n"
        "  scene changed per scene: max 0, median 0\n"
        "total: 2 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched\n"
        f"  reason {ALPHA}: 2\n"
        "  character 1: 2\n"
        "  gate minimal_change: 1\n"
        "  scene eligible: 0\n"
        "  scene plans: 0\n"
        "  scene planned lines: 0 (required 0)\n"
        "  scene changed lines: 0\n"
        "  scene replanned: 0\n"
        "  scene reverted lines: 0\n"
        "  scene changed per scene: max 0, median 0\n"
    )


def test_no_provisional_file_when_the_run_has_no_chapter_one_line(tmp_path: Path) -> None:
    _run(tmp_path, _claude(), [], jev=None, scenes=(SCENE_B, SCENE_C))
    assert not _c01_path(tmp_path).exists()


def test_through_one_runs_only_chapter_one_and_writes_its_outputs(tmp_path: Path) -> None:
    claude = _claude()
    jev = FakeJev(JEV_HIGH)
    printed: list[str] = []
    outcomes = _run(tmp_path, claude, printed, jev=jev, through=1)
    assert outcomes == EXPECTED_OUTCOMES[:4]
    assert not any(text in prompt for prompt in _all_prompts(claude, jev) for text in (NS_B, NS_C, NS_OUT, EN_B1, EN_B2, EN_C1, EN_OUT))
    assert len(jev.calls) == 1
    assert _scene_order(printed) == ["54193dbc"]
    assert _dialog_files(tmp_path / "repo" / "dialog" / LENS) == ["chapter-01.toml"]
    assert len((tmp_path / "work" / LENS / report.OUTCOMES).read_text(encoding="utf-8").splitlines()) == 4
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == EXPECTED_PROVISIONAL


def test_through_one_drops_the_chapterless_and_later_chapter_lines_of_a_kept_scene(tmp_path: Path) -> None:
    claude = FakeClaude()
    claude.bible = {1: EMPTY_BIBLE}
    outcomes = _run(tmp_path, _add_mixed(claude), [], jev=None, scenes=(MIXED,), scene_table=True, through=1)
    assert [(o.namespace, o.key) for o in outcomes] == [(NS_MIX, "2")]
    assert not any(text in part for _, parts, _ in claude.calls for part in parts for text in (EN_MIX1, EN_MIX3))


@pytest.mark.parametrize("limit", [0, -2])
def test_through_below_one_fails_before_any_claude_call(tmp_path: Path, limit: int) -> None:
    claude = _claude()
    with pytest.raises(Fail, match="through must be at least 1"):
        _run(tmp_path, claude, [], jev=None, through=limit)
    assert claude.batches == 0
    assert claude.calls == []


def test_register_declares_the_through_option_with_all_chapters_by_default() -> None:
    assert _parse(["run", "--lens", LENS]).through is None
    assert _parse(["run", "--lens", LENS, "--through", "2"]).through == 2


def test_run_command_passes_through_to_the_run(lens_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude = _claude()
    jev = FakeJev(JEV_HIGH)
    _bind(monkeypatch, claude, jev)
    args = _parse(["run", "--lens", LENS, "--through", "1"])

    args.fn(args)

    assert _dialog_files(lens_repo / "dialog" / LENS) == ["chapter-01.toml"]
    assert len(jev.calls) == 1
    assert (lens_repo / "reports" / f"{LENS}-c01.txt").read_text(encoding="utf-8") == EXPECTED_PROVISIONAL


# Made-up plan lines, scenes and answers for the revert of a whole plan. Not game text.
PLAN_TWO_ID = "made-up-plan-two"
PLAN_SCENE = Scene(
    "made-up-scene-e",
    (
        _line(NS_C, "2", None, EN_ONE, 3),
        _line(NS_C, "3", None, EN_TWO, 3),
        _line(NS_C, "4", None, EN_A3, 3),
        _line(NS_C, "5", None, EN_B2, 3),
    ),
)


def _plan_context(tmp_path: Path, bibles: Sequence[Mapping[str, Any]] = (), lens: Lens = LENS_OBJ) -> _Context:
    return _Context(
        lens=lens,
        store=Store(tmp_path / "work" / LENS),
        classifier=FakeClassifier(),
        renderer=FakeRenderer({}),
        run_id=RUN,
        jev=None,
        bibles=list(bibles),
        arcs=arcs.consolidate(bibles),
        lines={},
        table_lines=frozenset(),
        redo={},
        vanilla_hashes={},
        base_hashes={},
    )


def _arc_answers(claude: FakeClaude, answers: Mapping[str, dict[str, Any] | Unfinished | Rejected]) -> Ask:
    """Answers the arc judge of each named arc by its id and every other call from the FakeClaude. Not game text."""

    def ask(model: str, parts: Sequence[str], schema: Mapping[str, Any]) -> dict[str, Any] | Unfinished | Rejected:
        if schema is arcs.SCHEMA:
            arc_id = json.loads(_part(parts, "Arc:\n"))["id"]
            if arc_id in answers:
                return answers[arc_id]
        return claude.ask(model, parts, schema)

    return ask


def test_coherence_revert_of_a_line_pass_change_reverts_only_the_named_line(tmp_path: Path) -> None:
    claude = FakeClaude()
    claude.coherence = [_verdict(False, (NS_C, "2")), _verdict(False, (NS_C, "2"))]
    changes = [_change(NS_C, "2", 3), _change(NS_C, "3", 3)]
    outcomes = [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "changed", None, "voice"),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "changed", None, "voice"),
    ]
    kept_outcomes, kept_changes = _coherent(_plan_context(tmp_path), PLAN_SCENE, outcomes, changes, claude.start_batch())
    assert kept_outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "gate_failed", "scene", None),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "changed", None, "voice"),
    ]
    assert kept_changes == [_change(NS_C, "3", 3)]


def test_arc_revert_of_one_plan_line_reverts_every_changed_line_of_that_plan(tmp_path: Path) -> None:
    claude = FakeClaude()
    answers = {ARC_A: _arc_verdict(False, (NS_A, "1"))}
    changes = [_change(NS_A, "1", 1, plan=PLAN_ID), _change(NS_B, "1", 2, plan=PLAN_ID)]
    outcomes = [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
    ]
    context = _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS)
    kept_outcomes, kept_changes = _arc_judged(context, ARC_SCENES, outcomes, changes, _arc_answers(claude, answers))
    assert kept_outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None, plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None, plan=PLAN_ID),
    ]
    assert kept_changes == []


def test_arc_revert_of_one_plan_leaves_the_other_plan_to_ship(tmp_path: Path) -> None:
    claude = FakeClaude()
    answers = {ARC_A: _arc_verdict(False, (NS_A, "1")), ARC_C: _arc_verdict(True)}
    changes = [_change(NS_A, "1", 1, plan=PLAN_ID), _change(NS_B, "1", 2, plan=PLAN_ID), _change(NS_A, "2", 1, plan=PLAN_TWO_ID)]
    outcomes = [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
        Outcome(NS_A, "2", 1, "made-up-speaker-c", (ALPHA,), "changed", None, "voice", plan=PLAN_TWO_ID),
    ]
    context = _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS)
    kept_outcomes, kept_changes = _arc_judged(context, ARC_SCENES, outcomes, changes, _arc_answers(claude, answers))
    assert kept_outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None, plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None, plan=PLAN_ID),
        Outcome(NS_A, "2", 1, "made-up-speaker-c", (ALPHA,), "changed", None, "voice", plan=PLAN_TWO_ID),
    ]
    assert kept_changes == [changes[2]]


def test_arc_unfinished_plan_line_leaves_every_changed_line_of_that_plan_unfinished(tmp_path: Path) -> None:
    claude = FakeClaude()
    answers = {ARC_C: Unfinished("Made-up arc failure. Not game text.")}
    changes = [_change(NS_A, "1", 1, plan=PLAN_ID), _change(NS_A, "2", 1, plan=PLAN_ID), _change(NS_B, "1", 2)]
    outcomes = [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
        Outcome(NS_A, "2", 1, "made-up-speaker-c", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
    ]
    context = _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS)
    kept_outcomes, kept_changes = _arc_judged(context, ARC_SCENES, outcomes, changes, _arc_answers(claude, answers))
    assert kept_outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "unfinished", None, None, plan=PLAN_ID),
        Outcome(NS_A, "2", 1, "made-up-speaker-c", (ALPHA,), "unfinished", None, None, plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
    ]
    assert kept_changes == [changes[2]]


# Made-up line-pass and scene-pass changes for the two arc passes, in arc a and arc c. Not game text.
TWO_PASS_BASE = [
    Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
    Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
]
TWO_PASS_PLAN = Outcome(NS_A, "2", 1, "made-up-speaker-c", (ALPHA,), "changed", None, "voice", plan=PLAN_ID)
TWO_PASS_LINE_PASS = [_change(NS_A, "1", 1), _change(NS_B, "1", 2)]


def _arc_judge_keys(root: Path) -> list[str]:
    store = Store(root / "work" / LENS)
    return sorted(key for key in store.finished() if (saved := store.read(key)) is not None and saved.stage == arcs.STAGE)


def test_first_pass_prompts_and_arc_judge_keys_match_with_and_without_scene_pass_rewrites(tmp_path: Path) -> None:
    without_claude = FakeClaude()
    without_root = tmp_path / "without"
    without = _arc_judged(
        _plan_context(without_root, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS),
        ARC_SCENES,
        TWO_PASS_BASE,
        TWO_PASS_LINE_PASS,
        without_claude.start_batch(),
    )
    with_claude = FakeClaude()
    with_root = tmp_path / "with"
    with_pass = _arc_judged(
        _plan_context(with_root, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS),
        ARC_SCENES,
        [*TWO_PASS_BASE, TWO_PASS_PLAN],
        [*TWO_PASS_LINE_PASS, _change(NS_A, "2", 1, plan=PLAN_ID)],
        with_claude.start_batch(),
    )
    assert _first_pass_arc_calls(without_claude)
    assert _first_pass_arc_calls(with_claude) == _first_pass_arc_calls(without_claude)
    assert _arc_judge_keys(with_root) == _arc_judge_keys(without_root)
    assert _arc_judge_keys(with_root)
    assert _scene_arc_calls(without_claude) == []
    assert _scene_arc_calls(with_claude)
    assert without[0] == TWO_PASS_BASE
    assert [o for o in with_pass[0] if o.plan is None] == without[0]


def test_rerun_that_adds_the_scene_pass_on_the_same_store_makes_no_first_pass_call(tmp_path: Path) -> None:
    context = _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS)
    _arc_judged(context, ARC_SCENES, TWO_PASS_BASE, TWO_PASS_LINE_PASS, FakeClaude().start_batch())
    rerun = FakeClaude()
    _arc_judged(
        context,
        ARC_SCENES,
        [*TWO_PASS_BASE, TWO_PASS_PLAN],
        [*TWO_PASS_LINE_PASS, _change(NS_A, "2", 1, plan=PLAN_ID)],
        rerun.start_batch(),
    )
    assert _first_pass_arc_calls(rerun) == []
    assert _scene_arc_calls(rerun)


def test_second_pass_revert_of_one_plan_line_reverts_the_whole_plan_to_gate_failed_arc(tmp_path: Path) -> None:
    claude = FakeClaude()
    claude.scene_arc = [_arc_verdict(False, (NS_A, "1")), _arc_verdict(False, (NS_A, "1"))]
    changes = [_change(NS_A, "1", 1, plan=PLAN_ID), _change(NS_B, "1", 2, plan=PLAN_ID)]
    outcomes = [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
    ]
    kept_outcomes, kept_changes = _arc_judged(
        _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS), ARC_SCENES, outcomes, changes, claude.start_batch()
    )
    assert kept_outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None, plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None, plan=PLAN_ID),
    ]
    assert kept_changes == []
    assert _first_pass_arc_calls(claude) == []


def test_second_pass_answer_naming_a_line_pass_line_never_reverts_it(tmp_path: Path) -> None:
    claude = FakeClaude()
    claude.scene_arc = [_arc_verdict(False, (NS_B, "1"))]
    changes = [_change(NS_B, "1", 2), _change(NS_A, "1", 1, plan=PLAN_ID)]
    outcomes = [
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
    ]
    kept_outcomes, kept_changes = _arc_judged(
        _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS), ARC_SCENES, outcomes, changes, claude.start_batch()
    )
    assert kept_outcomes == [
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "unfinished", None, None, plan=PLAN_ID),
    ]
    assert kept_changes == [changes[0]]


def test_line_pass_line_its_first_pass_reverted_has_no_rewrite_in_the_second_pass(tmp_path: Path) -> None:
    claude = FakeClaude()
    claude.arc = [_arc_verdict(False, (NS_B, "1")), _arc_verdict(False, (NS_B, "1"))]
    changes = [_change(NS_B, "1", 2), _change(NS_A, "1", 1, plan=PLAN_ID)]
    outcomes = [
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice"),
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
    ]
    kept_outcomes, kept_changes = _arc_judged(
        _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}], SCENE_LENS), ARC_SCENES, outcomes, changes, claude.start_batch()
    )
    assert _outcome_of(kept_outcomes, (NS_B, "1")) == Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "gate_failed", "arc", None)
    [second] = _scene_arc_calls(claude)
    assert _arc_refs(second) == [(NS_A, "1")]
    assert kept_changes == [changes[1]]


def test_a_lens_without_a_scene_section_makes_no_second_pass_call(tmp_path: Path) -> None:
    claude = FakeClaude()
    changes = [_change(NS_A, "1", 1, plan=PLAN_ID), _change(NS_B, "1", 2, plan=PLAN_ID)]
    outcomes = [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
        Outcome(NS_B, "1", 2, "made-up-speaker-a", (ALPHA,), "changed", None, "voice", plan=PLAN_ID),
    ]
    kept_outcomes, kept_changes = _arc_judged(
        _plan_context(tmp_path, [{**ARC_BIBLE, "chapter": 1}]), ARC_SCENES, outcomes, changes, claude.start_batch()
    )
    assert _arc_calls(claude) == []
    assert kept_outcomes == outcomes
    assert kept_changes == changes


# Made-up scene-pass lens, scene and answers. Not game text.
PATTERN_ONE = "made-up-pattern-one"
PATTERN_TWO = "made-up-pattern-two"
SCENE_LENS = replace(
    LENS_OBJ,
    scene=SceneSection(
        patterns=(
            ScenePattern(PATTERN_ONE, "Made-up pattern one question. Not game text."),
            ScenePattern(PATTERN_TWO, "Made-up pattern two question. Not game text."),
        ),
        criteria=("Made-up scene criterion. Not game text.",),
        plan="Made-up scene plan rule. Not game text.",
        rubric="Made-up scene rubric. Not game text.",
    ),
)
OPUS_S2 = "Made-up line two of scene b. Not game text."
OPUS_S3 = "Made-up line three of scene b. Not game text."
OPUS_S4 = "Made-up line four of scene b. Not game text."
VOICE_FAR = "Qwx zvk pjt mrb dlf hgn. Not game text."
LONG = "Made-up overlong rewrite of the line, far past the length bar. Not game text."
TABLE_SCENE = Scene(
    "made-up-scene-table",
    (
        _line(NS_A, "1", None, EN_A1, 1),
        _line(NS_A, "2", None, EN_A2, 1),
        _line(NS_A, "3", None, EN_A3, 1),
        _line(NS_A, "4", None, EN_A4, 1),
    ),
    table=True,
)
KEEP_ANSWER: dict[str, Any] = {"keep": True, "keep_reason": "plot", "lines": []}


def _plan_answer(*lines: tuple[str, str, bool]) -> dict[str, Any]:
    english = {(line.namespace, line.key): line.english for line in TABLE_SCENE.lines}
    planned = [
        {
            "namespace": namespace,
            "key": key,
            "intent": "Made-up scene intent. Not game text.",
            "carrier": english[(namespace, key)],
            "required": required,
        }
        for namespace, key, required in lines
    ]
    return {"keep": False, "keep_reason": "none", "lines": planned}


def _scene_claude() -> FakeClaude:
    """Line 1 is flagged by the line pass. Lines 2 and 3 are the scene pass's planned lines, planned required and optional."""
    claude = FakeClaude()
    claude.bible = {}
    claude.flags = {(NS_A, "1"): [ALPHA]}
    claude.drafts = {(NS_A, "1"): [OPUS_A1], (NS_A, "2"): [OPUS_S2], (NS_A, "3"): [OPUS_S3]}
    claude.scene_screen = {
        "patterns": [
            {"id": PATTERN_TWO, "carriers": [{"namespace": NS_A, "key": "2"}]},
            {"id": PATTERN_ONE, "carriers": [{"namespace": NS_A, "key": "2"}]},
        ]
    }
    claude.scene_plan = _plan_answer((NS_A, "2", True), (NS_A, "3", False))
    return claude


def _scene_run(
    tmp_path: Path,
    claude: FakeClaude,
    *,
    scene: Scene = TABLE_SCENE,
    lens: Lens = SCENE_LENS,
) -> list[Outcome]:
    # The voice of OPUS_S2 is far from its English line, so the opus draft is the nearer candidate and ships.
    return _run(
        tmp_path,
        claude,
        [],
        jev=None,
        scenes=(scene,),
        lens=lens,
        renderer=FakeRenderer({OPUS_S2: VOICE_FAR}),
    )


def _scene_prompts(claude: FakeClaude, schema: Mapping[str, Any]) -> list[list[str]]:
    return [parts for _, parts, called in claude.calls if called is schema]


def _scene_judge_prompts(claude: FakeClaude) -> list[list[str]]:
    return [parts for _, parts, schema in claude.calls if schema is coherence.SCHEMA and _is_scene_judge(parts)]


def _two_line_pass_claude() -> FakeClaude:
    """Lines 1 and 4 are flagged by the line pass. Lines 2 and 3 are the scene pass's planned lines. Not game text."""
    claude = _scene_claude()
    claude.flags[(NS_A, "4")] = [ALPHA]
    claude.drafts[(NS_A, "4")] = [OPUS_A4]
    return claude


def _store_keys(tmp_path: Path, stage: str) -> set[str]:
    results = (tmp_path / "work" / LENS / "results").glob("*.json")
    return {path.stem for path in results if json.loads(path.read_text(encoding="utf-8"))["stage"] == stage}


def test_line_rubric_coherence_of_a_table_scene_matches_the_same_line_pass_without_the_scene_pass(tmp_path: Path) -> None:
    scene_claude = _two_line_pass_claude()
    scene_claude.coherence = [_verdict(False, (NS_A, "1")), _verdict(False, (NS_A, "1"))]
    scene_outcomes = _run(tmp_path / "scene", scene_claude, [], jev=None, scenes=(TABLE_SCENE,), lens=SCENE_LENS)
    plain_claude = _two_line_pass_claude()
    plain_claude.coherence = [_verdict(False, (NS_A, "1")), _verdict(False, (NS_A, "1"))]
    plain_outcomes = _run(tmp_path / "plain", plain_claude, [], jev=None, scenes=(TABLE_SCENE,))
    assert {_outcome_of(scene_outcomes, ref).outcome for ref in ((NS_A, "2"), (NS_A, "3"))} == {"changed"}
    assert len(_coherence_calls(scene_claude)) == 2
    assert _coherence_calls(scene_claude) == _coherence_calls(plain_claude)
    assert len(_store_keys(tmp_path / "plain", coherence.STAGE)) == 2
    assert _store_keys(tmp_path / "scene", coherence.STAGE) == _store_keys(tmp_path / "plain", coherence.STAGE)
    for ref in ((NS_A, "1"), (NS_A, "4")):
        assert _outcome_of(scene_outcomes, ref) == _outcome_of(plain_outcomes, ref)


def test_one_line_pass_change_and_two_scene_pass_changes_make_no_line_rubric_coherence_call(tmp_path: Path) -> None:
    claude = _scene_claude()
    outcomes = _scene_run(tmp_path, claude)
    assert _coherence_calls(claude) == []
    assert _outcome_of(outcomes, (NS_A, "2")).outcome == "changed"
    assert _outcome_of(outcomes, (NS_A, "3")).outcome == "changed"


@pytest.mark.parametrize(
    ("answers", "line_pass", "shipped"),
    [
        (
            [_verdict(False, (NS_A, "1")), _verdict(False, (NS_A, "1"))],
            Outcome(NS_A, "1", 1, None, (ALPHA,), "gate_failed", "scene", None),
            {(NS_A, "2"), (NS_A, "3"), (NS_A, "4")},
        ),
        (
            [Unfinished("Made-up coherence failure. Not game text.")],
            Outcome(NS_A, "1", 1, None, (ALPHA,), "unfinished", None, None),
            {(NS_A, "2"), (NS_A, "3")},
        ),
    ],
)
def test_scene_pass_lines_ship_when_coherence_reverts_or_fails_on_the_line_pass_lines(
    tmp_path: Path, answers: list[dict[str, Any] | Unfinished | Rejected], line_pass: Outcome, shipped: set[tuple[str, str]]
) -> None:
    claude = _two_line_pass_claude()
    claude.coherence = answers
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    reasons = (PATTERN_ONE, PATTERN_TWO)
    assert _outcome_of(outcomes, (NS_A, "1")) == line_pass
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _outcome_of(outcomes, (NS_A, "3")) == Outcome(NS_A, "3", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _dialog_entries(tmp_path) == shipped


def _dialog_tables(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    for path in (tmp_path / "repo" / "dialog" / LENS).iterdir():
        for namespace, table in tomllib.loads(path.read_text(encoding="utf-8")).items():
            for key, entry in table.items():
                tables[(namespace, key)] = entry
    return tables


def _untouched(namespace: str, key: str) -> Outcome:
    return Outcome(namespace, key, 1, None, (), "untouched", None, None)


def test_planned_required_and_optional_lines_ship_as_changed_with_hit_ids_plan_and_writer(tmp_path: Path) -> None:
    claude = _scene_claude()
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    reasons = (PATTERN_ONE, PATTERN_TWO)
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _outcome_of(outcomes, (NS_A, "3")) == Outcome(NS_A, "3", 1, None, reasons, "changed", None, "opus", None, plan)
    tables = _dialog_tables(tmp_path)
    assert set(tables) == {(NS_A, "1"), (NS_A, "2"), (NS_A, "3")}
    assert tables[(NS_A, "2")]["text"] == OPUS_S2
    assert tables[(NS_A, "2")]["writer"] == "opus"
    assert tables[(NS_A, "2")]["reason"] == [PATTERN_ONE, PATTERN_TWO]
    assert tables[(NS_A, "3")]["text"] == OPUS_S3
    assert tables[(NS_A, "3")]["reason"] == [PATTERN_ONE, PATTERN_TWO]


def test_plan_gets_the_line_pass_flagged_lines_as_final_and_no_excluded_lines(tmp_path: Path) -> None:
    claude = _scene_claude()
    _scene_run(tmp_path, claude)
    [parts] = _scene_prompts(claude, scene_pass.PLAN_SCHEMA)
    assert json.loads(_part(parts, "Final lines:\n")) == [{"namespace": NS_A, "key": "1"}]
    assert json.loads(_part(parts, "Excluded lines:\n")) == []


def test_planned_lines_never_reach_the_reason_judge(tmp_path: Path) -> None:
    claude = _scene_claude()
    _scene_run(tmp_path, claude)
    judged = {text for _, text in _judged(claude, JUDGE_SCHEMA)}
    assert OPUS_A1 in judged
    assert not judged & {OPUS_S2, OPUS_S3, VOICE_FAR}


def test_required_line_failing_its_gates_in_both_plans_blocks_every_scene_pass_change(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "2")] = [LONG]
    claude.drafts[(NS_A, "3")] = [LONG]
    claude.drafts[(NS_A, "4")] = [LONG]
    claude.scene_replan = _plan_answer((NS_A, "4", True))
    outcomes = _scene_run(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "1")).outcome == "changed"
    for key in ("2", "3", "4"):
        assert _outcome_of(outcomes, (NS_A, key)) == _untouched(NS_A, key)
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}
    assert len(_drafts_for(claude, (NS_A, "2"))) == 2
    assert len(_scene_prompts(claude, scene_pass.PLAN_SCHEMA)) == 2


def test_scene_judge_receives_the_passing_rewrites_under_the_scene_rubric(tmp_path: Path) -> None:
    claude = _scene_claude()
    _scene_run(tmp_path, claude)
    [parts] = _scene_judge_prompts(claude)
    rewrites = {(line["namespace"], line["key"]): line["rewrite"] for line in json.loads(_part(parts, "Scene lines:\n"))}
    assert rewrites == {(NS_A, "1"): OPUS_A1, (NS_A, "2"): OPUS_S2, (NS_A, "3"): OPUS_S3, (NS_A, "4"): None}


def test_scene_judge_reads_the_line_pass_rewrites_of_the_changed_line_pass_lines(tmp_path: Path) -> None:
    claude = _two_line_pass_claude()
    _scene_run(tmp_path, claude)
    [parts] = _scene_judge_prompts(claude)
    rewrites = {(line["namespace"], line["key"]): line["rewrite"] for line in json.loads(_part(parts, "Scene lines:\n"))}
    assert rewrites == {(NS_A, "1"): OPUS_A1, (NS_A, "2"): OPUS_S2, (NS_A, "3"): OPUS_S3, (NS_A, "4"): OPUS_A4}


def test_scene_judge_gets_no_rewrite_for_a_line_pass_line_coherence_reverted(tmp_path: Path) -> None:
    claude = _two_line_pass_claude()
    claude.coherence = [_verdict(False, (NS_A, "1")), _verdict(False, (NS_A, "1"))]
    _scene_run(tmp_path, claude)
    [parts] = _scene_judge_prompts(claude)
    rewrites = {(line["namespace"], line["key"]): line["rewrite"] for line in json.loads(_part(parts, "Scene lines:\n"))}
    assert rewrites == {(NS_A, "1"): None, (NS_A, "2"): OPUS_S2, (NS_A, "3"): OPUS_S3, (NS_A, "4"): OPUS_A4}


def test_judge_naming_a_line_pass_line_on_both_passes_leaves_it_shipped_and_reverts_no_scene_line(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_judge = [_verdict(False, (NS_A, "1")), _verdict(False, (NS_A, "1"))]
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    reasons = (PATTERN_ONE, PATTERN_TWO)
    assert _outcome_of(outcomes, (NS_A, "1")).outcome == "changed"
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _outcome_of(outcomes, (NS_A, "3")) == Outcome(NS_A, "3", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "2"), (NS_A, "3")}
    assert _records(tmp_path) == [
        _expected_record(patterns=reasons, plan=plan, planned=2, required=1, changed=2),
    ]


def test_judge_naming_a_line_pass_and_a_scene_pass_line_reverts_only_the_scene_pass_line(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_judge = [_verdict(False, (NS_A, "1"), (NS_A, "3")), _verdict(False, (NS_A, "1"), (NS_A, "3"))]
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    reasons = (PATTERN_ONE, PATTERN_TWO)
    assert _outcome_of(outcomes, (NS_A, "1")).outcome == "changed"
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "2")}
    assert _records(tmp_path) == [
        _expected_record(patterns=reasons, plan=plan, planned=2, required=1, changed=1, reverted=1),
    ]


def test_judge_reverted_optional_line_is_dropped_and_the_rest_ship(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_judge = [_verdict(False, (NS_A, "3")), _verdict(False, (NS_A, "3"))]
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    reasons = (PATTERN_ONE, PATTERN_TWO)
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "2")}
    assert len(_scene_judge_prompts(claude)) == 2


def test_judge_reverted_required_line_triggers_one_replan_that_excludes_it_and_ships_the_replan_with_its_id(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "4")] = [OPUS_S4]
    claude.scene_judge = [_verdict(False, (NS_A, "2")), _verdict(False, (NS_A, "2"))]
    claude.scene_replan = _plan_answer((NS_A, "4", True))
    outcomes = _scene_run(tmp_path, claude)
    first, replan = _scene_prompts(claude, scene_pass.PLAN_SCHEMA)
    assert json.loads(_part(replan, "Excluded lines:\n")) == [{"namespace": NS_A, "key": "2"}]
    assert json.loads(_part(first, "Excluded lines:\n")) == []
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "4")])
    assert _outcome_of(outcomes, (NS_A, "4")) == Outcome(
        NS_A, "4", 1, None, (PATTERN_ONE, PATTERN_TWO), "changed", None, "opus", None, plan
    )
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "4")}
    [replan_judge] = _scene_judge_prompts(claude)[2:]
    assert [line["key"] for line in json.loads(_part(replan_judge, "Scene lines:\n")) if line["rewrite"]] == ["1", "4"]


def test_gate_failed_required_line_triggers_one_replan_that_excludes_it_and_ships_the_replan_with_its_id(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "2")] = [LONG]
    claude.scene_replan = _plan_answer((NS_A, "3", True))
    outcomes = _scene_run(tmp_path, claude)
    first, replan = _scene_prompts(claude, scene_pass.PLAN_SCHEMA)
    assert json.loads(_part(first, "Excluded lines:\n")) == []
    assert json.loads(_part(replan, "Excluded lines:\n")) == [{"namespace": NS_A, "key": "2"}]
    assert json.loads(_part(replan, "Final lines:\n")) == [{"namespace": NS_A, "key": "1"}]
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "3")])
    assert _outcome_of(outcomes, (NS_A, "3")) == Outcome(
        NS_A, "3", 1, None, (PATTERN_ONE, PATTERN_TWO), "changed", None, "opus", None, plan
    )
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "3")}
    assert len(_scene_judge_prompts(claude)) == 1


def test_replan_keep_leaves_no_scene_pass_change_and_makes_no_third_plan(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "2")] = [LONG]
    claude.scene_replan = KEEP_ANSWER
    outcomes = _scene_run(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}
    assert len(_scene_prompts(claude, scene_pass.PLAN_SCHEMA)) == 2


def test_optional_planned_line_failing_its_gates_twice_is_dropped_and_the_required_line_ships(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "3")] = [LONG]
    outcomes = _scene_run(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "2")).outcome == "changed"
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "2")}
    assert len(_drafts_for(claude, (NS_A, "3"))) == 2


def test_keep_plan_changes_nothing_and_drafts_no_planned_line(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_plan = KEEP_ANSWER
    outcomes = _scene_run(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _drafts_for(claude, (NS_A, "2")) == []
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}


def test_scene_with_no_pattern_hit_makes_no_plan_call_and_changes_nothing(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_screen = {"patterns": []}
    outcomes = _scene_run(tmp_path, claude)
    assert len(_scene_prompts(claude, scene_pass.SCREEN_SCHEMA)) == 1
    assert _scene_prompts(claude, scene_pass.PLAN_SCHEMA) == []
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}


def test_non_table_scene_makes_no_scene_pass_call(tmp_path: Path) -> None:
    claude = _scene_claude()
    outcomes = _scene_run(tmp_path, claude, scene=replace(TABLE_SCENE, table=False))
    assert _scene_prompts(claude, scene_pass.SCREEN_SCHEMA) == []
    assert _scene_prompts(claude, scene_pass.PLAN_SCHEMA) == []
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}


def test_lens_without_a_scene_section_makes_no_scene_pass_call(tmp_path: Path) -> None:
    claude = _scene_claude()
    outcomes = _scene_run(tmp_path, claude, lens=LENS_OBJ)
    assert _scene_prompts(claude, scene_pass.SCREEN_SCHEMA) == []
    assert _scene_prompts(claude, scene_pass.PLAN_SCHEMA) == []
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}


@pytest.mark.parametrize("failure", ["screen", "plan", "draft", "judge", "replan"])
def test_unfinished_scene_pass_stage_leaves_every_unflagged_line_unfinished_and_changes_nothing(tmp_path: Path, failure: str) -> None:
    claude = _scene_claude()
    if failure == "screen":
        claude.scene_screen = Unfinished("Made-up screen failure. Not game text.")
    elif failure == "plan":
        claude.scene_plan = Rejected("Made-up plan rejection. Not game text.")
    elif failure == "draft":
        claude.drafts[(NS_A, "2")] = Rejected("Made-up draft rejection. Not game text.")
    elif failure == "judge":
        claude.scene_judge = [Unfinished("Made-up scene judge failure. Not game text.")]
    else:
        claude.drafts[(NS_A, "2")] = [LONG]
        claude.scene_replan = Unfinished("Made-up re-plan failure. Not game text.")
    outcomes = _scene_run(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "1")).outcome == "changed"
    for key in ("2", "3", "4"):
        assert _outcome_of(outcomes, (NS_A, key)) == Outcome(NS_A, key, 1, None, (), "unfinished", None, None)
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}


# Made-up scene-pass redrafts and reviewer note. Not game text.
REDRAFT_A2 = "Made-up redraft of scene a line two. Not game text."
SCENE_REDO = "Made-up scene redo note. Not game text."
SCENE_REASONS = (PATTERN_ONE, PATTERN_TWO)


def test_listed_planned_line_is_redrafted_with_its_note_and_the_redraft_ships_with_its_plan(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"2" = "{SCENE_REDO}"\n')
    claude = _scene_claude()
    claude.redrafts[(NS_A, "2")] = [REDRAFT_A2]
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    assert _note_drafts(claude) == [([(NS_A, "2")], SCENE_REDO)]
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, SCENE_REASONS, "changed", None, "opus", None, plan, True)
    assert _dialog_tables(tmp_path)[(NS_A, "2")]["text"] == REDRAFT_A2
    assert _outcome_of(outcomes, (NS_A, "3")).redo is False


def test_scene_run_draft_and_render_results_of_unlisted_lines_are_stored_with_the_redo_file(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    listed = tmp_path / "listed"
    _scene_run(plain, _scene_claude())
    _redo_file(listed, f'[{NS_A}]\n"2" = "{SCENE_REDO}"\n')
    claude = _scene_claude()
    claude.redrafts[(NS_A, "2")] = [REDRAFT_A2]
    _scene_run(listed, claude)
    plain_stored = _stored(plain / "work" / LENS)
    assert set(plain_stored.values()) >= {"draft", "render"}
    assert set(plain_stored.items()) <= set(_stored(listed / "work" / LENS).items())


@pytest.mark.parametrize("failure", ["gates", "judge"])
def test_scene_pass_redraft_failing_its_gates_or_the_scene_judge_is_gate_failed_under_redo_with_no_plan_and_no_dialog_entry(
    tmp_path: Path, failure: str
) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"2" = "{SCENE_REDO}"\n')
    claude = _scene_claude()
    claude.scene_plan = _plan_answer((NS_A, "2", False), (NS_A, "3", False))
    if failure == "gates":
        claude.redrafts[(NS_A, "2")] = [LONG]
    else:
        claude.redrafts[(NS_A, "2")] = [REDRAFT_A2]
        claude.scene_judge = [_verdict(False, (NS_A, "2")), _verdict(False, (NS_A, "2"))]
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    notes = _note_drafts(claude)
    assert notes and all(refs == [(NS_A, "2")] and note == SCENE_REDO for refs, note in notes)
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, SCENE_REASONS, "gate_failed", "redo", None, None, None, True)
    assert _outcome_of(outcomes, (NS_A, "3")) == Outcome(NS_A, "3", 1, None, SCENE_REASONS, "changed", None, "opus", None, plan)
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "3")}


def test_listed_planned_line_whose_normal_gating_fails_makes_no_note_call(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"3" = "{SCENE_REDO}"\n')
    claude = _scene_claude()
    claude.drafts[(NS_A, "3")] = [LONG]
    outcomes = _scene_run(tmp_path, claude)
    assert _note_drafts(claude) == []
    assert _outcome_of(outcomes, (NS_A, "3")) == replace(_untouched(NS_A, "3"), redo=True)
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "2")}


def test_redo_failure_of_a_required_line_is_reported_when_the_replan_also_blocks_the_scene(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"2" = "{SCENE_REDO}"\n')
    claude = _scene_claude()
    claude.redrafts[(NS_A, "2")] = [LONG]
    claude.drafts[(NS_A, "4")] = [LONG]
    claude.scene_replan = _plan_answer((NS_A, "4", True))
    outcomes = _scene_run(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, SCENE_REASONS, "gate_failed", "redo", None, None, None, True)
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _outcome_of(outcomes, (NS_A, "4")) == _untouched(NS_A, "4")
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}


def test_redo_failure_of_a_required_line_is_reported_when_the_replan_is_unfinished(tmp_path: Path) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"2" = "{SCENE_REDO}"\n')
    claude = _scene_claude()
    claude.redrafts[(NS_A, "2")] = [LONG]
    claude.scene_replan = Unfinished("Made-up re-plan failure. Not game text.")
    outcomes = _scene_run(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, SCENE_REASONS, "gate_failed", "redo", None, None, None, True)
    for key in ("3", "4"):
        assert _outcome_of(outcomes, (NS_A, key)) == Outcome(NS_A, key, 1, None, (), "unfinished", None, None)
    assert _dialog_entries(tmp_path) == {(NS_A, "1")}


# Made-up scene records. Not game text.
GATE_LENGTH = "length"


def _records(tmp_path: Path) -> list[scene_record.SceneRecord]:
    return scene_record.read_scenes(tmp_path / "work" / LENS / scene_record.SCENES)


def _expected_record(**fields: Any) -> scene_record.SceneRecord:
    base: dict[str, Any] = {
        "scene": TABLE_SCENE.key,
        "chapter": 1,
        "eligible": True,
        "patterns": (),
        "plan": None,
        "keep_reason": None,
        "planned": 0,
        "required": 0,
        "changed": 0,
        "reverted": 0,
        "replanned": 0,
        "gate_failures": {},
    }
    return scene_record.SceneRecord(**{**base, **fields})


def test_shipped_plan_writes_one_record_with_its_plan_patterns_and_counts(tmp_path: Path) -> None:
    _scene_run(tmp_path, _scene_claude())
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    assert _records(tmp_path) == [
        _expected_record(patterns=(PATTERN_ONE, PATTERN_TWO), plan=plan, planned=2, required=1, changed=2),
    ]


def test_one_record_per_in_scope_scene_in_run_order_with_ineligible_scenes_marked(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _scene_claude(),
        [],
        jev=None,
        scenes=(SCENE_B, TABLE_SCENE),
        lens=SCENE_LENS,
        renderer=FakeRenderer({OPUS_S2: VOICE_FAR}),
    )
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    assert _records(tmp_path) == [
        _expected_record(patterns=(PATTERN_ONE, PATTERN_TWO), plan=plan, planned=2, required=1, changed=2),
        _expected_record(scene=SCENE_B.key, chapter=2, eligible=False),
    ]


@pytest.mark.parametrize(("lens", "table"), [(LENS_OBJ, True), (SCENE_LENS, False)])
def test_lens_without_a_scene_section_or_a_non_table_scene_records_ineligible_with_empty_counts(
    tmp_path: Path, lens: Lens, table: bool
) -> None:
    _scene_run(tmp_path, _scene_claude(), scene=replace(TABLE_SCENE, table=table), lens=lens)
    assert _records(tmp_path) == [_expected_record(eligible=False)]


def test_kept_scene_records_its_keep_reason_and_no_plan(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_plan = KEEP_ANSWER
    _scene_run(tmp_path, claude)
    assert _records(tmp_path) == [_expected_record(patterns=(PATTERN_ONE, PATTERN_TWO), keep_reason="plot")]


def test_scene_with_no_pattern_hit_records_eligible_with_empty_counts(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_screen = {"patterns": []}
    _scene_run(tmp_path, claude)
    assert _records(tmp_path) == [_expected_record()]


def test_replan_record_names_the_replan_id_and_counts_its_gate_failures(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "2")] = [LONG]
    claude.scene_replan = _plan_answer((NS_A, "3", True))
    _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "3")])
    assert _records(tmp_path) == [
        _expected_record(
            patterns=(PATTERN_ONE, PATTERN_TWO),
            plan=plan,
            planned=1,
            required=1,
            changed=1,
            replanned=1,
            gate_failures={GATE_LENGTH: 1},
        ),
    ]


def test_gate_failures_count_both_plans_and_the_record_names_the_replan(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "2")] = [LONG]
    claude.drafts[(NS_A, "3")] = [LONG]
    claude.drafts[(NS_A, "4")] = [LONG]
    claude.scene_replan = _plan_answer((NS_A, "4", True))
    _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "4")])
    assert _records(tmp_path) == [
        _expected_record(
            patterns=(PATTERN_ONE, PATTERN_TWO),
            plan=plan,
            planned=1,
            required=1,
            replanned=1,
            gate_failures={GATE_LENGTH: 3},
        ),
    ]


GATE_SCENE = "scene"


def _scene_texts(tmp_path: Path) -> list[str]:
    """The dialog tables and scene records a run wrote, for checking that no carrier text reached them."""
    dialog = [path.read_text(encoding="utf-8") for path in (tmp_path / "repo" / "dialog" / LENS).iterdir()]
    return [*dialog, (tmp_path / "work" / LENS / scene_record.SCENES).read_text(encoding="utf-8")]


@pytest.mark.parametrize("draft", [EN_A3.upper(), EN_A3.lower()], ids=["upper", "lower"])
def test_optional_planned_line_whose_only_candidate_keeps_its_carrier_in_another_case_ships_nothing_and_counts_a_scene_gate_failure(
    tmp_path: Path, draft: str
) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "3")] = [draft]
    printed: list[str] = []
    outcomes = _run(
        tmp_path,
        claude,
        printed,
        jev=None,
        scenes=(TABLE_SCENE,),
        lens=SCENE_LENS,
        renderer=FakeRenderer({OPUS_S2: VOICE_FAR}),
    )
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    reasons = (PATTERN_ONE, PATTERN_TWO)
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, reasons, "changed", None, "opus", None, plan)
    assert _outcome_of(outcomes, (NS_A, "3")) == _untouched(NS_A, "3")
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "2")}
    assert _records(tmp_path) == [
        _expected_record(patterns=reasons, plan=plan, planned=2, required=1, changed=1, gate_failures={GATE_SCENE: 1}),
    ]
    assert not any(EN_A3.casefold() in text.casefold() for text in [*printed, *_scene_texts(tmp_path)])


def test_required_planned_line_whose_only_candidate_keeps_its_carrier_triggers_one_replan_counting_a_scene_gate_failure(
    tmp_path: Path,
) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "2")] = [EN_A2.upper()]
    claude.scene_replan = _plan_answer((NS_A, "3", True))
    outcomes = _scene_run(tmp_path, claude)
    _, replan = _scene_prompts(claude, scene_pass.PLAN_SCHEMA)
    assert json.loads(_part(replan, "Excluded lines:\n")) == [{"namespace": NS_A, "key": "2"}]
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "3")])
    assert _outcome_of(outcomes, (NS_A, "3")) == Outcome(
        NS_A, "3", 1, None, (PATTERN_ONE, PATTERN_TWO), "changed", None, "opus", None, plan
    )
    assert _outcome_of(outcomes, (NS_A, "2")) == _untouched(NS_A, "2")
    assert _dialog_entries(tmp_path) == {(NS_A, "1"), (NS_A, "3")}
    assert _records(tmp_path) == [
        _expected_record(
            patterns=(PATTERN_ONE, PATTERN_TWO),
            plan=plan,
            planned=1,
            required=1,
            changed=1,
            replanned=1,
            gate_failures={GATE_SCENE: 1},
        ),
    ]
    assert not any(EN_A2.casefold() in text.casefold() for text in _scene_texts(tmp_path))


def test_free_candidate_ships_over_a_nearer_candidate_that_keeps_its_carrier(tmp_path: Path) -> None:
    keeper = f"{EN_A2} Yes."
    claude = _scene_claude()
    claude.drafts[(NS_A, "2")] = [keeper, VOICE_FAR]
    outcomes = _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(
        NS_A, "2", 1, None, (PATTERN_ONE, PATTERN_TWO), "changed", None, "opus", None, plan
    )
    assert _dialog_tables(tmp_path)[(NS_A, "2")]["text"] == VOICE_FAR
    assert not any(keeper.casefold() in text.casefold() for text in _scene_texts(tmp_path))


def test_scene_judge_revert_of_an_optional_line_counts_as_reverted(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_judge = [_verdict(False, (NS_A, "3")), _verdict(False, (NS_A, "3"))]
    _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    assert _records(tmp_path) == [
        _expected_record(patterns=(PATTERN_ONE, PATTERN_TWO), plan=plan, planned=2, required=1, changed=1, reverted=1),
    ]


def test_scene_judge_revert_of_a_required_line_counts_in_both_plans_and_names_the_replan(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.drafts[(NS_A, "4")] = [OPUS_S4]
    claude.scene_judge = [_verdict(False, (NS_A, "2")), _verdict(False, (NS_A, "2"))]
    claude.scene_replan = _plan_answer((NS_A, "4", True))
    _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "4")])
    assert _records(tmp_path) == [
        _expected_record(
            patterns=(PATTERN_ONE, PATTERN_TWO),
            plan=plan,
            planned=1,
            required=1,
            changed=1,
            reverted=1,
            replanned=1,
        ),
    ]


def test_coherence_revert_of_a_line_pass_line_leaves_the_scene_pass_plan_whole_in_its_record(tmp_path: Path) -> None:
    claude = _two_line_pass_claude()
    claude.coherence = [_verdict(False, (NS_A, "1")), _verdict(False, (NS_A, "1"))]
    _scene_run(tmp_path, claude)
    plan = scene_pass.plan_id(TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    assert _records(tmp_path) == [
        _expected_record(patterns=(PATTERN_ONE, PATTERN_TWO), plan=plan, planned=2, required=1, changed=2),
    ]


@pytest.mark.parametrize(
    ("failure", "patterns"),
    [("screen", ()), ("plan", (PATTERN_ONE, PATTERN_TWO)), ("replan", (PATTERN_ONE, PATTERN_TWO))],
)
def test_unfinished_scene_pass_keeps_its_patterns_with_no_plan_and_zero_counts(
    tmp_path: Path, failure: str, patterns: tuple[str, ...]
) -> None:
    claude = _scene_claude()
    if failure == "screen":
        claude.scene_screen = Unfinished("Made-up screen failure. Not game text.")
    elif failure == "plan":
        claude.scene_plan = Rejected("Made-up plan rejection. Not game text.")
    else:
        claude.drafts[(NS_A, "2")] = [LONG]
        claude.scene_replan = Unfinished("Made-up re-plan failure. Not game text.")
    _scene_run(tmp_path, claude)
    assert _records(tmp_path) == [_expected_record(patterns=patterns)]


def test_scenes_file_reads_back_and_holds_no_line_text(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_judge = [_verdict(False, (NS_A, "3")), _verdict(False, (NS_A, "3"))]
    claude.drafts[(NS_A, "4")] = [LONG]
    _scene_run(tmp_path, claude)
    path = tmp_path / "work" / LENS / scene_record.SCENES
    assert len(scene_record.read_scenes(path)) == 1
    written = path.read_text(encoding="utf-8")
    assert not any(text in written for text in (*ENGLISH, OPUS_A1, VOICE_A1, OPUS_S2, OPUS_S3, OPUS_S4, VOICE_FAR, LONG))


def test_rerun_with_a_scene_section_reuses_every_line_pass_result_and_asks_only_the_scene_screen(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.scene_screen = {"patterns": []}
    _scene_run(tmp_path, claude, lens=LENS_OBJ)
    claude.calls.clear()
    _scene_run(tmp_path, claude)
    assert len(claude.calls) == 1
    assert len(_scene_prompts(claude, scene_pass.SCREEN_SCHEMA)) == 1
    assert _records(tmp_path) == [_expected_record()]


# Made-up provisional scene lines for a shipped scene pass, and the same scene with an arc revert. Not game text.
ARC_TABLE_SCENE = Scene(
    "made-up-scene-arc-table",
    (
        _line(NS_A, "1", "made-up-speaker-a", EN_A1, 1),
        _line(NS_A, "2", "made-up-speaker-a", EN_A2, 1),
        _line(NS_A, "3", "made-up-speaker-a", EN_A3, 1),
        _line(NS_A, "4", "made-up-speaker-c", EN_A4, 1),
    ),
    table=True,
)
SCENE_PASS_PROVISIONAL = (
    "chapter 1: 3 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  no speaker: 3\n"
    "  scene eligible: 1\n"
    f"  scene flagged {PATTERN_ONE}: 1\n"
    f"  scene flagged {PATTERN_TWO}: 1\n"
    "  scene plans: 1\n"
    "  scene planned lines: 2 (required 1)\n"
    "  scene changed lines: 2\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 2, median 2\n"
    "total: 3 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  no speaker: 3\n"
    "  scene eligible: 1\n"
    f"  scene flagged {PATTERN_ONE}: 1\n"
    f"  scene flagged {PATTERN_TWO}: 1\n"
    "  scene plans: 1\n"
    "  scene planned lines: 2 (required 1)\n"
    "  scene changed lines: 2\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 2, median 2\n"
)

SCENE_PASS_PROVISIONAL_ARC = (
    "chapter 1: 3 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 3\n"
    "  scene eligible: 1\n"
    f"  scene flagged {PATTERN_ONE}: 1\n"
    f"  scene flagged {PATTERN_TWO}: 1\n"
    "  scene plans: 1\n"
    "  scene planned lines: 2 (required 1)\n"
    "  scene changed lines: 2\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 2, median 2\n"
    "  scene character 1: 2\n"
    "total: 3 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 3\n"
    "  scene eligible: 1\n"
    f"  scene flagged {PATTERN_ONE}: 1\n"
    f"  scene flagged {PATTERN_TWO}: 1\n"
    "  scene plans: 1\n"
    "  scene planned lines: 2 (required 1)\n"
    "  scene changed lines: 2\n"
    "  scene replanned: 0\n"
    "  scene reverted lines: 0\n"
    "  scene changed per scene: max 2, median 2\n"
    "  scene character 1: 2\n"
)


def test_provisional_file_shows_the_chapter_one_scene_lines_of_a_shipped_scene_pass(tmp_path: Path) -> None:
    _scene_run(tmp_path, _scene_claude())
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == SCENE_PASS_PROVISIONAL


def test_provisional_scene_lines_are_taken_before_an_arc_revert_of_a_scene_pass_line(tmp_path: Path) -> None:
    claude = _scene_claude()
    claude.bible = {1: ARC_BIBLE}
    claude.scene_arc = [_arc_verdict(False, (NS_A, "2")), _arc_verdict(False, (NS_A, "2"))]
    outcomes = _scene_run(tmp_path, claude, scene=ARC_TABLE_SCENE)
    plan = scene_pass.plan_id(ARC_TABLE_SCENE.key, [(NS_A, "2"), (NS_A, "3")])
    assert _outcome_of(outcomes, (NS_A, "2")).gate == "arc"
    assert _records(tmp_path) == [
        _expected_record(
            scene=ARC_TABLE_SCENE.key, patterns=(PATTERN_ONE, PATTERN_TWO), plan=plan, planned=2, required=1, changed=0, reverted=2
        )
    ]
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == SCENE_PASS_PROVISIONAL_ARC


def test_provisional_scene_lines_leave_out_a_scene_without_a_chapter_one_line(tmp_path: Path) -> None:
    _run(
        tmp_path,
        _scene_claude(),
        [],
        jev=None,
        scenes=(SCENE_B, TABLE_SCENE),
        lens=SCENE_LENS,
        renderer=FakeRenderer({OPUS_S2: VOICE_FAR}),
    )
    assert _c01_path(tmp_path).read_text(encoding="utf-8") == SCENE_PASS_PROVISIONAL


# Queering phase 1 fixtures. Made-up speakers, notes and lines. Not game text.
QUEER_LENS = replace(LENS_OBJ, selection="queering")
NOTES_VILLAIN = {"speaker": "made-up-speaker-a", "notes": "Made-up villain notes. Not game text.", "gender": "made-up-gender-a"}
NOTES_FLIRT = {"speaker": "made-up-speaker-c", "notes": "Made-up flirt notes. Not game text.", "gender": "made-up-gender-c"}
QUEER_BIBLE: dict[str, Any] = {"characters": [NOTES_VILLAIN, NOTES_FLIRT], "facts": [], "callbacks": [], "arcs": []}
QUEER_SCENE_A = Scene(
    "made-up-scene-a",
    (
        _line(NS_A, "1", "made-up-speaker-a", EN_A1, 1),
        _line(NS_A, "2", None, EN_A2, 1),
        _line(NS_A, "3", "made-up-speaker-c", EN_A3, 1),
    ),
)
QUEER_SCENE_B = Scene("made-up-scene-b", (_line(NS_B, "1", "made-up-speaker-b", EN_B1, 2), _line(NS_B, "2", None, EN_B2, 2)))
QUEER_SCENES = (QUEER_SCENE_A, QUEER_SCENE_B, SCENE_C)
# Buckets are the frozen queering.select answers for these speakers. Not game text.
EXPECTED_QUEER_OUTCOMES = [
    Outcome(NS_A, "1", 1, "made-up-speaker-a", (), "untouched", None, None),
    Outcome(NS_A, "2", 1, None, (ALPHA,), "untouched", None, None, bucket="untouched"),
    Outcome(NS_A, "3", 1, "made-up-speaker-c", (BETA,), "untouched", None, None, bucket="not-redirected"),
    Outcome(NS_B, "1", 2, "made-up-speaker-b", (ALPHA,), "untouched", None, None, bucket="untouched"),
    Outcome(NS_B, "2", 2, None, (), "untouched", None, None),
    Outcome(NS_C, "1", None, None, (ALPHA,), "changed", None, "opus", bucket="queer"),
]
QUEER_FLAGS = {(NS_A, "2"): [ALPHA], (NS_A, "3"): [BETA], (NS_B, "1"): [ALPHA], (NS_C, "1"): [ALPHA]}
SCREEN_ELIGIBLE = "Eligible lines, flag only these:\n"


def _queer_claude() -> FakeClaude:
    claude = FakeClaude()
    claude.bible = {1: QUEER_BIBLE}
    claude.flags = dict(QUEER_FLAGS)
    claude.drafts = {(NS_C, "1"): [OPUS_C1]}
    return claude


def _screens(claude: FakeClaude) -> list[tuple[str, list[dict[str, str]]]]:
    """Each screen's model and eligible lines, sorted by those lines, since the scenes' screens run concurrently."""
    calls = [(model, json.loads(_part(parts, SCREEN_ELIGIBLE))) for model, parts, schema in claude.calls if schema is SCREEN_SCHEMA]
    return sorted(calls, key=lambda call: [(ref["namespace"], ref["key"]) for ref in call[1]])


def test_queering_screens_only_markable_lines_on_haiku_and_writes_each_marked_line_with_its_bucket(tmp_path: Path) -> None:
    claude = _queer_claude()
    jev = FakeJev(JEV_HIGH)
    outcomes = _run(tmp_path, claude, [], jev=jev, scenes=QUEER_SCENES, lens=QUEER_LENS)
    assert _screens(claude) == [
        (claude_module.HAIKU, [{"namespace": NS_A, "key": "2"}, {"namespace": NS_A, "key": "3"}]),
        (claude_module.HAIKU, [{"namespace": NS_B, "key": "1"}, {"namespace": NS_B, "key": "2"}]),
        (claude_module.HAIKU, [{"namespace": NS_C, "key": "1"}]),
    ]
    assert outcomes == EXPECTED_QUEER_OUTCOMES
    assert jev.calls == []
    assert all(schema is SCREEN_SCHEMA for model, _, schema in claude.calls if model == claude_module.HAIKU)
    # The bible, one batch per scene, the speakerless draft of scene c, and the arc judge.
    assert claude.batches == 3 + len(QUEER_SCENES)
    records = [json.loads(line) for line in (tmp_path / "work" / LENS / report.OUTCOMES).read_text(encoding="utf-8").splitlines()]
    assert ["bucket" in record for record in records] == [outcome.bucket is not None for outcome in EXPECTED_QUEER_OUTCOMES]


def test_queering_scene_without_a_markable_line_makes_no_call_and_leaves_every_line_untouched(tmp_path: Path) -> None:
    claude = _queer_claude()
    claude.bible = {1: {**QUEER_BIBLE, "characters": [NOTES_VILLAIN]}}
    scene = Scene("made-up-scene-a", (_line(NS_A, "1", "made-up-speaker-a", EN_A1, 1),))
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(scene,), lens=QUEER_LENS)
    assert _screens(claude) == []
    assert outcomes == [Outcome(NS_A, "1", 1, "made-up-speaker-a", (), "untouched", None, None)]


def test_queering_failed_screen_leaves_every_line_of_its_scene_unfinished(tmp_path: Path) -> None:
    claude = _queer_claude()
    claude.screen_failure_refs = {(NS_A, "3"): Unfinished("Made-up screen failure. Not game text.")}
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=QUEER_SCENES, lens=QUEER_LENS)
    assert outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (), "unfinished", None, None),
        Outcome(NS_A, "2", 1, None, (), "unfinished", None, None),
        Outcome(NS_A, "3", 1, "made-up-speaker-c", (), "unfinished", None, None),
        *EXPECTED_QUEER_OUTCOMES[3:],
    ]


# Queering phase 2 fixtures. Made-up speakers, notes, lines and redo note. Not game text.
PHASE_SPEAKER = "made-up-speaker-d"
PHASE_NOTE = "Made-up phase redo note. Not game text."
NOTES_PHASE = {"speaker": PHASE_SPEAKER, "notes": "Made-up notes of speaker d. Not game text.", "gender": "made-up-gender-d"}
# A chapter's bible names only the speakers that speak in that chapter.
PHASE_BIBLE_ONE: dict[str, Any] = {**QUEER_BIBLE, "characters": [NOTES_FLIRT, NOTES_PHASE]}
PHASE_BIBLE_TWO: dict[str, Any] = {**QUEER_BIBLE, "characters": [NOTES_VILLAIN, NOTES_PHASE]}
PHASE_SCENES = (
    Scene(
        "made-up-scene-a",
        (_line(NS_A, "1", PHASE_SPEAKER, EN_A1, 1), _line(NS_A, "2", None, EN_A2, 1), _line(NS_A, "3", "made-up-speaker-c", EN_A3, 1)),
    ),
    Scene(
        "made-up-scene-b",
        (_line(NS_B, "1", PHASE_SPEAKER, EN_B1, 2), _line(NS_B, "2", None, EN_B2, 2), _line(NS_B, "3", "made-up-speaker-a", EN_A4, 2)),
    ),
)
PHASE_FLAGS = {(NS_A, "1"): [ALPHA], (NS_A, "2"): [ALPHA], (NS_A, "3"): [BETA], (NS_B, "1"): [ALPHA], (NS_B, "2"): [ALPHA]}
PHASE_DRAFTS = {(NS_A, "1"): [OPUS_A1], (NS_A, "2"): [OPUS_A2], (NS_B, "1"): [OPUS_B1], (NS_B, "2"): [OPUS_B2]}
PHASE_OUTCOMES = [
    Outcome(NS_A, "1", 1, PHASE_SPEAKER, (ALPHA,), "changed", None, "opus", bucket="queer"),
    Outcome(NS_A, "2", 1, None, (ALPHA,), "changed", None, "opus", bucket="queer"),
    Outcome(NS_A, "3", 1, "made-up-speaker-c", (BETA,), "untouched", None, None, bucket="not-redirected"),
    Outcome(NS_B, "1", 2, PHASE_SPEAKER, (ALPHA,), "changed", None, "opus", bucket="queer"),
    Outcome(NS_B, "2", 2, None, (ALPHA,), "changed", None, "opus", bucket="queer"),
    Outcome(NS_B, "3", 2, "made-up-speaker-a", (), "untouched", None, None),
]


@pytest.fixture
def every_line_queer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every line outside the core cast and the antagonists lands in the queer bucket, so these fixtures need no hash value. Not game text.
    monkeypatch.setattr(queering, "QUEER_PERCENT", 100)


def _phase_claude() -> FakeClaude:
    claude = FakeClaude()
    claude.bible = {1: PHASE_BIBLE_ONE, 2: PHASE_BIBLE_TWO}
    claude.flags = dict(PHASE_FLAGS)
    claude.drafts = dict(PHASE_DRAFTS)
    return claude


def _phase(tmp_path: Path, claude: FakeClaude) -> list[Outcome]:
    # FakeRenderer({}) voices nothing, so each draft is its own candidate and every shipped text is the opus draft.
    return _run(tmp_path, claude, [], jev=None, renderer=FakeRenderer({}), scenes=PHASE_SCENES, lens=QUEER_LENS)


def _speaker_draft_calls(claude: FakeClaude) -> list[list[str]]:
    return [parts for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA and any(part.startswith("Scenes:\n") for part in parts)]


def _scene_draft_calls(claude: FakeClaude) -> list[list[str]]:
    return [parts for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA and any(part.startswith("Targets:\n") for part in parts)]


def _dialog_records(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for path in (tmp_path / "repo" / "dialog" / LENS).iterdir():
        for namespace, table in tomllib.loads(path.read_text(encoding="utf-8")).items():
            records.update({(namespace, key): record for key, record in table.items()})
    return records


def test_queering_drafts_a_speakers_selected_lines_across_scenes_in_one_call(tmp_path: Path, every_line_queer: None) -> None:
    claude = _phase_claude()
    _phase(tmp_path, claude)
    speaker_calls = _speaker_draft_calls(claude)
    assert len(speaker_calls) == 1
    assert len(json.loads(_part(speaker_calls[0], "Scenes:\n"))) == 2
    assert _draft_refs(speaker_calls[0]) == [(NS_A, "1"), (NS_B, "1")]


def test_queering_drafts_a_speakerless_selected_line_once_per_scene(tmp_path: Path, every_line_queer: None) -> None:
    claude = _phase_claude()
    _phase(tmp_path, claude)
    assert sorted(_draft_refs(parts) for parts in _scene_draft_calls(claude)) == [[(NS_A, "2")], [(NS_B, "2")]]


def test_queering_intent_carries_each_selected_lines_bucket_and_untouched_or_not_redirected_lines_get_none(
    tmp_path: Path, every_line_queer: None
) -> None:
    claude = _phase_claude()
    _phase(tmp_path, claude)
    intents = sorted(
        (_target(parts), json.loads(_part(parts, "Target line:\n"))["bucket"])
        for _, parts, schema in claude.calls
        if schema is INTENT_SCHEMA
    )
    assert intents == [((NS_A, "1"), "queer"), ((NS_A, "2"), "queer"), ((NS_B, "1"), "queer"), ((NS_B, "2"), "queer")]


def test_queering_gates_and_writes_each_selected_line_to_dialog_with_its_vanilla_hash(tmp_path: Path, every_line_queer: None) -> None:
    claude = _phase_claude()
    outcomes = _phase(tmp_path, claude)
    assert outcomes == PHASE_OUTCOMES
    assert {ref: (record["text"], record["vanilla"]) for ref, record in _dialog_records(tmp_path).items()} == {
        (NS_A, "1"): (OPUS_A1, vanilla_hash(EN_A1)),
        (NS_A, "2"): (OPUS_A2, vanilla_hash(EN_A2)),
        (NS_B, "1"): (OPUS_B1, vanilla_hash(EN_B1)),
        (NS_B, "2"): (OPUS_B2, vanilla_hash(EN_B2)),
    }


def test_queering_redo_listed_selected_line_is_redrafted_with_its_note(tmp_path: Path, every_line_queer: None) -> None:
    _redo_file(tmp_path, f'[{NS_A}]\n"1" = "{PHASE_NOTE}"\n')
    claude = _phase_claude()
    claude.redrafts = {(NS_A, "1"): [REDRAFT_A1]}
    outcomes = _phase(tmp_path, claude)
    noted = [parts for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA and f"Reviewer note:\n{PHASE_NOTE}" in parts]
    assert [_draft_refs(parts) for parts in noted] == [[(NS_A, "1")]]
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(
        NS_A, "1", 1, PHASE_SPEAKER, (ALPHA,), "changed", None, "opus", redo=True, bucket="queer"
    )
    assert _dialog_records(tmp_path)[(NS_A, "1")]["text"] == REDRAFT_A1


def test_queering_intent_keep_stays_unchanged_and_keeps_its_bucket(tmp_path: Path, every_line_queer: None) -> None:
    claude = _phase_claude()
    claude.keeps = {(NS_A, "1"): "plot"}
    outcomes = _phase(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(
        NS_A, "1", 1, PHASE_SPEAKER, (ALPHA,), "kept_for_plot", None, None, keep_reason="plot", bucket="queer"
    )
    assert [_draft_refs(parts) for parts in _speaker_draft_calls(claude)] == [[(NS_B, "1")]]
    assert set(_dialog_records(tmp_path)) == {(NS_A, "2"), (NS_B, "1"), (NS_B, "2")}


def test_queering_arc_revert_fails_a_selected_line_and_keeps_its_bucket(tmp_path: Path, every_line_queer: None) -> None:
    claude = _phase_claude()
    claude.bible = {1: PHASE_BIBLE_ONE, 2: {**PHASE_BIBLE_TWO, "arcs": [ARC_A_ENTRY]}}
    claude.arc = [_arc_verdict(False, (NS_A, "1")), _arc_verdict(False, (NS_A, "1"))]
    outcomes = _phase(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, PHASE_SPEAKER, (ALPHA,), "gate_failed", "arc", None, bucket="queer")
    assert set(_dialog_records(tmp_path)) == {(NS_A, "2"), (NS_B, "1"), (NS_B, "2")}


def test_queering_chapter_one_report_is_written_after_phase_two_and_counts_its_changes(tmp_path: Path, every_line_queer: None) -> None:
    _phase(tmp_path, _phase_claude())
    lines = _c01_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "chapter 1: 2 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched"
    assert "total: 2 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched" in lines


def test_queering_failed_speaker_draft_leaves_each_of_its_lines_unfinished_with_its_bucket(tmp_path: Path, every_line_queer: None) -> None:
    claude = _phase_claude()
    claude.drafts[(NS_A, "1")] = Unfinished("Made-up draft failure. Not game text.")
    outcomes = _phase(tmp_path, claude)
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, PHASE_SPEAKER, (ALPHA,), "unfinished", None, None, bucket="queer")
    assert _outcome_of(outcomes, (NS_B, "1")) == Outcome(NS_B, "1", 2, PHASE_SPEAKER, (ALPHA,), "unfinished", None, None, bucket="queer")
    assert set(_dialog_records(tmp_path)) == {(NS_A, "2"), (NS_B, "2")}


# Queering phase 2 coherence, judge and base tests, on the phase fixtures above. Made-up lines. Not game text.
def _queer_coherence_claude() -> FakeClaude:
    claude = FakeClaude()
    claude.flags = {D1: [ALPHA], D2: [ALPHA]}
    claude.drafts = {D1: [OPUS_D1], D2: [OPUS_D2]}
    return claude


def test_queering_scene_with_two_selected_changes_reverts_a_line_the_coherence_check_names_on_both_passes(
    tmp_path: Path, every_line_queer: None
) -> None:
    claude = _queer_coherence_claude()
    claude.coherence = [_verdict(False, D1), _verdict(False, D1)]
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(SCENE_D,), lens=QUEER_LENS)
    assert len(_coherence_calls(claude)) == 2
    assert outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "gate_failed", "scene", None, bucket="queer"),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "changed", None, "opus", bucket="queer"),
    ]
    assert _dialog_entries(tmp_path) == {D2}


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up coherence failure. Not game text."), Rejected("Made-up coherence rejection. Not game text.")],
)
def test_queering_scene_with_two_selected_changes_leaves_both_unfinished_when_coherence_fails(
    tmp_path: Path, every_line_queer: None, failure: Unfinished | Rejected
) -> None:
    claude = _queer_coherence_claude()
    claude.coherence = [failure]
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=(SCENE_D,), lens=QUEER_LENS)
    assert outcomes == [
        Outcome(NS_C, "2", 3, None, (ALPHA,), "unfinished", None, None, bucket="queer"),
        Outcome(NS_C, "3", 3, None, (ALPHA,), "unfinished", None, None, bucket="queer"),
    ]
    assert _dialog_entries(tmp_path) == set()


def test_queering_selected_line_candidates_are_judged_before_they_ship(tmp_path: Path, every_line_queer: None) -> None:
    claude = _phase_claude()
    _phase(tmp_path, claude)
    judged = _judged(claude, JUDGE_SCHEMA)
    assert {(ref, candidate) for ref, candidate in judged} >= {
        ((NS_A, "1"), OPUS_A1),
        ((NS_A, "2"), OPUS_A2),
        ((NS_B, "1"), OPUS_B1),
        ((NS_B, "2"), OPUS_B2),
    }


@pytest.mark.parametrize(
    ("failed", "kind", "gate"),
    [("minimal_change", "gate_failed", "minimal_change"), ("plot", "kept_for_plot", "plot")],
)
def test_queering_selected_line_whose_first_candidate_fails_the_judge_is_retried_once_through_its_scene_then_fails_its_gate(
    tmp_path: Path, every_line_queer: None, failed: str, kind: Literal["gate_failed", "kept_for_plot"], gate: str
) -> None:
    claude = _phase_claude()
    claude.judgments = {**claude.judgments, OPUS_A1: _judgment(**{failed: False})}
    outcomes = _phase(tmp_path, claude)
    line_calls = [parts for _, parts, schema in claude.calls if schema is DRAFT_SCHEMA and (NS_A, "1") in _draft_refs(parts)]
    speaker_calls = [parts for parts in line_calls if any(part.startswith("Scenes:\n") for part in parts)]
    retries = [parts for parts in line_calls if any(part.startswith("The previous candidates failed") for part in parts)]
    assert len(line_calls) == 2
    assert len(speaker_calls) == 1
    assert len(retries) == 1
    assert any(part.startswith("Targets:\n") for part in retries[0])
    assert _draft_refs(retries[0]) == [(NS_A, "1")]
    assert any(part == f"The previous candidates failed the {failed} gate. Write candidates that pass it." for part in retries[0])
    assert _outcome_of(outcomes, (NS_A, "1")) == Outcome(NS_A, "1", 1, PHASE_SPEAKER, (ALPHA,), kind, gate, None, bucket="queer")
    assert (NS_A, "1") not in _dialog_records(tmp_path)
    assert set(_dialog_records(tmp_path)) == {(NS_A, "2"), (NS_B, "1"), (NS_B, "2")}


def test_queering_selected_line_started_from_a_base_rewrite_records_the_game_hash_and_the_base_hash(
    tmp_path: Path, every_line_queer: None
) -> None:
    _base_file(tmp_path, _base_record(NS_A, "2", EN_A2, BASE_A2))
    claude = _phase_claude()
    outcomes = _run(tmp_path, claude, [], jev=None, renderer=FakeRenderer({}), scenes=PHASE_SCENES, lens=replace(QUEER_LENS, base=BASE))
    prompts = [part for _, parts, _ in claude.calls for part in parts]
    assert not any(EN_A2 in text for text in prompts)
    assert any(BASE_A2 in text for text in prompts)
    assert _outcome_of(outcomes, (NS_A, "2")) == Outcome(NS_A, "2", 1, None, (ALPHA,), "changed", None, "opus", bucket="queer")
    chapter_one = tomllib.loads((tmp_path / "repo" / "dialog" / LENS / "chapter-01.toml").read_text(encoding="utf-8"))
    assert list(chapter_one[NS_A]["2"]) == BASE_FIELD_ORDER
    assert chapter_one[NS_A]["2"] == {
        "vanilla": vanilla_hash(EN_A2),
        "base": vanilla_hash(BASE_A2),
        "text": OPUS_A2,
        "lens": LENS,
        "reason": [ALPHA],
        "writer": "opus",
        "verdict": "passed",
        "run": RUN,
    }


THROUGH_SCENES = (
    Scene("made-up-scene-a", (_line(NS_A, "1", "made-up-speaker-a", EN_A1, 1), _line(NS_A, "2", None, EN_A2, 1))),
    Scene("made-up-scene-b", (_line(NS_B, "1", "made-up-speaker-a", EN_B1, 2), _line(NS_B, "2", None, EN_B2, 2))),
)
THROUGH_BIBLE: dict[str, Any] = {"characters": [NOTES_A], "facts": [], "callbacks": [], "arcs": [ARC_A_ENTRY]}


def test_queering_through_takes_the_core_cast_from_the_whole_game_bible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The made-up speaker has two distinct lines in the whole game and one through chapter 1, and its one arc is in the chapter 2 bible, so only the whole game makes it core.
    monkeypatch.setattr(queering, "CORE_LINES", 2)
    monkeypatch.setattr(queering, "CORE_ARCS", 1)
    claude = FakeClaude()
    claude.bible = {2: THROUGH_BIBLE}
    claude.flags = {(NS_A, "2"): [ALPHA]}
    outcomes = _run(tmp_path, claude, [], jev=None, scenes=THROUGH_SCENES, lens=QUEER_LENS, through=1)
    assert _screens(claude) == [(claude_module.HAIKU, [{"namespace": NS_A, "key": "2"}])]
    assert outcomes == [
        Outcome(NS_A, "1", 1, "made-up-speaker-a", (), "untouched", None, None),
        Outcome(NS_A, "2", 1, None, (ALPHA,), "untouched", None, None, bucket="untouched"),
    ]


BASE_STORE_KEYS = [
    "0f954c5161ccc32919dce4c1e134415fd65c9616aee1ee267819a57b684c71c9",
    "1973ad859fecb7aa6d8f905f2ed4a6d1b8c70cc126299b55556dafbb9bbec76b",
    "1c906c821e9e4cd52a56ad08c8579b0e2401d89ae9f77e3247592e248909aa11",
    "1ec48dd8746fe794ec03e6bd800448c2ac17dbd3fe14b87d9a5f64fce0d3d4dc",
    "204768b22efff7f04f11615d05cae61f4618f20f4ddb1782dddba05a62ece72b",
    "267f040653cfe1fa6774d6295800103c0de1eebdddcf9dca63e598a15df617bd",
    "30e8c0b950e206e15bf6c2134854971126b7142f1b9ade8848b5e30ed212c244",
    "31f1aa279cdcae0c30711826292956e4eb327211964796c627d3d434af210995",
    "4edd7e1f3eae41280e6ff9fa0ce498c6da9e446aa0f7cbeefc1b3e05a84ffe0e",
    "5a9aded30398cd92521aaa26eb3e3aff7b7cdb8eee77b4b770f15c1f653f0924",
    "61188a0ffabc6f0f55aaee66af442b4f301c706a86cb395bd4b0b73817201919",
    "6215c3f013265b41ad49c02ecd8bc5121713fd5be7a0888d8b90a4697bbc8425",
    "71f523b32c56aacba71962632f774f4e5734fb65a53b82bd7d618e6aa9879a1b",
    "72bdb606c3e289217c94fc4b826140315748244636c939ab04377612ccea9454",
    "77a384b69f563389f94dd9a32268d8fc1e47606d43164aa85cdb92aa058f99ec",
    "8545341d04894994679e8cbdb45107660885d1a23ffbbfef0126d8cd1091eed8",
    "9247cccff6e5c62b349c1fda10ae7c227e4ab2723202b8f2e51541bae7296a92",
    "99820fdd20ce2a79b92a92906e201e25e531db72ace59e8e4e0ad007b013532b",
    "9a2d17922ab4f218a02d45e129bbb3351b7b11f5b48b95d47c417e5309725f4b",
    "9de6840b5925a6e8d7ed99ad90d3bef8a66e80c3c6d526bf4822134798440a30",
    "a091aeaf3a85742dd76a09a040f99d1bb13ffecf1c194d930bd005cddcf7fef5",
    "adc7a230aa15effc7ffe7e8b7dc4c8bb6e67a9a14f3df6975901c2cc9dd45fae",
    "c0d74faedd0ccdf9babf08f276b33417f24f8c376fe386ec0fae6b448026cd68",
    "c61f5c450d221cf9e77cc1d34a9386e3408caaddbad4ffae2eee462d3d51b38f",
    "c80e2ad106b10f39aed67a7b577d7967a1eb83fb9e74c690d8dba503969da3a4",
    "c8675946ea69be91e5fa793d7c8f03912f28ffb75b0830919aac683dc2301d65",
    "c9fe13620cb28023ecde6922d0e49f3fb4e031482a185be06fed754996c9f550",
    "cab7f0411f1523e2a40e05a0b4582744fb32fbbf4135f7c80b09d6193402682c",
    "ccd56503b2f6e8d657b2bbdc0b894795b804b2121d897e5707173a2a03d64897",
    "d38dc0b69c4ad781c20a6fe150a24e57a9c8d43b1112c32917de0d5fe9434f18",
    "d5c7148a421b19b580592fc9edfd4ce3e0831c70cb2e9e7a1a122d97c29a2396",
    "e0570d655d522ca0fbd0ea642623ef342ff7d0baa1eabaf71b7149ebb0199389",
    "e06f76b30e0c8c0545d681a170858418c9f1c14e825ad642fa0e38c601d81879",
    "ef9bfdff62d6d9535faba38e728544bf00cea5be939bcd446d19e70516dd1498",
    "f2f39a1bee9b9f5c7fb32ae66566ed4ccb085969dc7f83949c6019623617a2c3",
    "fade91289ff7dd32b8db1d0c24c916421c76519414cb7e300ce55958244a3261",
]


def test_lens_without_selection_keeps_its_store_keys(tmp_path: Path) -> None:
    _run(tmp_path, _claude(), [], jev=FakeJev(JEV_HIGH))
    assert Store(tmp_path / "work" / LENS).finished() == BASE_STORE_KEYS


def test_bucket_is_written_only_for_a_marked_outcome(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    marked = replace(OUTCOMES[0], outcome="untouched", bucket="queer")
    write_outcomes(path, [marked, OUTCOMES[1]])
    marked_record, unmarked_record = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert marked_record["bucket"] == "queer"
    assert list(marked_record)[-2:] == ["redo", "bucket"]
    assert "bucket" not in unmarked_record
    assert [record.outcome for record in report._records(path, TRIAGE)] == ["untouched", "changed"]
