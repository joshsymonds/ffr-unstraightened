import hashlib
from collections.abc import Mapping
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline import claude, queering

# Made-up speakers and notes. Not game text.
SYNTH_A = "Synth-A"
SYNTH_B = "Synth-B"
SYNTH_C = "Synth-C"


def _bible(chapter: int, characters: list[tuple[str, str]], arcs: list[tuple[str, list[str]]]) -> Mapping[str, Any]:
    return {
        "chapter": chapter,
        "characters": [{"speaker": speaker, "notes": notes, "gender": "unknown"} for speaker, notes in characters],
        "arcs": [{"id": arc_id, "characters": members, "state": "Made-up state. Not game text.", "beats": []} for arc_id, members in arcs],
    }


def _share(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big") % 100


def test_core_cast_admits_speaker_at_both_thresholds() -> None:
    arcs = [(f"arc-{i:02d}", [SYNTH_A]) for i in range(20)]
    bibles = [_bible(1, [], arcs)]

    assert queering.core_cast(bibles, {SYNTH_A: 475}) == frozenset({SYNTH_A})


def test_core_cast_rejects_speaker_just_below_line_threshold() -> None:
    arcs = [(f"arc-{i:02d}", [SYNTH_A]) for i in range(20)]
    bibles = [_bible(1, [], arcs)]

    assert queering.core_cast(bibles, {SYNTH_A: 474}) == frozenset()


def test_core_cast_rejects_speaker_just_below_arc_threshold() -> None:
    arcs = [(f"arc-{i:02d}", [SYNTH_A]) for i in range(19)]
    bibles = [_bible(1, [], arcs)]

    assert queering.core_cast(bibles, {SYNTH_A: 900}) == frozenset()


def test_core_cast_counts_each_arc_id_once_across_chapters() -> None:
    arcs = [(f"arc-{i:02d}", [SYNTH_A]) for i in range(19)]
    bibles = [_bible(1, [], arcs), _bible(2, [], arcs)]

    assert queering.core_cast(bibles, {SYNTH_A: 900}) == frozenset()


def test_antagonists_match_keyword_forms() -> None:
    bibles = [
        _bible(
            1,
            [
                (SYNTH_A, "Made-up villain of the tale."),
                (SYNTH_B, "Made-up villainous schemer."),
                (SYNTH_C, "Made-up antagonistic rival."),
            ],
            [],
        ),
        _bible(2, [("Synth-D", "Made-up nemesis-like foe.")], []),
        _bible(3, [("Synth-E", "Made-up nemesis.")], []),
        _bible(4, [("Synth-F", "Made-up archenemy.")], []),
        _bible(5, [("Synth-G", "Made-up archvillain.")], []),
    ]

    assert queering.antagonists(bibles) == frozenset({SYNTH_A, SYNTH_B, SYNTH_C, "Synth-D", "Synth-E", "Synth-F", "Synth-G"})


def test_antagonists_ignore_unrelated_notes() -> None:
    bibles = [
        _bible(
            1,
            [
                (SYNTH_A, "Made-up friendly guide."),
                (SYNTH_B, "Made-up nemeses plural."),
                (SYNTH_C, "Made-up villagers of the town."),
            ],
            [],
        )
    ]

    assert queering.antagonists(bibles) == frozenset()


def test_antagonists_count_negated_mentions() -> None:
    bibles = [_bible(1, [(SYNTH_A, "Made-up, not a villain in any sense.")], [])]

    assert queering.antagonists(bibles) == frozenset({SYNTH_A})


def test_flirt_picks_speaker_with_most_entries() -> None:
    bibles = [
        _bible(1, [(SYNTH_A, "Made-up flirts with everyone."), (SYNTH_B, "Made-up flirtatious quip.")], []),
        _bible(2, [(SYNTH_A, "Made-up flirtation in chapter two.")], []),
    ]

    assert queering.flirt(bibles) == SYNTH_A


def test_flirt_is_none_without_flirt_notes() -> None:
    bibles = [_bible(1, [(SYNTH_A, "Made-up quiet guard.")], [])]

    assert queering.flirt(bibles) is None


def test_flirt_tie_raises_fail_without_speaker_ids() -> None:
    bibles = [
        _bible(
            1,
            [
                (SYNTH_A, "Made-up flirts once."),
                (SYNTH_B, "Made-up flirts once."),
            ],
            [],
        ),
        _bible(2, [(SYNTH_A, "Made-up flirts again."), (SYNTH_B, "Made-up flirts again.")], []),
        _bible(3, [(SYNTH_A, "Made-up flirts a third time."), (SYNTH_B, "Made-up flirts a third time.")], []),
    ]

    with pytest.raises(Fail) as info:
        queering.flirt(bibles)

    message = str(info.value)
    assert "2" in message
    assert "3" in message
    assert SYNTH_A not in message
    assert SYNTH_B not in message


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def _speaker_lines(count: int) -> list[tuple[str, str, str | None]]:
    return [("Made-up-ns", str(i), f"Synth-{i}") for i in range(count)]


@pytest.mark.parametrize(("count", "expected"), [(1, (1, 0)), (7, (2, 1)), (20, (3, 1)), (21, (4, 2))])
def test_rank_gives_queer_and_trans_their_ceiling_shares(count: int, expected: tuple[int, int]) -> None:
    buckets = list(queering.rank(_speaker_lines(count), frozenset(), frozenset(), None).values())

    assert len(buckets) == count
    assert (buckets.count("queer"), buckets.count("trans")) == expected


def test_rank_orders_units_by_sha256_of_the_speaker_id() -> None:
    speakers = [f"Synth-{i}" for i in range(21)]
    order = sorted(speakers, key=_digest)
    ranked = queering.rank(_speaker_lines(21), frozenset(), frozenset(), None)

    assert {speaker: ranked[("Made-up-ns", str(i))] for i, speaker in enumerate(speakers)} == {
        **{speaker: "queer" for speaker in order[:4]},
        **{speaker: "trans" for speaker in order[4:6]},
        **{speaker: "untouched" for speaker in order[6:]},
    }


def test_rank_makes_each_speakerless_line_its_own_unit_by_namespace_and_key() -> None:
    marked = [("Made-up-ns", str(i), None) for i in range(10)] + [("Other-ns", str(i), "") for i in range(11)]
    units = [f"Made-up-ns/{i}" for i in range(10)] + [f"Other-ns/{i}" for i in range(11)]
    order = sorted(units, key=_digest)
    queer, trans = set(order[:4]), set(order[4:6])
    ranked = queering.rank(marked, frozenset(), frozenset(), None)

    assert ranked == {
        (namespace, key): "queer" if f"{namespace}/{key}" in queer else "trans" if f"{namespace}/{key}" in trans else "untouched"
        for namespace, key, _ in marked
    }


def test_rank_excludes_core_antagonist_and_flirt_lines() -> None:
    ranked_lines = _speaker_lines(21)
    excluded = [("Made-up-ns", "c", SYNTH_A), ("Made-up-ns", "a", SYNTH_B), ("Made-up-ns", "f", SYNTH_C)]
    ranked = queering.rank([*ranked_lines, *excluded], frozenset({SYNTH_A}), frozenset({SYNTH_B}), SYNTH_C)

    assert ranked == queering.rank(ranked_lines, frozenset(), frozenset(), None)
    assert ("Made-up-ns", "c") not in ranked
    assert ("Made-up-ns", "a") not in ranked
    assert ("Made-up-ns", "f") not in ranked


def test_rank_gives_every_marked_line_of_a_speaker_that_speakers_bucket() -> None:
    marked = [*_speaker_lines(21), ("Other-ns", "1", "Synth-0"), ("Other-ns", "2", "Synth-0")]
    ranked = queering.rank(marked, frozenset(), frozenset(), None)

    assert ranked[("Other-ns", "1")] == ranked[("Made-up-ns", "0")] == ranked[("Other-ns", "2")]


def test_rank_is_stable_across_input_order() -> None:
    marked = [*_speaker_lines(21), ("Made-up-ns", "x", None)]

    assert queering.rank(marked, frozenset(), frozenset(), None) == queering.rank(list(reversed(marked)), frozenset(), frozenset(), None)


def test_redirected_matches_hash_over_ref() -> None:
    for key in (str(k) for k in range(200)):
        assert queering.redirected("Made-up-ns", key) == (_share(f"Made-up-ns/{key}") < 50)


def test_redirected_is_near_half_and_stable() -> None:
    total = 20000
    hits = sum(queering.redirected("Made-up-ns", str(i)) for i in range(total))

    assert abs(hits / total - 0.5) < 0.01
    assert queering.redirected("Made-up-ns", "7") == queering.redirected("Made-up-ns", "7")


def test_selection_order_and_rewritten_set() -> None:
    assert queering.SELECTIONS == ("queer", "trans", "untouched", "redirect", "not-redirected")
    assert frozenset({"queer", "trans", "redirect"}) == queering.REWRITTEN


def test_markable_admits_flirt_even_when_core_or_antagonist() -> None:
    assert queering.markable(SYNTH_A, frozenset({SYNTH_A}), frozenset(), SYNTH_A)
    assert queering.markable(SYNTH_A, frozenset(), frozenset({SYNTH_A}), SYNTH_A)


def test_markable_admits_speakerless_and_minor_speakers() -> None:
    assert queering.markable(None, frozenset({SYNTH_A}), frozenset({SYNTH_B}), SYNTH_C)
    assert queering.markable("", frozenset({SYNTH_A}), frozenset({SYNTH_B}), SYNTH_C)
    assert queering.markable(SYNTH_C, frozenset({SYNTH_A}), frozenset({SYNTH_B}), SYNTH_A)
    assert queering.markable(SYNTH_C, frozenset({SYNTH_A}), frozenset({SYNTH_B}), None)


def test_markable_refuses_core_and_antagonist_speakers() -> None:
    assert not queering.markable(SYNTH_A, frozenset({SYNTH_A}), frozenset(), None)
    assert not queering.markable(SYNTH_B, frozenset(), frozenset({SYNTH_B}), None)
    assert not queering.markable(SYNTH_A, frozenset({SYNTH_A}), frozenset(), SYNTH_B)


def test_select_redirects_flirt_lines_both_ways() -> None:
    redirect_key = next(str(k) for k in range(200) if queering.redirected("Made-up-ns", str(k)))
    hold_key = next(str(k) for k in range(200) if not queering.redirected("Made-up-ns", str(k)))

    assert queering.select("Made-up-ns", redirect_key, SYNTH_A, SYNTH_A, {}) == "redirect"
    assert queering.select("Made-up-ns", hold_key, SYNTH_A, SYNTH_A, {}) == "not-redirected"


def test_select_keeps_the_flirt_split_over_a_ranked_bucket() -> None:
    redirect_key = next(str(k) for k in range(200) if queering.redirected("Made-up-ns", str(k)))
    hold_key = next(str(k) for k in range(200) if not queering.redirected("Made-up-ns", str(k)))
    ranked: dict[tuple[str, str], queering.Bucket] = {("Made-up-ns", redirect_key): "queer", ("Made-up-ns", hold_key): "trans"}

    assert queering.select("Made-up-ns", redirect_key, SYNTH_A, SYNTH_A, ranked) == "redirect"
    assert queering.select("Made-up-ns", hold_key, SYNTH_A, SYNTH_A, ranked) == "not-redirected"


def test_select_gives_each_other_line_its_ranked_bucket_or_untouched() -> None:
    ranked: dict[tuple[str, str], queering.Bucket] = {("Made-up-ns", "1"): "queer", ("Made-up-ns", "2"): "trans"}

    assert queering.select("Made-up-ns", "1", SYNTH_B, None, ranked) == "queer"
    assert queering.select("Made-up-ns", "2", None, None, ranked) == "trans"
    assert queering.select("Made-up-ns", "3", SYNTH_B, None, ranked) == "untouched"


def test_haiku_model_name() -> None:
    assert claude.HAIKU == "haiku"
