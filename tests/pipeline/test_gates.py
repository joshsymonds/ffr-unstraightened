import pytest

from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import GATES, Candidate, Failed, Judgment, Passed, Verdict, check

# Made-up lines only. Not game text.
ENGLISH = "Made-up line one. Not game text."
JAPANESE = "合成テキスト一。"
TEXT_OK = "Made-up line 1. Not game text."
THRESHOLD = 0.5


def _judgment(**overrides: bool) -> Judgment:
    fields = {"meaning": True, "plot": True, "minimal_change": True, "callback": True, "restores_japanese": True}
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
}


def test_gate_names_are_the_chain_order() -> None:
    assert GATES == ("placeholder", "tags", "length", "classifier", "judge", "meaning", "plot", "minimal_change", "callback")


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
    ],
    ids=["plot-before-minimal-change", "minimal-change-before-callback"],
)
def test_judgment_failing_two_checks_names_the_earlier(judgment: Judgment, gate: str) -> None:
    candidate = _candidate(TEXT_OK)

    verdict, _, _ = _run(candidate, result=judgment)

    assert verdict == Failed(candidate, gate)


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
