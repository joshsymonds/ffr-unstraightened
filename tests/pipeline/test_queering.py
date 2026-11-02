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


def test_bucket_matches_hash_over_speaker_id() -> None:
    for speaker in (f"Synth-{i}" for i in range(200)):
        v = _share(speaker)
        expected = "queer" if v < 15 else "trans" if v < 20 else "untouched"

        assert queering.bucket("Made-up-ns", "1", speaker, frozenset(), frozenset()) == expected


def test_bucket_is_per_speaker_across_keys() -> None:
    for speaker in (f"Synth-{i}" for i in range(50)):
        buckets = {queering.bucket(f"Made-up-ns-{n}", str(k), speaker, frozenset(), frozenset()) for n in range(3) for k in range(20)}

        assert len(buckets) == 1


def test_bucket_hashes_ref_when_speakerless() -> None:
    for key in (str(k) for k in range(200)):
        v = _share(f"Made-up-ns/{key}")
        expected = "queer" if v < 15 else "trans" if v < 20 else "untouched"

        assert queering.bucket("Made-up-ns", key, None, frozenset(), frozenset()) == expected
        assert queering.bucket("Made-up-ns", key, "", frozenset(), frozenset()) == expected


def test_bucket_marks_speakerless_lines_by_ref_not_one_bucket() -> None:
    buckets = {queering.bucket("Made-up-ns", str(k), None, frozenset(), frozenset()) for k in range(200)}

    assert buckets == {"queer", "trans", "untouched"}


def test_bucket_excludes_core_and_antagonist_speakers() -> None:
    queer_id = next(
        s for s in (f"Synth-{i}" for i in range(200)) if queering.bucket("Made-up-ns", "1", s, frozenset(), frozenset()) == "queer"
    )
    trans_id = next(
        s for s in (f"Synth-{i}" for i in range(200)) if queering.bucket("Made-up-ns", "1", s, frozenset(), frozenset()) == "trans"
    )

    assert queering.bucket("Made-up-ns", "1", queer_id, frozenset({queer_id}), frozenset()) == "untouched"
    assert queering.bucket("Made-up-ns", "1", queer_id, frozenset(), frozenset({queer_id})) == "untouched"
    assert queering.bucket("Made-up-ns", "1", trans_id, frozenset({trans_id}), frozenset()) == "untouched"
    assert queering.bucket("Made-up-ns", "1", trans_id, frozenset(), frozenset({trans_id})) == "untouched"


def test_bucket_shares_near_targets() -> None:
    counts = {"queer": 0, "trans": 0, "untouched": 0}
    total = 20000
    for i in range(total):
        counts[queering.bucket("Made-up-ns", "1", f"Synth-{i}", frozenset(), frozenset())] += 1

    assert abs(counts["queer"] / total - 0.15) < 0.01
    assert abs(counts["trans"] / total - 0.05) < 0.01
    assert abs(counts["untouched"] / total - 0.80) < 0.01


def test_bucket_is_stable_across_calls() -> None:
    first = [queering.bucket("Made-up-ns", str(k), f"Synth-{k}", frozenset(), frozenset()) for k in range(100)]
    second = [queering.bucket("Made-up-ns", str(k), f"Synth-{k}", frozenset(), frozenset()) for k in range(100)]

    assert first == second


def test_redirected_matches_hash_over_ref() -> None:
    for key in (str(k) for k in range(200)):
        assert queering.redirected("Made-up-ns", key) == (_share(f"Made-up-ns/{key}") < 50)


def test_redirected_is_near_half_and_stable() -> None:
    total = 20000
    hits = sum(queering.redirected("Made-up-ns", str(i)) for i in range(total))

    assert abs(hits / total - 0.5) < 0.01
    assert queering.redirected("Made-up-ns", "7") == queering.redirected("Made-up-ns", "7")


def test_haiku_model_name() -> None:
    assert claude.HAIKU == "haiku"
