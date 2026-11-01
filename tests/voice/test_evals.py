import pytest

from ffrd.game import Fail
from ffrd.voice.evals import CLASSIFIER_BAR, SHORT_WORDS, Scored, auroc, classifier_metrics, word_count


def test_constants_match_the_frozen_interface() -> None:
    assert CLASSIFIER_BAR == 0.90
    assert SHORT_WORDS == 8


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
