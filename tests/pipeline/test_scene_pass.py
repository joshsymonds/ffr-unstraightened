import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, ScenePattern, SceneSection, TriageQuestion
from ffrd.pipeline.scene_pass import (
    PLAN_INSTRUCTIONS,
    PLAN_SCHEMA,
    PLAN_STAGE,
    SCREEN_INSTRUCTIONS,
    SCREEN_SCHEMA,
    SCREEN_STAGE,
    Plan,
    PlannedLine,
    plan,
    plan_id,
    screen,
)
from ffrd.pipeline.stages import Scene, SceneLine
from ffrd.pipeline.store import Store
from ffrd.pipeline.store import key as store_key

# Made-up scene, lens and answers. Not game text.
LINE_ONE = SceneLine("ST_C01_001_01", "1", "Ash", "Made-up first line. Not game text.", "Made-up Japanese one. Not game text.", 1)
LINE_TWO = SceneLine("ST_C01_001_01", "2", None, "Made-up narration. Not game text.", "Made-up Japanese two. Not game text.", 1)
LINE_THREE = SceneLine("ST_C01_001_01", "3", "Ash", "Made-up third line. Not game text.", "Made-up Japanese three. Not game text.", 1)
SCENE = Scene("made-up-scene", (LINE_ONE, LINE_TWO, LINE_THREE))
OUT_OF_SCENE = {"namespace": "ST_C09_001_01", "key": "9"}

SCENE_SECTION = SceneSection(
    patterns=(
        ScenePattern(id="made-up-alpha", question="Made-up pattern question alpha. Not game text."),
        ScenePattern(id="made-up-beta", question="Made-up pattern question beta. Not game text."),
    ),
    criteria=("Made-up scene criterion one. Not game text.",),
    plan="Made-up scene plan. Not game text.",
    rubric="Made-up scene rubric. Not game text.",
)

LENS = Lens(
    name="made-up",
    triage=(TriageQuestion("made-up-triage", "Made-up triage question. Not game text."),),
    screen_criteria=("Made-up screen criterion. Not game text.",),
    intent_rule="Made-up intent rule. Not game text.",
    judge_rubric="Made-up judge rubric. Not game text.",
    scene=SCENE_SECTION,
)

# Made-up cast. Not game text.
CAST = [{"chapter": 1, "gender": "made-up-gender", "notes": "Made-up notes. Not game text.", "speaker": "Ash"}]


def _ref(line: SceneLine) -> dict[str, str]:
    return {"namespace": line.namespace, "key": line.key}


def _answer(*patterns: tuple[str, list[dict[str, str]]]) -> dict[str, Any]:
    return {"patterns": [{"id": pid, "carriers": carriers} for pid, carriers in patterns]}


HIT_ONE = _answer(("made-up-beta", [_ref(LINE_TWO)]))


class Fake:
    """Stands in for a Claude call: returns queued answers, raises queued exceptions, records each call's arguments."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def test_screen_asks_sonnet_once_with_schema_and_four_parts_in_order(tmp_path: Path) -> None:
    ask = Fake(HIT_ONE)

    screen(Store(tmp_path), LENS, SCENE, CAST, ask)

    assert len(ask.calls) == 1
    model, parts, schema = ask.calls[0]
    assert model == claude.SONNET
    assert schema == SCREEN_SCHEMA
    assert len(parts) == 4
    assert parts[0] == SCREEN_INSTRUCTIONS
    assert parts[1] == "Scene rules:\n" + LENS.stage_text("scene")
    assert parts[2].startswith("Scene lines:\n")
    assert parts[3] == "Cast:\n" + json.dumps(CAST, ensure_ascii=False, sort_keys=True)


def test_screen_prompt_lines_carry_namespace_key_speaker_and_english_only(tmp_path: Path) -> None:
    ask = Fake(_answer())

    screen(Store(tmp_path), LENS, SCENE, CAST, ask)

    parts = ask.calls[0][1]
    lines_part = parts[2]
    assert "Made-up Japanese one. Not game text." not in "\n".join(parts)
    assert '"japanese"' not in lines_part
    assert '"chapter"' not in lines_part
    assert '"english": "Made-up first line. Not game text."' in lines_part


def test_screen_returns_pattern_ids_in_lens_order_with_merged_deduplicated_carriers(tmp_path: Path) -> None:
    answer = {
        "patterns": [
            {"id": "made-up-beta", "carriers": [_ref(LINE_TWO), _ref(LINE_ONE)]},
            {"id": "made-up-alpha", "carriers": [_ref(LINE_ONE)]},
            {"id": "made-up-beta", "carriers": [_ref(LINE_ONE), _ref(LINE_THREE)]},
        ]
    }

    result = screen(Store(tmp_path), LENS, SCENE, CAST, Fake(answer))

    assert result == {
        "made-up-alpha": ((LINE_ONE.namespace, LINE_ONE.key),),
        "made-up-beta": (
            (LINE_TWO.namespace, LINE_TWO.key),
            (LINE_ONE.namespace, LINE_ONE.key),
            (LINE_THREE.namespace, LINE_THREE.key),
        ),
    }
    assert isinstance(result, dict)
    assert list(result) == ["made-up-alpha", "made-up-beta"]


def test_screen_with_no_pattern_hit_returns_empty_mapping(tmp_path: Path) -> None:
    assert screen(Store(tmp_path), LENS, SCENE, CAST, Fake(_answer())) == {}


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (
            _answer(("made-up-gamma", [_ref(LINE_ONE)])),
            "pattern 'made-up-gamma' is not a scene pattern id in the lens",
        ),
        (
            _answer(("made-up-alpha", [OUT_OF_SCENE])),
            "carrier line ST_C09_001_01/9 is not in scene made-up-scene",
        ),
    ],
    ids=["unknown-pattern", "out-of-scene-carrier"],
)
def test_screen_rejects_unknown_pattern_or_out_of_scene_carrier_and_stores_nothing(
    tmp_path: Path, answer: dict[str, Any], detail: str
) -> None:
    store = Store(tmp_path)

    result = screen(store, LENS, SCENE, CAST, Fake(answer))

    assert result == Rejected(detail)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_screen_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, HIT_ONE)

    assert screen(store, LENS, SCENE, CAST, ask) == failure
    assert store.finished() == []

    assert screen(store, LENS, SCENE, CAST, ask) == {"made-up-beta": ((LINE_TWO.namespace, LINE_TWO.key),)}
    assert len(ask.calls) == 2


def test_screen_is_stored_under_scene_screen_stage_and_scene_lens_text(tmp_path: Path) -> None:
    store = Store(tmp_path)
    screen(store, LENS, SCENE, CAST, Fake(HIT_ONE))
    inputs = {"scene": SCENE.key, "lines": [asdict(line) for line in SCENE.lines], "cast": CAST}

    assert SCREEN_STAGE == "scene_screen"
    assert store.read(store_key(SCREEN_STAGE, inputs, LENS.stage_text("scene"))) is not None


def test_fresh_store_on_same_root_returns_stored_screen_without_asking(tmp_path: Path) -> None:
    expected = {"made-up-beta": ((LINE_TWO.namespace, LINE_TWO.key),)}
    screen(Store(tmp_path), LENS, SCENE, CAST, Fake(HIT_ONE))
    ask = Fake()

    assert screen(Store(tmp_path), LENS, SCENE, CAST, ask) == expected
    assert ask.calls == []


def test_editing_lens_scene_text_asks_again(tmp_path: Path) -> None:
    edited = replace(LENS, scene=replace(SCENE_SECTION, plan="Made-up edited scene plan. Not game text."))
    screen(Store(tmp_path), LENS, SCENE, CAST, Fake(HIT_ONE))
    ask = Fake(_answer())

    assert screen(Store(tmp_path), edited, SCENE, CAST, ask) == {}
    assert len(ask.calls) == 1


# Made-up plan inputs. Not game text.
HITS = {
    "made-up-beta": ((LINE_TWO.namespace, LINE_TWO.key),),
    "made-up-alpha": ((LINE_ONE.namespace, LINE_ONE.key),),
}
HIT_LIST = [
    {"id": "made-up-beta", "carriers": [_ref(LINE_TWO)]},
    {"id": "made-up-alpha", "carriers": [_ref(LINE_ONE)]},
]
FINAL = ((LINE_ONE.namespace, LINE_ONE.key),)
EXCLUDED = ((LINE_THREE.namespace, LINE_THREE.key),)
PLAN_INTENT = "Made-up intent. Not game text."
PLAN_LINE_ONE = PlannedLine(LINE_ONE.namespace, LINE_ONE.key, "Made-up intent one. Not game text.", True)
PLAN_LINE_TWO = PlannedLine(LINE_TWO.namespace, LINE_TWO.key, "Made-up intent two. Not game text.", False)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _pair(line: SceneLine) -> tuple[str, str]:
    return (line.namespace, line.key)


def _entry(ref: tuple[str, str], required: bool = True, intent: str = PLAN_INTENT) -> dict[str, Any]:
    namespace, key = ref
    return {"namespace": namespace, "key": key, "intent": intent, "required": required}


def _planned(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"keep": False, "keep_reason": "none", "lines": list(entries)}


def _kept(reason: str = "protection") -> dict[str, Any]:
    return {"keep": True, "keep_reason": reason, "lines": []}


def test_plan_asks_opus_once_with_schema_and_seven_parts_in_order(tmp_path: Path) -> None:
    ask = Fake(_planned(_entry(_pair(LINE_TWO))))

    plan(Store(tmp_path), LENS, SCENE, CAST, HITS, FINAL, EXCLUDED, ask)

    assert len(ask.calls) == 1
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == PLAN_SCHEMA
    assert len(parts) == 7
    assert parts[0] == PLAN_INSTRUCTIONS
    assert parts[1] == "Scene rules:\n" + LENS.stage_text("scene")
    assert parts[2].startswith("Scene lines:\n")
    assert '"japanese"' in parts[2]
    assert parts[3] == "Cast:\n" + _dump(CAST)
    assert parts[4] == "Patterns hit:\n" + _dump(HIT_LIST)
    assert parts[5] == "Final lines:\n" + _dump([_ref(LINE_ONE)])
    assert parts[6] == "Excluded lines:\n" + _dump([_ref(LINE_THREE)])


def test_kept_scene_returns_its_reason_with_no_id_and_no_lines(tmp_path: Path) -> None:
    result = plan(Store(tmp_path), LENS, SCENE, CAST, HITS, FINAL, EXCLUDED, Fake(_kept("protection")))

    assert result == Plan(id=None, keep_reason="protection", lines=())


def test_planned_scene_returns_lines_in_answer_order_with_intents_required_and_plan_id(tmp_path: Path) -> None:
    answer = _planned(
        _entry(_pair(LINE_TWO), required=False, intent="Made-up intent two. Not game text."),
        _entry(_pair(LINE_ONE), required=True, intent="Made-up intent one. Not game text."),
    )

    result = plan(Store(tmp_path), LENS, SCENE, CAST, HITS, (), (), Fake(answer))

    expected_id = plan_id(SCENE.key, [_pair(LINE_TWO), _pair(LINE_ONE)])
    assert result == Plan(id=expected_id, keep_reason=None, lines=(PLAN_LINE_TWO, PLAN_LINE_ONE))


def test_plan_id_is_sha256_prefix_of_scene_and_sorted_refs() -> None:
    refs = [_pair(LINE_TWO), _pair(LINE_ONE)]
    canonical = json.dumps(
        {"scene": "made-up-scene", "lines": [["ST_C01_001_01", "1"], ["ST_C01_001_01", "2"]]},
        ensure_ascii=False,
        sort_keys=True,
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    assert plan_id(SCENE.key, refs) == expected
    assert plan_id(SCENE.key, list(reversed(refs))) == expected
    assert plan_id(SCENE.key, [_pair(LINE_ONE)]) != expected
    assert plan_id("other-made-up-scene", refs) != expected


@pytest.mark.parametrize(
    ("answer", "final", "excluded", "detail"),
    [
        (
            {"keep": True, "keep_reason": "none", "lines": []},
            (),
            (),
            "a kept scene must name a keep reason",
        ),
        (
            {"keep": True, "keep_reason": "plot", "lines": [_entry(_pair(LINE_ONE))]},
            (),
            (),
            "a kept scene must plan no lines",
        ),
        (
            {"keep": False, "keep_reason": "plot", "lines": [_entry(_pair(LINE_ONE))]},
            (),
            (),
            "a planned scene must set keep_reason to none",
        ),
        (
            _planned(),
            (),
            (),
            "a planned scene must plan at least one line",
        ),
        (
            _planned(*[_entry(_pair(LINE_ONE))] * 5),
            (),
            (),
            "plan names 5 lines, more than 4",
        ),
        (
            _planned(_entry((OUT_OF_SCENE["namespace"], OUT_OF_SCENE["key"]))),
            (),
            (),
            "planned line ST_C09_001_01/9 is not in scene made-up-scene",
        ),
        (
            _planned(_entry(_pair(LINE_ONE))),
            FINAL,
            (),
            "planned line ST_C01_001_01/1 is a line-pass line",
        ),
        (
            _planned(_entry(_pair(LINE_TWO))),
            (),
            (_pair(LINE_TWO),),
            "planned line ST_C01_001_01/2 is excluded",
        ),
        (
            _planned(_entry(_pair(LINE_ONE)), _entry(_pair(LINE_ONE))),
            (),
            (),
            "planned line ST_C01_001_01/1 is planned twice",
        ),
    ],
    ids=[
        "kept-without-reason",
        "kept-with-lines",
        "planned-with-reason",
        "planned-without-lines",
        "over-cap",
        "out-of-scene",
        "line-pass",
        "excluded",
        "twice",
    ],
)
def test_plan_rejects_invalid_answer_and_stores_nothing(
    tmp_path: Path, answer: dict[str, Any], final: Any, excluded: Any, detail: str
) -> None:
    store = Store(tmp_path)

    result = plan(store, LENS, SCENE, CAST, HITS, final, excluded, Fake(answer))

    assert result == Rejected(detail)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_plan_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, _planned(_entry(_pair(LINE_TWO))))

    assert plan(store, LENS, SCENE, CAST, HITS, (), (), ask) == failure
    assert store.finished() == []

    result = plan(store, LENS, SCENE, CAST, HITS, (), (), ask)

    assert result == Plan(
        id=plan_id(SCENE.key, [_pair(LINE_TWO)]),
        keep_reason=None,
        lines=(PlannedLine(LINE_TWO.namespace, LINE_TWO.key, PLAN_INTENT, True),),
    )
    assert len(ask.calls) == 2


def test_plan_is_stored_under_scene_plan_stage_with_lens_scene_text_and_inputs(tmp_path: Path) -> None:
    store = Store(tmp_path)
    plan(store, LENS, SCENE, CAST, HITS, FINAL, EXCLUDED, Fake(_kept()))
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "cast": CAST,
        "hits": HIT_LIST,
        "final": [_ref(LINE_ONE)],
        "excluded": [_ref(LINE_THREE)],
    }

    assert PLAN_STAGE == "scene_plan"
    assert store.read(store_key(PLAN_STAGE, inputs, LENS.stage_text("scene"))) is not None


def test_fresh_store_on_same_root_returns_stored_plan_without_asking(tmp_path: Path) -> None:
    answer = _planned(_entry(_pair(LINE_TWO), required=False, intent="Made-up intent two. Not game text."))
    first = plan(Store(tmp_path), LENS, SCENE, CAST, HITS, (), (), Fake(answer))
    ask = Fake()

    assert plan(Store(tmp_path), LENS, SCENE, CAST, HITS, (), (), ask) == first
    assert ask.calls == []


def test_different_excluded_lines_ask_again(tmp_path: Path) -> None:
    plan(Store(tmp_path), LENS, SCENE, CAST, HITS, (), (), Fake(_kept()))
    ask = Fake(_kept("plot"))

    assert plan(Store(tmp_path), LENS, SCENE, CAST, HITS, (), EXCLUDED, ask) == Plan(id=None, keep_reason="plot", lines=())
    assert len(ask.calls) == 1
