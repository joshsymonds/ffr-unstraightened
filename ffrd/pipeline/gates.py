"""Gate chain for rewrites: a candidate passes placeholder, tag, length, classifier and judge gates in that order with the first failure named, and a line gets one writer retry before it stays vanilla."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ffrd.build import PLACEHOLDER, STYLE_OPEN
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished

GATES: tuple[str, ...] = ("placeholder", "tags", "length", "classifier", "judge", "meaning", "plot", "minimal_change", "callback")


@dataclass(frozen=True)
class Candidate:
    english: str
    japanese: str
    text: str


@dataclass(frozen=True)
class Judgment:
    meaning: bool
    plot: bool
    minimal_change: bool
    callback: bool
    restores_japanese: bool


@dataclass(frozen=True)
class Passed:
    candidate: Candidate
    judgment: Judgment


@dataclass(frozen=True)
class Failed:
    candidate: Candidate
    gate: str


Verdict = Passed | Failed | Unfinished | RateLimited
Classifier = Callable[[str], float]
Judge = Callable[[Candidate], Judgment | Unfinished | RateLimited | Rejected]


def check(candidate: Candidate, *, classify: Classifier, threshold: float, judge: Judge) -> Verdict:
    text, english = candidate.text, candidate.english
    if set(PLACEHOLDER.findall(text)) - set(PLACEHOLDER.findall(english)):
        return Failed(candidate, "placeholder")
    if len(STYLE_OPEN.findall(text)) != text.count("</>"):
        return Failed(candidate, "tags")
    if len(text) * 5 > len(english) * 6 or text.count("\n") != english.count("\n"):
        return Failed(candidate, "length")
    if classify(text) < threshold:
        return Failed(candidate, "classifier")
    verdict = judge(candidate)
    if isinstance(verdict, Rejected):
        return Failed(candidate, "judge")
    if isinstance(verdict, Unfinished | RateLimited):
        return verdict
    for gate, ok in (
        ("meaning", verdict.meaning),
        ("plot", verdict.plot),
        ("minimal_change", verdict.minimal_change),
        ("callback", verdict.callback),
    ):
        if not ok:
            return Failed(candidate, gate)
    return Passed(candidate, verdict)


@dataclass(frozen=True)
class Kept:
    gate: str


Writer = Callable[[str | None], Sequence[Candidate] | Unfinished | RateLimited]


def _gate_round(
    produced: Sequence[Candidate] | Unfinished | RateLimited,
    *,
    classify: Classifier,
    threshold: float,
    judge: Judge,
) -> tuple[Passed, ...] | str | Unfinished | RateLimited:
    """Gates one writer round. A str result is the round's failing gate, or "writer" when it gave no candidates."""
    if isinstance(produced, Unfinished | RateLimited):
        return produced
    passed: list[Passed] = []
    first_failure: str | None = None
    for candidate in produced:
        verdict = check(candidate, classify=classify, threshold=threshold, judge=judge)
        if isinstance(verdict, Unfinished | RateLimited):
            return verdict
        if isinstance(verdict, Passed):
            passed.append(verdict)
        elif first_failure is None:
            first_failure = verdict.gate
    if passed:
        return tuple(passed)
    if first_failure is None:
        return "writer"
    return first_failure


def gate_line(
    write: Writer,
    *,
    classify: Classifier,
    threshold: float,
    judge: Judge,
) -> tuple[Passed, ...] | Kept | Unfinished | RateLimited:
    """Gates a line's first round; on failure asks the writer once more, told the failing gate, and keeps the line vanilla if that also fails."""
    first = _gate_round(write(None), classify=classify, threshold=threshold, judge=judge)
    if not isinstance(first, str):
        return first
    retry = _gate_round(write(first), classify=classify, threshold=threshold, judge=judge)
    if isinstance(retry, str):
        return Kept(retry)
    return retry
