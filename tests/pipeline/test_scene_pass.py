import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, ScenePattern, SceneSection, TriageQuestion
from ffrd.pipeline.scene_pass import SCREEN_INSTRUCTIONS, SCREEN_SCHEMA, SCREEN_STAGE, screen
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
