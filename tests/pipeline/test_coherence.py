import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.coherence import INSTRUCTIONS, RECHECK_INSTRUCTIONS, SCHEMA, STAGE, check
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.stages import Scene, SceneLine
from ffrd.pipeline.store import Store

# Made-up scene, lens, arcs and answers. Not game text.
LINE_ONE = SceneLine("ST_C01_001_01", "1", "Ash", "Made-up first line. Not game text.", "Made-up Japanese one. Not game text.", 1)
LINE_TWO = SceneLine("ST_C01_001_01", "2", None, "Made-up narration. Not game text.", "Made-up Japanese two. Not game text.", 1)
LINE_THREE = SceneLine("ST_C01_001_01", "3", "Brin", "Made-up third line. Not game text.", "Made-up Japanese three. Not game text.", 1)
SCENE = Scene("made-up-scene", (LINE_ONE, LINE_TWO, LINE_THREE))
REWRITES = {
    ("ST_C01_001_01", "1"): "Made-up rewrite one. Not game text.",
    ("ST_C01_001_01", "2"): "Made-up rewrite two. Not game text.",
    ("ST_C01_001_01", "3"): "Made-up rewrite three. Not game text.",
}
ARCS = [{"arc": "made-up-arc. Not game text.", "lines": [{"namespace": "ST_C01_001_01", "key": "1"}]}]
OTHER_ARCS = [{"arc": "made-up-other-arc. Not game text.", "lines": []}]
UNKNOWN_REF = {"namespace": "ST_C99_999_99", "key": "9"}

LENS = Lens(
    name="made-up",
    triage=(TriageQuestion("made-up-alpha", "Made-up question alpha. Not game text."),),
    screen_criteria=("Made-up criterion one. Not game text.",),
    intent_rule="Made-up intent rule. Not game text.",
    judge_rubric="Made-up judge rubric. Not game text.",
)


def _ref(line: SceneLine) -> tuple[str, str]:
    return (line.namespace, line.key)


def _ref_dict(line: SceneLine) -> dict[str, str]:
    return {"namespace": line.namespace, "key": line.key}


def _answer(coherent: bool, *lines: SceneLine) -> dict[str, Any]:
    return {"coherent": coherent, "revert": [_ref_dict(line) for line in lines]}


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _scene_dicts() -> list[dict[str, Any]]:
    return [
        {
            "namespace": line.namespace,
            "key": line.key,
            "speaker": line.speaker,
            "english": line.english,
            "rewrite": REWRITES.get(_ref(line)),
        }
        for line in SCENE.lines
    ]


class Fake:
    """Stands in for the Opus call: returns queued answers, raises queued exceptions, records each call's arguments."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _run(store: Store, ask: Fake, lens: Lens = LENS, arcs: Any = ARCS) -> Any:
    return check(store, lens, SCENE, REWRITES, arcs, ask)


def test_coherent_first_pass_returns_no_reverts_after_one_call(tmp_path: Path) -> None:
    ask = Fake(_answer(True))

    assert _run(Store(tmp_path), ask) == frozenset()
    assert len(ask.calls) == 1
    assert ask.calls[0][0] == claude.OPUS


def test_first_pass_prompt_carries_instructions_rubric_arcs_and_scene_lines(tmp_path: Path) -> None:
    ask = Fake(_answer(True))

    _run(Store(tmp_path), ask)

    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema is SCHEMA
    assert parts == [
        INSTRUCTIONS,
        "Judge rubric:\n" + LENS.stage_text("judge"),
        "Arcs:\n" + _j(ARCS),
        "Scene lines:\n" + _j(_scene_dicts()),
    ]


def test_line_reverts_only_when_both_passes_name_it_and_recheck_sees_proposed_reverts(tmp_path: Path) -> None:
    ask = Fake(_answer(False, LINE_ONE, LINE_TWO), _answer(False, LINE_ONE, LINE_THREE))

    assert _run(Store(tmp_path), ask) == frozenset({_ref(LINE_ONE)})
    assert len(ask.calls) == 2

    _, recheck_parts, recheck_schema = ask.calls[1]
    assert recheck_schema is SCHEMA
    assert recheck_parts[0] == RECHECK_INSTRUCTIONS
    assert "Scene lines:\n" + _j(_scene_dicts()) in recheck_parts
    assert recheck_parts[-1] == "Proposed reverts:\n" + _j([_ref_dict(LINE_ONE), _ref_dict(LINE_TWO)])


def test_coherent_recheck_returns_no_reverts(tmp_path: Path) -> None:
    ask = Fake(_answer(False, LINE_ONE), _answer(True))

    assert _run(Store(tmp_path), ask) == frozenset()
    assert len(ask.calls) == 2


def test_fresh_store_on_same_root_returns_stored_result_without_asking(tmp_path: Path) -> None:
    _run(Store(tmp_path), Fake(_answer(False, LINE_ONE, LINE_TWO), _answer(False, LINE_ONE, LINE_THREE)))
    ask = Fake()

    assert _run(Store(tmp_path), ask) == frozenset({_ref(LINE_ONE)})
    assert ask.calls == []


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_first_pass_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, _answer(True))

    assert _run(store, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert _run(store, ask) == frozenset()
    assert len(ask.calls) == 2


def test_failed_recheck_is_returned_and_first_pass_stays_stored(tmp_path: Path) -> None:
    store = Store(tmp_path)
    failure = Unfinished("Made-up unfinished detail. Not game text.")
    first = Fake(_answer(False, LINE_ONE, LINE_TWO), failure)

    assert _run(store, first) == failure
    assert len(store.finished()) == 1
    assert len(store.unfinished()) == 1

    retry = Fake(_answer(False, LINE_ONE))

    assert _run(store, retry) == frozenset({_ref(LINE_ONE)})
    assert len(retry.calls) == 1
    assert retry.calls[0][1][-1] == "Proposed reverts:\n" + _j([_ref_dict(LINE_ONE), _ref_dict(LINE_TWO)])


def test_revert_naming_no_rewritten_line_is_rejected_and_nothing_stored(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake({"coherent": False, "revert": [UNKNOWN_REF]})

    result = _run(store, ask)

    assert isinstance(result, Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_recheck_revert_naming_no_rewritten_line_is_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(_answer(False, LINE_ONE), {"coherent": False, "revert": [UNKNOWN_REF]})

    result = _run(store, ask)

    assert isinstance(result, Rejected)
    assert len(store.finished()) == 1
    assert len(store.unfinished()) == 1


@pytest.mark.parametrize(
    "changed",
    [{"arcs": OTHER_ARCS}, {"lens": replace(LENS, judge_rubric="Made-up changed rubric. Not game text.")}],
    ids=["arcs", "rubric"],
)
def test_changed_arcs_or_rubric_asks_again(tmp_path: Path, changed: dict[str, Any]) -> None:
    _run(Store(tmp_path), Fake(_answer(True)))
    ask = Fake(_answer(True))

    assert _run(Store(tmp_path), ask, **changed) == frozenset()
    assert len(ask.calls) == 1


def test_stage_name_is_scene_coherence() -> None:
    assert STAGE == "scene_coherence"
