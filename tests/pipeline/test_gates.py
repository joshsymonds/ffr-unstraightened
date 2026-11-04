from collections.abc import Sequence

import pytest

from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import (
    GATES,
    Candidate,
    Failed,
    Judgment,
    Kept,
    Passed,
    Verdict,
    check,
    gate_line,
    gate_mechanical,
    mechanical,
)

# Made-up lines only. Not game text.
ENGLISH = "Made-up line one. Not game text."
JAPANESE = "合成テキスト一。"
TEXT_OK = "Made-up line 1. Not game text."
THRESHOLD = 0.5


def _judgment(**overrides: bool) -> Judgment:
    fields = {
        "meaning": True,
        "plot": True,
        "minimal_change": True,
        "callback": True,
        "fixed": True,
        "restores_japanese": True,
        "no_invention": True,
        "point": True,
    }
    fields.update(overrides)
    return Judgment(**fields)


ALL_TRUE = _judgment()


def _candidate(text: str, english: str = ENGLISH) -> Candidate:
    return Candidate(english=english, japanese=JAPANESE, text=text)


class FakeClassify:
    """Returns a fixed score and records every text it was asked to score."""

    def __init__(self, score: float) -> None:
        self.score = score
        self.seen: list[str] = []

    def __call__(self, text: str) -> float:
        self.seen.append(text)
        return self.score


class FakeJudge:
    """Returns a fixed verdict and records every candidate it was given."""

    def __init__(self, result: Judgment | Unfinished | RateLimited | Rejected) -> None:
        self.result = result
        self.seen: list[Candidate] = []

    def __call__(self, candidate: Candidate) -> Judgment | Unfinished | RateLimited | Rejected:
        self.seen.append(candidate)
        return self.result


def _run(
    candidate: Candidate, score: float = 1.0, result: Judgment | Unfinished | RateLimited | Rejected = ALL_TRUE
) -> tuple[Verdict, FakeClassify, FakeJudge]:
    classify = FakeClassify(score)
    judge = FakeJudge(result)
    verdict = check(candidate, classify=classify, threshold=THRESHOLD, judge=judge)
    return verdict, classify, judge


# One candidate per gate name, each failing only at that gate.
FAILING: dict[str, tuple[Candidate, float, Judgment | Rejected]] = {
    "placeholder": (_candidate("Made-up {name} line. Not game text."), 1.0, ALL_TRUE),
    "tags": (_candidate("<text>Made-up line."), 1.0, ALL_TRUE),
    "length": (_candidate("Made-up line one. Not game text. Extra made-up words here."), 1.0, ALL_TRUE),
    "classifier": (_candidate(TEXT_OK), 0.49, ALL_TRUE),
    "judge": (_candidate(TEXT_OK), 1.0, Rejected("synthetic rejection")),
    "meaning": (_candidate(TEXT_OK), 1.0, _judgment(meaning=False)),
    "plot": (_candidate(TEXT_OK), 1.0, _judgment(plot=False)),
    "minimal_change": (_candidate(TEXT_OK), 1.0, _judgment(minimal_change=False)),
    "callback": (_candidate(TEXT_OK), 1.0, _judgment(callback=False)),
    "fixed": (_candidate(TEXT_OK), 1.0, _judgment(fixed=False)),
    "no_invention": (_candidate(TEXT_OK), 1.0, _judgment(no_invention=False)),
    "point": (_candidate(TEXT_OK), 1.0, _judgment(point=False)),
}


def test_gate_names_are_the_chain_order() -> None:
    assert GATES == (
        "placeholder",
        "tags",
        "length",
        "classifier",
        "judge",
        "meaning",
        "plot",
        "minimal_change",
        "callback",
        "fixed",
        "no_invention",
        "point",
    )


def test_clean_candidate_passes_with_its_judgment() -> None:
    verdict, _, _ = _run(_candidate(TEXT_OK))

    assert verdict == Passed(_candidate(TEXT_OK), ALL_TRUE)


def test_every_gate_name_has_a_failing_case() -> None:
    assert set(FAILING) == set(GATES)


@pytest.mark.parametrize("gate", GATES)
def test_failing_gate_is_named_in_the_verdict(gate: str) -> None:
    candidate, score, result = FAILING[gate]

    verdict, _, _ = _run(candidate, score=score, result=result)

    assert verdict == Failed(candidate, gate)


def test_placeholder_dropped_from_the_rewrite_passes() -> None:
    english = "Made-up {name} line. Not game text."
    candidate = _candidate("Made-up line. Not game text.", english=english)

    verdict, _, _ = _run(candidate)

    assert verdict == Passed(candidate, ALL_TRUE)


def test_balanced_tags_pass() -> None:
    candidate = _candidate("<text>Made-up line.</>")

    verdict, _, _ = _run(candidate)

    assert verdict == Passed(candidate, ALL_TRUE)


def test_length_exactly_120_percent_of_english_passes() -> None:
    english = "0123456789"
    candidate = _candidate("0123456789AB", english=english)

    verdict, _, _ = _run(candidate)

    assert verdict == Passed(candidate, ALL_TRUE)


def test_length_one_character_over_120_percent_fails() -> None:
    english = "0123456789"
    candidate = _candidate("0123456789ABC", english=english)

    verdict, _, _ = _run(candidate)

    assert verdict == Failed(candidate, "length")


def test_changed_line_break_count_fails_length() -> None:
    english = "Made-up line one.\nMade-up line two."
    candidate = _candidate("Made-up line one. Made-up line two.", english=english)

    verdict, _, _ = _run(candidate)

    assert verdict == Failed(candidate, "length")


def test_matching_line_break_count_passes() -> None:
    english = "Made-up line one.\nMade-up line two."
    candidate = _candidate("Made-up one.\nMade-up two.", english=english)

    verdict, _, _ = _run(candidate)

    assert verdict == Passed(candidate, ALL_TRUE)


def test_classifier_score_at_threshold_passes() -> None:
    candidate = _candidate(TEXT_OK)

    verdict, _, _ = _run(candidate, score=THRESHOLD)

    assert verdict == Passed(candidate, ALL_TRUE)


def test_meaning_false_is_reported_before_plot_false() -> None:
    candidate = _candidate(TEXT_OK)

    verdict, _, _ = _run(candidate, result=_judgment(meaning=False, plot=False))

    assert verdict == Failed(candidate, "meaning")


def test_plot_false_is_reported_before_callback_false() -> None:
    candidate = _candidate(TEXT_OK)

    verdict, _, _ = _run(candidate, result=_judgment(plot=False, callback=False))

    assert verdict == Failed(candidate, "plot")


@pytest.mark.parametrize(
    ("text", "gate"),
    [
        ("<text>Made-up {name} line.", "placeholder"),
        ("<text>Made-up line one. Not game text. Extra made-up words.", "tags"),
    ],
    ids=["placeholder-before-tags", "tags-before-length"],
)
def test_text_failing_two_validators_names_the_earlier(text: str, gate: str) -> None:
    candidate = _candidate(text)

    verdict, _, _ = _run(candidate)

    assert verdict == Failed(candidate, gate)


@pytest.mark.parametrize(
    ("judgment", "gate"),
    [
        (_judgment(plot=False, minimal_change=False), "plot"),
        (_judgment(minimal_change=False, callback=False), "minimal_change"),
        (_judgment(callback=False, fixed=False), "callback"),
        (_judgment(fixed=False, no_invention=False), "fixed"),
        (_judgment(no_invention=False, point=False), "no_invention"),
    ],
    ids=[
        "plot-before-minimal-change",
        "minimal-change-before-callback",
        "callback-before-fixed",
        "fixed-before-no-invention",
        "no-invention-before-point",
    ],
)
def test_judgment_failing_two_checks_names_the_earlier(judgment: Judgment, gate: str) -> None:
    candidate = _candidate(TEXT_OK)

    verdict, _, _ = _run(candidate, result=judgment)

    assert verdict == Failed(candidate, gate)


def test_judgment_from_the_six_original_keys_has_both_new_verdicts_true() -> None:
    six: dict[str, bool] = {
        "meaning": True,
        "plot": True,
        "minimal_change": True,
        "callback": True,
        "fixed": True,
        "restores_japanese": True,
    }
    judgment = Judgment(**six)

    assert judgment.no_invention is True
    assert judgment.point is True


def test_judgment_from_the_six_original_keys_passes_the_gate() -> None:
    six: dict[str, bool] = {
        "meaning": True,
        "plot": True,
        "minimal_change": True,
        "callback": True,
        "fixed": True,
        "restores_japanese": True,
    }
    candidate = _candidate(TEXT_OK)

    verdict, _, _ = _run(candidate, result=Judgment(**six))

    assert verdict == Passed(candidate, Judgment(**six))


def test_judge_receives_the_whole_candidate_including_the_japanese_line() -> None:
    candidate = _candidate(TEXT_OK)

    _, _, judge = _run(candidate)

    assert judge.seen == [candidate]
    assert judge.seen[0].japanese == JAPANESE


def test_classifier_scores_the_rewrite() -> None:
    candidate = _candidate(TEXT_OK)

    _, classify, _ = _run(candidate)

    assert classify.seen == [TEXT_OK]


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate("Made-up {name} line. Not game text."),
        _candidate("<text>Made-up line."),
        _candidate("Made-up line one. Not game text. Extra made-up words here."),
    ],
    ids=["placeholder", "tags", "length"],
)
def test_validator_or_length_failure_stops_before_classifier_and_judge(candidate: Candidate) -> None:
    verdict, classify, judge = _run(candidate)

    assert isinstance(verdict, Failed)
    assert classify.seen == []
    assert judge.seen == []


def test_classifier_failure_stops_before_judge() -> None:
    candidate = _candidate(TEXT_OK)

    verdict, classify, judge = _run(candidate, score=0.49)

    assert verdict == Failed(candidate, "classifier")
    assert classify.seen == [TEXT_OK]
    assert judge.seen == []


@pytest.mark.parametrize("result", [Unfinished("synthetic transport"), RateLimited("synthetic limit")])
def test_transport_or_rate_limit_comes_back_unchanged(result: Unfinished | RateLimited) -> None:
    verdict, _, _ = _run(_candidate(TEXT_OK), result=result)

    assert verdict is result


class FakeWriter:
    """Returns scripted rounds in call order and records the gate name each call was told."""

    def __init__(self, *rounds: Sequence[Candidate] | Unfinished | RateLimited) -> None:
        self.rounds = list(rounds)
        self.calls: list[str | None] = []

    def __call__(self, gate: str | None) -> Sequence[Candidate] | Unfinished | RateLimited:
        self.calls.append(gate)
        return self.rounds[len(self.calls) - 1]


PASS_A = _candidate("Made-up line 1. Not game text.")
PASS_B = _candidate("Made-up line 2. Not game text.")
FAIL_PLACEHOLDER = FAILING["placeholder"][0]
FAIL_TAGS = FAILING["tags"][0]
FAIL_LENGTH = FAILING["length"][0]


def _gate(
    writer: FakeWriter, result: Judgment | Unfinished | RateLimited | Rejected = ALL_TRUE, score: float = 1.0
) -> tuple[Verdict | Kept | tuple[Passed, ...], FakeClassify, FakeJudge]:
    classify = FakeClassify(score)
    judge = FakeJudge(result)
    verdict = gate_line(writer, classify=classify, threshold=THRESHOLD, judge=judge)
    return verdict, classify, judge


def test_passing_first_round_is_returned_without_a_retry() -> None:
    writer = FakeWriter([PASS_A])

    verdict, _, _ = _gate(writer)

    assert verdict == (Passed(PASS_A, ALL_TRUE),)
    assert writer.calls == [None]


def test_every_passing_candidate_of_a_round_is_returned_in_candidate_order() -> None:
    writer = FakeWriter([PASS_B, FAIL_PLACEHOLDER, PASS_A])

    verdict, _, _ = _gate(writer)

    assert verdict == (Passed(PASS_B, ALL_TRUE), Passed(PASS_A, ALL_TRUE))
    assert writer.calls == [None]


def test_failed_first_round_retries_once_told_the_first_failing_gate() -> None:
    writer = FakeWriter([FAIL_TAGS, FAIL_PLACEHOLDER], [PASS_A])

    verdict, _, _ = _gate(writer)

    assert verdict == (Passed(PASS_A, ALL_TRUE),)
    assert writer.calls == [None, "tags"]


def test_retry_candidates_are_gated_and_the_retry_round_is_returned() -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], [PASS_B, FAIL_LENGTH, PASS_A])

    verdict, _, _ = _gate(writer)

    assert verdict == (Passed(PASS_B, ALL_TRUE), Passed(PASS_A, ALL_TRUE))
    assert writer.calls == [None, "placeholder"]


def test_failed_retry_keeps_the_line_with_the_retry_round_gate() -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], [FAIL_LENGTH])

    verdict, _, _ = _gate(writer)

    assert verdict == Kept("length")
    assert writer.calls == [None, "placeholder"]


def test_writer_is_never_asked_a_third_time() -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], [FAIL_LENGTH], [PASS_A])

    verdict, _, _ = _gate(writer)

    assert verdict == Kept("length")
    assert writer.calls == [None, "placeholder"]


def test_retry_judge_failure_is_the_kept_gate() -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], [PASS_A])

    verdict, _, _ = _gate(writer, result=_judgment(meaning=False))

    assert verdict == Kept("meaning")
    assert writer.calls == [None, "placeholder"]


def test_empty_first_round_retries_told_writer() -> None:
    writer = FakeWriter([], [PASS_A])

    verdict, _, _ = _gate(writer)

    assert verdict == (Passed(PASS_A, ALL_TRUE),)
    assert writer.calls == [None, "writer"]


def test_empty_retry_keeps_the_line_as_writer() -> None:
    writer = FakeWriter([], [])

    verdict, _, _ = _gate(writer)

    assert verdict == Kept("writer")
    assert writer.calls == [None, "writer"]


@pytest.mark.parametrize("stop", [Unfinished("synthetic transport"), RateLimited("synthetic limit")])
def test_writer_stop_in_first_round_is_returned_and_not_retried(stop: Unfinished | RateLimited) -> None:
    writer = FakeWriter(stop)

    verdict, _, _ = _gate(writer)

    assert verdict is stop
    assert writer.calls == [None]


@pytest.mark.parametrize("stop", [Unfinished("synthetic transport"), RateLimited("synthetic limit")])
def test_writer_stop_in_retry_is_returned_and_not_kept(stop: Unfinished | RateLimited) -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], stop)

    verdict, _, _ = _gate(writer)

    assert verdict is stop
    assert writer.calls == [None, "placeholder"]


@pytest.mark.parametrize("stop", [Unfinished("synthetic transport"), RateLimited("synthetic limit")])
def test_judge_stop_in_first_round_is_returned_and_not_retried(stop: Unfinished | RateLimited) -> None:
    writer = FakeWriter([PASS_A])

    verdict, _, _ = _gate(writer, result=stop)

    assert verdict is stop
    assert writer.calls == [None]


@pytest.mark.parametrize("stop", [Unfinished("synthetic transport"), RateLimited("synthetic limit")])
def test_judge_stop_in_retry_is_returned_and_not_kept(stop: Unfinished | RateLimited) -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], [PASS_A])

    verdict, _, _ = _gate(writer, result=stop)

    assert verdict is stop
    assert writer.calls == [None, "placeholder"]


def test_stop_ends_the_round_so_an_earlier_pass_is_not_returned() -> None:
    writer = FakeWriter([PASS_A, PASS_B])

    def judge(candidate: Candidate) -> Judgment | Unfinished:
        return Unfinished("synthetic transport") if candidate == PASS_B else ALL_TRUE

    verdict = gate_line(writer, classify=FakeClassify(1.0), threshold=THRESHOLD, judge=judge)

    assert verdict == Unfinished("synthetic transport")
    assert writer.calls == [None]


MECHANICAL_GATES = ("placeholder", "tags", "length", "classifier")
JUDGE_GATES = ("judge", "meaning", "plot", "minimal_change", "callback", "fixed", "no_invention", "point")


def _mechanical_run(candidate: Candidate, score: float = 1.0) -> tuple[Failed | None, FakeClassify]:
    classify = FakeClassify(score)
    return mechanical(candidate, classify=classify, threshold=THRESHOLD), classify


def test_clean_candidate_passes_mechanical() -> None:
    failure, _ = _mechanical_run(_candidate(TEXT_OK))

    assert failure is None


@pytest.mark.parametrize("gate", MECHANICAL_GATES)
def test_mechanical_names_each_mechanical_gate(gate: str) -> None:
    candidate, score, _ = FAILING[gate]

    failure, _ = _mechanical_run(candidate, score=score)

    assert failure == Failed(candidate, gate)


@pytest.mark.parametrize("gate", JUDGE_GATES)
def test_mechanical_does_not_apply_the_judge_gates(gate: str) -> None:
    candidate, score, _ = FAILING[gate]

    failure, _ = _mechanical_run(candidate, score=score)

    assert failure is None


def test_mechanical_classifier_score_at_threshold_passes() -> None:
    failure, classify = _mechanical_run(_candidate(TEXT_OK), score=THRESHOLD)

    assert failure is None
    assert classify.seen == [TEXT_OK]


def _mechanical_gate(
    writer: FakeWriter, score: float = 1.0
) -> tuple[tuple[Candidate, ...] | Kept | Unfinished | RateLimited, FakeClassify]:
    classify = FakeClassify(score)
    verdict = gate_mechanical(writer, classify=classify, threshold=THRESHOLD)
    return verdict, classify


def test_mechanical_passing_first_round_returns_every_passing_candidate_without_a_retry() -> None:
    writer = FakeWriter([PASS_B, FAIL_PLACEHOLDER, PASS_A])

    verdict, _ = _mechanical_gate(writer)

    assert verdict == (PASS_B, PASS_A)
    assert writer.calls == [None]


def test_mechanical_candidate_failing_only_judge_gates_passes() -> None:
    judged_out = FAILING["meaning"][0]
    writer = FakeWriter([judged_out])

    verdict, _ = _mechanical_gate(writer)

    assert verdict == (judged_out,)
    assert writer.calls == [None]


def test_mechanical_failed_first_round_retries_once_told_the_first_failing_gate() -> None:
    writer = FakeWriter([FAIL_TAGS, FAIL_PLACEHOLDER], [PASS_A])

    verdict, _ = _mechanical_gate(writer)

    assert verdict == (PASS_A,)
    assert writer.calls == [None, "tags"]


def test_mechanical_classifier_failure_is_the_gate_the_retry_is_told() -> None:
    writer = FakeWriter([FAILING["classifier"][0]], [PASS_A])
    scores = iter([0.49, 1.0])

    verdict = gate_mechanical(writer, classify=lambda _text: next(scores), threshold=THRESHOLD)

    assert verdict == (PASS_A,)
    assert writer.calls == [None, "classifier"]


def test_mechanical_empty_first_round_retries_told_writer() -> None:
    writer = FakeWriter([], [PASS_A])

    verdict, _ = _mechanical_gate(writer)

    assert verdict == (PASS_A,)
    assert writer.calls == [None, "writer"]


def test_mechanical_failed_retry_keeps_the_line_with_the_retry_round_gate() -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], [FAIL_LENGTH])

    verdict, _ = _mechanical_gate(writer)

    assert verdict == Kept("length")
    assert writer.calls == [None, "placeholder"]


def test_mechanical_writer_is_never_asked_a_third_time() -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], [FAIL_LENGTH], [PASS_A])

    verdict, _ = _mechanical_gate(writer)

    assert verdict == Kept("length")
    assert writer.calls == [None, "placeholder"]


@pytest.mark.parametrize("stop", [Unfinished("synthetic transport"), RateLimited("synthetic limit")])
def test_mechanical_writer_stop_in_first_round_is_returned_and_not_retried(stop: Unfinished | RateLimited) -> None:
    writer = FakeWriter(stop)

    verdict, _ = _mechanical_gate(writer)

    assert verdict is stop
    assert writer.calls == [None]


@pytest.mark.parametrize("stop", [Unfinished("synthetic transport"), RateLimited("synthetic limit")])
def test_mechanical_writer_stop_in_retry_is_returned_and_not_kept(stop: Unfinished | RateLimited) -> None:
    writer = FakeWriter([FAIL_PLACEHOLDER], stop)

    verdict, _ = _mechanical_gate(writer)

    assert verdict is stop
    assert writer.calls == [None, "placeholder"]
