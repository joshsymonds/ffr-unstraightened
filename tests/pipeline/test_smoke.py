import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline import arcs, bible, coherence, stages
from ffrd.pipeline import jev as jev_module
from ffrd.pipeline.claude import Unfinished
from ffrd.pipeline.lens import TriageQuestion
from ffrd.pipeline.smoke import cmd_smoke, register
from ffrd.pipeline.stages import (
    CROSS_SCHEMA,
    DRAFT_SCHEMA,
    INTENT_SCHEMA,
    JUDGE_SCHEMA,
    JUDGE_VERDICTS,
    RESCREEN_SCHEMA,
    SCREEN_SCHEMA,
    Ask,
    Bindings,
    Scene,
    SceneLine,
)

# Made-up scene keys, namespaces and lines. Not game text.
SCENE_A = "made-up-scene-a"
SCENE_B = "made-up-scene-b"
SCENE_ONE_SPEAKER = "made-up-scene-one-speaker"
SCENE_NO_SCOPE = "made-up-scene-no-scope"
NS_A = "made-up-ns-a"
NS_B = "made-up-ns-b"
NS_ONE = "made-up-ns-one"
NS_HARD = "ST_UI_Made"
SPEAKER_A = "made-up-speaker-a"
SPEAKER_B = "made-up-speaker-b"
SMOKE_OUTPUT_FORBIDDEN = ("Made-up english", "Not game text", "Reworded", "Voiced", "Made-up japanese")


def _line(namespace: str, key: str, speaker: str | None, *, chapter: int | None = 1) -> SceneLine:
    # Made-up English line. Not game text.
    return SceneLine(namespace, key, speaker, f"Made-up english {key}. Not game text.", f"Made-up japanese {key}.", chapter)


def _speaker_scene(key: str, namespace: str) -> Scene:
    return Scene(key, (_line(namespace, "1", SPEAKER_A), _line(namespace, "2", SPEAKER_B), _line(namespace, "3", None)))


def _reworded(english: str) -> str:
    return english.replace("Made-up", "Reworded", 1)


def _section(parts: Sequence[str], heading: str) -> Any:
    for part in parts:
        if part.startswith(heading):
            return json.loads(part[len(heading) :])
    raise AssertionError(f"no prompt part starts with {heading!r}")


class FakeClaude:
    """Answers every stage from the prompt it is given, so a full smoke run reaches every stage. Not game text."""

    def __init__(self, *, arcs_in_bible: bool = True, unfinished_intent: frozenset[str] = frozenset()) -> None:
        self.arcs_in_bible = arcs_in_bible
        self.unfinished_intent = unfinished_intent

    def ask(self, model: str, parts: Sequence[str], schema: Mapping[str, Any]) -> Any:
        if schema is bible.SCHEMA:
            return self._bible(parts)
        if schema is bible.SETUP_SCHEMA:
            return {"setups": []}
        if schema is SCREEN_SCHEMA:
            speakers = [line for line in _section(parts, "Scene lines:\n") if line["speaker"]][:2]
            return {"flagged": [{"namespace": s["namespace"], "key": s["key"], "reasons": ["smoke-fixable"]} for s in speakers]}
        if schema is RESCREEN_SCHEMA:
            return {"flagged": []}
        if schema is INTENT_SCHEMA:
            target = _section(parts, "Target line:\n")
            if target["key"] in self.unfinished_intent:
                return Unfinished("Made-up intent failure. Not game text.")
            return {"intent": "Made-up intent. Not game text."}
        if schema is DRAFT_SCHEMA:
            english = {(line["namespace"], line["key"]): line["english"] for line in _section(parts, "Scene lines:\n")}
            targets = _section(parts, "Targets:\n")
            entries = [
                {"namespace": t["namespace"], "key": t["key"], "drafts": [_reworded(english[(t["namespace"], t["key"])])]} for t in targets
            ]
            return {"drafts": entries}
        if schema is JUDGE_SCHEMA:
            return {verdict: True for verdict in JUDGE_VERDICTS}
        if schema is CROSS_SCHEMA:
            return {"consistent": True}
        if schema is coherence.SCHEMA:
            return {"coherent": True, "revert": []}
        if schema is arcs.SCHEMA:
            return {"holds": True, "revert": []}
        raise AssertionError("unexpected schema")

    def _bible(self, parts: Sequence[str]) -> dict[str, Any]:
        lines = [line for line in _section(parts, "Chapter 1 lines:\n") if line["speaker"]]
        speakers = list(dict.fromkeys(line["speaker"] for line in lines))
        refs = [{"namespace": line["namespace"], "key": line["key"]} for line in lines]
        callbacks = [{"line": refs[0], "depends": [refs[1]], "meaning": "Made-up meaning. Not game text."}] if len(refs) >= 2 else []
        arcs_out = (
            [
                {
                    "id": "made-up-arc",
                    "characters": speakers,
                    "state": "Made-up state. Not game text.",
                    "beats": [{"line": refs[0], "kind": "setup", "earned": True}],
                }
            ]
            if self.arcs_in_bible and refs
            else []
        )
        characters = [{"speaker": s, "notes": "Made-up notes. Not game text.", "gender": "made-up"} for s in speakers]
        return {"characters": characters, "facts": [], "callbacks": callbacks, "arcs": arcs_out}


class FakeJev:
    """Stands in for the Jev triage call, with one answer for every scene. Not game text."""

    def __init__(self, answer: dict[str, float]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, scene: str, questions: Sequence[TriageQuestion]) -> dict[str, float]:
        self.calls.append((scene, [question.id for question in questions]))
        return dict(self.answer)


class FakeRenderer:
    """Stands in for the voice render: a voiced variant of each draft. Not game text."""

    def render(self, speaker: str | None, text: str) -> str:
        return text.replace("Reworded", "Voiced", 1)


class FakeClassifier:
    threshold = 0.5

    def score(self, text: str) -> float:
        return 1.0


class FakeSource:
    def __init__(self, scenes: Sequence[Scene], *, scene_table: bool) -> None:
        self._scenes = list(scenes)
        self.scene_table = scene_table

    def scenes(self) -> Sequence[Scene]:
        return self._scenes


JEV_HIGH = {"smoke-fixable": 0.9, "smoke-second": 0.9}

FULL_STAGE_LINES = [
    "stage bible: 1",
    "stage bible_setups: 1",
    "stage screen: 1",
    "stage triage: 1",
    "stage rescreen: 1",
    "stage intent: 2",
    "stage draft: 1",
    "stage render: 2",
    "stage classifier: 4",
    "stage judge: 4",
    "stage cross_judge: 4",
    "stage scene_coherence: 1",
    "stage arc_judge: 1",
    "stage selection: 3",
]


@pytest.fixture
def smoke_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    monkeypatch.chdir(root)
    return root


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    claude: FakeClaude,
    jev: FakeJev,
    scenes: Sequence[Scene],
    *,
    scene_table: bool = True,
) -> None:
    class Batch:
        def __init__(self) -> None:
            self.ask: Ask = claude.ask

    def bindings() -> Bindings:
        return Bindings(scenes=FakeSource(scenes, scene_table=scene_table), classifier=FakeClassifier(), renderer=FakeRenderer())

    monkeypatch.setattr(stages, "bindings", bindings)
    monkeypatch.setattr(jev_module, "triage", jev)
    monkeypatch.setattr(stages, "Batch", Batch)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ffrd")
    register(parser.add_subparsers(required=True))
    return parser.parse_args(argv)


def _files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def test_register_declares_the_smoke_command_with_an_optional_scene_key() -> None:
    assert _parse(["smoke"]).fn is cmd_smoke
    assert _parse(["smoke"]).scene is None
    assert _parse(["smoke", "--scene", SCENE_B]).scene == SCENE_B


def test_run_reaches_every_stage_prints_keys_and_counts_and_writes_only_under_work_smoke(
    smoke_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claude = FakeClaude()
    jev = FakeJev(JEV_HIGH)
    _bind(monkeypatch, claude, jev, [_speaker_scene(SCENE_A, NS_A)])
    before = _files(smoke_repo)

    cmd_smoke(_parse(["smoke"]))

    printed = capsys.readouterr().out.splitlines()
    assert printed == [
        "prefix ST_C<n>_: 0",
        "prefix ST_SubEv: 0",
        "prefix ST_ChrEv: 0",
        "prefix ST_FEV: 0",
        "prefix ST_LegendEV: 0",
        "prefix ST_GimEv: 0",
        "prefix ST_BtlEv: 0",
        "prefix ST_EV: 0",
        "prefix ST_DialogChoice: 0",
        "prefix ST_MainSummary: 0",
        "prefix ST_UnitDescription: 0",
        "prefix outside fallback: 3",
        "prefix hard excluded: 0",
        f"scene {SCENE_A}: 2 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched",
        "total: 2 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched",
        *FULL_STAGE_LINES,
    ]
    assert not any(text in line for line in printed for text in SMOKE_OUTPUT_FORBIDDEN)
    assert len(jev.calls) == 1
    new = _files(smoke_repo) - before
    assert new, "the run wrote nothing"
    assert all(path.parts[:2] == ("work", "smoke") for path in new)
    assert not (smoke_repo / "dialog").exists()
    assert (smoke_repo / "work" / "smoke" / "dialog" / "smoke" / "chapter-01.toml").is_file()


def test_a_stage_the_run_never_reaches_fails_naming_it(
    smoke_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bind(monkeypatch, FakeClaude(arcs_in_bible=False), FakeJev(JEV_HIGH), [_speaker_scene(SCENE_A, NS_A)])

    with pytest.raises(Fail, match=r"^smoke: no result from arc_judge$"):
        cmd_smoke(_parse(["smoke"]))

    out = capsys.readouterr().out.splitlines()
    assert "stage arc_judge: 0" in out
    assert "stage selection: 3" in out


def test_an_unfinished_outcome_fails_the_selection_stage(
    smoke_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bind(
        monkeypatch,
        FakeClaude(unfinished_intent=frozenset({"2"})),
        FakeJev(JEV_HIGH),
        [_speaker_scene(SCENE_A, NS_A)],
    )

    with pytest.raises(Fail, match=r"^smoke: no result from scene_coherence, selection$"):
        cmd_smoke(_parse(["smoke"]))

    out = capsys.readouterr().out.splitlines()
    assert "total: 1 changed, 0 kept for plot, 0 gate failed, 1 unfinished, 1 untouched" in out
    assert "stage selection: 0" in out


def test_default_pick_is_the_first_scene_with_two_spoken_lines(
    smoke_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    one_speaker = Scene(SCENE_ONE_SPEAKER, (_line(NS_ONE, "1", SPEAKER_A), _line(NS_ONE, "2", None)))
    _bind(monkeypatch, FakeClaude(), FakeJev(JEV_HIGH), [one_speaker, _speaker_scene(SCENE_A, NS_A), _speaker_scene(SCENE_B, NS_B)])

    cmd_smoke(_parse(["smoke"]))

    scene_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("scene ")]
    assert [line.split(":")[0] for line in scene_lines] == [f"scene {SCENE_A}"]


def test_scene_key_picks_that_scene_and_an_unknown_key_fails_naming_it(
    smoke_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    no_scope = Scene(SCENE_NO_SCOPE, (SceneLine(NS_HARD, "1", SPEAKER_A, "Made-up english. Not game text.", "Made-up japanese.", 1),))
    _bind(monkeypatch, FakeClaude(), FakeJev(JEV_HIGH), [_speaker_scene(SCENE_A, NS_A), _speaker_scene(SCENE_B, NS_B), no_scope])

    cmd_smoke(_parse(["smoke", "--scene", SCENE_B]))
    scene_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("scene ")]
    assert [line.split(":")[0] for line in scene_lines] == [f"scene {SCENE_B}"]

    for missing in ("made-up-scene-missing", SCENE_NO_SCOPE):
        with pytest.raises(Fail, match=re.escape(missing)):
            cmd_smoke(_parse(["smoke", "--scene", missing]))


def test_prefix_report_counts_each_family_once_and_the_table_only_and_hard_excluded_lines(
    smoke_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    family_lines = [
        ("ST_C1_Made", "1"),
        ("ST_C12_Made", "1"),
        ("ST_SubEv_Made", "1"),
        ("ST_ChrEv_Made", "1"),
        ("ST_FEV_Made", "1"),
        ("ST_LegendEV_Made", "1"),
        ("ST_GimEv_Made", "1"),
        ("ST_BtlEv_Made", "1"),
        ("ST_EV_Made", "1"),
        ("ST_DialogChoice", "1"),
        ("ST_MainSummary_Made", "1"),
        ("ST_UnitDescription_Made", "1"),
        ("ST_DialogChoiceExtra", "1"),
        ("ST_Made_Story", "1"),
        ("ST_UI_Made", "1"),
        ("ST_C2_MadeName", "1"),
    ]
    first = Scene(
        "made-up-scene-families",
        tuple(SceneLine(ns, key, None, "Made-up english. Not game text.", "Made-up japanese.", None) for ns, key in family_lines),
    )
    repeat = Scene(
        "made-up-scene-repeat", (SceneLine("ST_SubEv_Made", "1", None, "Made-up english. Not game text.", "Made-up japanese.", None),)
    )
    _bind(monkeypatch, FakeClaude(), FakeJev(JEV_HIGH), [first, repeat, _speaker_scene(SCENE_A, NS_A)])

    cmd_smoke(_parse(["smoke"]))

    printed = capsys.readouterr().out.splitlines()
    assert printed[:13] == [
        "prefix ST_C<n>_: 2",
        "prefix ST_SubEv: 1",
        "prefix ST_ChrEv: 1",
        "prefix ST_FEV: 1",
        "prefix ST_LegendEV: 1",
        "prefix ST_GimEv: 1",
        "prefix ST_BtlEv: 1",
        "prefix ST_EV: 1",
        "prefix ST_DialogChoice: 1",
        "prefix ST_MainSummary: 1",
        "prefix ST_UnitDescription: 1",
        # Two family-scene lines plus the three lines of the speaker scene, all outside the fallback allowlist.
        "prefix outside fallback: 5",
        "prefix hard excluded: 2",
    ]
