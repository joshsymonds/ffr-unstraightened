import argparse
import json
import re
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest

from ffrd.game import Fail, vanilla_hash
from ffrd.pipeline import arcs, bible, coherence, report, stages
from ffrd.pipeline import claude as claude_module
from ffrd.pipeline import jev as jev_module
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.run import Change, Outcome, cmd_run, register, run, write_dialog, write_outcomes
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
OUTCOME_KEYS = {"namespace", "key", "chapter", "speaker", "reasons", "outcome", "gate", "writer"}
FIELD_ORDER = ["vanilla", "text", "lens", "reason", "writer", "verdict", "run"]

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


def _target(parts: Sequence[str]) -> tuple[str, str]:
    ref = json.loads(_part(parts, "Target line:\n"))
    return ref["namespace"], ref["key"]


def _draft_refs(parts: Sequence[str]) -> list[tuple[str, str]]:
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
        # Lines whose intent answer says keep, so the line stays unchanged. Not game text.
        self.keeps: set[tuple[str, str]] = set()
        self.drafts: dict[tuple[str, str], list[str] | Unfinished | Rejected] = {}
        self.judgments: dict[str, dict[str, bool]] = {}
        self.judge_failures: dict[str, Unfinished | Rejected] = {}
        self.consistent: dict[str, bool] = {}
        self.cross_failures: dict[str, Unfinished | Rejected] = {}
        # Answers for coherence calls in order, first pass then recheck; an empty queue answers coherent. Not game text.
        self.coherence: list[dict[str, Any] | Unfinished | Rejected] = []
        # Answers for arc judge calls in order, first pass then recheck; an empty queue answers holds with no reverts. Not game text.
        self.arc: list[dict[str, Any] | Unfinished | Rejected] = []

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
            return answer if isinstance(answer, Unfinished) else {"intent": answer, "keep": ref in self.keeps}
        if schema is DRAFT_SCHEMA:
            refs = _draft_refs(parts)
            for ref in refs:
                answer = self.drafts[ref]
                if isinstance(answer, Unfinished | Rejected):
                    return answer
            return {"drafts": [{"namespace": namespace, "key": key, "drafts": self.drafts[(namespace, key)]} for namespace, key in refs]}
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
            return self.coherence.pop(0) if self.coherence else {"coherent": True, "revert": []}
        if schema is arcs.SCHEMA:
            return self.arc.pop(0) if self.arc else {"holds": True, "revert": []}
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
) -> list[Outcome]:
    return run(
        tmp_path / "repo",
        LENS_OBJ,
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
    claude.keeps = {(NS_A, "1")}
    outcomes = _run(tmp_path, claude, [], jev=FakeJev(JEV_HIGH))
    assert outcomes[0] == Outcome(NS_A, "1", 1, "made-up-speaker-a", (ALPHA,), "untouched", None, None)
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
    return [parts for _, parts, schema in claude.calls if schema is coherence.SCHEMA]


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
    lens_repo: Path, capsys: pytest.CaptureFixture[str]
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
    "total: 1 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "  gate minimal_change: 1\n"
)
EXPECTED_PROVISIONAL_ARC = (
    "chapter 1: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "total: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
)
EXPECTED_PROVISIONAL_MIXED = (
    "chapter 1: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
    "total: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched\n"
    f"  reason {ALPHA}: 1\n"
    "  character 1: 1\n"
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
        "total: 2 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched\n"
        f"  reason {ALPHA}: 2\n"
        "  character 1: 2\n"
        "  gate minimal_change: 1\n"
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
