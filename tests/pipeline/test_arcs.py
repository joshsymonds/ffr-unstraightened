import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ffrd.pipeline import claude
from ffrd.pipeline.arcs import (
    INSTRUCTIONS,
    RECHECK_INSTRUCTIONS,
    SCHEMA,
    STAGE,
    Verdicts,
    cast,
    consolidate,
    context,
    judge,
    lines,
    with_changes,
)
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.stages import Scene, SceneLine
from ffrd.pipeline.store import Store

# Made-up bibles and arcs. Not game text.
BIBLE_ONE = {
    "chapter": 1,
    "characters": [
        {"speaker": "Ash", "notes": "Made-up note about Ash in chapter one. Not game text."},
        {"speaker": "Brin", "notes": "Made-up note about Brin in chapter one. Not game text."},
    ],
    "arcs": [
        {
            "id": "arc-a",
            "characters": ["Ash", "Brin"],
            "state": "Made-up state one. Not game text.",
            "beats": [
                {
                    "line": {"namespace": "ST_C01_001_01", "key": "1"},
                    "kind": "setup",
                    "earned": False,
                }
            ],
        },
        {
            "id": "arc-b",
            "characters": ["Brin"],
            "state": "Made-up state for arc b. Not game text.",
            "beats": [],
        },
    ],
}

BIBLE_UNPLACED = {
    "chapter": None,
    "characters": [
        {
            "speaker": "Ash",
            "notes": "Made-up note about Ash, unplaced. Not game text.",
            "gender": "Made-up gender value. Not game text.",
        },
        {"speaker": "Corr", "notes": "Made-up note about Corr, unplaced. Not game text."},
    ],
    "arcs": [
        {
            "id": "arc-a",
            "characters": ["Corr", "Ash"],
            "state": "Made-up later state. Not game text.",
            "beats": [
                {
                    "line": {"namespace": "ST_C00_002_01", "key": "3"},
                    "kind": "payoff",
                    "earned": True,
                }
            ],
        },
    ],
}

BIBLE_TWO = {
    "chapter": 2,
    "characters": [
        {"speaker": "Brin", "notes": "Made-up note about Brin in chapter two. Not game text."},
    ],
    "arcs": [
        {
            "id": "arc-c",
            "characters": ["Brin"],
            "state": "Made-up state for arc c. Not game text.",
            "beats": [
                {
                    "line": {"namespace": "ST_C02_001_01", "key": "1"},
                    "kind": "setup",
                    "earned": False,
                }
            ],
        },
        {
            "id": "arc-a",
            "characters": ["Brin"],
            "state": "Made-up state two. Not game text.",
            "beats": [],
        },
    ],
}

BIBLES = [BIBLE_ONE, BIBLE_UNPLACED, BIBLE_TWO]


def test_consolidate_merges_one_arc_id_across_chapters_and_none() -> None:
    arcs = consolidate(BIBLES)
    arc_a = next(arc for arc in arcs if arc["id"] == "arc-a")

    assert arc_a["states"] == [
        {"chapter": 1, "state": "Made-up state one. Not game text."},
        {"chapter": None, "state": "Made-up later state. Not game text."},
        {"chapter": 2, "state": "Made-up state two. Not game text."},
    ]
    assert arc_a["beats"] == [
        {
            "line": {"namespace": "ST_C01_001_01", "key": "1"},
            "kind": "setup",
            "earned": False,
            "chapter": 1,
        },
        {
            "line": {"namespace": "ST_C00_002_01", "key": "3"},
            "kind": "payoff",
            "earned": True,
            "chapter": None,
        },
    ]
    assert arc_a["characters"] == ["Ash", "Brin", "Corr"]


def test_consolidate_keeps_first_appearance_order_of_ids() -> None:
    arcs = consolidate(BIBLES)

    assert [arc["id"] for arc in arcs] == ["arc-a", "arc-b", "arc-c"]


def test_consolidate_gives_one_record_per_distinct_id() -> None:
    arcs = consolidate(BIBLES)

    assert len(arcs) == 3
    arc_b = next(arc for arc in arcs if arc["id"] == "arc-b")
    assert arc_b == {
        "id": "arc-b",
        "characters": ["Brin"],
        "states": [{"chapter": 1, "state": "Made-up state for arc b. Not game text."}],
        "beats": [],
    }


def test_context_keeps_only_arcs_sharing_a_scene_speaker() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, ["Ash"], "Brin")

    assert [arc["id"] for arc in result["arcs"]] == ["arc-a"]


def test_context_keeps_given_arc_order() -> None:
    arcs = consolidate(BIBLES)

    result = context(list(reversed(arcs)), BIBLES, ["Brin"], "Brin")

    assert [arc["id"] for arc in result["arcs"]] == ["arc-c", "arc-b", "arc-a"]


def test_context_ignores_none_speakers_when_selecting_arcs() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, [None], None)

    assert result["arcs"] == []


def test_context_returns_target_notes_from_each_chapter() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, ["Ash"], "Ash")

    assert result["characters"] == [
        {
            "notes": "Made-up note about Ash in chapter one. Not game text.",
            "chapter": 1,
        },
        {
            "notes": "Made-up note about Ash, unplaced. Not game text.",
            "gender": "Made-up gender value. Not game text.",
            "chapter": None,
        },
    ]


def test_context_returns_no_character_notes_for_none_speaker() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, ["Ash", None], None)

    assert result["characters"] == []


def test_cast_returns_every_scene_speaker_entry_with_chapter() -> None:
    result = cast(BIBLES, ["Ash", "Corr", None])

    assert result == [
        {"speaker": "Ash", "notes": "Made-up note about Ash in chapter one. Not game text.", "chapter": 1},
        {
            "speaker": "Ash",
            "notes": "Made-up note about Ash, unplaced. Not game text.",
            "gender": "Made-up gender value. Not game text.",
            "chapter": None,
        },
        {
            "speaker": "Corr",
            "notes": "Made-up note about Corr, unplaced. Not game text.",
            "chapter": None,
        },
    ]


def test_cast_ignores_none_in_speakers() -> None:
    assert cast(BIBLES, [None]) == []


def test_functions_leave_inputs_unchanged() -> None:
    bibles = copy.deepcopy(BIBLES)
    snapshot = copy.deepcopy(BIBLES)

    arcs = consolidate(bibles)
    context(arcs, bibles, ["Ash"], "Brin")
    cast(bibles, ["Ash", "Brin"])

    assert bibles == snapshot


# Made-up scenes, arcs, lens and answers for lines and judge. Not game text.
LINE_ASH_ONE = SceneLine("ST_C01_001_01", "1", "Ash", "Made-up Ash line one. Not game text.", "Made-up Japanese one. Not game text.", 1)
LINE_BRIN_ONE = SceneLine("ST_C01_001_01", "2", "Brin", "Made-up Brin line one. Not game text.", "Made-up Japanese two. Not game text.", 1)
LINE_ASH_TWO = SceneLine("ST_C02_001_01", "1", "Ash", "Made-up Ash line two. Not game text.", "Made-up Japanese three. Not game text.", 2)
LINE_ASH_BEAT = SceneLine("ST_C02_001_01", "2", "Ash", "Made-up beat line. Not game text.", "Made-up Japanese four. Not game text.", 2)
LINE_NARRATION = SceneLine("ST_C00_001_01", "5", None, "Made-up narration. Not game text.", "Made-up Japanese five. Not game text.", None)
LINE_ASH_ONE_COPY = SceneLine("ST_C01_001_01", "1", "Ash", "Made-up later copy. Not game text.", "Made-up Japanese six. Not game text.", 1)
SCENE_ONE = Scene("made-up-scene-one", (LINE_ASH_BEAT, LINE_ASH_TWO))
SCENE_TWO = Scene("made-up-scene-two", (LINE_NARRATION, LINE_ASH_ONE, LINE_BRIN_ONE, LINE_ASH_ONE_COPY))
SCENES = [SCENE_ONE, SCENE_TWO]
REWRITES = {
    ("ST_C01_001_01", "1"): "Made-up rewrite one. Not game text.",
    ("ST_C01_001_01", "2"): "Made-up rewrite two. Not game text.",
    ("ST_C02_001_01", "1"): "Made-up rewrite three. Not game text.",
}
ARC = {
    "id": "made-up-arc. Not game text.",
    "characters": ["Ash"],
    "beats": [
        {"line": {"namespace": "ST_C02_001_01", "key": "2"}},
        {"line": {"namespace": "ST_C00_001_01", "key": "5"}},
        {"line": {"namespace": "ST_C99_999_99", "key": "9"}},
    ],
}
ARC_TWO = {
    "id": "made-up-arc-two. Not game text.",
    "characters": ["Ash"],
    "beats": [{"line": {"namespace": "ST_C01_001_01", "key": "1"}}],
}
PLAIN_ARC = {
    "id": "made-up-plain-arc. Not game text.",
    "characters": ["Corr"],
    "beats": [{"line": {"namespace": "ST_C00_001_01", "key": "5"}}],
}
CORR_ARC = {"id": "made-up-corr-arc. Not game text.", "characters": ["Corr"], "beats": []}
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


def _answer(holds: bool, *reverts: SceneLine) -> dict[str, Any]:
    return {"holds": holds, "revert": [_ref_dict(line) for line in reverts]}


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _line_dicts(arc_lines: list[SceneLine], rewrites: dict[tuple[str, str], str]) -> list[dict[str, Any]]:
    return [
        {
            "namespace": line.namespace,
            "key": line.key,
            "speaker": line.speaker,
            "chapter": line.chapter,
            "english": line.english,
            "rewrite": rewrites.get(_ref(line)),
        }
        for line in arc_lines
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


def _judge(store: Store, ask: Fake, arcs: list[dict[str, Any]]) -> Verdicts:
    return judge(store, LENS, arcs, SCENES, REWRITES, ask)


def test_stage_name_is_arc_judge() -> None:
    assert STAGE == "arc_judge"


def test_lines_takes_beats_and_changed_character_lines_in_story_order() -> None:
    assert lines(ARC, SCENES, REWRITES) == [LINE_ASH_ONE, LINE_ASH_BEAT, LINE_ASH_TWO, LINE_NARRATION]


def test_lines_keeps_first_occurrence_and_each_key_once() -> None:
    result = lines(ARC, SCENES, REWRITES)

    assert result.count(LINE_ASH_ONE) == 1
    assert LINE_ASH_ONE_COPY not in result


def test_arc_without_changed_lines_makes_no_call(tmp_path: Path) -> None:
    ask = Fake()

    assert _judge(Store(tmp_path), ask, [PLAIN_ARC]) == Verdicts(frozenset(), frozenset())
    assert ask.calls == []


def test_holds_on_first_pass_reverts_nothing(tmp_path: Path) -> None:
    ask = Fake(_answer(True))

    assert _judge(Store(tmp_path), ask, [ARC]) == Verdicts(frozenset(), frozenset())
    assert len(ask.calls) == 1
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema is SCHEMA
    assert parts[0] == INSTRUCTIONS


def test_first_pass_prompt_carries_instructions_rubric_arc_and_arc_lines(tmp_path: Path) -> None:
    ask = Fake(_answer(True))

    _judge(Store(tmp_path), ask, [ARC])

    _, parts, _ = ask.calls[0]
    assert parts == [
        INSTRUCTIONS,
        "Judge rubric:\n" + LENS.stage_text("judge"),
        "Arc:\n" + _j(ARC),
        "Arc lines:\n" + _j(_line_dicts([LINE_ASH_ONE, LINE_ASH_BEAT, LINE_ASH_TWO, LINE_NARRATION], REWRITES)),
    ]


def test_only_refs_named_by_both_passes_revert_and_recheck_sees_proposed_reverts(tmp_path: Path) -> None:
    ask = Fake(_answer(False, LINE_ASH_ONE, LINE_ASH_TWO), _answer(False, LINE_ASH_ONE))

    assert _judge(Store(tmp_path), ask, [ARC]) == Verdicts(frozenset({_ref(LINE_ASH_ONE)}), frozenset())
    assert len(ask.calls) == 2

    _, recheck_parts, recheck_schema = ask.calls[1]
    assert recheck_schema is SCHEMA
    assert recheck_parts[0] == RECHECK_INSTRUCTIONS
    assert recheck_parts[-1] == "Proposed reverts:\n" + _j([_ref_dict(LINE_ASH_ONE), _ref_dict(LINE_ASH_TWO)])


def test_holds_on_recheck_reverts_nothing(tmp_path: Path) -> None:
    ask = Fake(_answer(False, LINE_ASH_ONE), _answer(True))

    assert _judge(Store(tmp_path), ask, [ARC]) == Verdicts(frozenset(), frozenset())
    assert len(ask.calls) == 2


def test_second_arc_sees_first_arc_reverts(tmp_path: Path) -> None:
    ask = Fake(_answer(False, LINE_ASH_ONE), _answer(False, LINE_ASH_ONE), _answer(True))

    assert _judge(Store(tmp_path), ask, [ARC, ARC_TWO]) == Verdicts(frozenset({_ref(LINE_ASH_ONE)}), frozenset())
    assert len(ask.calls) == 3

    _, second_parts, _ = ask.calls[2]
    assert second_parts[3] == "Arc lines:\n" + _j(
        _line_dicts([LINE_ASH_ONE, LINE_ASH_TWO], {("ST_C02_001_01", "1"): REWRITES[("ST_C02_001_01", "1")]})
    )


@pytest.mark.parametrize(
    "answers",
    [
        (Unfinished("Made-up unfinished detail. Not game text."),),
        (Rejected("Made-up rejected detail. Not game text."),),
        ({"holds": False, "revert": [UNKNOWN_REF]},),
        (_answer(False, LINE_ASH_ONE), Unfinished("Made-up unfinished detail. Not game text.")),
        (_answer(False, LINE_ASH_ONE), {"holds": False, "revert": [UNKNOWN_REF]}),
    ],
    ids=["first-unfinished", "first-rejected", "first-unknown-ref", "recheck-unfinished", "recheck-unknown-ref"],
)
def test_failed_pass_puts_every_changed_line_of_the_arc_in_unfinished(tmp_path: Path, answers: tuple[Any, ...]) -> None:
    ask = Fake(*answers)

    assert _judge(Store(tmp_path), ask, [ARC]) == Verdicts(frozenset(), frozenset({_ref(LINE_ASH_ONE), _ref(LINE_ASH_TWO)}))


def test_unfinished_keys_are_removed_for_later_arcs(tmp_path: Path) -> None:
    ask = Fake(Unfinished("Made-up unfinished detail. Not game text."))

    assert _judge(Store(tmp_path), ask, [ARC, ARC_TWO]) == Verdicts(frozenset(), frozenset({_ref(LINE_ASH_ONE), _ref(LINE_ASH_TWO)}))
    assert len(ask.calls) == 1


def test_fresh_store_on_same_root_returns_stored_verdicts_without_asking(tmp_path: Path) -> None:
    first = _judge(Store(tmp_path), Fake(_answer(False, LINE_ASH_ONE), _answer(False, LINE_ASH_ONE)), [ARC])
    ask = Fake()

    assert _judge(Store(tmp_path), ask, [ARC]) == first
    assert ask.calls == []


def test_with_changes_counts_arcs_whose_lines_hold_a_rewritten_key() -> None:
    assert with_changes([ARC, PLAIN_ARC, CORR_ARC, ARC_TWO], SCENES, REWRITES) == 2
