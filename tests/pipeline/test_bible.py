from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ffrd.pipeline.bible import SCHEMA, Line, build, chapter, touching
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.store import Store

# Made-up lines and bibles. Not game text.
C1_LINES = [
    Line("ST_C01_001_01", "1", "Ash", "Made-up first line. Not game text."),
    Line("ST_C01_001_01", "2", "Brin", "Made-up second line. Not game text."),
]
C2_LINES = [Line("ST_C02_001_01", "1", "Ash", "Made-up chapter two line. Not game text.")]
LATE_SPEAKER_LINES = [Line("ST_C02_002_01", "1", "Cass", "Made-up chapter two line by a late speaker. Not game text.")]
C1_SCOPE = {(line.namespace, line.key) for line in C1_LINES}
C1_SPEAKERS = {line.speaker for line in C1_LINES}

MADE_UP_ARC = {
    "id": "made-up-arc",
    "characters": ["Ash"],
    "state": "Made-up arc state one. Not game text.",
    "beats": [{"line": {"namespace": "ST_C01_001_01", "key": "1"}, "kind": "setup", "earned": False}],
}

BIBLE_ONE = {
    "characters": [{"speaker": "Ash", "notes": "Made-up note about Ash. Not game text.", "gender": "Made-up gender. Not game text."}],
    "facts": [
        {
            "fact": "Made-up fact one. Not game text.",
            "lines": [{"namespace": "ST_C01_001_01", "key": "1"}],
        }
    ],
    "callbacks": [
        {
            "line": {"namespace": "ST_C01_001_01", "key": "2"},
            "depends": [],
            "meaning": "Made-up callback meaning one. Not game text.",
        }
    ],
    "arcs": [MADE_UP_ARC],
}

BIBLE_TWO = {
    "characters": [{"speaker": "Ash", "notes": "Made-up note about Ash later. Not game text.", "gender": "Made-up gender. Not game text."}],
    "facts": [],
    "callbacks": [
        {
            "line": {"namespace": "ST_C02_001_01", "key": "1"},
            "depends": [{"namespace": "ST_C01_001_01", "key": "2"}],
            "meaning": "Made-up callback meaning two. Not game text.",
        }
    ],
    "arcs": [
        {
            "id": "made-up-arc",
            "characters": ["Ash"],
            "state": "Made-up arc state two. Not game text.",
            "beats": [{"line": {"namespace": "ST_C02_001_01", "key": "1"}, "kind": "payoff", "earned": True}],
        }
    ],
}


class FakeAsk:
    """Stands in for the model call: returns queued answers, raises queued exceptions, records each prompt."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []

    def __call__(self, parts: Sequence[str], schema: Mapping[str, Any]) -> Any:
        self.calls.append((tuple(parts), schema))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _prompt(ask: FakeAsk, index: int) -> str:
    return "\n".join(ask.calls[index][0])


def test_schema_requires_every_output_key_and_no_extra_properties() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == {"characters", "facts", "callbacks", "arcs"}


def test_schema_requires_character_gender() -> None:
    character = SCHEMA["properties"]["characters"]["items"]

    assert set(character["required"]) == {"speaker", "notes", "gender"}
    assert character["properties"]["gender"] == {"type": "string"}
    assert character["additionalProperties"] is False


def test_schema_arc_has_stable_id_speaker_characters_one_sentence_state_and_beats() -> None:
    arc = SCHEMA["properties"]["arcs"]["items"]

    assert set(arc["required"]) == {"id", "characters", "state", "beats"}
    assert arc["additionalProperties"] is False
    assert arc["properties"]["id"] == {"type": "string"}
    assert arc["properties"]["characters"] == {"type": "array", "items": {"type": "string"}, "minItems": 1}
    assert arc["properties"]["state"] == {"type": "string"}


def test_schema_arc_beat_has_line_ref_five_kinds_and_earned() -> None:
    beat = SCHEMA["properties"]["arcs"]["items"]["properties"]["beats"]["items"]

    assert set(beat["required"]) == {"line", "kind", "earned"}
    assert beat["additionalProperties"] is False
    assert set(beat["properties"]["line"]["required"]) == {"namespace", "key"}
    assert beat["properties"]["kind"] == {"type": "string", "enum": ["setup", "escalation", "declaration", "payoff", "other"]}
    assert beat["properties"]["earned"] == {"type": "boolean"}


def test_chapter_asks_with_schema_and_lines_and_returns_checked_bible_with_chapter(tmp_path: Path) -> None:
    ask = FakeAsk(BIBLE_ONE)

    result = chapter(Store(tmp_path), 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, ask)

    assert result == {**BIBLE_ONE, "chapter": 1}
    assert len(ask.calls) == 1
    assert ask.calls[0][1] == SCHEMA
    assert "Made-up first line. Not game text." in _prompt(ask, 0)
    assert "Made-up second line. Not game text." in _prompt(ask, 0)


def test_fresh_store_on_same_root_returns_stored_bible_without_asking(tmp_path: Path) -> None:
    chapter(Store(tmp_path), 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, FakeAsk(BIBLE_ONE))
    ask = FakeAsk()

    result = chapter(Store(tmp_path), 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, ask)

    assert result == {**BIBLE_ONE, "chapter": 1}
    assert ask.calls == []


def test_build_passes_earlier_bible_and_rerun_asks_only_the_failed_chapter(tmp_path: Path) -> None:
    chapters = [(1, C1_LINES), (2, C2_LINES)]
    first = FakeAsk(BIBLE_ONE, Rejected("Made-up rejected detail. Not game text."))

    failed = build(Store(tmp_path), chapters, first)

    assert failed == Rejected("Made-up rejected detail. Not game text.")
    assert len(first.calls) == 2
    assert "Made-up callback meaning one. Not game text." in _prompt(first, 1)

    rerun = FakeAsk(BIBLE_TWO)
    built = build(Store(tmp_path), chapters, rerun)

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**BIBLE_TWO, "chapter": 2}]
    assert len(rerun.calls) == 1
    assert "Made-up chapter two line. Not game text." in _prompt(rerun, 0)


def test_build_stores_an_arc_that_continues_from_an_earlier_chapter(tmp_path: Path) -> None:
    store = Store(tmp_path)

    built = build(store, [(1, C1_LINES), (2, C2_LINES)], FakeAsk(BIBLE_ONE, BIBLE_TWO))

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**BIBLE_TWO, "chapter": 2}]
    assert len(store.finished()) == 2


def test_build_accepts_arc_character_who_speaks_in_a_later_passed_chapter(tmp_path: Path) -> None:
    answer = {**BIBLE_ONE, "arcs": [{**MADE_UP_ARC, "characters": ["Cass"], "beats": []}]}
    late = {"characters": [], "facts": [], "callbacks": [], "arcs": []}

    built = build(Store(tmp_path), [(1, C1_LINES), (2, LATE_SPEAKER_LINES)], FakeAsk(answer, late))

    assert built == [{**answer, "chapter": 1}, {**late, "chapter": 2}]


@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        (Unfinished("Made-up unfinished detail. Not game text."), Unfinished),
        (Rejected("Made-up rejected detail. Not game text."), Rejected),
        (
            {
                "characters": [{"speaker": "Nobody", "notes": "Made-up note. Not game text.", "gender": "Made-up gender. Not game text."}],
                "facts": [],
                "callbacks": [],
                "arcs": [],
            },
            Rejected,
        ),
        (
            {
                "characters": [],
                "facts": [
                    {
                        "fact": "Made-up fact with an unknown ref. Not game text.",
                        "lines": [{"namespace": "ST_C09_001_01", "key": "1"}],
                    }
                ],
                "callbacks": [],
                "arcs": [],
            },
            Rejected,
        ),
        (
            {**BIBLE_ONE, "arcs": [{**MADE_UP_ARC, "characters": ["Nobody"]}]},
            Rejected,
        ),
        (
            {
                **BIBLE_ONE,
                "arcs": [
                    {**MADE_UP_ARC, "beats": [{"line": {"namespace": "ST_C09_001_01", "key": "1"}, "kind": "setup", "earned": False}]}
                ],
            },
            Rejected,
        ),
    ],
    ids=["unfinished", "rejected", "unknown-speaker", "unknown-ref", "arc-unknown-character", "arc-unknown-beat-ref"],
)
def test_failed_chapter_is_returned_stores_nothing_and_stays_unfinished(
    tmp_path: Path, answer: Any, failure: type[Unfinished] | type[Rejected]
) -> None:
    store = Store(tmp_path)

    result = chapter(store, 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, FakeAsk(answer))

    assert isinstance(result, failure)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_ask_exception_propagates_and_leaves_chapter_unfinished(tmp_path: Path) -> None:
    store = Store(tmp_path)

    with pytest.raises(RuntimeError, match="Made-up ask failure"):
        chapter(store, 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, FakeAsk(RuntimeError("Made-up ask failure. Not game text.")))

    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_touching_finds_callback_by_line_and_by_dependent_across_chapters() -> None:
    bibles = [{**BIBLE_ONE, "chapter": 1}, {**BIBLE_TWO, "chapter": 2}]

    by_line_and_dependent = touching(bibles, "ST_C01_001_01", "2")
    by_later_line = touching(bibles, "ST_C02_001_01", "1")
    untouched = touching(bibles, "ST_C09_001_01", "1")

    assert by_line_and_dependent == [
        {**BIBLE_ONE["callbacks"][0], "chapter": 1},
        {**BIBLE_TWO["callbacks"][0], "chapter": 2},
    ]
    assert by_later_line == [{**BIBLE_TWO["callbacks"][0], "chapter": 2}]
    assert untouched == []


# Made-up unplaced line and bibles. Not game text.
UNPLACED_LINES = [Line("ST_Menu_001", "1", "Brin", "Made-up unplaced line. Not game text.")]

BIBLE_UNPLACED = {
    "characters": [{"speaker": "Brin", "notes": "Made-up note about Brin. Not game text.", "gender": "Made-up gender. Not game text."}],
    "facts": [],
    "callbacks": [
        {
            "line": {"namespace": "ST_Menu_001", "key": "1"},
            "depends": [{"namespace": "ST_C01_001_01", "key": "1"}],
            "meaning": "Made-up callback meaning unplaced. Not game text.",
        }
    ],
    "arcs": [],
}

BIBLE_LATER_REF = {
    "characters": [],
    "facts": [
        {
            "fact": "Made-up fact citing a later chapter. Not game text.",
            "lines": [{"namespace": "ST_C02_001_01", "key": "1"}],
        }
    ],
    "callbacks": [],
    "arcs": [],
}


def test_build_sends_unplaced_lines_last_as_chapter_none_reading_earlier_bibles(tmp_path: Path) -> None:
    ask = FakeAsk(BIBLE_ONE, BIBLE_UNPLACED)

    built = build(Store(tmp_path), [(1, C1_LINES), (None, UNPLACED_LINES)], ask)

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**BIBLE_UNPLACED, "chapter": None}]
    assert len(ask.calls) == 2
    assert "Unplaced lines:\n" in _prompt(ask, 1)
    assert "Made-up unplaced line. Not game text." in _prompt(ask, 1)
    assert "Made-up callback meaning one. Not game text." in _prompt(ask, 1)


def test_build_accepts_ref_to_a_line_of_a_later_passed_chapter(tmp_path: Path) -> None:
    built = build(Store(tmp_path), [(1, C1_LINES), (2, C2_LINES)], FakeAsk(BIBLE_LATER_REF, BIBLE_TWO))

    assert built == [{**BIBLE_LATER_REF, "chapter": 1}, {**BIBLE_TWO, "chapter": 2}]


def test_build_rejects_ref_to_no_passed_line_and_stores_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    unknown = {
        **BIBLE_ONE,
        "facts": [{"fact": "Made-up fact with an unknown ref. Not game text.", "lines": [{"namespace": "ST_C09_001_01", "key": "1"}]}],
    }

    failed = build(store, [(1, C1_LINES), (None, UNPLACED_LINES)], FakeAsk(unknown))

    assert isinstance(failed, Rejected)
    assert store.finished() == []
