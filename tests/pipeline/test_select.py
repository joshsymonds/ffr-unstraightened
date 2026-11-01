import itertools

from ffrd.pipeline.gates import Candidate, Judgment, Passed
from ffrd.pipeline.select import choose, distance, rank

# Made-up lines only. Not game text.
ENGLISH = "abc"


def _judgment(restores: bool) -> Judgment:
    return Judgment(meaning=True, plot=True, minimal_change=True, callback=True, restores_japanese=restores)


def _passed(text: str, restores: bool, english: str = ENGLISH) -> Passed:
    return Passed(candidate=Candidate(english=english, japanese="合成", text=text), judgment=_judgment(restores))


def test_distance_identical_strings_is_zero() -> None:
    assert distance("abc", "abc") == 0.0


def test_distance_both_empty_is_zero() -> None:
    assert distance("", "") == 0.0


def test_distance_one_empty_is_one() -> None:
    assert distance("", "abcd") == 1.0
    assert distance("abcd", "") == 1.0


def test_distance_single_substitution_is_normalized_by_length() -> None:
    assert distance("abc", "abd") == 1 / 3


def test_distance_insertions_are_normalized_by_longer_length() -> None:
    # Two insertions over a longer length of four, not over the shorter two.
    assert distance("ab", "abcd") == 2 / 4


def test_distance_counts_non_ascii_characters_not_bytes() -> None:
    # One substitution between two characters; byte counting would give 1 / 3.
    assert distance("éa", "éb") == 1 / 2
    assert distance("日本語", "日本酒") == 1 / 3


def test_rank_puts_restoring_candidate_before_closer_non_restoring() -> None:
    far_restoring = _passed("xyz", restores=True)
    close_plain = _passed("abd", restores=False)
    assert rank([close_plain, far_restoring]) == [far_restoring, close_plain]


def test_rank_orders_equal_judgments_by_smaller_distance() -> None:
    # "aaa" sorts before "abd" as text but is farther from "abc", so only the distance key can put "abd" first.
    far = _passed("aaa", restores=False)
    near = _passed("abd", restores=False)
    assert distance("aaa", ENGLISH) > distance("abd", ENGLISH)
    assert rank([far, near]) == [near, far]


def test_rank_breaks_full_ties_by_text_regardless_of_input_order() -> None:
    later = _passed("abe", restores=False)
    earlier = _passed("abd", restores=False)
    for ordering in itertools.permutations([later, earlier]):
        assert rank(list(ordering)) == [earlier, later]


def test_rank_returns_empty_list_for_no_candidates() -> None:
    assert rank([]) == []


def test_choose_returns_first_ranked_candidate() -> None:
    far_restoring = _passed("xyz", restores=True)
    close_plain = _passed("abd", restores=False)
    assert choose([close_plain, far_restoring]) == far_restoring


def test_choose_returns_none_for_no_candidates() -> None:
    assert choose([]) is None
