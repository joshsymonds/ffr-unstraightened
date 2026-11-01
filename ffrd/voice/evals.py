"""Classifier metrics for the voice eval: AUROC of game-likeness scores separating held-out game lines (label 1) from their paraphrases (label 0), overall and for short lines."""

import hashlib
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction

from ffrd.game import Fail
from ffrd.voice.classifier import Pair

CLASSIFIER_BAR = 0.90
SHORT_WORDS = 8
RENDER_SCORE_BAR = 0.5
MEANING_MARGIN = 0.03
# Exact decimal so the meaning boundary is compared without float rounding.
_MEANING_MARGIN_EXACT = Fraction(str(MEANING_MARGIN))

Renderer = Callable[[str, str], str]
ScoreMany = Callable[[Sequence[str]], list[float]]
HoldsMany = Callable[[Sequence[tuple[str, str]]], list[bool]]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def heldout_sample(pairs: Sequence[Pair]) -> list[Pair]:
    """One pair per held-out line: the paraphrase with the lowest sha256, lines ordered by sha256 of the original."""
    chosen: dict[str, Pair] = {}
    for pair in pairs:
        best = chosen.get(pair.original)
        if best is None or _digest(pair.paraphrase) < _digest(best.paraphrase):
            chosen[pair.original] = pair
    return sorted(chosen.values(), key=lambda pair: _digest(pair.original))


def render_metrics(pairs: Sequence[Pair], render: Renderer, score_many: ScoreMany, holds_many: HoldsMany) -> dict[str, int | float | bool]:
    sample = heldout_sample(pairs)
    if not sample:
        raise Fail("render eval needs at least one held-out pair")
    renders = [render(pair.speaker, pair.paraphrase) for pair in sample]
    render_scores = score_many(renders)
    paraphrase_scores = score_many([pair.paraphrase for pair in sample])
    render_holds = holds_many([(pair.paraphrase, rendered) for pair, rendered in zip(sample, renders, strict=True)])
    pair_holds = holds_many([(pair.paraphrase, pair.original) for pair in sample])
    n = len(sample)
    median_render = statistics.median(render_scores)
    median_paraphrase = statistics.median(paraphrase_scores)
    render_hits = sum(render_holds)
    pair_hits = sum(pair_holds)
    game_likeness_pass = median_render >= RENDER_SCORE_BAR and median_render > median_paraphrase
    meaning_pass = Fraction(render_hits, n) >= Fraction(pair_hits, n) - _MEANING_MARGIN_EXACT
    return {
        "n": n,
        "median_render_score": median_render,
        "median_paraphrase_score": median_paraphrase,
        "render_entailment_rate": render_hits / n,
        "pairs_entailment_rate": pair_hits / n,
        "game_likeness_pass": game_likeness_pass,
        "meaning_pass": meaning_pass,
        "pass": game_likeness_pass and meaning_pass,
    }


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
