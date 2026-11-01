"""Candidate selection: passing candidates rank by restoring the Japanese meaning, then by smallest normalized edit distance from the English line, then by text; the first ranked candidate ships."""

from collections.abc import Sequence

from ffrd.pipeline.gates import Passed


def distance(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1] / max(len(a), len(b))


def rank(passing: Sequence[Passed]) -> list[Passed]:
    return sorted(
        passing,
        key=lambda p: (not p.judgment.restores_japanese, distance(p.candidate.text, p.candidate.english), p.candidate.text),
    )


def choose(passing: Sequence[Passed]) -> Passed | None:
    ranked = rank(passing)
    return ranked[0] if ranked else None
