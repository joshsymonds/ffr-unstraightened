import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.coherence import SCHEMA as COHERENCE_SCHEMA
from ffrd.pipeline.lens import Lens, ScenePattern, SceneSection, TriageQuestion
from ffrd.pipeline.scene_pass import (
    JUDGE_STAGE,
    PLAN_INSTRUCTIONS,
    PLAN_SCHEMA,
    PLAN_STAGE,
    SCREEN_INSTRUCTIONS,
    SCREEN_SCHEMA,
    SCREEN_STAGE,
    Plan,
    PlannedLine,
    judge,
    keeps_carrier,
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


def test_screen_asks_opus_once_with_schema_and_four_parts_in_order(tmp_path: Path) -> None:
    ask = Fake(HIT_ONE)

    screen(Store(tmp_path), LENS, SCENE, CAST, ask)

    assert len(ask.calls) == 1
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
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
    inputs = {"scene": SCENE.key, "lines": [asdict(line) for line in SCENE.lines], "cast": CAST, "model": "opus"}

    assert SCREEN_STAGE == "scene_screen"
    assert store.read(store_key(SCREEN_STAGE, inputs, LENS.stage_text("scene"))) is not None


def test_screen_key_carries_model_so_sonnet_era_answers_are_not_reused(tmp_path: Path) -> None:
    store = Store(tmp_path)
    screen(store, LENS, SCENE, CAST, Fake(HIT_ONE))
    inputs = {"scene": SCENE.key, "lines": [asdict(line) for line in SCENE.lines], "cast": CAST}
    with_model = {**inputs, "model": "opus"}

    assert store.read(store_key(SCREEN_STAGE, with_model, LENS.stage_text("scene"))) is not None
    assert store_key(SCREEN_STAGE, with_model, LENS.stage_text("scene")) != store_key(SCREEN_STAGE, inputs, LENS.stage_text("scene"))
    assert store.read(store_key(SCREEN_STAGE, inputs, LENS.stage_text("scene"))) is None


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

# Made-up carrier. Not game text.
CARRIER = "Made-up narration"


def _planned_line(line: SceneLine, intent: str, required: bool, carrier: str) -> PlannedLine:
    folded = carrier.casefold()
    return PlannedLine(
        namespace=line.namespace,
        key=line.key,
        intent=intent,
        required=required,
        quote_hash=hashlib.sha256(folded.encode("utf-8")).hexdigest()[:16],
        quote_length=len(folded),
    )


PLAN_LINE_ONE = _planned_line(LINE_ONE, "Made-up intent one. Not game text.", True, LINE_ONE.english)
PLAN_LINE_TWO = _planned_line(LINE_TWO, "Made-up intent two. Not game text.", False, CARRIER)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _pair(line: SceneLine) -> tuple[str, str]:
    return (line.namespace, line.key)


_ENGLISH = {_pair(line): line.english for line in SCENE.lines}


def _entry(
    ref: tuple[str, str],
    required: bool = True,
    intent: str = PLAN_INTENT,
    carrier: str | None = None,
) -> dict[str, Any]:
    namespace, key = ref
    if carrier is None:
        carrier = _ENGLISH.get(ref, "")
    return {"namespace": namespace, "key": key, "intent": intent, "carrier": carrier, "required": required}


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
        _entry(_pair(LINE_TWO), required=False, intent="Made-up intent two. Not game text.", carrier=CARRIER),
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


def test_plan_schema_requires_a_string_carrier_per_planned_line() -> None:
    item = PLAN_SCHEMA["properties"]["lines"]["items"]

    assert item["properties"]["carrier"] == {"type": "string"}
    assert "carrier" in item["required"]


def test_valid_carrier_yields_the_same_plan_and_no_carrier_is_stored(tmp_path: Path) -> None:
    store = Store(tmp_path)
    carrier = "Made-up narration"
    answer = _planned(_entry(_pair(LINE_TWO), required=False, intent="Made-up intent two. Not game text.", carrier=carrier))

    result = plan(store, LENS, SCENE, CAST, HITS, (), (), Fake(answer))

    assert result == Plan(id=plan_id(SCENE.key, [_pair(LINE_TWO)]), keep_reason=None, lines=(PLAN_LINE_TWO,))
    keys = store.finished()
    assert len(keys) == 1
    saved = store.read(keys[0])
    assert saved is not None
    stored = _dump(saved.value)
    assert "carrier" not in stored
    assert carrier not in stored


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
        (
            _planned(_entry(_pair(LINE_ONE), carrier="")),
            (),
            (),
            "planned line ST_C01_001_01/1 has an empty carrier",
        ),
        (
            _planned(_entry(_pair(LINE_TWO), carrier=" \t ")),
            (),
            (),
            "planned line ST_C01_001_01/2 has an empty carrier",
        ),
        (
            _planned(_entry(_pair(LINE_THREE), carrier="Made-up carrier absent from its line. Not game text.")),
            (),
            (),
            "planned line ST_C01_001_01/3 carrier is not in its line",
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
        "carrier-empty",
        "carrier-whitespace",
        "carrier-not-in-line",
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
        lines=(_planned_line(LINE_TWO, PLAN_INTENT, True, LINE_TWO.english),),
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


CARRIED_LINE = _planned_line(LINE_TWO, PLAN_INTENT, True, CARRIER)


@pytest.mark.parametrize(
    "text",
    [
        "Made-up narration. Not game text.",
        "Made-up line. Made-up narration. Not game text.",
        "Made-up line, then Made-up narration",
        "MADE-UP NARRATION. Not game text.",
    ],
    ids=["start", "middle", "end", "other-case"],
)
def test_keeps_carrier_is_true_when_rewrite_holds_carrier_ignoring_case(text: str) -> None:
    assert keeps_carrier(CARRIED_LINE, text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Made-up narrazion. Not game text.",
        "Made-up narr. Not game text.",
        "Made-up story. Not game text.",
        "Made-up",
    ],
    ids=["one-character-changed", "truncated", "absent", "shorter-than-carrier"],
)
def test_keeps_carrier_is_false_when_rewrite_changes_or_lacks_carrier(text: str) -> None:
    assert keeps_carrier(CARRIED_LINE, text) is False


def test_stored_plan_line_holds_carrier_fingerprint_and_length_but_not_carrier(tmp_path: Path) -> None:
    store = Store(tmp_path)
    answer = _planned(_entry(_pair(LINE_TWO), required=False, intent="Made-up intent two. Not game text.", carrier=CARRIER))
    plan(store, LENS, SCENE, CAST, HITS, (), (), Fake(answer))
    keys = store.finished()
    assert len(keys) == 1
    saved = store.read(keys[0])
    assert saved is not None
    line = saved.value["lines"][0]

    assert line["quote_hash"] == hashlib.sha256(CARRIER.casefold().encode("utf-8")).hexdigest()[:16]
    assert line["quote_length"] == len(CARRIER)
    assert CARRIER not in _dump(saved.value)


# Made-up judge inputs. Not game text.
JUDGE_REWRITES = {
    (LINE_ONE.namespace, LINE_ONE.key): "Made-up rewrite one. Not game text.",
    (LINE_TWO.namespace, LINE_TWO.key): "Made-up rewrite two. Not game text.",
}
JUDGE_ARCS = [{"arc": "Made-up arc. Not game text.", "lines": [_ref(LINE_ONE)]}]


def _judge_answer(coherent: bool, *lines: SceneLine) -> dict[str, Any]:
    return {"coherent": coherent, "revert": [_ref(line) for line in lines]}


def test_judge_asks_opus_with_coherence_schema_and_scene_rubric_in_both_passes(tmp_path: Path) -> None:
    ask = Fake(_judge_answer(False, LINE_ONE), _judge_answer(False, LINE_ONE))

    judge(Store(tmp_path), LENS, SCENE, JUDGE_REWRITES, JUDGE_ARCS, ask)

    assert len(ask.calls) == 2
    for model, parts, schema in ask.calls:
        assert model == claude.OPUS
        assert schema == COHERENCE_SCHEMA
        assert parts[1] == "Judge rubric:\n" + LENS.stage_text("scene")


def test_coherent_first_pass_returns_no_reverts_after_one_call(tmp_path: Path) -> None:
    ask = Fake(_judge_answer(True))

    assert judge(Store(tmp_path), LENS, SCENE, JUDGE_REWRITES, JUDGE_ARCS, ask) == frozenset()
    assert len(ask.calls) == 1


def test_line_named_on_both_passes_reverts_and_line_named_only_on_first_does_not(tmp_path: Path) -> None:
    ask = Fake(_judge_answer(False, LINE_ONE, LINE_TWO), _judge_answer(False, LINE_ONE))

    result = judge(Store(tmp_path), LENS, SCENE, JUDGE_REWRITES, JUDGE_ARCS, ask)

    assert result == frozenset({_pair(LINE_ONE)})


def test_judge_result_is_stored_under_scene_judge_with_scene_text_and_reused_by_fresh_store(tmp_path: Path) -> None:
    store = Store(tmp_path)
    judge(store, LENS, SCENE, JUDGE_REWRITES, JUDGE_ARCS, Fake(_judge_answer(True)))
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "rewrites": [{"namespace": ns, "key": key, "text": text} for (ns, key), text in sorted(JUDGE_REWRITES.items())],
        "arcs": JUDGE_ARCS,
    }

    assert JUDGE_STAGE == "scene_judge"
    assert store.read(store_key(JUDGE_STAGE, inputs, LENS.stage_text("scene"))) is not None

    ask = Fake()
    assert judge(Store(tmp_path), LENS, SCENE, JUDGE_REWRITES, JUDGE_ARCS, ask) == frozenset()
    assert ask.calls == []


def test_unfinished_judge_answer_is_returned_unstored(tmp_path: Path) -> None:
    store = Store(tmp_path)
    failure = Unfinished("Made-up unfinished detail. Not game text.")

    assert judge(store, LENS, SCENE, JUDGE_REWRITES, JUDGE_ARCS, Fake(failure)) == failure
    assert store.finished() == []
