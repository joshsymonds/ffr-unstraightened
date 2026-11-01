from collections.abc import Sequence

import pytest

from ffrd.game import Fail
from ffrd.voice.classifier import Pair
from ffrd.voice.evals import (
    CLASSIFIER_BAR,
    MEANING_MARGIN,
    RENDER_SCORE_BAR,
    SHORT_WORDS,
    Scored,
    auroc,
    classifier_metrics,
    heldout_sample,
    render_metrics,
    word_count,
)


def test_constants_match_the_frozen_interface() -> None:
    assert CLASSIFIER_BAR == 0.90
    assert SHORT_WORDS == 8
    assert RENDER_SCORE_BAR == 0.5
    assert MEANING_MARGIN == 0.03


def test_word_count_splits_on_whitespace() -> None:
    assert word_count("alpha beta  gamma\tdelta\n") == 4
    assert word_count("") == 0


def test_auroc_perfect_separation_is_one() -> None:
    assert auroc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0


def test_auroc_reversed_separation_is_zero() -> None:
    assert auroc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0


def test_auroc_all_tied_is_one_half() -> None:
    assert auroc([0.5, 0.5, 0.5, 0.5], [1, 1, 0, 0]) == 0.5


def test_auroc_mixed_case_with_ties_matches_hand_computed_value() -> None:
    # Positives 0.9, 0.5 against negatives 0.5, 0.1:
    # (0.9,0.5) win, (0.9,0.1) win, (0.5,0.5) tie = 0.5, (0.5,0.1) win.
    # 3.5 / (2 * 2) = 0.875
    assert auroc([0.9, 0.5, 0.5, 0.1], [1, 1, 0, 0]) == 0.875


def test_auroc_is_order_independent() -> None:
    assert auroc([0.1, 0.9, 0.5, 0.5], [0, 1, 0, 1]) == auroc([0.5, 0.5, 0.9, 0.1], [1, 0, 1, 0])


def test_auroc_raises_on_length_mismatch() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9], [1])


def test_auroc_raises_on_label_other_than_zero_or_one() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9, 0.4], [0, 1, 2])


def test_auroc_raises_when_positive_class_is_empty() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9], [0, 0])


def test_auroc_raises_when_negative_class_is_empty() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9], [1, 1])


def test_auroc_raises_on_empty_input() -> None:
    with pytest.raises(Fail):
        auroc([], [])


def test_classifier_metrics_has_exactly_the_contract_keys() -> None:
    scored = [Scored(label=1, words=10, score=0.9), Scored(label=0, words=10, score=0.1)]
    assert set(classifier_metrics(scored)) == {"n", "auroc", "n_short", "auroc_short", "pass"}


def test_classifier_metrics_reports_overall_figures() -> None:
    scored = [
        Scored(label=1, words=10, score=0.9),
        Scored(label=1, words=10, score=0.8),
        Scored(label=0, words=10, score=0.2),
        Scored(label=0, words=10, score=0.1),
    ]
    metrics = classifier_metrics(scored)
    assert metrics["n"] == 4
    assert metrics["auroc"] == 1.0
    assert metrics["pass"] is True


def test_short_subset_is_split_by_the_game_line_word_count() -> None:
    # 7 words is short; 8 words is not.
    scored = [
        Scored(label=1, words=7, score=0.9),
        Scored(label=0, words=7, score=0.1),
        Scored(label=1, words=8, score=0.1),
        Scored(label=0, words=8, score=0.9),
    ]
    metrics = classifier_metrics(scored)
    assert metrics["n_short"] == 2
    assert metrics["auroc_short"] == 1.0
    assert metrics["auroc"] == 0.5


def test_auroc_short_is_none_when_short_subset_is_one_class() -> None:
    scored = [
        Scored(label=1, words=3, score=0.9),
        Scored(label=1, words=3, score=0.4),
        Scored(label=0, words=20, score=0.1),
        Scored(label=1, words=20, score=0.8),
    ]
    metrics = classifier_metrics(scored)
    assert metrics["n_short"] == 2
    assert metrics["auroc_short"] is None


def test_auroc_short_is_none_when_no_item_is_short() -> None:
    scored = [Scored(label=1, words=12, score=0.9), Scored(label=0, words=12, score=0.1)]
    metrics = classifier_metrics(scored)
    assert metrics["n_short"] == 0
    assert metrics["auroc_short"] is None


def test_pass_is_true_at_exactly_the_bar() -> None:
    # One positive against ten negatives: nine wins out of ten pairs = 0.90.
    scored = [Scored(label=1, words=10, score=5.0)]
    scored += [Scored(label=0, words=10, score=1.0)] * 9
    scored += [Scored(label=0, words=10, score=9.0)]
    metrics = classifier_metrics(scored)
    assert metrics["auroc"] == 0.90
    assert metrics["pass"] is True


def test_pass_is_false_just_below_the_bar() -> None:
    # One positive against 1000 negatives: 899 wins out of 1000 pairs = 0.899.
    scored = [Scored(label=1, words=10, score=5.0)]
    scored += [Scored(label=0, words=10, score=1.0)] * 899
    scored += [Scored(label=0, words=10, score=9.0)] * 101
    metrics = classifier_metrics(scored)
    assert metrics["auroc"] == pytest.approx(0.899)
    assert metrics["pass"] is False


def test_short_figure_is_reported_but_does_not_gate_pass() -> None:
    # The short pair is reversed (short AUROC 0.0). Ten long positives beat every
    # negative and ten long negatives sit below every positive, so the overall
    # figure is 120 wins of 121 pairs: the only loss is the reversed short pair.
    scored = [Scored(label=1, words=3, score=0.1), Scored(label=0, words=3, score=0.9)]
    scored += [Scored(label=1, words=20, score=0.95)] * 10
    scored += [Scored(label=0, words=20, score=0.01)] * 10
    metrics = classifier_metrics(scored)
    assert metrics["auroc_short"] == 0.0
    assert metrics["auroc"] == pytest.approx(120 / 121)
    assert metrics["pass"] is True


def make_pair(original: str, paraphrase: str, speaker: str = "spk-a") -> Pair:
    return Pair(
        scene="scene-x",
        speaker=speaker,
        paraphrase=paraphrase,
        original=original,
        slot="slot-1",
        model="model-m",
        split="heldout",
    )


class Fakes:
    """Table-driven callables for render_metrics that record every call in one log."""

    def __init__(self, scores: dict[str, float] | None = None, holds: dict[tuple[str, str], bool] | None = None) -> None:
        self.scores = scores or {}
        self.holds = holds or {}
        self.log: list[str] = []
        self.render_inputs: list[tuple[str, str]] = []
        self.hold_inputs: list[list[tuple[str, str]]] = []

    def render(self, speaker: str, paraphrase: str) -> str:
        self.log.append("render")
        self.render_inputs.append((speaker, paraphrase))
        return f"R:{paraphrase}"

    def score_many(self, texts: Sequence[str]) -> list[float]:
        self.log.append("score")
        return [self.scores.get(text, 0.0) for text in texts]

    def holds_many(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
        self.log.append("holds")
        self.hold_inputs.append(list(pairs))
        return [self.holds.get(pair, True) for pair in pairs]


def test_heldout_sample_takes_the_lowest_digest_paraphrase_of_each_line() -> None:
    # sha256 order: para-a2 (41...) < para-a1 (b7...) < para-a3 (eb...);
    # para-b2 (59...) < para-b1 (6f...). The winners are not first in input order.
    pairs = [
        make_pair("line-alpha", "para-a1"),
        make_pair("line-alpha", "para-a2"),
        make_pair("line-alpha", "para-a3"),
        make_pair("line-beta", "para-b1"),
        make_pair("line-beta", "para-b2"),
    ]
    sample = heldout_sample(pairs)
    assert [(pair.original, pair.paraphrase) for pair in sample] == [
        ("line-alpha", "para-a2"),
        ("line-beta", "para-b2"),
    ]


def test_heldout_sample_orders_lines_by_sha256_of_the_original() -> None:
    # sha256("line-alpha") = 4e... sorts before sha256("line-beta") = f5..., though
    # line-beta is given first in the input.
    pairs = [make_pair("line-beta", "para-b1"), make_pair("line-alpha", "para-a1")]
    sample = heldout_sample(pairs)
    assert [pair.original for pair in sample] == ["line-alpha", "line-beta"]


def test_heldout_sample_of_nothing_is_empty() -> None:
    assert heldout_sample([]) == []


def test_every_render_happens_before_the_first_score_call() -> None:
    pairs = [make_pair("line-alpha", "para-a1", "spk-1"), make_pair("line-beta", "para-b1", "spk-2")]
    fakes = Fakes()
    render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert fakes.log == ["render", "render", "score", "score", "holds", "holds"]
    assert sorted(fakes.render_inputs) == [("spk-1", "para-a1"), ("spk-2", "para-b1")]


def test_render_metrics_on_a_hand_built_case() -> None:
    # Renders score 0.9, 0.7, 0.6, 0.1 (median 0.65); paraphrases 0.5, 0.4, 0.3, 0.2
    # (median 0.35). Render meaning holds 3 of 4, pairs hold 3 of 4.
    pairs = [make_pair(f"line-{i}", f"para-{i}", f"spk-{i}") for i in range(1, 5)]
    fakes = Fakes(
        scores={
            "R:para-1": 0.9,
            "R:para-2": 0.7,
            "R:para-3": 0.6,
            "R:para-4": 0.1,
            "para-1": 0.5,
            "para-2": 0.4,
            "para-3": 0.3,
            "para-4": 0.2,
        },
        holds={
            ("para-1", "R:para-1"): True,
            ("para-2", "R:para-2"): True,
            ("para-3", "R:para-3"): True,
            ("para-4", "R:para-4"): False,
            ("para-1", "line-1"): True,
            ("para-2", "line-2"): True,
            ("para-3", "line-3"): True,
            ("para-4", "line-4"): False,
        },
    )
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["n"] == 4
    assert metrics["median_render_score"] == pytest.approx(0.65)
    assert metrics["median_paraphrase_score"] == pytest.approx(0.35)
    assert metrics["render_entailment_rate"] == 0.75
    assert metrics["pairs_entailment_rate"] == 0.75
    assert metrics["game_likeness_pass"] is True
    assert metrics["meaning_pass"] is True
    assert metrics["pass"] is True


def test_game_likeness_passes_at_exactly_the_score_bar() -> None:
    fakes = Fakes(scores={"R:para-1": 0.5, "para-1": 0.2})
    metrics = render_metrics([make_pair("line-1", "para-1")], fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["median_render_score"] == 0.5
    assert metrics["game_likeness_pass"] is True


def test_game_likeness_fails_just_below_the_score_bar() -> None:
    fakes = Fakes(scores={"R:para-1": 0.49, "para-1": 0.2})
    metrics = render_metrics([make_pair("line-1", "para-1")], fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["game_likeness_pass"] is False


def test_game_likeness_fails_when_render_median_equals_paraphrase_median() -> None:
    fakes = Fakes(scores={"R:para-1": 0.8, "para-1": 0.8})
    metrics = render_metrics([make_pair("line-1", "para-1")], fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["game_likeness_pass"] is False
    assert metrics["pass"] is False


def meaning_fakes(render_hits: int, pair_hits: int) -> tuple[list[Pair], Fakes]:
    """100 held-out lines; the first render_hits renders and first pair_hits pairs hold."""
    pairs = [make_pair(f"line-{i:03d}", f"para-{i:03d}") for i in range(100)]
    holds: dict[tuple[str, str], bool] = {}
    for i in range(100):
        holds[(f"para-{i:03d}", f"R:para-{i:03d}")] = i < render_hits
        holds[(f"para-{i:03d}", f"line-{i:03d}")] = i < pair_hits
    scores = {f"R:para-{i:03d}": 0.9 for i in range(100)} | {f"para-{i:03d}": 0.1 for i in range(100)}
    return pairs, Fakes(scores=scores, holds=holds)


def test_meaning_passes_at_exactly_the_pairs_rate_minus_the_margin() -> None:
    # Pairs hold 90 of 100; renders hold 87 of 100, which is exactly 0.90 - 0.03.
    pairs, fakes = meaning_fakes(render_hits=87, pair_hits=90)
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["render_entailment_rate"] == 0.87
    assert metrics["pairs_entailment_rate"] == 0.90
    assert metrics["meaning_pass"] is True
    assert metrics["pass"] is True


def test_meaning_fails_just_below_the_pairs_rate_minus_the_margin() -> None:
    pairs, fakes = meaning_fakes(render_hits=86, pair_hits=90)
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["meaning_pass"] is False
    assert metrics["game_likeness_pass"] is True
    assert metrics["pass"] is False


def test_render_metrics_raises_fail_on_empty_input_before_any_call() -> None:
    fakes = Fakes()
    with pytest.raises(Fail):
        render_metrics([], fakes.render, fakes.score_many, fakes.holds_many)
    assert fakes.log == []


def test_render_metrics_result_has_exactly_the_contract_keys_and_no_text() -> None:
    pairs = [make_pair("line-secret-o", "para-secret-p", "spk-secret-s")]
    fakes = Fakes(scores={"R:para-secret-p": 0.9, "para-secret-p": 0.1})
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert set(metrics) == {
        "n",
        "median_render_score",
        "median_paraphrase_score",
        "render_entailment_rate",
        "pairs_entailment_rate",
        "game_likeness_pass",
        "meaning_pass",
        "pass",
    }
    assert all(isinstance(value, int | float | bool) for value in metrics.values())
    assert "secret" not in repr(metrics)
