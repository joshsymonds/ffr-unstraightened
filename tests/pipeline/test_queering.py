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


def test_redirected_matches_hash_over_ref() -> None:
    for key in (str(k) for k in range(200)):
        assert queering.redirected("Made-up-ns", key) == (_share(f"Made-up-ns/{key}") < 50)


def test_redirected_is_near_half_and_stable() -> None:
    total = 20000
    hits = sum(queering.redirected("Made-up-ns", str(i)) for i in range(total))

    assert abs(hits / total - 0.5) < 0.01
    assert queering.redirected("Made-up-ns", "7") == queering.redirected("Made-up-ns", "7")


def test_selection_order_and_rewritten_set() -> None:
    assert queering.SELECTIONS == ("queer", "world", "nonbinary", "trans", "untouched", "redirect", "not-redirected")
    assert frozenset({"queer", "redirect"}) == queering.REWRITTEN


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


def test_selectable_admits_speakerless_and_minor_speakers() -> None:
    assert queering.selectable(None, frozenset({SYNTH_A}), frozenset({SYNTH_B}), SYNTH_C)
    assert queering.selectable("", frozenset({SYNTH_A}), frozenset({SYNTH_B}), SYNTH_C)
    assert queering.selectable(SYNTH_C, frozenset({SYNTH_A}), frozenset({SYNTH_B}), None)


def test_selectable_refuses_core_antagonist_and_flirt_speakers() -> None:
    assert not queering.selectable(SYNTH_A, frozenset({SYNTH_A}), frozenset(), None)
    assert not queering.selectable(SYNTH_B, frozenset(), frozenset({SYNTH_B}), None)
    assert not queering.selectable(SYNTH_C, frozenset(), frozenset(), SYNTH_C)


def test_unit_is_speaker_or_namespace_and_key() -> None:
    assert queering.unit("Made-up-ns", "7", SYNTH_A) == SYNTH_A
    assert queering.unit("Made-up-ns", "7", None) == "Made-up-ns/7"
    assert queering.unit("Made-up-ns", "7", "") == "Made-up-ns/7"


@pytest.mark.parametrize(
    ("share", "size", "expected"),
    [(15, 23, 3), (50, 3, 2), (50, 1, 1), (5, 216, 11), (0, 37, 0), (100, 37, 37), (0, 0, 0), (100, 0, 0)],
)
def test_target_is_the_share_of_the_size_rounded_half_up(share: int, size: int, expected: int) -> None:
    assert queering.target(share, size) == expected


SYNTH_UNITS = [f"Synth-{i}" for i in range(21)] + ["Made-up-ns/3", "Made-up-ns/4"]


def _salted_order(units: list[str], salt: str) -> list[str]:
    return sorted(units, key=lambda unit: (hashlib.sha256(f"{salt}/{unit}".encode()).digest(), unit))


def test_order_sorts_units_by_salted_digest() -> None:
    assert queering.order(SYNTH_UNITS, queering.QUEER_SALT) == _salted_order(SYNTH_UNITS, queering.QUEER_SALT)


def test_order_ignores_input_order_and_duplicates() -> None:
    expected = queering.order(SYNTH_UNITS, queering.QUEER_SALT)

    assert queering.order(list(reversed(SYNTH_UNITS)), queering.QUEER_SALT) == expected
    assert queering.order([*SYNTH_UNITS, *SYNTH_UNITS], queering.QUEER_SALT) == expected
    assert len(expected) == len(set(SYNTH_UNITS))


def test_queer_and_trans_salts_are_distinct_and_order_differently() -> None:
    assert queering.QUEER_SALT != queering.TRANS_SALT
    assert queering.order(SYNTH_UNITS, queering.QUEER_SALT) != queering.order(SYNTH_UNITS, queering.TRANS_SALT)


def test_every_side_salt_is_distinct_and_orders_the_same_units_differently() -> None:
    salts = [queering.QUEER_SALT, queering.WORLD_SALT, queering.NONBINARY_SALT, queering.TRANS_SALT]
    assert len(set(salts)) == len(salts)
    orders = [queering.order(SYNTH_UNITS, salt) for salt in salts]
    assert all(orders[i] != orders[j] for i in range(len(orders)) for j in range(i + 1, len(orders)))


def test_reason_ids_name_the_two_screen_questions() -> None:
    assert queering.ROMANCE == "romance"
    assert queering.GENDER_ROLES == "gender-roles"


def test_haiku_model_name() -> None:
    assert claude.HAIKU == "haiku"
