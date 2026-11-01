import json
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline.claude import Unfinished
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.stages import Ask, Scene, SceneLine
from ffrd.pipeline.store import Store
from ffrd.pipeline.tune import SAMPLE, THRESHOLD_FILE, Tuning, load_threshold, sample, save_threshold, tune

# Made-up scene, lens and answers. Not game text.
LENS = Lens(
    name="made-up",
    triage=(
        TriageQuestion("made-up-alpha", "Made-up question alpha. Not game text."),
        TriageQuestion("made-up-beta", "Made-up question beta. Not game text."),
    ),
    screen_criteria=("Made-up criterion one. Not game text.",),
    intent_rule="",
    judge_rubric="",
)
FLAGGED = {"flagged": [{"namespace": "ST_C01_001_01", "key": "1", "reasons": ["made-up-alpha"]}]}
NOT_FLAGGED: dict[str, Any] = {"flagged": []}
UNFINISHED = Unfinished("Made-up unfinished detail. Not game text.")


def scene(index: int) -> Scene:
    line = SceneLine("ST_C01_001_01", "1", None, f"Made-up line {index}. Not game text.", "Made-up Japanese. Not game text.", 1)
    return Scene(f"made-up-{index}", (line,))


def scores(alpha: float, beta: float) -> dict[str, float]:
    return {"made-up-alpha": alpha, "made-up-beta": beta}


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


class Asks:
    """Hands out one fresh fake ask per sampled scene, in order, and keeps each one for inspection."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.made: list[Fake] = []

    def __call__(self) -> Ask:
        fake = Fake(self.answers.pop(0)) if self.answers else Fake()
        self.made.append(fake)
        return fake


def test_sample_returns_every_scene_when_there_are_at_most_size() -> None:
    few = [scene(i) for i in range(3)]
    exact = [scene(i) for i in range(SAMPLE)]

    assert sample(few) == few
    assert sample(exact) == exact


def test_sample_takes_evenly_spaced_scenes_when_there_are_more_than_size() -> None:
    many = [scene(i) for i in range(250)]

    picked = sample(many)

    assert len(picked) == SAMPLE
    assert [s.key for s in picked[:4]] == ["made-up-0", "made-up-2", "made-up-5", "made-up-7"]
    assert picked[-1].key == "made-up-247"


def test_threshold_is_lowest_positive_score_and_passing_counts_every_scene_at_or_above_it(tmp_path: Path) -> None:
    scenes = [scene(0), scene(1), scene(2), scene(3)]
    jev = Fake(scores(0.9, 0.1), scores(0.1, 0.4), scores(0.2, 0.6), scores(0.7, 0.2))
    asks = Asks(FLAGGED, NOT_FLAGGED, FLAGGED, NOT_FLAGGED)

    tuning = tune(Store(tmp_path), LENS, scenes, jev, asks)

    assert tuning == Tuning(threshold=0.6, scenes=4, positives=2, passing=3)
    assert len(jev.calls) == 4


def test_second_tune_reuses_stored_answers_and_asks_nothing_again(tmp_path: Path) -> None:
    scenes = [scene(0), scene(1)]
    first = tune(Store(tmp_path), LENS, scenes, Fake(scores(0.9, 0.1), scores(0.1, 0.2)), Asks(FLAGGED, NOT_FLAGGED))
    jev = Fake()
    asks = Asks()

    second = tune(Store(tmp_path), LENS, scenes, jev, asks)

    assert second == first
    assert jev.calls == []
    assert [ask.calls for ask in asks.made] == [[], []]


@pytest.mark.parametrize("triage_fails", [True, False], ids=["triage-fails", "screen-fails"])
def test_unfinished_scene_fails_with_count_after_every_scene_is_tried(tmp_path: Path, triage_fails: bool) -> None:
    scenes = [scene(0), scene(1), scene(2)]
    jev = Fake(scores(0.9, 0.1), UNFINISHED if triage_fails else scores(0.2, 0.3), scores(0.1, 0.2))
    asks = Asks(FLAGGED, NOT_FLAGGED if triage_fails else UNFINISHED, NOT_FLAGGED)

    with pytest.raises(Fail) as excinfo:
        tune(Store(tmp_path), LENS, scenes, jev, asks)

    assert str(excinfo.value) == "1 of 3 sampled scenes unfinished; rerun ffrd tune"
    assert len(jev.calls) == 3


def test_no_flagged_scene_fails_so_no_threshold_is_set(tmp_path: Path) -> None:
    jev = Fake(scores(0.9, 0.1), scores(0.2, 0.3))
    asks = Asks(NOT_FLAGGED, NOT_FLAGGED)

    with pytest.raises(Fail) as excinfo:
        tune(Store(tmp_path), LENS, [scene(0), scene(1)], jev, asks)

    assert str(excinfo.value) == "no sampled scene was flagged, so no threshold can be set"


def test_saved_threshold_loads_back_and_writes_the_four_fields(tmp_path: Path) -> None:
    directory = tmp_path / "made-up" / "work"

    save_threshold(directory, Tuning(threshold=0.6, scenes=4, positives=2, passing=3))

    assert json.loads((directory / THRESHOLD_FILE).read_text(encoding="utf-8")) == {
        "threshold": 0.6,
        "scenes": 4,
        "positives": 2,
        "passing": 3,
    }
    assert load_threshold(directory) == 0.6


@pytest.mark.parametrize("value", [0, 1, 0.25], ids=["zero", "one", "fraction"])
def test_load_threshold_accepts_numbers_from_0_to_1_inclusive(tmp_path: Path, value: float) -> None:
    (tmp_path / THRESHOLD_FILE).write_text(json.dumps({"threshold": value}), encoding="utf-8")

    assert load_threshold(tmp_path) == float(value)


def test_load_threshold_names_the_real_path_when_missing(tmp_path: Path) -> None:
    with pytest.raises(Fail) as excinfo:
        load_threshold(tmp_path)

    assert str(excinfo.value) == (f"no triage threshold at {tmp_path / THRESHOLD_FILE}; run `ffrd tune --lens <name>` or pass --no-triage")


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"threshold": 1.5}',
        '{"threshold": -0.1}',
        '{"threshold": NaN}',
        '{"threshold": "0.5"}',
        '{"threshold": true}',
        "[0.5]",
        "{}",
        '{"threshold": null}',
    ],
    ids=["non-json", "above-one", "below-zero", "nan", "string", "bool", "list", "no-field", "null"],
)
def test_load_threshold_names_the_path_when_the_file_is_not_a_threshold(tmp_path: Path, text: str) -> None:
    path = tmp_path / THRESHOLD_FILE
    path.write_text(text, encoding="utf-8")

    with pytest.raises(Fail) as excinfo:
        load_threshold(tmp_path)

    assert str(path) in str(excinfo.value)


def test_load_threshold_names_the_path_when_unreadable(tmp_path: Path) -> None:
    path = tmp_path / THRESHOLD_FILE
    path.mkdir()

    with pytest.raises(Fail) as excinfo:
        load_threshold(tmp_path)

    assert str(path) in str(excinfo.value)


def test_save_threshold_names_the_path_when_the_directory_cannot_be_created(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(Fail) as excinfo:
        save_threshold(blocked, Tuning(threshold=0.6, scenes=4, positives=2, passing=3))

    assert str(blocked / THRESHOLD_FILE) in str(excinfo.value)
