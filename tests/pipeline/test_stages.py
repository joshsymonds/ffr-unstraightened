import json
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from ffrd import scenes as scene_file
from ffrd.game import Fail
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.accounts import Account
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Judgment
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.stages import (
    BUCKET_JUDGE_INSTRUCTIONS,
    BUCKET_JUDGE_SCHEMA,
    BUCKET_JUDGE_VERDICTS,
    CANDIDATE_INSTRUCTIONS,
    CANDIDATE_SCHEMA,
    CHOSEN_NAME,
    CROSS_SCHEMA,
    DRAFT_INSTRUCTIONS,
    DRAFT_SCHEMA,
    INSERTABLE,
    INTENT_INSTRUCTIONS,
    INTENT_SCHEMA,
    JUDGE_INSTRUCTIONS,
    JUDGE_SCHEMA,
    PICK_DRAFT_INSTRUCTIONS,
    PICK_DRAFT_SCHEMA,
    RESCREEN_INSTRUCTIONS,
    RESCREEN_SCHEMA,
    SCREEN_SCHEMA,
    SPEAKER_DRAFT_INSTRUCTIONS,
    TRANS_CANDIDATE,
    TRANS_PICK_DRAFT_SCHEMA,
    TRANS_SHAPE_INSTRUCTIONS,
    TRANS_SHAPES,
    Batch,
    Intent,
    PickChange,
    Scene,
    SceneLine,
    Target,
    _family_scenes,
    _table_scenes,
    candidates,
    cross_judge,
    draft,
    draft_speaker,
    intent,
    judge,
    pick_draft,
    render,
    rescreen,
    scene_text,
    screen,
    triage,
)
from ffrd.pipeline.store import Store
from ffrd.pipeline.store import key as store_key

# Made-up scene, lens and answers. Not game text.
LINE_ONE = SceneLine("ST_C01_001_01", "1", "Ash", "Made-up first line. Not game text.", "Made-up Japanese one. Not game text.", 1)
LINE_TWO = SceneLine("ST_C01_001_01", "2", None, "Made-up narration. Not game text.", "Made-up Japanese two. Not game text.", 1)
LINE_THREE = SceneLine("ST_C01_001_01", "3", "Ash", "Made-up third line. Not game text.", "Made-up Japanese three. Not game text.", 1)
LINE_FOUR = SceneLine("ST_C01_001_01", "4", None, "Made-up closing line. Not game text.", "Made-up Japanese four. Not game text.", 1)
LONG_LINE = SceneLine(
    "ST_C01_001_01",
    "5",
    "Ash",
    "Made-up overlong line, longer than the size limit. Not game text.",
    "Made-up Japanese five. Not game text.",
    1,
)
SCENE = Scene("made-up-scene", (LINE_ONE, LINE_TWO))
FOUR_LINE_SCENE = Scene("made-up-scene", (LINE_ONE, LINE_TWO, LINE_THREE, LINE_FOUR))
ONE_LINE_SCENE = Scene("made-up-scene", (LINE_ONE,))
LONG_LINE_SCENE = Scene("made-up-scene", (LONG_LINE,))

LENS = Lens(
    name="made-up",
    triage=(
        TriageQuestion("made-up-alpha", "Made-up question alpha. Not game text."),
        TriageQuestion("made-up-beta", "Made-up question beta. Not game text."),
    ),
    screen_criteria=("Made-up criterion one. Not game text.",),
    intent_rule="Made-up intent rule. Not game text.",
    judge_rubric="Made-up judge rubric. Not game text.",
)

PROBABILITIES = {"made-up-alpha": 0.25, "made-up-beta": 0.75}
FLAGGED_ONE = {
    "flagged": [{"namespace": LINE_ONE.namespace, "key": LINE_ONE.key, "reasons": ["made-up-alpha"]}],
}
# Made-up arc context and cast. Not game text.
CONTEXT = {
    "arcs": [
        {"id": "made-up-arc", "characters": ["Ash"], "states": [{"chapter": 1, "state": "Made-up state. Not game text."}], "beats": []}
    ],
    "characters": [{"chapter": 1, "gender": "made-up-gender", "notes": "Made-up notes. Not game text."}],
}
CHANGED_CONTEXT = {"arcs": [], "characters": []}
CAST = [{"chapter": 1, "gender": "made-up-gender", "notes": "Made-up notes. Not game text.", "speaker": "Ash"}]
CHANGED_CAST = [{"chapter": 1, "gender": "made-up-gender", "notes": "Made-up edited notes. Not game text.", "speaker": "Ash"}]


class Fake:
    """Stands in for a model or Jev call: returns queued answers, raises queued exceptions, records each call's arguments."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


RACERS = 8
RACE_PAUSE = 0.05


SIZE_LIMIT = 60
SIZE_REJECTION = "Made-up size rejection. Not game text."


class SizedJev:
    """Stands in for Jev: rejects scene text over SIZE_LIMIT characters and answers shorter text from answers keyed by that text. Records each call's arguments. Not game text."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, text: str, questions: Any) -> Any:
        self.calls.append((text, questions))
        if len(text) > SIZE_LIMIT:
            return Rejected(SIZE_REJECTION)
        return self.answers[text]


def _text(*lines: SceneLine) -> str:
    return scene_text(Scene("made-up-scene", lines))


class SlowFake(Fake):
    """A Fake that pauses before it answers, so racing threads overlap inside it. Not game text."""

    def __call__(self, *args: Any) -> Any:
        time.sleep(RACE_PAUSE)
        return super().__call__(*args)


def _race(call: Callable[[], Any]) -> list[Any]:
    """Starts RACERS threads on one call at once and returns their answers in submit order. A thread's error is re-raised."""
    start = threading.Barrier(RACERS, timeout=10)

    def run() -> Any:
        start.wait()
        return call()

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        futures = [pool.submit(run) for _ in range(RACERS)]
        return [future.result() for future in futures]


def test_scene_text_puts_speaker_before_english_and_bare_english_for_narration() -> None:
    assert scene_text(SCENE) == "Ash: Made-up first line. Not game text.\nMade-up narration. Not game text."


def test_triage_sends_scene_text_and_questions_and_returns_probabilities(tmp_path: Path) -> None:
    jev = Fake(PROBABILITIES)

    result = triage(Store(tmp_path), LENS, SCENE, jev)

    assert result == PROBABILITIES
    assert jev.calls == [(scene_text(SCENE), LENS.triage)]


def test_fresh_store_on_same_root_returns_stored_triage_without_calling_jev(tmp_path: Path) -> None:
    triage(Store(tmp_path), LENS, SCENE, Fake(PROBABILITIES))
    jev = Fake()

    assert triage(Store(tmp_path), LENS, SCENE, jev) == PROBABILITIES
    assert jev.calls == []


@pytest.mark.parametrize(
    ("failure", "scene"),
    [
        (RateLimited("Made-up rate limit detail. Not game text."), SCENE),
        (Unfinished("Made-up unfinished detail. Not game text."), SCENE),
        (Rejected("Made-up rejected detail. Not game text."), ONE_LINE_SCENE),
    ],
    ids=["rate-limited", "unfinished", "rejected"],
)
def test_failed_triage_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any, scene: Scene) -> None:
    store = Store(tmp_path)
    jev = Fake(failure, PROBABILITIES)

    assert triage(store, LENS, scene, jev) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert triage(store, LENS, scene, jev) == PROBABILITIES
    assert len(jev.calls) == 2
    assert store.unfinished() == []


def test_rejected_scene_is_split_in_halves_and_combined_by_per_question_maximum(tmp_path: Path) -> None:
    store = Store(tmp_path)
    jev = SizedJev(
        {_text(LINE_ONE): {"made-up-alpha": 0.9, "made-up-beta": 0.1}, _text(LINE_TWO): {"made-up-alpha": 0.2, "made-up-beta": 0.6}}
    )

    assert triage(store, LENS, SCENE, jev) == {"made-up-alpha": 0.9, "made-up-beta": 0.6}
    assert [text for text, _ in jev.calls] == [_text(LINE_ONE, LINE_TWO), _text(LINE_ONE), _text(LINE_TWO)]
    assert store.unfinished() == []

    fresh = SizedJev({})
    assert triage(Store(tmp_path), LENS, SCENE, fresh) == {"made-up-alpha": 0.9, "made-up-beta": 0.6}
    assert fresh.calls == []


def test_half_still_over_size_is_split_again_and_every_part_is_combined(tmp_path: Path) -> None:
    jev = SizedJev(
        {
            _text(LINE_ONE): {"made-up-alpha": 0.1, "made-up-beta": 0.9},
            _text(LINE_TWO): {"made-up-alpha": 0.8, "made-up-beta": 0.2},
            _text(LINE_THREE): {"made-up-alpha": 0.3, "made-up-beta": 0.95},
            _text(LINE_FOUR): {"made-up-alpha": 0.5, "made-up-beta": 0.05},
        }
    )

    assert triage(Store(tmp_path), LENS, FOUR_LINE_SCENE, jev) == {"made-up-alpha": 0.8, "made-up-beta": 0.95}
    assert [text for text, _ in jev.calls] == [
        _text(*FOUR_LINE_SCENE.lines),
        _text(LINE_ONE, LINE_TWO),
        _text(LINE_ONE),
        _text(LINE_TWO),
        _text(LINE_THREE, LINE_FOUR),
        _text(LINE_THREE),
        _text(LINE_FOUR),
    ]


def test_scene_under_size_makes_exactly_one_call(tmp_path: Path) -> None:
    jev = SizedJev({_text(LINE_ONE): PROBABILITIES})

    assert triage(Store(tmp_path), LENS, ONE_LINE_SCENE, jev) == PROBABILITIES
    assert jev.calls == [(_text(LINE_ONE), LENS.triage)]


@pytest.mark.parametrize(
    "failure",
    [RateLimited("Made-up rate limit detail. Not game text."), Unfinished("Made-up unfinished detail. Not game text.")],
    ids=["rate-limited", "unfinished"],
)
def test_failed_half_propagates_and_nothing_is_stored(tmp_path: Path, failure: RateLimited | Unfinished) -> None:
    store = Store(tmp_path)
    jev = SizedJev({_text(LINE_ONE): PROBABILITIES, _text(LINE_TWO): failure})

    assert triage(store, LENS, SCENE, jev) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_rejected_one_line_scene_is_returned_rejected_and_stores_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    jev = SizedJev({})

    assert triage(store, LENS, LONG_LINE_SCENE, jev) == Rejected(SIZE_REJECTION)
    assert len(jev.calls) == 1
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_editing_triage_text_reruns_triage_and_screen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    jev = Fake(PROBABILITIES, PROBABILITIES)
    ask = Fake(FLAGGED_ONE, FLAGGED_ONE)
    triage(store, LENS, SCENE, jev)
    screen(store, LENS, SCENE, CAST, ask)
    edited = replace(LENS, triage=(TriageQuestion("made-up-alpha", "Edited question. Not game text."), LENS.triage[1]))

    triage(store, edited, SCENE, jev)
    screen(store, edited, SCENE, CAST, ask)

    assert len(jev.calls) == 2
    assert len(ask.calls) == 2


def test_editing_only_screen_criteria_reruns_screen_and_not_triage(tmp_path: Path) -> None:
    store = Store(tmp_path)
    jev = Fake(PROBABILITIES)
    ask = Fake(FLAGGED_ONE, FLAGGED_ONE)
    triage(store, LENS, SCENE, jev)
    screen(store, LENS, SCENE, CAST, ask)
    edited = replace(LENS, screen_criteria=("Edited criterion. Not game text.",))

    triage(store, edited, SCENE, jev)
    screen(store, edited, SCENE, CAST, ask)

    assert len(jev.calls) == 1
    assert len(ask.calls) == 2


def test_screen_schema_requires_flagged_and_forbids_extra_properties() -> None:
    flagged = SCREEN_SCHEMA["properties"]["flagged"]
    item = flagged["items"]

    assert SCREEN_SCHEMA["required"] == ["flagged"]
    assert SCREEN_SCHEMA["additionalProperties"] is False
    assert item["required"] == ["namespace", "key", "reasons"]
    assert item["additionalProperties"] is False
    assert item["properties"]["reasons"]["minItems"] == 1


def _part(parts: Sequence[str], prefix: str) -> str:
    return next(part for part in parts if part.startswith(prefix)).removeprefix(prefix)


def _heading_part(heading: str, value: Any) -> str:
    return f"{heading}:\n" + json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_screen_asks_opus_with_schema_and_returns_reasons_in_lens_order(tmp_path: Path) -> None:
    answer = {
        "flagged": [
            {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key, "reasons": ["made-up-beta", "made-up-alpha"]},
            {"namespace": LINE_TWO.namespace, "key": LINE_TWO.key, "reasons": ["made-up-beta"]},
        ]
    }
    ask = Fake(answer)

    result = screen(Store(tmp_path), LENS, SCENE, CAST, ask)

    assert result == {
        (LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha", "made-up-beta"),
        (LINE_TWO.namespace, LINE_TWO.key): ("made-up-beta",),
    }
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == SCREEN_SCHEMA
    prompt = "\n".join(parts)
    assert "Made-up criterion one. Not game text." in prompt
    assert "made-up-alpha" in prompt
    assert "Made-up first line. Not game text." in prompt


def test_screen_store_key_carries_the_model_so_sonnet_era_answers_are_not_reused(tmp_path: Path) -> None:
    screen(Store(tmp_path), LENS, SCENE, CAST, Fake({"flagged": []}))
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {"scene": SCENE.key, "lines": [asdict(line) for line in SCENE.lines], "cast": CAST}
    lens_text = LENS.stage_text("screen") + "\n" + LENS.stage_text("triage")

    assert record.stem == store_key("screen", {**inputs, "model": "opus"}, lens_text)
    assert store_key("screen", inputs, lens_text) != record.stem


def test_screen_prompt_lines_carry_namespace_key_speaker_and_english_only(tmp_path: Path) -> None:
    ask = Fake({"flagged": []})

    screen(Store(tmp_path), LENS, SCENE, CAST, ask)

    prompt = "\n".join(ask.calls[0][1])
    lines_part = _part(ask.calls[0][1], "Scene lines:\n")
    assert "Made-up Japanese one. Not game text." not in prompt
    assert "Made-up Japanese two. Not game text." not in prompt
    assert '"japanese"' not in prompt
    assert '"chapter"' not in lines_part
    assert '"english": "Made-up first line. Not game text."' in lines_part


def test_screen_with_no_flagged_lines_returns_empty_mapping(tmp_path: Path) -> None:
    assert screen(Store(tmp_path), LENS, SCENE, CAST, Fake({"flagged": []})) == {}


@pytest.mark.parametrize(
    "answer",
    [
        {"flagged": [{"namespace": "ST_C09_001_01", "key": "1", "reasons": ["made-up-alpha"]}]},
        {"flagged": [{"namespace": LINE_ONE.namespace, "key": LINE_ONE.key, "reasons": ["made-up-gamma"]}]},
    ],
    ids=["unknown-line", "unknown-reason"],
)
def test_screen_rejects_unknown_line_or_reason_and_stores_nothing(tmp_path: Path, answer: Any) -> None:
    store = Store(tmp_path)

    result = screen(store, LENS, SCENE, CAST, Fake(answer))

    assert isinstance(result, Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_screen_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, FLAGGED_ONE)

    assert screen(store, LENS, SCENE, CAST, ask) == failure
    assert store.finished() == []

    assert screen(store, LENS, SCENE, CAST, ask) == {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}
    assert len(ask.calls) == 2


def test_fresh_store_on_same_root_returns_stored_screen_without_asking(tmp_path: Path) -> None:
    expected = {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}
    screen(Store(tmp_path), LENS, SCENE, CAST, Fake(FLAGGED_ONE))
    ask = Fake()

    assert screen(Store(tmp_path), LENS, SCENE, CAST, ask) == expected
    assert ask.calls == []


def test_default_screen_store_key_is_pinned_to_the_base_hex(tmp_path: Path) -> None:
    screen(Store(tmp_path), LENS, SCENE, CAST, Fake({"flagged": []}))

    [record] = (tmp_path / "results").glob("*.json")
    assert record.stem == "6f1c6ec9cce3650dd3b85196d9b120873bd2e1174701293fee4b894f557377d2"


def test_screen_with_haiku_asks_haiku_and_stores_under_the_haiku_model(tmp_path: Path) -> None:
    ask = Fake({"flagged": []})

    screen(Store(tmp_path), LENS, SCENE, CAST, ask, model=claude.HAIKU)

    model, _parts, _schema = ask.calls[0]
    assert model == claude.HAIKU
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {"scene": SCENE.key, "lines": [asdict(line) for line in SCENE.lines], "cast": CAST}
    lens_text = LENS.stage_text("screen") + "\n" + LENS.stage_text("triage")
    assert record.stem == store_key("screen", {**inputs, "model": "haiku"}, lens_text)
    assert record.stem != store_key("screen", {**inputs, "model": "opus"}, lens_text)


def test_screen_with_eligible_still_sends_every_line_and_names_only_the_eligible_refs(tmp_path: Path) -> None:
    ask = Fake({"flagged": []})

    screen(Store(tmp_path), LENS, SCENE, CAST, ask, eligible=[(LINE_ONE.namespace, LINE_ONE.key)])

    parts = ask.calls[0][1]
    lines_part = _part(parts, "Scene lines:\n")
    assert '"english": "Made-up first line. Not game text."' in lines_part
    assert '"english": "Made-up narration. Not game text."' in lines_part
    eligible_part = _part(parts, "Eligible lines, flag only these:\n")
    assert json.loads(eligible_part) == [{"namespace": LINE_ONE.namespace, "key": LINE_ONE.key}]


def test_screen_rejects_a_flag_outside_eligible_and_stores_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    answer = {"flagged": [{"namespace": LINE_TWO.namespace, "key": LINE_TWO.key, "reasons": ["made-up-alpha"]}]}

    result = screen(store, LENS, SCENE, CAST, Fake(answer), eligible=[(LINE_ONE.namespace, LINE_ONE.key)])

    assert isinstance(result, Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_screen_with_eligible_returns_flags_on_eligible_lines_as_today(tmp_path: Path) -> None:
    result = screen(Store(tmp_path), LENS, SCENE, CAST, Fake(FLAGGED_ONE), eligible=[(LINE_ONE.namespace, LINE_ONE.key)])

    assert result == {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}


def test_screen_store_key_carries_the_eligible_refs_sorted(tmp_path: Path) -> None:
    eligible = [(LINE_TWO.namespace, LINE_TWO.key), (LINE_ONE.namespace, LINE_ONE.key)]

    screen(Store(tmp_path), LENS, SCENE, CAST, Fake({"flagged": []}), eligible=eligible)

    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "cast": CAST,
        "eligible": [{"namespace": LINE_ONE.namespace, "key": LINE_ONE.key}, {"namespace": LINE_TWO.namespace, "key": LINE_TWO.key}],
    }
    lens_text = LENS.stage_text("screen") + "\n" + LENS.stage_text("triage")
    assert record.stem == store_key("screen", {**inputs, "model": "opus"}, lens_text)


TABLE_ROWS = [
    scene_file.SceneLine("made-up-table-scene", 1, "ST_C01_001_01", "1", "Ash", None),
    scene_file.SceneLine("made-up-table-scene", 2, "ST_C01_001_01", "2", None, None),
]
TABLE_ENGLISH = {
    ("ST_C01_001_01", "1"): "Made-up table line one. Not game text.",
    ("ST_C01_001_01", "2"): "Made-up table line two. Not game text.",
}


def test_table_scenes_are_built_with_the_table_flag() -> None:
    scenes = _table_scenes(TABLE_ROWS, TABLE_ENGLISH, {})

    assert [scene.key for scene in scenes] == ["made-up-table-scene"]
    assert [scene.table for scene in scenes] == [True]


def test_family_scenes_are_built_without_the_table_flag() -> None:
    english = {("ST_DialogChoice_MadeUp", "1"): "Made-up choice line. Not game text."}
    scenes = _family_scenes(english, {}, set())

    assert [scene.key for scene in scenes] == ["ST_DialogChoice_MadeUp"]
    assert [scene.table for scene in scenes] == [False]


def test_scene_table_flag_defaults_to_false() -> None:
    assert SCENE.table is False


def test_table_scene_equals_and_hashes_like_the_same_key_and_lines_without_the_flag() -> None:
    table = replace(SCENE, table=True)

    assert table == SCENE
    assert hash(table) == hash(SCENE)


def test_screen_result_stored_for_a_table_scene_is_reused_for_the_same_scene_without_the_flag(tmp_path: Path) -> None:
    expected = {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}
    screen(Store(tmp_path), LENS, replace(SCENE, table=True), CAST, Fake(FLAGGED_ONE))
    ask = Fake()

    assert screen(Store(tmp_path), LENS, SCENE, CAST, ask) == expected
    assert ask.calls == []


def test_batch_that_never_asks_never_chooses_an_account() -> None:
    choose = Fake(4)

    Batch(choose=choose, ask=Fake())

    assert choose.calls == []


def test_batch_chooses_once_then_follows_the_account_each_call_returns() -> None:
    choose = Fake(3)
    ask = Fake((5, FLAGGED_ONE), (6, FLAGGED_ONE), (6, FLAGGED_ONE))
    batch = Batch(choose=choose, ask=ask)

    assert batch.ask(claude.SONNET, ["Made-up part one. Not game text."], SCREEN_SCHEMA) == FLAGGED_ONE
    assert batch.ask("opus", ["Made-up part two. Not game text."], SCREEN_SCHEMA) == FLAGGED_ONE
    assert batch.ask("opus", ["Made-up part three. Not game text."], SCREEN_SCHEMA) == FLAGGED_ONE

    assert choose.calls == [()]
    assert [call[0] for call in ask.calls] == [3, 5, 6]
    assert ask.calls[0] == (3, claude.SONNET, ["Made-up part one. Not game text."], SCREEN_SCHEMA)


def test_batch_returns_the_failure_the_claude_ask_returns() -> None:
    failure = Unfinished("Made-up unfinished detail. Not game text.")

    assert Batch(choose=Fake(1), ask=Fake((1, failure))).ask(claude.SONNET, [], SCREEN_SCHEMA) == failure


def test_batch_default_choice_uses_the_least_used_cswap_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accounts, "choose", Fake(Account(number=2, weekly_pct=1.0, resets=())))
    ask = Fake((2, FLAGGED_ONE))

    Batch(ask=ask).ask(claude.SONNET, [], SCREEN_SCHEMA)

    assert ask.calls[0][0] == 2


def test_racing_first_asks_choose_one_account_and_every_ask_is_answered() -> None:
    choose = SlowFake(*[3] * RACERS)
    ask = Fake(*[(3, FLAGGED_ONE)] * RACERS)
    batch = Batch(choose=choose, ask=ask)

    answers = _race(lambda: batch.ask(claude.SONNET, ["Made-up racing part. Not game text."], SCREEN_SCHEMA))

    assert answers == [FLAGGED_ONE] * RACERS
    assert choose.calls == [()]
    assert [call[0] for call in ask.calls] == [3] * RACERS


# Made-up line-stage fixtures. Not game text.
CALLBACK = {"line": {"namespace": LINE_TWO.namespace, "key": LINE_TWO.key}, "depends": [], "chapter": 1}
REASONS = ("made-up-alpha",)
INTENT_TEXT = "Made-up intent. Not game text."
INTENT_ANSWER = {"intent": "Made-up intent. Not game text.", "keep": False, "keep_reason": "none"}
INTENT = Intent(text="Made-up intent. Not game text.", keep=False, keep_reason="none")
TARGET_ONE = Target(LINE_ONE, "Made-up intent.", (CALLBACK,), CONTEXT)
TARGET_TWO = Target(LINE_TWO, "Made-up intent two. Not game text.", (), CHANGED_CONTEXT)
DRAFT_ONE = ("Made-up draft one. Not game text.", "Made-up draft two. Not game text.")
DRAFT_TWO = ("Made-up draft of line two. Not game text.",)
ENTRY_ONE = {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key, "drafts": list(DRAFT_ONE)}
ENTRY_TWO = {"namespace": LINE_TWO.namespace, "key": LINE_TWO.key, "drafts": list(DRAFT_TWO)}
ENTRY_UNKNOWN = {"namespace": "made-up-namespace", "key": "9", "drafts": ["Made-up unknown draft. Not game text."]}
DRAFT_ANSWER = {"drafts": [ENTRY_ONE]}
DRAFT_TWO_ANSWER = {"drafts": [ENTRY_ONE, ENTRY_TWO]}
JUDGE_ANSWER = {
    "meaning": True,
    "plot": True,
    "minimal_change": True,
    "callback": True,
    "fixed": True,
    "restores_japanese": False,
}
JUDGMENT = Judgment(meaning=True, plot=True, minimal_change=True, callback=True, fixed=True, restores_japanese=False)
CROSS_ANSWER = {"consistent": True}


class FakeRenderer:
    """Stands in for a voice renderer: answers render calls from a queue, recording each call's arguments."""

    def __init__(self, *answers: Any) -> None:
        self.fake = Fake(*answers)

    def render(self, speaker: str | None, text: str) -> str:
        return self.fake(speaker, text)


def test_intent_schema_requires_intent_keep_and_keep_reason_and_forbids_extra_properties() -> None:
    assert INTENT_SCHEMA["type"] == "object"
    assert INTENT_SCHEMA["required"] == ["intent", "keep", "keep_reason"]
    assert INTENT_SCHEMA["properties"]["intent"]["type"] == "string"
    assert INTENT_SCHEMA["properties"]["keep"]["type"] == "boolean"
    assert INTENT_SCHEMA["additionalProperties"] is False


def test_intent_schema_keep_reason_is_exactly_the_listed_reasons_in_order() -> None:
    assert INTENT_SCHEMA["properties"]["keep_reason"] == {
        "type": "string",
        "enum": [
            "no-hit",
            "earned",
            "no-invented-motive",
            "gender-rule-narrow",
            "own-wish",
            "oaths-outside-romance",
            "protection",
            "plot",
            "condemned-in-scene",
            "none",
        ],
    }


def test_intent_instructions_tell_the_model_to_set_keep_when_the_line_stays_unchanged() -> None:
    assert "keep" in INTENT_INSTRUCTIONS
    assert "unchanged" in INTENT_INSTRUCTIONS


def test_intent_instructions_tell_the_model_to_name_the_listed_keep_reason_or_none() -> None:
    assert "keep_reason" in INTENT_INSTRUCTIONS
    assert "none" in INTENT_INSTRUCTIONS


def test_intent_asks_opus_with_schema_and_returns_intent(tmp_path: Path) -> None:
    ask = Fake(INTENT_ANSWER)

    result = intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask)

    assert result == INTENT
    model, _parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == INTENT_SCHEMA


def test_intent_returns_keep_true_with_its_keep_reason_when_the_rule_leaves_the_line_unchanged(tmp_path: Path) -> None:
    ask = Fake({"intent": "Made-up kept intent. Not game text.", "keep": True, "keep_reason": "earned"})

    result = intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask)

    assert result == Intent(text="Made-up kept intent. Not game text.", keep=True, keep_reason="earned")


def test_intent_returns_keep_true_with_condemned_in_scene_when_the_rule_leaves_the_line_unchanged(tmp_path: Path) -> None:
    ask = Fake({"intent": "Made-up kept intent. Not game text.", "keep": True, "keep_reason": "condemned-in-scene"})

    result = intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask)

    assert result == Intent(text="Made-up kept intent. Not game text.", keep=True, keep_reason="condemned-in-scene")


def test_intent_store_key_carries_its_schema_version(tmp_path: Path) -> None:
    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, Fake(INTENT_ANSWER))
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "line": {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key},
        "reasons": list(REASONS),
        "callbacks": [CALLBACK],
        "context": CONTEXT,
        "schema": 2,
    }
    text = LENS.stage_text("intent")

    assert record.stem == store_key("intent", inputs, text)
    assert store_key("intent", {k: v for k, v in inputs.items() if k != "schema"}, text) != record.stem


def test_default_intent_store_key_is_pinned_to_the_base_hex(tmp_path: Path) -> None:
    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, Fake(INTENT_ANSWER))
    [record] = (tmp_path / "results").glob("*.json")

    assert record.stem == "76f0011e1e506a5b4df0fa28f7a2bda527660c53ef3d3889d83e0e72686d5b23"


def test_intent_with_bucket_stores_under_a_key_that_carries_the_bucket(tmp_path: Path) -> None:
    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, Fake(INTENT_ANSWER), bucket="trans")
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "line": {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key},
        "reasons": list(REASONS),
        "callbacks": [CALLBACK],
        "context": CONTEXT,
        "schema": 2,
    }
    text = LENS.stage_text("intent")

    assert record.stem == store_key("intent", {**inputs, "bucket": "trans"}, text)
    assert record.stem != store_key("intent", inputs, text)


def test_draft_store_key_does_not_carry_the_intent_schema_version(tmp_path: Path) -> None:
    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], None, Fake(DRAFT_ANSWER))
    [record] = (tmp_path / "results").glob("*.json")
    target = {
        "namespace": LINE_ONE.namespace,
        "key": LINE_ONE.key,
        "intent": TARGET_ONE.intent,
        "callbacks": [CALLBACK],
        "context": CONTEXT,
    }
    inputs = {"scene": SCENE.key, "lines": [asdict(line) for line in SCENE.lines], "targets": [target], "retry": None}

    assert record.stem == store_key("draft", inputs, LENS.stage_text("intent"))


def test_judge_store_key_does_not_carry_a_model_field(tmp_path: Path) -> None:
    judge(
        Store(tmp_path),
        LENS,
        SCENE,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        REASONS,
        INTENT_TEXT,
        CONTEXT,
        Fake(JUDGE_ANSWER),
    )
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "line": {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key},
        "text": "Made-up candidate. Not game text.",
        "callbacks": [CALLBACK],
        "reasons": list(REASONS),
        "intent": INTENT_TEXT,
        "context": CONTEXT,
    }
    lens_text = LENS.stage_text("judge") + "\n" + LENS.stage_text("triage")

    assert record.stem == store_key("judge", inputs, lens_text)


def test_intent_prompt_carries_rule_callbacks_japanese_lines_and_target_reasons(tmp_path: Path) -> None:
    ask = Fake(INTENT_ANSWER)

    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert "Made-up intent rule. Not game text." in prompt
    assert '"chapter": 1' in prompt
    assert '"japanese": "Made-up Japanese one. Not game text."' in prompt
    assert '"reasons": ["made-up-alpha"]' in prompt


def test_intent_default_target_line_has_no_bucket(tmp_path: Path) -> None:
    ask = Fake(INTENT_ANSWER)

    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask)

    target = _part(ask.calls[0][1], "Target line:\n")
    assert target == '{"key": "1", "namespace": "ST_C01_001_01", "reasons": ["made-up-alpha"]}'


def test_intent_with_bucket_puts_it_on_the_target_line(tmp_path: Path) -> None:
    ask = Fake(INTENT_ANSWER)

    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask, bucket="trans")

    target = _part(ask.calls[0][1], "Target line:\n")
    assert json.loads(target) == {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key, "reasons": ["made-up-alpha"], "bucket": "trans"}


def test_fresh_store_on_same_root_returns_stored_intent_without_asking(tmp_path: Path) -> None:
    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, Fake(INTENT_ANSWER))
    ask = Fake()

    assert intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask) == INTENT
    assert ask.calls == []


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_intent_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, INTENT_ANSWER)

    assert intent(store, LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert intent(store, LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask) == INTENT
    assert len(ask.calls) == 2
    assert store.unfinished() == []


def test_editing_intent_rule_reruns_intent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(INTENT_ANSWER, INTENT_ANSWER)
    intent(store, LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask)
    edited = replace(LENS, intent_rule="Edited intent rule. Not game text.")

    intent(store, edited, SCENE, LINE_ONE, REASONS, [CALLBACK], CONTEXT, ask)

    assert len(ask.calls) == 2


def test_draft_target_with_a_bucket_stores_under_the_same_key_as_without(tmp_path: Path) -> None:
    draft(Store(tmp_path / "plain"), LENS, SCENE, [TARGET_ONE], None, Fake(DRAFT_ANSWER))
    draft(Store(tmp_path / "bucketed"), LENS, SCENE, [replace(TARGET_ONE, bucket=BUCKET)], None, Fake(DRAFT_ANSWER))
    [plain] = (tmp_path / "plain" / "results").glob("*.json")
    [bucketed] = (tmp_path / "bucketed" / "results").glob("*.json")

    assert bucketed.stem == plain.stem


def test_speaker_draft_target_with_a_bucket_stores_under_the_same_key_as_without(tmp_path: Path) -> None:
    draft_speaker(Store(tmp_path / "plain"), LENS, [(SCENE, [TARGET_ONE])], Fake(DRAFT_ANSWER))
    draft_speaker(Store(tmp_path / "bucketed"), LENS, [(SCENE, [replace(TARGET_ONE, bucket=BUCKET)])], Fake(DRAFT_ANSWER))
    [plain] = (tmp_path / "plain" / "results").glob("*.json")
    [bucketed] = (tmp_path / "bucketed" / "results").glob("*.json")

    assert bucketed.stem == plain.stem


def test_draft_schema_requires_drafts_per_target_and_forbids_extra_properties() -> None:
    entry = DRAFT_SCHEMA["properties"]["drafts"]["items"]
    assert DRAFT_SCHEMA["type"] == "object"
    assert DRAFT_SCHEMA["required"] == ["drafts"]
    assert DRAFT_SCHEMA["properties"]["drafts"]["type"] == "array"
    assert entry["type"] == "object"
    assert entry["required"] == ["namespace", "key", "drafts"]
    assert entry["properties"]["namespace"] == {"type": "string"}
    assert entry["properties"]["key"] == {"type": "string"}
    assert entry["properties"]["drafts"] == {"type": "array", "items": {"type": "string"}, "minItems": 1}
    assert entry["additionalProperties"] is False
    assert DRAFT_SCHEMA["additionalProperties"] is False


def test_draft_sends_every_target_in_one_call_and_returns_drafts_per_line(tmp_path: Path) -> None:
    ask = Fake(DRAFT_TWO_ANSWER)

    result = draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE, TARGET_TWO], None, ask)

    assert result == {(LINE_ONE.namespace, LINE_ONE.key): DRAFT_ONE, (LINE_TWO.namespace, LINE_TWO.key): DRAFT_TWO}
    assert len(ask.calls) == 1
    model, _parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == DRAFT_SCHEMA


def test_draft_prompt_carries_the_rule_scene_lines_and_each_targets_intent_callbacks_and_context(tmp_path: Path) -> None:
    ask = Fake(DRAFT_TWO_ANSWER)

    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE, TARGET_TWO], None, ask)

    parts = ask.calls[0][1]
    prompt = "\n".join(parts)
    assert "Made-up intent rule. Not game text." in prompt
    assert "{...}" in prompt
    assert "<text ...>" in prompt
    assert '"japanese": "Made-up Japanese one. Not game text."' in prompt
    assert [line["key"] for line in json.loads(_part(parts, "Scene lines:\n"))] == ["1", "2"]
    assert json.loads(_part(parts, "Targets:\n")) == [
        {
            "namespace": LINE_ONE.namespace,
            "key": LINE_ONE.key,
            "intent": "Made-up intent.",
            "callbacks": [CALLBACK],
            "context": CONTEXT,
        },
        {
            "namespace": LINE_TWO.namespace,
            "key": LINE_TWO.key,
            "intent": "Made-up intent two. Not game text.",
            "callbacks": [],
            "context": CHANGED_CONTEXT,
        },
    ]


def test_draft_target_carries_its_arc_context(tmp_path: Path) -> None:
    ask = Fake(DRAFT_TWO_ANSWER)

    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE, TARGET_TWO], None, ask)

    assert [target["context"] for target in json.loads(_part(ask.calls[0][1], "Targets:\n"))] == [CONTEXT, CHANGED_CONTEXT]


def test_changed_draft_context_asks_again(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(DRAFT_ANSWER, DRAFT_ANSWER)
    draft(store, LENS, SCENE, [TARGET_ONE], None, ask)

    draft(store, LENS, SCENE, [replace(TARGET_ONE, context=CHANGED_CONTEXT)], None, ask)

    assert len(ask.calls) == 2
    assert json.loads(_part(ask.calls[1][1], "Targets:\n"))[0]["context"] == CHANGED_CONTEXT


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"drafts": [ENTRY_ONE]}, id="missing"),
        pytest.param({"drafts": [ENTRY_ONE, ENTRY_TWO, ENTRY_UNKNOWN]}, id="unknown"),
        pytest.param({"drafts": [ENTRY_ONE, ENTRY_ONE, ENTRY_TWO]}, id="duplicate"),
    ],
)
def test_draft_entries_that_do_not_name_each_target_once_are_rejected_and_stored_nothing(tmp_path: Path, answer: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(answer, DRAFT_TWO_ANSWER)

    assert isinstance(draft(store, LENS, SCENE, [TARGET_ONE, TARGET_TWO], None, ask), Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert draft(store, LENS, SCENE, [TARGET_ONE, TARGET_TWO], None, ask) == {
        (LINE_ONE.namespace, LINE_ONE.key): DRAFT_ONE,
        (LINE_TWO.namespace, LINE_TWO.key): DRAFT_TWO,
    }
    assert len(ask.calls) == 2


def test_draft_names_the_retry_gate_only_on_retry(tmp_path: Path) -> None:
    first = Fake(DRAFT_ANSWER)
    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], None, first)
    retry = Fake(DRAFT_ANSWER)
    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], "classifier", retry)

    assert "classifier" not in "\n".join(first.calls[0][1])
    assert "classifier" in "\n".join(retry.calls[0][1])


def test_draft_retry_is_part_of_its_key(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(DRAFT_ANSWER, DRAFT_ANSWER)
    draft(store, LENS, SCENE, [TARGET_ONE], None, ask)

    draft(store, LENS, SCENE, [TARGET_ONE], "classifier", ask)

    assert len(ask.calls) == 2


# Made-up speaker-draft fixtures: one speaker's targets across two scenes. Not game text.
SCENE_TWO = Scene("made-up-scene-two", (LINE_THREE, LINE_FOUR))
TARGET_THREE = Target(LINE_THREE, "Made-up intent three. Not game text.", (), CHANGED_CONTEXT)
DRAFT_THREE = ("Made-up draft of line three. Not game text.",)
ENTRY_THREE = {"namespace": LINE_THREE.namespace, "key": LINE_THREE.key, "drafts": list(DRAFT_THREE)}
SPEAKER_GROUPS = [(SCENE, [TARGET_ONE, TARGET_TWO]), (SCENE_TWO, [TARGET_THREE])]
SPEAKER_ANSWER = {"drafts": [ENTRY_ONE, ENTRY_TWO, ENTRY_THREE]}
SPEAKER_RESULT = {
    (LINE_ONE.namespace, LINE_ONE.key): DRAFT_ONE,
    (LINE_TWO.namespace, LINE_TWO.key): DRAFT_TWO,
    (LINE_THREE.namespace, LINE_THREE.key): DRAFT_THREE,
}


def test_speaker_draft_sends_every_scene_in_one_opus_call_with_draft_schema_and_returns_drafts_per_line(tmp_path: Path) -> None:
    ask = Fake(SPEAKER_ANSWER)

    result = draft_speaker(Store(tmp_path), LENS, SPEAKER_GROUPS, ask)

    assert result == SPEAKER_RESULT
    assert len(ask.calls) == 1
    model, _parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == DRAFT_SCHEMA


def test_speaker_draft_prompt_groups_each_scenes_lines_and_targets_and_says_one_speaker(tmp_path: Path) -> None:
    ask = Fake(SPEAKER_ANSWER)

    draft_speaker(Store(tmp_path), LENS, SPEAKER_GROUPS, ask)

    parts = ask.calls[0][1]
    assert DRAFT_INSTRUCTIONS in parts
    assert SPEAKER_DRAFT_INSTRUCTIONS in parts
    assert "Made-up intent rule. Not game text." in "\n".join(parts)
    scenes = json.loads(_part(parts, "Scenes:\n"))
    assert [scene["scene"] for scene in scenes] == [SCENE.key, SCENE_TWO.key]
    assert [[line["key"] for line in scene["lines"]] for scene in scenes] == [["1", "2"], ["3", "4"]]
    assert [[target["key"] for target in scene["targets"]] for scene in scenes] == [["1", "2"], ["3"]]
    assert scenes[1]["targets"] == [
        {
            "namespace": LINE_THREE.namespace,
            "key": LINE_THREE.key,
            "intent": "Made-up intent three. Not game text.",
            "callbacks": [],
            "context": CHANGED_CONTEXT,
        }
    ]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"drafts": [ENTRY_ONE, ENTRY_TWO]}, id="missing"),
        pytest.param({"drafts": [ENTRY_ONE, ENTRY_TWO, ENTRY_THREE, ENTRY_UNKNOWN]}, id="unknown"),
        pytest.param({"drafts": [ENTRY_ONE, ENTRY_ONE, ENTRY_TWO, ENTRY_THREE]}, id="duplicate"),
    ],
)
def test_speaker_draft_entries_that_do_not_name_each_target_once_are_rejected_and_stored_nothing(tmp_path: Path, answer: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(answer, SPEAKER_ANSWER)

    assert isinstance(draft_speaker(store, LENS, SPEAKER_GROUPS, ask), Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert draft_speaker(store, LENS, SPEAKER_GROUPS, ask) == SPEAKER_RESULT
    assert len(ask.calls) == 2


def test_speaker_draft_is_stored_under_its_speaker_draft_key_and_a_second_call_reuses_it(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(SPEAKER_ANSWER)
    draft_speaker(store, LENS, SPEAKER_GROUPS, ask)
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "scenes": [
            {
                "scene": SCENE.key,
                "lines": [asdict(line) for line in SCENE.lines],
                "targets": [
                    {
                        "namespace": LINE_ONE.namespace,
                        "key": LINE_ONE.key,
                        "intent": TARGET_ONE.intent,
                        "callbacks": [CALLBACK],
                        "context": CONTEXT,
                    },
                    {
                        "namespace": LINE_TWO.namespace,
                        "key": LINE_TWO.key,
                        "intent": TARGET_TWO.intent,
                        "callbacks": [],
                        "context": CHANGED_CONTEXT,
                    },
                ],
            },
            {
                "scene": SCENE_TWO.key,
                "lines": [asdict(line) for line in SCENE_TWO.lines],
                "targets": [
                    {
                        "namespace": LINE_THREE.namespace,
                        "key": LINE_THREE.key,
                        "intent": TARGET_THREE.intent,
                        "callbacks": [],
                        "context": CHANGED_CONTEXT,
                    }
                ],
            },
        ]
    }

    assert record.stem == store_key("speaker_draft", inputs, LENS.stage_text("intent"))
    assert json.loads(record.read_text(encoding="utf-8"))["stage"] == "speaker_draft"
    assert draft_speaker(Store(tmp_path), LENS, SPEAKER_GROUPS, ask) == SPEAKER_RESULT
    assert len(ask.calls) == 1


REVIEWER_NOTE = "Made-up reviewer note. Not game text."
REVIEWER_NOTE_TWO = "Made-up second reviewer note. Not game text."
NOTE_INSTRUCTION_START = "A reviewer read the earlier rewrite of this target line"


def test_draft_without_note_has_no_note_part_and_keeps_its_pre_change_key(tmp_path: Path) -> None:
    ask = Fake(DRAFT_ANSWER)
    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], None, ask)
    [record] = (tmp_path / "results").glob("*.json")
    parts = ask.calls[0][1]
    target = {
        "namespace": LINE_ONE.namespace,
        "key": LINE_ONE.key,
        "intent": TARGET_ONE.intent,
        "callbacks": [CALLBACK],
        "context": CONTEXT,
    }
    inputs = {"scene": SCENE.key, "lines": [asdict(line) for line in SCENE.lines], "targets": [target], "retry": None}

    assert not any(part.startswith("Reviewer note:") for part in parts)
    assert NOTE_INSTRUCTION_START not in "\n".join(parts)
    assert record.stem == store_key("draft", inputs, LENS.stage_text("intent"))


def test_draft_with_note_sends_the_fix_instruction_then_the_note_right_after_targets(tmp_path: Path) -> None:
    ask = Fake(DRAFT_ANSWER)

    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], None, ask, REVIEWER_NOTE)

    parts = ask.calls[0][1]
    targets_at = next(index for index, part in enumerate(parts) if part.startswith("Targets:\n"))
    instruction = next(part for part in parts if part.startswith(NOTE_INSTRUCTION_START))
    assert parts[targets_at + 1 :] == [instruction, f"Reviewer note:\n{REVIEWER_NOTE}"]


def test_each_reviewer_note_is_its_own_stored_draft_and_a_repeated_note_reuses_it(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(DRAFT_ANSWER, DRAFT_ANSWER)

    first = draft(store, LENS, SCENE, [TARGET_ONE], None, ask, REVIEWER_NOTE)
    second = draft(store, LENS, SCENE, [TARGET_ONE], None, ask, REVIEWER_NOTE_TWO)
    repeated = draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], None, Fake(), REVIEWER_NOTE)

    assert first == second == repeated == {(LINE_ONE.namespace, LINE_ONE.key): DRAFT_ONE}
    assert len(ask.calls) == 2
    assert len(list((tmp_path / "results").glob("*.json"))) == 2


def test_draft_note_is_part_of_its_key(tmp_path: Path) -> None:
    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], None, Fake(DRAFT_ANSWER), REVIEWER_NOTE)
    [record] = (tmp_path / "results").glob("*.json")
    target = {
        "namespace": LINE_ONE.namespace,
        "key": LINE_ONE.key,
        "intent": TARGET_ONE.intent,
        "callbacks": [CALLBACK],
        "context": CONTEXT,
    }
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "targets": [target],
        "retry": None,
        "note": REVIEWER_NOTE,
    }

    assert record.stem == store_key("draft", inputs, LENS.stage_text("intent"))


def test_retry_with_note_carries_the_note_and_the_retry_gate(tmp_path: Path) -> None:
    ask = Fake(DRAFT_ANSWER)

    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE], "classifier", ask, REVIEWER_NOTE)

    parts = ask.calls[0][1]
    assert parts[-2] == f"Reviewer note:\n{REVIEWER_NOTE}"
    assert parts[-1] == "The previous candidates failed the classifier gate. Write candidates that pass it."


def test_fresh_store_on_same_root_returns_stored_drafts_without_asking(tmp_path: Path) -> None:
    expected = {(LINE_ONE.namespace, LINE_ONE.key): DRAFT_ONE, (LINE_TWO.namespace, LINE_TWO.key): DRAFT_TWO}
    draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE, TARGET_TWO], None, Fake(DRAFT_TWO_ANSWER))
    ask = Fake()

    assert draft(Store(tmp_path), LENS, SCENE, [TARGET_ONE, TARGET_TWO], None, ask) == expected
    assert ask.calls == []


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_draft_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, DRAFT_ANSWER)

    assert draft(store, LENS, SCENE, [TARGET_ONE], None, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert draft(store, LENS, SCENE, [TARGET_ONE], None, ask) == {(LINE_ONE.namespace, LINE_ONE.key): DRAFT_ONE}
    assert len(ask.calls) == 2


def test_editing_intent_rule_reruns_draft(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(DRAFT_ANSWER, DRAFT_ANSWER)
    draft(store, LENS, SCENE, [TARGET_ONE], None, ask)
    edited = replace(LENS, intent_rule="Edited intent rule. Not game text.")

    draft(store, edited, SCENE, [TARGET_ONE], None, ask)

    assert len(ask.calls) == 2


def test_render_stores_the_renderer_text_and_does_not_call_it_twice(tmp_path: Path) -> None:
    renderer = FakeRenderer("Made-up rendered. Not game text.")
    store = Store(tmp_path)

    assert render(store, renderer, "Ash", "Made-up first line. Not game text.") == "Made-up rendered. Not game text."
    assert renderer.fake.calls == [("Ash", "Made-up first line. Not game text.")]

    assert render(Store(tmp_path), renderer, "Ash", "Made-up first line. Not game text.") == "Made-up rendered. Not game text."
    assert len(renderer.fake.calls) == 1


def test_render_runtime_error_raises_fail_without_text_and_stores_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    renderer = FakeRenderer(RuntimeError("Made-up renderer failure. Not game text."))

    with pytest.raises(Fail) as excinfo:
        render(store, renderer, "Made-up speaker. Not game text.", "Made-up narration. Not game text.")

    message = str(excinfo.value)
    assert message == "voice render failed (RuntimeError)"
    for leaked in ("Made-up renderer failure", "Made-up speaker", "Made-up narration"):
        assert leaked not in message
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_render_fail_raised_by_renderer_is_reported_by_class_name_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    renderer = FakeRenderer(Fail("Made-up renderer refusal. Not game text."))

    with pytest.raises(Fail) as excinfo:
        render(store, renderer, None, "Made-up narration. Not game text.")

    assert str(excinfo.value) == "voice render failed (Fail)"
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_judge_schema_requires_the_six_verdicts_and_forbids_extra_properties() -> None:
    verdicts = ["meaning", "plot", "minimal_change", "callback", "fixed", "restores_japanese"]

    assert JUDGE_SCHEMA["type"] == "object"
    assert JUDGE_SCHEMA["required"] == verdicts
    assert set(JUDGE_SCHEMA["properties"]) == set(verdicts)
    assert all(JUDGE_SCHEMA["properties"][verdict] == {"type": "boolean"} for verdict in verdicts)
    assert JUDGE_SCHEMA["additionalProperties"] is False


def test_judge_asks_opus_with_schema_and_returns_judgment(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    result = judge(
        Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask
    )

    assert result == Judgment(meaning=True, plot=True, minimal_change=True, callback=True, fixed=True, restores_japanese=False)
    model, _parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == JUDGE_SCHEMA


def test_judge_prompt_carries_the_japanese_line_candidate_and_rubric(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert "Made-up Japanese one. Not game text." in prompt
    assert "Made-up candidate. Not game text." in prompt
    assert "Made-up judge rubric. Not game text." in prompt
    assert '"japanese": "Made-up Japanese two. Not game text."' in prompt


def test_fresh_store_on_same_root_returns_stored_judgment_without_asking(tmp_path: Path) -> None:
    judge(
        Store(tmp_path),
        LENS,
        SCENE,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        REASONS,
        INTENT_TEXT,
        CONTEXT,
        Fake(JUDGE_ANSWER),
    )
    ask = Fake()

    assert (
        judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)
        == JUDGMENT
    )
    assert ask.calls == []


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_judge_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, JUDGE_ANSWER)

    assert (
        judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask) == failure
    )
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert (
        judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask) == JUDGMENT
    )
    assert len(ask.calls) == 2


def test_editing_judge_rubric_reruns_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(JUDGE_ANSWER, JUDGE_ANSWER)
    judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)
    edited = replace(LENS, judge_rubric="Edited judge rubric. Not game text.")

    judge(store, edited, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)

    assert len(ask.calls) == 2


JUDGE_INSTRUCTIONS_PINNED = (
    "Judge a candidate rewrite of the target line. Return JSON with six booleans. meaning: the candidate keeps "
    "everything the judge rubric protects: the plot facts, who loves whom, the character traits and the beat. plot: "
    "the plot is unchanged. minimal_change: the candidate changes no more than it must; moving toward the Japanese "
    "line counts as minimal change. callback: every callback still holds. fixed: the candidate answers no to every "
    "line reason question. restores_japanese: the candidate restores the meaning of the Japanese line. The Japanese "
    "line is the meaning reference. The judge rubric overrides the Japanese line wherever they conflict. Refer to "
    "lines only by namespace and key."
)
BUCKET_JUDGE_INSTRUCTIONS_PINNED = (
    "Judge a candidate rewrite of the target line, drafted under the bucket named below. Return JSON with eight booleans. "
    "meaning: the candidate keeps everything the judge rubric protects: the plot facts, who loves whom, the character "
    "traits and the beat. plot: the plot is unchanged. minimal_change: the candidate changes no more than it must; moving "
    "toward the Japanese line counts as minimal change. callback: every callback still holds. fixed: the candidate makes "
    "the bucket's edit, present and legible, as the judge rubric's [fixed] label defines it for the bucket. no_invention: "
    "the candidate passes the judge rubric's [no-invention] label. point: the candidate passes the judge rubric's "
    "[keep-point] label. restores_japanese: the candidate restores the meaning of the Japanese line. The Japanese line is "
    "the meaning reference. The judge rubric overrides the Japanese line wherever they conflict. Refer to lines only by "
    "namespace and key."
)
JUDGE_WITHOUT_BUCKET_KEY = "0d858f5920a69b814a9f72e6ded443b16da0070a8f0708a5bd9e4ba183e287a4"
BUCKET = "made-up-bucket"
BUCKET_JUDGE_ANSWER = {**JUDGE_ANSWER, "no_invention": True, "point": True}
BUCKET_JUDGMENT = Judgment(
    meaning=True,
    plot=True,
    minimal_change=True,
    callback=True,
    fixed=True,
    restores_japanese=False,
    no_invention=True,
    point=True,
)


def test_bucket_judge_verdicts_are_the_six_plus_no_invention_and_point() -> None:
    assert BUCKET_JUDGE_VERDICTS == (
        "meaning",
        "plot",
        "minimal_change",
        "callback",
        "fixed",
        "restores_japanese",
        "no_invention",
        "point",
    )


def test_bucket_judge_schema_requires_the_eight_verdicts_and_forbids_extra_properties() -> None:
    verdicts = list(BUCKET_JUDGE_VERDICTS)

    assert BUCKET_JUDGE_SCHEMA["type"] == "object"
    assert BUCKET_JUDGE_SCHEMA["required"] == verdicts
    assert set(BUCKET_JUDGE_SCHEMA["properties"]) == set(verdicts)
    assert all(BUCKET_JUDGE_SCHEMA["properties"][verdict] == {"type": "boolean"} for verdict in verdicts)
    assert BUCKET_JUDGE_SCHEMA["additionalProperties"] is False


def test_bucket_judge_instructions_are_the_judge_instructions_with_the_bucket_edits() -> None:
    assert BUCKET_JUDGE_INSTRUCTIONS == BUCKET_JUDGE_INSTRUCTIONS_PINNED


def test_judge_without_bucket_keeps_the_base_instructions_schema_and_store_key(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)

    assert JUDGE_INSTRUCTIONS == JUDGE_INSTRUCTIONS_PINNED
    _model, parts, schema = ask.calls[0]
    assert parts[0] == JUDGE_INSTRUCTIONS_PINNED
    assert schema == JUDGE_SCHEMA
    assert not any(part.startswith("Bucket:\n") for part in parts)
    [record] = (tmp_path / "results").glob("*.json")
    assert record.stem == JUDGE_WITHOUT_BUCKET_KEY


def test_judge_with_bucket_asks_bucket_instructions_and_schema_with_a_bucket_part_after_the_rubric(tmp_path: Path) -> None:
    ask = Fake(BUCKET_JUDGE_ANSWER)

    result = judge(
        Store(tmp_path),
        LENS,
        SCENE,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        REASONS,
        INTENT_TEXT,
        CONTEXT,
        ask,
        bucket=BUCKET,
    )

    assert result == BUCKET_JUDGMENT
    _model, parts, schema = ask.calls[0]
    assert parts[0] == BUCKET_JUDGE_INSTRUCTIONS
    assert schema == BUCKET_JUDGE_SCHEMA
    assert parts[1] == f"Judge rubric:\n{LENS.stage_text('judge')}"
    assert parts[2] == f"Bucket:\n{BUCKET}"


@pytest.mark.parametrize("verdict", ["no_invention", "point"])
def test_bucket_judge_returns_a_false_no_invention_or_point_answer_as_false(tmp_path: Path, verdict: str) -> None:
    answer = {**BUCKET_JUDGE_ANSWER, verdict: False}

    result = judge(
        Store(tmp_path),
        LENS,
        SCENE,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        REASONS,
        INTENT_TEXT,
        CONTEXT,
        Fake(answer),
        bucket=BUCKET,
    )

    assert getattr(result, verdict) is False
    assert result == replace(BUCKET_JUDGMENT, **{verdict: False})


def test_bucket_judge_store_key_carries_the_bucket(tmp_path: Path) -> None:
    judge(
        Store(tmp_path),
        LENS,
        SCENE,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        REASONS,
        INTENT_TEXT,
        CONTEXT,
        Fake(BUCKET_JUDGE_ANSWER),
        bucket=BUCKET,
    )
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "scene": SCENE.key,
        "lines": [asdict(line) for line in SCENE.lines],
        "line": {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key},
        "text": "Made-up candidate. Not game text.",
        "callbacks": [CALLBACK],
        "reasons": list(REASONS),
        "intent": INTENT_TEXT,
        "context": CONTEXT,
    }
    lens_text = LENS.stage_text("judge") + "\n" + LENS.stage_text("triage")

    assert record.stem == store_key("judge", {**inputs, "bucket": BUCKET}, lens_text)
    assert record.stem != store_key("judge", inputs, lens_text)


def test_fresh_store_on_same_root_returns_stored_bucket_judgment_without_asking(tmp_path: Path) -> None:
    judge(
        Store(tmp_path),
        LENS,
        SCENE,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        REASONS,
        INTENT_TEXT,
        CONTEXT,
        Fake(BUCKET_JUDGE_ANSWER),
        bucket=BUCKET,
    )
    ask = Fake()

    assert (
        judge(
            Store(tmp_path),
            LENS,
            SCENE,
            LINE_ONE,
            "Made-up candidate. Not game text.",
            [CALLBACK],
            REASONS,
            INTENT_TEXT,
            CONTEXT,
            ask,
            bucket=BUCKET,
        )
        == BUCKET_JUDGMENT
    )
    assert ask.calls == []


def test_cross_judge_schema_requires_consistent_and_forbids_extra_properties() -> None:
    assert CROSS_SCHEMA["type"] == "object"
    assert CROSS_SCHEMA["required"] == ["consistent"]
    assert CROSS_SCHEMA["properties"]["consistent"] == {"type": "boolean"}
    assert CROSS_SCHEMA["additionalProperties"] is False


def test_cross_judge_asks_opus_with_schema_and_returns_consistent(tmp_path: Path) -> None:
    ask = Fake(CROSS_ANSWER)

    result = cross_judge(
        Store(tmp_path), LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask
    )

    assert result is True
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == CROSS_SCHEMA
    prompt = "\n".join(parts)
    assert "Made-up candidate. Not game text." in prompt
    assert "Made-up judge rubric. Not game text." in prompt
    assert "Made-up narration. Not game text." in prompt


def test_fresh_store_on_same_root_returns_stored_cross_judge_without_asking(tmp_path: Path) -> None:
    cross_judge(
        Store(tmp_path),
        LENS,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        (LINE_TWO,),
        REASONS,
        INTENT_TEXT,
        CONTEXT,
        Fake(CROSS_ANSWER),
    )
    ask = Fake()

    assert (
        cross_judge(
            Store(tmp_path),
            LENS,
            LINE_ONE,
            "Made-up candidate. Not game text.",
            [CALLBACK],
            (LINE_TWO,),
            REASONS,
            INTENT_TEXT,
            CONTEXT,
            ask,
        )
        is True
    )
    assert ask.calls == []


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_cross_judge_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, CROSS_ANSWER)

    assert (
        cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask)
        == failure
    )
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert (
        cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask)
        is True
    )
    assert len(ask.calls) == 2


def test_editing_judge_rubric_reruns_cross_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(CROSS_ANSWER, CROSS_ANSWER)
    cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask)
    edited = replace(LENS, judge_rubric="Edited judge rubric. Not game text.")

    cross_judge(store, edited, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask)

    assert len(ask.calls) == 2


# Made-up re-screen fixtures. Not game text.


def test_judge_prompt_carries_the_line_reason_questions_and_intent(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert 'Line reasons:\n[{"id": "made-up-alpha", "question": "Made-up question alpha. Not game text."}]' in prompt
    assert f"Intent:\n{INTENT_TEXT}" in prompt


def test_judge_instructions_say_the_lens_overrides_the_japanese_line(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)

    instructions = ask.calls[0][1][0]
    assert "The judge rubric overrides the Japanese line wherever they conflict." in instructions


def test_cross_judge_prompt_carries_the_line_reason_questions_and_intent(tmp_path: Path) -> None:
    ask = Fake(CROSS_ANSWER)

    cross_judge(
        Store(tmp_path), LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask
    )

    prompt = "\n".join(ask.calls[0][1])
    assert 'Line reasons:\n[{"id": "made-up-alpha", "question": "Made-up question alpha. Not game text."}]' in prompt
    assert f"Intent:\n{INTENT_TEXT}" in prompt


def test_different_intent_reruns_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(JUDGE_ANSWER, JUDGE_ANSWER)
    judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)

    judge(
        store,
        LENS,
        SCENE,
        LINE_ONE,
        "Made-up candidate. Not game text.",
        [CALLBACK],
        REASONS,
        "Made-up other intent. Not game text.",
        CONTEXT,
        ask,
    )

    assert len(ask.calls) == 2


def test_editing_triage_question_reruns_judge_and_cross_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(JUDGE_ANSWER, CROSS_ANSWER, JUDGE_ANSWER, CROSS_ANSWER)
    judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)
    cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask)
    edited = replace(LENS, triage=(TriageQuestion("made-up-alpha", "Edited question. Not game text."), LENS.triage[1]))

    judge(store, edited, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, CONTEXT, ask)
    cross_judge(store, edited, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, CONTEXT, ask)

    assert len(ask.calls) == 4


QUESTION_ALPHA = LENS.triage[0]
QUESTION_BETA = LENS.triage[1]
LINE_ONE_REF = {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key}
LINE_TWO_REF = {"namespace": LINE_TWO.namespace, "key": LINE_TWO.key}
RESCREEN_ANSWER = {"flagged": [LINE_ONE_REF, LINE_TWO_REF]}


def test_rescreen_schema_requires_flagged_and_forbids_extra_properties() -> None:
    item = RESCREEN_SCHEMA["properties"]["flagged"]["items"]

    assert RESCREEN_SCHEMA["type"] == "object"
    assert RESCREEN_SCHEMA["required"] == ["flagged"]
    assert RESCREEN_SCHEMA["additionalProperties"] is False
    assert RESCREEN_SCHEMA["properties"]["flagged"]["type"] == "array"
    assert item["required"] == ["namespace", "key"]
    assert item["properties"] == {"namespace": {"type": "string"}, "key": {"type": "string"}}
    assert item["additionalProperties"] is False


def test_rescreen_asks_opus_once_with_schema_and_maps_lines_to_the_question_id(tmp_path: Path) -> None:
    ask = Fake(RESCREEN_ANSWER)

    result = rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, CAST, ask)

    assert result == {
        (LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",),
        (LINE_TWO.namespace, LINE_TWO.key): ("made-up-alpha",),
    }
    assert len(ask.calls) == 1
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == RESCREEN_SCHEMA
    assert parts == [
        RESCREEN_INSTRUCTIONS,
        f"Screen criteria:\n{LENS.stage_text('screen')}",
        'Triage question:\n{"id": "made-up-alpha", "question": "Made-up question alpha. Not game text."}',
        parts[3],
        _heading_part("Cast", CAST),
    ]
    assert parts[3].startswith("Scene lines:\n")


def test_rescreen_prompt_carries_the_one_question_and_no_other(tmp_path: Path) -> None:
    ask = Fake({"flagged": []})

    rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, CAST, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert "made-up-alpha" in prompt
    assert "made-up-beta" not in prompt
    assert "Made-up question beta. Not game text." not in prompt


def test_rescreen_sends_the_same_scene_lines_as_screen(tmp_path: Path) -> None:
    screen_ask = Fake({"flagged": []})
    rescreen_ask = Fake({"flagged": []})
    screen(Store(tmp_path), LENS, SCENE, CAST, screen_ask)

    rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, CAST, rescreen_ask)

    assert rescreen_ask.calls[0][1][3] == screen_ask.calls[0][1][3]


def test_rescreen_with_no_flagged_lines_returns_empty_mapping(tmp_path: Path) -> None:
    assert rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, CAST, Fake({"flagged": []})) == {}


def test_rescreen_given_the_same_line_twice_returns_it_once(tmp_path: Path) -> None:
    ask = Fake({"flagged": [LINE_ONE_REF, LINE_ONE_REF]})

    result = rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, CAST, ask)

    assert result == {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}


def test_rescreen_rejects_a_line_outside_the_scene_and_stores_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake({"flagged": [{"namespace": "ST_C09_001_01", "key": "1"}]})

    result = rescreen(store, LENS, SCENE, QUESTION_ALPHA, CAST, ask)

    assert isinstance(result, Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_rescreen_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, RESCREEN_ANSWER)

    assert rescreen(store, LENS, SCENE, QUESTION_ALPHA, CAST, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert rescreen(store, LENS, SCENE, QUESTION_ALPHA, CAST, ask) == {
        (LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",),
        (LINE_TWO.namespace, LINE_TWO.key): ("made-up-alpha",),
    }
    assert len(ask.calls) == 2
    assert store.unfinished() == []


def test_fresh_store_on_same_root_returns_stored_rescreen_without_asking(tmp_path: Path) -> None:
    expected = {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",), (LINE_TWO.namespace, LINE_TWO.key): ("made-up-alpha",)}
    rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, CAST, Fake(RESCREEN_ANSWER))
    ask = Fake()

    assert rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, CAST, ask) == expected
    assert ask.calls == []


def test_each_triage_question_is_asked_on_its_own(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(RESCREEN_ANSWER, RESCREEN_ANSWER)
    rescreen(store, LENS, SCENE, QUESTION_ALPHA, CAST, ask)

    result = rescreen(store, LENS, SCENE, QUESTION_BETA, CAST, ask)

    assert result == {
        (LINE_ONE.namespace, LINE_ONE.key): ("made-up-beta",),
        (LINE_TWO.namespace, LINE_TWO.key): ("made-up-beta",),
    }
    assert len(ask.calls) == 2
    second = "\n".join(ask.calls[1][1])
    assert "made-up-beta" in second
    assert "made-up-alpha" not in second


def test_editing_screen_criteria_reruns_rescreen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(RESCREEN_ANSWER, RESCREEN_ANSWER)
    rescreen(store, LENS, SCENE, QUESTION_ALPHA, CAST, ask)
    edited = replace(LENS, screen_criteria=("Edited criterion. Not game text.",))

    rescreen(store, edited, SCENE, QUESTION_ALPHA, CAST, ask)

    assert len(ask.calls) == 2


# Each stage with its arc context or cast, the heading of that part, the changed value, and the answer it gets. Not game text.
CONTEXT_STAGES = [
    pytest.param(
        lambda store, extra, ask: intent(store, LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], extra, ask),
        INTENT_ANSWER,
        "Arc context",
        CONTEXT,
        CHANGED_CONTEXT,
        id="intent",
    ),
    pytest.param(
        lambda store, extra, ask: judge(
            store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, extra, ask
        ),
        JUDGE_ANSWER,
        "Arc context",
        CONTEXT,
        CHANGED_CONTEXT,
        id="judge",
    ),
    pytest.param(
        lambda store, extra, ask: cross_judge(
            store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, extra, ask
        ),
        CROSS_ANSWER,
        "Arc context",
        CONTEXT,
        CHANGED_CONTEXT,
        id="cross_judge",
    ),
    pytest.param(
        lambda store, extra, ask: screen(store, LENS, SCENE, extra, ask),
        {"flagged": []},
        "Cast",
        CAST,
        CHANGED_CAST,
        id="screen",
    ),
    pytest.param(
        lambda store, extra, ask: rescreen(store, LENS, SCENE, QUESTION_ALPHA, extra, ask),
        {"flagged": []},
        "Cast",
        CAST,
        CHANGED_CAST,
        id="rescreen",
    ),
]


@pytest.mark.parametrize(("call", "answer", "heading", "extra", "changed"), CONTEXT_STAGES)
def test_stage_prompt_carries_its_arc_context_or_cast_as_a_part(
    tmp_path: Path, call: Callable[[Store, Any, Any], Any], answer: Any, heading: str, extra: Any, changed: Any
) -> None:
    ask = Fake(answer)

    call(Store(tmp_path), extra, ask)

    assert _heading_part(heading, extra) in ask.calls[0][1]


@pytest.mark.parametrize(("call", "answer", "heading", "extra", "changed"), CONTEXT_STAGES)
def test_changed_arc_context_or_cast_asks_again(
    tmp_path: Path, call: Callable[[Store, Any, Any], Any], answer: Any, heading: str, extra: Any, changed: Any
) -> None:
    store = Store(tmp_path)
    ask = Fake(answer, answer)
    call(store, extra, ask)

    call(store, changed, ask)

    assert len(ask.calls) == 2
    assert _heading_part(heading, changed) in ask.calls[1][1]


PICK_GROUPS = [(SCENE, [LINE_ONE]), (SCENE_TWO, [LINE_THREE])]
PICK_KEEP = {"keep": True, "namespace": "", "key": "", "intent": "", "drafts": [], "shape": "none"}
PICK_CHANGE = {
    "keep": False,
    "namespace": LINE_THREE.namespace,
    "key": LINE_THREE.key,
    "intent": "Made-up trans detail intent. Not game text.",
    "drafts": ["Made-up trans draft. Not game text.", "Made-up second trans draft. Not game text."],
    "shape": CHOSEN_NAME,
}
# A world or nonbinary answer carries no shape. Not game text.
PICK_WORLD_CHANGE = {name: value for name, value in PICK_CHANGE.items() if name != "shape"}
PICK_RESULT = PickChange(
    namespace=LINE_THREE.namespace,
    key=LINE_THREE.key,
    intent="Made-up trans detail intent. Not game text.",
    drafts=("Made-up trans draft. Not game text.", "Made-up second trans draft. Not game text."),
    shape=CHOSEN_NAME,
)
# A made-up scene of nine lines, for the window around each line of a pick. Not game text.
WIDE_LINES = tuple(
    SceneLine(
        "ST_C01_002_01",
        str(number),
        None,
        f"Made-up wide line {number}. Not game text.",
        f"Made-up Japanese wide {number}. Not game text.",
        1,
    )
    for number in range(1, 10)
)
WIDE_SCENE = Scene("made-up-scene-wide", WIDE_LINES)


def test_pick_draft_schema_asks_for_one_entry_with_keep_namespace_key_intent_and_drafts() -> None:
    assert PICK_DRAFT_SCHEMA["type"] == "object"
    assert PICK_DRAFT_SCHEMA["required"] == ["keep", "namespace", "key", "intent", "drafts"]
    assert PICK_DRAFT_SCHEMA["additionalProperties"] is False
    assert PICK_DRAFT_SCHEMA["properties"]["keep"] == {"type": "boolean"}
    assert PICK_DRAFT_SCHEMA["properties"]["drafts"] == {"type": "array", "items": {"type": "string"}}


def test_trans_pick_draft_schema_adds_a_required_shape_of_the_four_shapes_or_none() -> None:
    assert TRANS_SHAPES == ("acceptance", "backing", "chosen-name", "present-life")
    assert CHOSEN_NAME == "chosen-name"
    assert TRANS_PICK_DRAFT_SCHEMA["type"] == "object"
    assert TRANS_PICK_DRAFT_SCHEMA["required"] == ["keep", "namespace", "key", "intent", "drafts", "shape"]
    assert TRANS_PICK_DRAFT_SCHEMA["additionalProperties"] is False
    assert TRANS_PICK_DRAFT_SCHEMA["properties"]["shape"] == {
        "type": "string",
        "enum": ["acceptance", "backing", "chosen-name", "present-life", "none"],
    }
    trans_properties = {name: value for name, value in TRANS_PICK_DRAFT_SCHEMA["properties"].items() if name != "shape"}
    assert trans_properties == PICK_DRAFT_SCHEMA["properties"]


def test_world_and_nonbinary_pick_draft_schema_has_no_shape() -> None:
    assert "shape" not in PICK_DRAFT_SCHEMA["properties"]
    assert "shape" not in PICK_DRAFT_SCHEMA["required"]


def test_trans_shape_instructions_name_the_four_shape_ids_and_none() -> None:
    assert TRANS_SHAPE_INSTRUCTIONS == (
        "The bucket's label names four shapes by id: acceptance, backing, chosen-name and present-life. "
        "Set shape to the id of the shape the change uses, and to none when no line changes."
    )


def test_pick_draft_instructions_keep_the_placeholder_tag_and_length_rules_without_partner_wording() -> None:
    assert "Keep every {...} placeholder" in PICK_DRAFT_INSTRUCTIONS
    assert "every <text ...>...</> tag" in PICK_DRAFT_INSTRUCTIONS
    assert "Stay within 120% of each English line's length" in PICK_DRAFT_INSTRUCTIONS
    assert "nonbinary" not in PICK_DRAFT_INSTRUCTIONS.lower()
    assert "partner" not in PICK_DRAFT_INSTRUCTIONS.lower()
    assert "they/them" not in PICK_DRAFT_INSTRUCTIONS


@pytest.mark.parametrize("bucket", ["world", "nonbinary", "trans"])
def test_pick_draft_instructions_name_no_single_bucket_and_point_at_the_bucket_label(bucket: str) -> None:
    assert bucket not in PICK_DRAFT_INSTRUCTIONS.lower()
    assert "-bucket]" in PICK_DRAFT_INSTRUCTIONS
    assert "she's trans" not in PICK_DRAFT_INSTRUCTIONS


def test_pick_draft_sends_one_opus_call_with_the_trans_schema_and_returns_the_chosen_line_and_shape(tmp_path: Path) -> None:
    ask = Fake(PICK_CHANGE)

    result = pick_draft(Store(tmp_path), LENS, "trans", PICK_GROUPS, ask)

    assert result == PICK_RESULT
    assert len(ask.calls) == 1
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == TRANS_PICK_DRAFT_SCHEMA
    assert PICK_DRAFT_INSTRUCTIONS in parts
    assert TRANS_SHAPE_INSTRUCTIONS in parts
    assert "Made-up intent rule. Not game text." in "\n".join(parts)


@pytest.mark.parametrize("bucket", ["world", "nonbinary"])
def test_world_and_nonbinary_pick_draft_sends_the_pick_schema_with_no_shape_part_and_returns_no_shape(tmp_path: Path, bucket: str) -> None:
    ask = Fake(PICK_WORLD_CHANGE)

    result = pick_draft(Store(tmp_path), LENS, bucket, PICK_GROUPS, ask)

    assert result == replace(PICK_RESULT, shape=None)
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == PICK_DRAFT_SCHEMA
    assert parts[0] == PICK_DRAFT_INSTRUCTIONS
    assert TRANS_SHAPE_INSTRUCTIONS not in parts


def test_pick_draft_no_change_returns_none_and_a_second_call_reuses_it(tmp_path: Path) -> None:
    ask = Fake(PICK_KEEP)

    assert pick_draft(Store(tmp_path), LENS, "trans", PICK_GROUPS, ask) is None
    assert pick_draft(Store(tmp_path), LENS, "trans", PICK_GROUPS, Fake()) is None
    assert len(ask.calls) == 1


def test_pick_draft_prompt_names_the_bucket(tmp_path: Path) -> None:
    ask = Fake(PICK_KEEP)

    pick_draft(Store(tmp_path), LENS, "trans", PICK_GROUPS, ask)

    assert _part(ask.calls[0][1], "Bucket:\n") == "trans"


def test_pick_draft_shows_each_line_with_the_three_scene_lines_either_side_and_no_line_further_away(tmp_path: Path) -> None:
    ask = Fake(PICK_KEEP)

    pick_draft(Store(tmp_path), LENS, "trans", [(WIDE_SCENE, [WIDE_LINES[0], WIDE_LINES[8]])], ask)

    entries = json.loads(_part(ask.calls[0][1], "Lines:\n"))
    assert [[line["key"] for line in entry["window"]] for entry in entries] == [["1", "2", "3", "4"], ["6", "7", "8", "9"]]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({**PICK_CHANGE, "namespace": LINE_TWO.namespace, "key": LINE_TWO.key}, id="line-outside-the-pick"),
        pytest.param({**PICK_CHANGE, "intent": ""}, id="change-without-intent"),
        pytest.param({**PICK_CHANGE, "drafts": []}, id="change-without-drafts"),
        pytest.param({**PICK_KEEP, "drafts": ["Made-up draft kept with no change. Not game text."]}, id="no-change-with-drafts"),
        pytest.param({**PICK_CHANGE, "shape": "none"}, id="change-with-no-shape"),
        pytest.param({**PICK_KEEP, "shape": "backing"}, id="keep-with-a-shape"),
    ],
)
def test_pick_draft_answers_that_break_the_pick_rules_are_rejected_and_stored_nothing(tmp_path: Path, answer: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(answer, PICK_CHANGE)

    assert isinstance(pick_draft(store, LENS, "trans", PICK_GROUPS, ask), Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert pick_draft(store, LENS, "trans", PICK_GROUPS, ask) == PICK_RESULT
    assert len(ask.calls) == 2


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
)
def test_pick_draft_failed_answer_passes_through_unstored(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, PICK_CHANGE)

    assert pick_draft(store, LENS, "trans", PICK_GROUPS, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert pick_draft(store, LENS, "trans", PICK_GROUPS, ask) == PICK_RESULT


def test_pick_draft_is_stored_under_its_pick_draft_key_with_its_bucket_and_a_fresh_store_reuses_it(tmp_path: Path) -> None:
    ask = Fake(PICK_CHANGE)
    pick_draft(Store(tmp_path), LENS, "trans", PICK_GROUPS, ask)
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "bucket": "trans",
        "scenes": [
            {
                "scene": SCENE.key,
                "lines": [asdict(line) for line in SCENE.lines],
                "targets": [{"namespace": LINE_ONE.namespace, "key": LINE_ONE.key}],
            },
            {
                "scene": SCENE_TWO.key,
                "lines": [asdict(line) for line in SCENE_TWO.lines],
                "targets": [{"namespace": LINE_THREE.namespace, "key": LINE_THREE.key}],
            },
        ],
        "shapes": list(TRANS_SHAPES),
    }

    assert record.stem == store_key("pick_draft", inputs, LENS.stage_text("intent"))
    assert json.loads(record.read_text(encoding="utf-8"))["stage"] == "pick_draft"
    assert pick_draft(Store(tmp_path), LENS, "trans", PICK_GROUPS, Fake()) == PICK_RESULT
    assert len(ask.calls) == 1


# Pinned to the store keys the unchanged world and nonbinary pick drafts had at the base revision d5bc8c1. Not game text.
@pytest.mark.parametrize(
    ("bucket", "digest"),
    [
        ("world", "47792a2aa000e6e110248fe1b57ec58d9dca836df1aea32e808fdc7af9692f6b"),
        ("nonbinary", "551867dbb716553a288eeb4d95caf37ca3fff490c54bb8abc39ff3d1dab4233e"),
    ],
)
def test_world_and_nonbinary_pick_drafts_keep_their_base_store_key(tmp_path: Path, bucket: str, digest: str) -> None:
    pick_draft(Store(tmp_path), LENS, bucket, PICK_GROUPS, Fake(PICK_WORLD_CHANGE))

    [record] = (tmp_path / "results").glob("*.json")
    assert record.stem == digest


CANDIDATE_REFS = [(LINE_ONE.namespace, LINE_ONE.key), (LINE_TWO.namespace, LINE_TWO.key), (LINE_THREE.namespace, LINE_THREE.key)]
CANDIDATE_LISTED = [{"namespace": namespace, "key": key} for namespace, key in CANDIDATE_REFS]
INSERTABLE_QUESTION = "Is the line about the speaker's home, family, work or daily life?"
TRANS_CANDIDATE_QUESTION = "Is the line about the speaker's family, name, belonging, someone standing up for them, or their past?"


def _marked(*entries: tuple[SceneLine, list[str]]) -> dict[str, Any]:
    return {"marked": [{"namespace": line.namespace, "key": line.key, "marks": marks} for line, marks in entries]}


CANDIDATE_ANSWER = _marked((LINE_ONE, [TRANS_CANDIDATE, INSERTABLE]), (LINE_TWO, [INSERTABLE]))
CANDIDATE_RESULT = {
    (LINE_ONE.namespace, LINE_ONE.key): (INSERTABLE, TRANS_CANDIDATE),
    (LINE_TWO.namespace, LINE_TWO.key): (INSERTABLE,),
}


def test_candidate_mark_ids_are_insertable_and_trans_candidate() -> None:
    assert INSERTABLE == "insertable"
    assert TRANS_CANDIDATE == "trans-candidate"


def test_candidates_asks_haiku_with_schema_and_returns_marks_in_constant_order_for_marked_lines_only(tmp_path: Path) -> None:
    ask = Fake(CANDIDATE_ANSWER)

    result = candidates(Store(tmp_path), FOUR_LINE_SCENE, CANDIDATE_REFS, ask)

    assert result == CANDIDATE_RESULT
    model, _parts, schema = ask.calls[0]
    assert model == claude.HAIKU
    assert schema == CANDIDATE_SCHEMA


def test_candidate_schema_requires_marked_and_forbids_extra_properties() -> None:
    entry = CANDIDATE_SCHEMA["properties"]["marked"]["items"]

    assert CANDIDATE_SCHEMA["required"] == ["marked"]
    assert CANDIDATE_SCHEMA["additionalProperties"] is False
    assert entry["required"] == ["namespace", "key", "marks"]
    assert entry["additionalProperties"] is False


def test_candidate_prompt_carries_both_questions_the_scene_lines_and_the_refs_to_answer(tmp_path: Path) -> None:
    ask = Fake(_marked())

    candidates(Store(tmp_path), SCENE, [(LINE_ONE.namespace, LINE_ONE.key)], ask)

    parts = ask.calls[0][1]
    prompt = "\n".join(parts)
    assert INSERTABLE_QUESTION in prompt
    assert TRANS_CANDIDATE_QUESTION in prompt
    assert CANDIDATE_INSTRUCTIONS in parts
    lines_part = _part(parts, "Scene lines:\n")
    assert '"english": "Made-up first line. Not game text."' in lines_part
    assert '"english": "Made-up narration. Not game text."' in lines_part
    assert "Made-up Japanese one. Not game text." not in prompt
    assert '"japanese"' not in prompt
    assert json.loads(_part(parts, "Lines to mark:\n")) == [{"namespace": LINE_ONE.namespace, "key": LINE_ONE.key}]


def test_candidates_store_under_the_candidates_stage_and_a_second_call_makes_no_ask(tmp_path: Path) -> None:
    candidates(Store(tmp_path), FOUR_LINE_SCENE, CANDIDATE_REFS, Fake(CANDIDATE_ANSWER))
    [record] = (tmp_path / "results").glob("*.json")
    inputs = {
        "scene": FOUR_LINE_SCENE.key,
        "lines": [asdict(line) for line in FOUR_LINE_SCENE.lines],
        "refs": CANDIDATE_LISTED,
    }
    assert record.stem == store_key("candidates", inputs, CANDIDATE_INSTRUCTIONS)
    assert json.loads(record.read_text(encoding="utf-8"))["stage"] == "candidates"
    ask = Fake()

    assert candidates(Store(tmp_path), FOUR_LINE_SCENE, CANDIDATE_REFS, ask) == CANDIDATE_RESULT
    assert ask.calls == []


def test_screen_on_the_same_scene_after_candidates_still_asks_and_is_stored_under_its_own_key(tmp_path: Path) -> None:
    store = Store(tmp_path)
    candidates(store, SCENE, [(LINE_ONE.namespace, LINE_ONE.key)], Fake(_marked((LINE_ONE, [INSERTABLE]))))
    ask = Fake({"flagged": []})

    assert screen(store, LENS, SCENE, CAST, ask) == {}
    assert len(ask.calls) == 1
    assert len(list((tmp_path / "results").glob("*.json"))) == 2


@pytest.mark.parametrize(
    "refs,answer",
    [
        ([(LINE_ONE.namespace, LINE_ONE.key)], _marked((LINE_TWO, [INSERTABLE]))),
        (CANDIDATE_REFS, _marked((LINE_ONE, ["made-up-mark"]))),
    ],
    ids=["ref-outside-listed-refs", "unknown-mark"],
)
def test_candidates_rejects_an_unlisted_ref_or_unknown_mark_and_stores_nothing(tmp_path: Path, refs: Any, answer: Any) -> None:
    store = Store(tmp_path)

    result = candidates(store, FOUR_LINE_SCENE, refs, Fake(answer))

    assert isinstance(result, Rejected)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_candidates_pass_through_unstored_and_are_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, CANDIDATE_ANSWER)

    assert candidates(store, FOUR_LINE_SCENE, CANDIDATE_REFS, ask) == failure
    assert store.finished() == []

    assert candidates(store, FOUR_LINE_SCENE, CANDIDATE_REFS, ask) == CANDIDATE_RESULT
    assert len(ask.calls) == 2
