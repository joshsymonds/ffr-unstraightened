"""Classifier metrics for the voice eval: AUROC of game-likeness scores separating held-out game lines (label 1) from their paraphrases (label 0), overall and for short lines."""

from collections.abc import Sequence
from dataclasses import dataclass

from ffrd.game import Fail

CLASSIFIER_BAR = 0.90
SHORT_WORDS = 8


def word_count(text: str) -> int:
    return len(text.split())


@dataclass(frozen=True)
class Scored:
    label: int
    words: int
    score: float


def auroc(scores: Sequence[float], labels: Sequence[int]) -> float:
    if len(scores) != len(labels):
        raise Fail("scores and labels differ in length")
    if any(label not in (0, 1) for label in labels):
        raise Fail("labels must be 0 or 1")
    n1 = sum(labels)
    n0 = len(labels) - n1
    if n1 == 0 or n0 == 0:
        raise Fail("AUROC needs both a game line and a paraphrase")
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    rank_sum = 0.0
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and scores[order[end + 1]] == scores[order[start]]:
            end += 1
        average_rank = (start + end) / 2 + 1
        rank_sum += average_rank * sum(labels[order[k]] for k in range(start, end + 1))
        start = end + 1
    u = rank_sum - n1 * (n1 + 1) / 2
    return u / (n1 * n0)


def classifier_metrics(scored: Sequence[Scored]) -> dict[str, int | float | bool | None]:
    overall = auroc([item.score for item in scored], [item.label for item in scored])
    short = [item for item in scored if item.words < SHORT_WORDS]
    short_labels = {item.label for item in short}
    auroc_short = auroc([item.score for item in short], [item.label for item in short]) if short_labels == {0, 1} else None
    return {
        "n": len(scored),
        "auroc": overall,
        "n_short": len(short),
        "auroc_short": auroc_short,
        "pass": overall >= CLASSIFIER_BAR,
    }
