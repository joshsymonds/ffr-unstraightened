from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.accounts import Account
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Judgment
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.stages import (
    CROSS_SCHEMA,
    DRAFT_SCHEMA,
    INTENT_SCHEMA,
    JUDGE_SCHEMA,
    RESCREEN_INSTRUCTIONS,
    RESCREEN_SCHEMA,
    SCREEN_SCHEMA,
    Batch,
    Scene,
    SceneLine,
    bindings,
    cross_judge,
    draft,
    intent,
    judge,
    render,
    rescreen,
    scene_text,
    screen,
    triage,
)
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
    intent_rule="Made-up intent rule. Not game text.",
    judge_rubric="Made-up judge rubric. Not game text.",
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


# Made-up line-stage fixtures. Not game text.
CALLBACK = {"line": {"namespace": LINE_TWO.namespace, "key": LINE_TWO.key}, "depends": [], "chapter": 1}
REASONS = ("made-up-alpha",)
INTENT_TEXT = "Made-up intent. Not game text."
INTENT_ANSWER = {"intent": "Made-up intent. Not game text."}
DRAFT_ANSWER = {"drafts": ["Made-up draft one. Not game text.", "Made-up draft two. Not game text."]}
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


def test_intent_schema_requires_intent_and_forbids_extra_properties() -> None:
    assert INTENT_SCHEMA["type"] == "object"
    assert INTENT_SCHEMA["required"] == ["intent"]
    assert INTENT_SCHEMA["properties"]["intent"]["type"] == "string"
    assert INTENT_SCHEMA["additionalProperties"] is False


def test_intent_asks_opus_with_schema_and_returns_intent(tmp_path: Path) -> None:
    ask = Fake(INTENT_ANSWER)

    result = intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], ask)

    assert result == "Made-up intent. Not game text."
    model, _parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == INTENT_SCHEMA


def test_intent_prompt_carries_rule_callbacks_japanese_lines_and_target_reasons(tmp_path: Path) -> None:
    ask = Fake(INTENT_ANSWER)

    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], ask)

    prompt = "\n".join(ask.calls[0][1])
    assert "Made-up intent rule. Not game text." in prompt
    assert '"chapter": 1' in prompt
    assert '"japanese": "Made-up Japanese one. Not game text."' in prompt
    assert '"reasons": ["made-up-alpha"]' in prompt


def test_fresh_store_on_same_root_returns_stored_intent_without_asking(tmp_path: Path) -> None:
    intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], Fake(INTENT_ANSWER))
    ask = Fake()

    assert intent(Store(tmp_path), LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], ask) == "Made-up intent. Not game text."
    assert ask.calls == []


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_intent_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, INTENT_ANSWER)

    assert intent(store, LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert intent(store, LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], ask) == "Made-up intent. Not game text."
    assert len(ask.calls) == 2
    assert store.unfinished() == []


def test_editing_intent_rule_reruns_intent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(INTENT_ANSWER, INTENT_ANSWER)
    intent(store, LENS, SCENE, LINE_ONE, REASONS, [CALLBACK], ask)
    edited = replace(LENS, intent_rule="Edited intent rule. Not game text.")

    intent(store, edited, SCENE, LINE_ONE, REASONS, [CALLBACK], ask)

    assert len(ask.calls) == 2


def test_draft_schema_requires_nonempty_drafts_and_forbids_extra_properties() -> None:
    assert DRAFT_SCHEMA["type"] == "object"
    assert DRAFT_SCHEMA["required"] == ["drafts"]
    assert DRAFT_SCHEMA["properties"]["drafts"]["type"] == "array"
    assert DRAFT_SCHEMA["properties"]["drafts"]["items"] == {"type": "string"}
    assert DRAFT_SCHEMA["properties"]["drafts"]["minItems"] == 1
    assert DRAFT_SCHEMA["additionalProperties"] is False


def test_draft_asks_opus_with_schema_and_returns_drafts_in_answer_order(tmp_path: Path) -> None:
    ask = Fake(DRAFT_ANSWER)

    result = draft(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, ask)

    assert result == ("Made-up draft one. Not game text.", "Made-up draft two. Not game text.")
    model, parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == DRAFT_SCHEMA
    prompt = "\n".join(parts)
    assert "Made-up intent rule. Not game text." in prompt
    assert "Made-up intent." in prompt
    assert "{...}" in prompt
    assert "<text ...>" in prompt


def test_draft_names_the_retry_gate_only_on_retry(tmp_path: Path) -> None:
    first = Fake(DRAFT_ANSWER)
    draft(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, first)
    retry = Fake(DRAFT_ANSWER)
    draft(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], "classifier", retry)

    assert "classifier" not in "\n".join(first.calls[0][1])
    assert "classifier" in "\n".join(retry.calls[0][1])


def test_draft_retry_is_part_of_its_key(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(DRAFT_ANSWER, DRAFT_ANSWER)
    draft(store, LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, ask)

    draft(store, LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], "classifier", ask)

    assert len(ask.calls) == 2


def test_fresh_store_on_same_root_returns_stored_drafts_without_asking(tmp_path: Path) -> None:
    expected = ("Made-up draft one. Not game text.", "Made-up draft two. Not game text.")
    draft(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, Fake(DRAFT_ANSWER))
    ask = Fake()

    assert draft(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, ask) == expected
    assert ask.calls == []


@pytest.mark.parametrize(
    "failure",
    [Unfinished("Made-up unfinished detail. Not game text."), Rejected("Made-up rejected detail. Not game text.")],
    ids=["unfinished", "rejected"],
)
def test_failed_draft_is_returned_unstored_and_asked_again(tmp_path: Path, failure: Any) -> None:
    store = Store(tmp_path)
    ask = Fake(failure, DRAFT_ANSWER)

    assert draft(store, LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert draft(store, LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, ask) == tuple(DRAFT_ANSWER["drafts"])
    assert len(ask.calls) == 2


def test_editing_intent_rule_reruns_draft(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(DRAFT_ANSWER, DRAFT_ANSWER)
    draft(store, LENS, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, ask)
    edited = replace(LENS, intent_rule="Edited intent rule. Not game text.")

    draft(store, edited, SCENE, LINE_ONE, "Made-up intent.", [CALLBACK], None, ask)

    assert len(ask.calls) == 2


def test_render_stores_the_renderer_text_and_does_not_call_it_twice(tmp_path: Path) -> None:
    renderer = FakeRenderer("Made-up rendered. Not game text.")
    store = Store(tmp_path)

    assert render(store, renderer, "Ash", "Made-up first line. Not game text.") == "Made-up rendered. Not game text."
    assert renderer.fake.calls == [("Ash", "Made-up first line. Not game text.")]

    assert render(Store(tmp_path), renderer, "Ash", "Made-up first line. Not game text.") == "Made-up rendered. Not game text."
    assert len(renderer.fake.calls) == 1


def test_render_exception_propagates_and_stores_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    renderer = FakeRenderer(RuntimeError("Made-up renderer failure. Not game text."))

    with pytest.raises(RuntimeError, match="Made-up renderer failure"):
        render(store, renderer, None, "Made-up narration. Not game text.")

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

    result = judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)

    assert result == Judgment(meaning=True, plot=True, minimal_change=True, callback=True, fixed=True, restores_japanese=False)
    model, _parts, schema = ask.calls[0]
    assert model == claude.OPUS
    assert schema == JUDGE_SCHEMA


def test_judge_prompt_carries_the_japanese_line_candidate_and_rubric(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert "Made-up Japanese one. Not game text." in prompt
    assert "Made-up candidate. Not game text." in prompt
    assert "Made-up judge rubric. Not game text." in prompt
    assert '"japanese": "Made-up Japanese two. Not game text."' in prompt


def test_fresh_store_on_same_root_returns_stored_judgment_without_asking(tmp_path: Path) -> None:
    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, Fake(JUDGE_ANSWER))
    ask = Fake()

    assert (
        judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)
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

    assert judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask) == JUDGMENT
    assert len(ask.calls) == 2


def test_editing_judge_rubric_reruns_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(JUDGE_ANSWER, JUDGE_ANSWER)
    judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)
    edited = replace(LENS, judge_rubric="Edited judge rubric. Not game text.")

    judge(store, edited, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)

    assert len(ask.calls) == 2


def test_cross_judge_schema_requires_consistent_and_forbids_extra_properties() -> None:
    assert CROSS_SCHEMA["type"] == "object"
    assert CROSS_SCHEMA["required"] == ["consistent"]
    assert CROSS_SCHEMA["properties"]["consistent"] == {"type": "boolean"}
    assert CROSS_SCHEMA["additionalProperties"] is False


def test_cross_judge_asks_opus_with_schema_and_returns_consistent(tmp_path: Path) -> None:
    ask = Fake(CROSS_ANSWER)

    result = cross_judge(
        Store(tmp_path), LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask
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
        Fake(CROSS_ANSWER),
    )
    ask = Fake()

    assert (
        cross_judge(
            Store(tmp_path), LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask
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
        cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask)
        == failure
    )
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert (
        cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask) is True
    )
    assert len(ask.calls) == 2


def test_editing_judge_rubric_reruns_cross_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(CROSS_ANSWER, CROSS_ANSWER)
    cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask)
    edited = replace(LENS, judge_rubric="Edited judge rubric. Not game text.")

    cross_judge(store, edited, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask)

    assert len(ask.calls) == 2


# Made-up re-screen fixtures. Not game text.


def test_judge_prompt_carries_the_line_reason_questions_and_intent(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert 'Line reasons:\n[{"id": "made-up-alpha", "question": "Made-up question alpha. Not game text."}]' in prompt
    assert f"Intent:\n{INTENT_TEXT}" in prompt


def test_judge_instructions_say_the_lens_overrides_the_japanese_line(tmp_path: Path) -> None:
    ask = Fake(JUDGE_ANSWER)

    judge(Store(tmp_path), LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)

    instructions = ask.calls[0][1][0]
    assert "The judge rubric overrides the Japanese line wherever they conflict." in instructions


def test_cross_judge_prompt_carries_the_line_reason_questions_and_intent(tmp_path: Path) -> None:
    ask = Fake(CROSS_ANSWER)

    cross_judge(Store(tmp_path), LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert 'Line reasons:\n[{"id": "made-up-alpha", "question": "Made-up question alpha. Not game text."}]' in prompt
    assert f"Intent:\n{INTENT_TEXT}" in prompt


def test_different_intent_reruns_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(JUDGE_ANSWER, JUDGE_ANSWER)
    judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)

    judge(
        store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, "Made-up other intent. Not game text.", ask
    )

    assert len(ask.calls) == 2


def test_editing_triage_question_reruns_judge_and_cross_judge(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(JUDGE_ANSWER, CROSS_ANSWER, JUDGE_ANSWER, CROSS_ANSWER)
    judge(store, LENS, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)
    cross_judge(store, LENS, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask)
    edited = replace(LENS, triage=(TriageQuestion("made-up-alpha", "Edited question. Not game text."), LENS.triage[1]))

    judge(store, edited, SCENE, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], REASONS, INTENT_TEXT, ask)
    cross_judge(store, edited, LINE_ONE, "Made-up candidate. Not game text.", [CALLBACK], (LINE_TWO,), REASONS, INTENT_TEXT, ask)

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

    result = rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, ask)

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
    ]
    assert parts[3].startswith("Scene lines:\n")


def test_rescreen_prompt_carries_the_one_question_and_no_other(tmp_path: Path) -> None:
    ask = Fake({"flagged": []})

    rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, ask)

    prompt = "\n".join(ask.calls[0][1])
    assert "made-up-alpha" in prompt
    assert "made-up-beta" not in prompt
    assert "Made-up question beta. Not game text." not in prompt


def test_rescreen_sends_the_same_scene_lines_as_screen(tmp_path: Path) -> None:
    screen_ask = Fake({"flagged": []})
    rescreen_ask = Fake({"flagged": []})
    screen(Store(tmp_path), LENS, SCENE, screen_ask)

    rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, rescreen_ask)

    assert rescreen_ask.calls[0][1][3] == screen_ask.calls[0][1][3]


def test_rescreen_with_no_flagged_lines_returns_empty_mapping(tmp_path: Path) -> None:
    assert rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, Fake({"flagged": []})) == {}


def test_rescreen_given_the_same_line_twice_returns_it_once(tmp_path: Path) -> None:
    ask = Fake({"flagged": [LINE_ONE_REF, LINE_ONE_REF]})

    result = rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, ask)

    assert result == {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",)}


def test_rescreen_rejects_a_line_outside_the_scene_and_stores_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake({"flagged": [{"namespace": "ST_C09_001_01", "key": "1"}]})

    result = rescreen(store, LENS, SCENE, QUESTION_ALPHA, ask)

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

    assert rescreen(store, LENS, SCENE, QUESTION_ALPHA, ask) == failure
    assert store.finished() == []
    assert len(store.unfinished()) == 1

    assert rescreen(store, LENS, SCENE, QUESTION_ALPHA, ask) == {
        (LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",),
        (LINE_TWO.namespace, LINE_TWO.key): ("made-up-alpha",),
    }
    assert len(ask.calls) == 2
    assert store.unfinished() == []


def test_fresh_store_on_same_root_returns_stored_rescreen_without_asking(tmp_path: Path) -> None:
    expected = {(LINE_ONE.namespace, LINE_ONE.key): ("made-up-alpha",), (LINE_TWO.namespace, LINE_TWO.key): ("made-up-alpha",)}
    rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, Fake(RESCREEN_ANSWER))
    ask = Fake()

    assert rescreen(Store(tmp_path), LENS, SCENE, QUESTION_ALPHA, ask) == expected
    assert ask.calls == []


def test_each_triage_question_is_asked_on_its_own(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ask = Fake(RESCREEN_ANSWER, RESCREEN_ANSWER)
    rescreen(store, LENS, SCENE, QUESTION_ALPHA, ask)

    result = rescreen(store, LENS, SCENE, QUESTION_BETA, ask)

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
    rescreen(store, LENS, SCENE, QUESTION_ALPHA, ask)
    edited = replace(LENS, screen_criteria=("Edited criterion. Not game text.",))

    rescreen(store, edited, SCENE, QUESTION_ALPHA, ask)

    assert len(ask.calls) == 2
