from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.accounts import Account
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.stages import SCREEN_SCHEMA, Batch, Scene, SceneLine, bindings, scene_text, screen, triage
from ffrd.pipeline.store import Store

# Made-up scene, lens and answers. Not game text.
LINE_ONE = SceneLine("ST_C01_001_01", "1", "Ash", "Made-up first line. Not game text.", "Made-up Japanese one. Not game text.", 1)
LINE_TWO = SceneLine("ST_C01_001_01", "2", None, "Made-up narration. Not game text.", "Made-up Japanese two. Not game text.", 1)
SCENE = Scene("made-up-scene", (LINE_ONE, LINE_TWO))

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

PROBABILITIES = {"made-up-alpha": 0.25, "made-up-beta": 0.75}
FLAGGED_ONE = {
    "flagged": [{"namespace": LINE_ONE.namespace, "key": LINE_ONE.key, "reasons": ["made-up-alpha"]}],
}


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
    "failure",
    [
        RateLimited("Made-up rate limit detail. Not game text."),
        Unfinished("Made-up unfinished detail. Not game text."),
        Rejected("Made-up rejected detail. Not game text."),
    ],
    ids=["rate-limited", "unfinished", "rejected"],
)
def test_failed_triage_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    jev = Fake(failure, PROBABILITIES)

    assert triage(store, LENS, SCENE, jev) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert triage(store, LENS, SCENE, jev) == PROBABILITIES
    assert len(jev.calls) == 2
    assert store.unfinished() == []


def test_editing_triage_text_reruns_triage_and_screen(tmp_path: Path) -> None:
    store = Store(tmp_path)
    jev = Fake(PROBABILITIES, PROBABILITIES)
    ask = Fake(FLAGGED_ONE, FLAGGED_ONE)
    triage(store, LENS, SCENE, jev)
    screen(store, LENS, SCENE, ask)
    edited = replace(LENS, triage=(TriageQuestion("made-up-alpha", "Edited question. Not game text."), LENS.triage[1]))

    triage(store, edited, SCENE, jev)
    screen(store, edited, SCENE, ask)

    assert len(jev.calls) == 2
    assert len(ask.calls) == 2


def test_editing_only_screen_criteria_reruns_screen_and_not_triage(tmp_path: Path) -> None:
    store = Store(tmp_path)
    jev = Fake(PROBABILITIES)
    ask = Fake(FLAGGED_ONE, FLAGGED_ONE)
    triage(store, LENS, SCENE, jev)
    screen(store, LENS, SCENE, ask)
    edited = replace(LENS, screen_criteria=("Edited criterion. Not game text.",))

    triage(store, edited, SCENE, jev)
    screen(store, edited, SCENE, ask)

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


def test_screen_asks_sonnet_with_schema_and_returns_reasons_in_lens_order(tmp_path: Path) -> None:
    answer = {
        "flagged": [
            {"namespace": LINE_ONE.namespace, "key": LINE_ONE.key, "reasons": ["made-up-beta", "made-up-alpha"]},
            {"namespace": LINE_TWO.namespace, "key": LINE_TWO.key, "reasons": ["made-up-beta"]},
        ]
    }
    ask = Fake(answer)

    result = screen(Store(tmp_path), LENS, SCENE, ask)

    assert result == {
        (LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha", "made-up-beta"),
        (LINE_TWO.namespace, LINE_TWO.key): ("made-up-beta",),
    }
    model, parts, schema = ask.calls[0]
    assert model == claude.SONNET
    assert schema == SCREEN_SCHEMA
    prompt = "\n".join(parts)
    assert "Made-up criterion one. Not game text." in prompt
    assert "made-up-alpha" in prompt
    assert "Made-up first line. Not game text." in prompt


def test_screen_prompt_lines_carry_namespace_key_speaker_and_english_only(tmp_path: Path) -> None:
    ask = Fake({"flagged": []})

    screen(Store(tmp_path), LENS, SCENE, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert "Made-up Japanese one. Not game text." not in prompt
    assert "Made-up Japanese two. Not game text." not in prompt
    assert '"japanese"' not in prompt
    assert '"chapter"' not in prompt
    assert '"english": "Made-up first line. Not game text."' in prompt


def test_screen_with_no_flagged_lines_returns_empty_mapping(tmp_path: Path) -> None:
    assert screen(Store(tmp_path), LENS, SCENE, Fake({"flagged": []})) == {}


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

    result = screen(store, LENS, SCENE, Fake(answer))

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

    assert screen(store, LENS, SCENE, ask) == failure
    assert store.finished() == []

    assert screen(store, LENS, SCENE, ask) == {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}
    assert len(ask.calls) == 2


def test_fresh_store_on_same_root_returns_stored_screen_without_asking(tmp_path: Path) -> None:
    expected = {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}
    screen(Store(tmp_path), LENS, SCENE, Fake(FLAGGED_ONE))
    ask = Fake()

    assert screen(Store(tmp_path), LENS, SCENE, ask) == expected
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


def test_bindings_fails_until_a_later_effort_binds_them() -> None:
    with pytest.raises(Fail) as excinfo:
        bindings()

    assert str(excinfo.value) == "no scene source, classifier and renderer are bound yet"
