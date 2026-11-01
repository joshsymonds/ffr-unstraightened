import copy

from ffrd.pipeline.arcs import cast, consolidate, context

# Made-up bibles and arcs. Not game text.
BIBLE_ONE = {
    "chapter": 1,
    "characters": [
        {"speaker": "Ash", "notes": "Made-up note about Ash in chapter one. Not game text."},
        {"speaker": "Brin", "notes": "Made-up note about Brin in chapter one. Not game text."},
    ],
    "arcs": [
        {
            "id": "arc-a",
            "characters": ["Ash", "Brin"],
            "state": "Made-up state one. Not game text.",
            "beats": [
                {
                    "line": {"namespace": "ST_C01_001_01", "key": "1"},
                    "kind": "setup",
                    "earned": False,
                }
            ],
        },
        {
            "id": "arc-b",
            "characters": ["Brin"],
            "state": "Made-up state for arc b. Not game text.",
            "beats": [],
        },
    ],
}

BIBLE_UNPLACED = {
    "chapter": None,
    "characters": [
        {
            "speaker": "Ash",
            "notes": "Made-up note about Ash, unplaced. Not game text.",
            "gender": "Made-up gender value. Not game text.",
        },
        {"speaker": "Corr", "notes": "Made-up note about Corr, unplaced. Not game text."},
    ],
    "arcs": [
        {
            "id": "arc-a",
            "characters": ["Corr", "Ash"],
            "state": "Made-up later state. Not game text.",
            "beats": [
                {
                    "line": {"namespace": "ST_C00_002_01", "key": "3"},
                    "kind": "payoff",
                    "earned": True,
                }
            ],
        },
    ],
}

BIBLE_TWO = {
    "chapter": 2,
    "characters": [
        {"speaker": "Brin", "notes": "Made-up note about Brin in chapter two. Not game text."},
    ],
    "arcs": [
        {
            "id": "arc-c",
            "characters": ["Brin"],
            "state": "Made-up state for arc c. Not game text.",
            "beats": [
                {
                    "line": {"namespace": "ST_C02_001_01", "key": "1"},
                    "kind": "setup",
                    "earned": False,
                }
            ],
        },
        {
            "id": "arc-a",
            "characters": ["Brin"],
            "state": "Made-up state two. Not game text.",
            "beats": [],
        },
    ],
}

BIBLES = [BIBLE_ONE, BIBLE_UNPLACED, BIBLE_TWO]


def test_consolidate_merges_one_arc_id_across_chapters_and_none() -> None:
    arcs = consolidate(BIBLES)
    arc_a = next(arc for arc in arcs if arc["id"] == "arc-a")

    assert arc_a["states"] == [
        {"chapter": 1, "state": "Made-up state one. Not game text."},
        {"chapter": None, "state": "Made-up later state. Not game text."},
        {"chapter": 2, "state": "Made-up state two. Not game text."},
    ]
    assert arc_a["beats"] == [
        {
            "line": {"namespace": "ST_C01_001_01", "key": "1"},
            "kind": "setup",
            "earned": False,
            "chapter": 1,
        },
        {
            "line": {"namespace": "ST_C00_002_01", "key": "3"},
            "kind": "payoff",
            "earned": True,
            "chapter": None,
        },
    ]
    assert arc_a["characters"] == ["Ash", "Brin", "Corr"]


def test_consolidate_keeps_first_appearance_order_of_ids() -> None:
    arcs = consolidate(BIBLES)

    assert [arc["id"] for arc in arcs] == ["arc-a", "arc-b", "arc-c"]


def test_consolidate_gives_one_record_per_distinct_id() -> None:
    arcs = consolidate(BIBLES)

    assert len(arcs) == 3
    arc_b = next(arc for arc in arcs if arc["id"] == "arc-b")
    assert arc_b == {
        "id": "arc-b",
        "characters": ["Brin"],
        "states": [{"chapter": 1, "state": "Made-up state for arc b. Not game text."}],
        "beats": [],
    }


def test_context_keeps_only_arcs_sharing_a_scene_speaker() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, ["Ash"], "Brin")

    assert [arc["id"] for arc in result["arcs"]] == ["arc-a"]


def test_context_keeps_given_arc_order() -> None:
    arcs = consolidate(BIBLES)

    result = context(list(reversed(arcs)), BIBLES, ["Brin"], "Brin")

    assert [arc["id"] for arc in result["arcs"]] == ["arc-c", "arc-b", "arc-a"]


def test_context_ignores_none_speakers_when_selecting_arcs() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, [None], None)

    assert result["arcs"] == []


def test_context_returns_target_notes_from_each_chapter() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, ["Ash"], "Ash")

    assert result["characters"] == [
        {
            "notes": "Made-up note about Ash in chapter one. Not game text.",
            "chapter": 1,
        },
        {
            "notes": "Made-up note about Ash, unplaced. Not game text.",
            "gender": "Made-up gender value. Not game text.",
            "chapter": None,
        },
    ]


def test_context_returns_no_character_notes_for_none_speaker() -> None:
    arcs = consolidate(BIBLES)

    result = context(arcs, BIBLES, ["Ash", None], None)

    assert result["characters"] == []


def test_cast_returns_every_scene_speaker_entry_with_chapter() -> None:
    result = cast(BIBLES, ["Ash", "Corr", None])

    assert result == [
        {"speaker": "Ash", "notes": "Made-up note about Ash in chapter one. Not game text.", "chapter": 1},
        {
            "speaker": "Ash",
            "notes": "Made-up note about Ash, unplaced. Not game text.",
            "gender": "Made-up gender value. Not game text.",
            "chapter": None,
        },
        {
            "speaker": "Corr",
            "notes": "Made-up note about Corr, unplaced. Not game text.",
            "chapter": None,
        },
    ]


def test_cast_ignores_none_in_speakers() -> None:
    assert cast(BIBLES, [None]) == []


def test_functions_leave_inputs_unchanged() -> None:
    bibles = copy.deepcopy(BIBLES)
    snapshot = copy.deepcopy(BIBLES)

    arcs = consolidate(bibles)
    context(arcs, bibles, ["Ash"], "Brin")
    cast(bibles, ["Ash", "Brin"])

    assert bibles == snapshot
