import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline import jev, stages
from ffrd.pipeline.claude import Unfinished
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.stages import Ask, Scene, SceneLine
from ffrd.pipeline.store import Store
from ffrd.pipeline.tune import (
    SAMPLE,
    THRESHOLD_FILE,
    Tuning,
    cmd_tune,
    load_threshold,
    register,
    sample,
    save_threshold,
    tune,
)

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


# Made-up lens file for the command tests. Not game text.
CLI_LENS = "made-up"
CLI_LENS_TOML = """\
[[triage]]
id = "made-up-alpha"
question = "Made-up question alpha. Not game text."

[[triage]]
id = "made-up-beta"
question = "Made-up question beta. Not game text."

[screen]
criteria = ["Made-up criterion one. Not game text."]

[intent]
rule = "Made-up intent rule. Not game text."

[judge]
rubric = "Made-up judge rubric. Not game text."
"""
OUT_OF_SCOPE_TEXT = "Made-up out-of-scope line. Not game text."
REAL_BINDINGS = stages.bindings
COUNTS_LINE = "threshold 0.9000: 1 of 2 sampled scenes flagged by the screen, 1 at or above it\n"

SCENE_ONE = Scene(
    "made-up-scene-1",
    (
        SceneLine("ST_C01_001_01", "1", None, "Made-up in-scope line. Not game text.", "Made-up Japanese. Not game text.", 1),
        SceneLine("ST_ItemName_001", "2", None, OUT_OF_SCOPE_TEXT, "Made-up Japanese. Not game text.", 1),
    ),
)
SCENE_TWO = Scene(
    "made-up-scene-2",
    (SceneLine("ST_C01_001_02", "1", None, "Made-up second line. Not game text.", "Made-up Japanese. Not game text.", 1),),
)


class _Source:
    def __init__(self, scenes: list[Scene], *, scene_table: bool) -> None:
        self._scenes = scenes
        self.scene_table = scene_table

    def scenes(self) -> list[Scene]:
        return self._scenes


class _Batch:
    def __init__(self, ask: Ask) -> None:
        self.ask = ask


@pytest.fixture
def cmd_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    (root / "lenses").mkdir()
    (root / "lenses" / f"{CLI_LENS}.toml").write_text(CLI_LENS_TOML)
    monkeypatch.chdir(root)
    return root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ffrd")
    register(parser.add_subparsers(required=True))
    return parser


def test_cmd_tune_scopes_scenes_saves_threshold_and_prints_only_the_counts(
    cmd_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(stages, "bindings", lambda: SimpleNamespace(scenes=_Source([SCENE_ONE, SCENE_TWO], scene_table=False)))
    jev_fake = Fake(scores(0.9, 0.1), scores(0.2, 0.3))
    batches = Asks(FLAGGED, NOT_FLAGGED)
    monkeypatch.setattr(jev, "triage", jev_fake)
    monkeypatch.setattr(stages, "Batch", lambda: _Batch(batches()))

    cmd_tune(argparse.Namespace(lens=CLI_LENS))

    assert capsys.readouterr().out == COUNTS_LINE
    assert load_threshold(cmd_repo / "work" / CLI_LENS) == 0.9
    assert json.loads((cmd_repo / "work" / CLI_LENS / THRESHOLD_FILE).read_text(encoding="utf-8")) == {
        "threshold": 0.9,
        "scenes": 2,
        "positives": 1,
        "passing": 1,
    }
    assert len(jev_fake.calls) == 2
    assert OUT_OF_SCOPE_TEXT not in repr(jev_fake.calls)
    assert OUT_OF_SCOPE_TEXT not in repr([fake.calls for fake in batches.made])


def test_cmd_tune_scopes_by_the_bound_source_scene_table(
    cmd_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(stages, "bindings", lambda: SimpleNamespace(scenes=_Source([SCENE_ONE, SCENE_TWO], scene_table=True)))
    jev_fake = Fake(scores(0.9, 0.1), scores(0.2, 0.3))
    batches = Asks(FLAGGED, NOT_FLAGGED)
    monkeypatch.setattr(jev, "triage", jev_fake)
    monkeypatch.setattr(stages, "Batch", lambda: _Batch(batches()))

    cmd_tune(argparse.Namespace(lens=CLI_LENS))

    assert OUT_OF_SCOPE_TEXT in repr(jev_fake.calls)


def test_cmd_tune_with_unbound_scenes_fails_with_the_bindings_message_and_writes_nothing(
    cmd_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(stages, "bindings", REAL_BINDINGS)
    jev_fake = Fake()
    monkeypatch.setattr(jev, "triage", jev_fake)
    monkeypatch.setattr(stages, "Batch", lambda: _Batch(Fake()))

    with pytest.raises(Fail) as excinfo:
        cmd_tune(argparse.Namespace(lens=CLI_LENS))

    assert str(excinfo.value) == "no scene source, classifier and renderer are bound yet"
    assert jev_fake.calls == []
    assert capsys.readouterr().out == ""
    assert not (cmd_repo / "work").exists()


def test_tune_is_registered_with_lens_required_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as missing:
        _parser().parse_args(["tune"])
    assert missing.value.code == 2
    assert "--lens" in capsys.readouterr().err

    args = _parser().parse_args(["tune", "--lens", CLI_LENS])
    assert args.fn is cmd_tune
    assert args.lens == CLI_LENS

    with pytest.raises(SystemExit) as helped:
        _parser().parse_args(["tune", "--help"])
    assert helped.value.code == 0
    tune_help = " ".join(capsys.readouterr().out.split())
    assert "--lens" in tune_help
    assert "lenses/<name>.toml" in tune_help
    assert "work/<name>/" in tune_help

    with pytest.raises(SystemExit) as top:
        _parser().parse_args(["--help"])
    assert top.value.code == 0
    top_help = " ".join(capsys.readouterr().out.split())
    assert "tune set a lens's Jev triage threshold from Sonnet screen labels on up to 100 scenes" in top_help
