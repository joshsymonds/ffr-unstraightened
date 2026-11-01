"""Single-candidate gate chain: a rewrite passes placeholder, tag, length, classifier and judge gates in that order, and the first failure is named."""

from collections.abc import Callable
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
