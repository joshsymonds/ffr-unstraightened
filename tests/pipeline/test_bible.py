import json
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ffrd.pipeline import claude
from ffrd.pipeline.bible import SCHEMA, SETUP_SCHEMA, SETUP_STAGE, STAGE, Line, build, chapter, setups, stored, touching
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.store import Store, key

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


# Made-up setups of chapters one and two, each reaching a line of the other chapter. Not game text.
SETUP_ONE = {
    "setups": [
        {
            "line": {"namespace": "ST_C01_001_01", "key": "1"},
            "depends": [{"namespace": "ST_C02_001_01", "key": "1"}],
            "meaning": "Made-up setup meaning one. Not game text.",
        }
    ]
}

SETUP_TWO = {
    "setups": [
        {
            "line": {"namespace": "ST_C02_001_01", "key": "1"},
            "depends": [{"namespace": "ST_C01_001_01", "key": "2"}],
            "meaning": "Made-up setup meaning two. Not game text.",
        }
    ]
}

NO_SETUPS = {"setups": []}
SCOPE_ALL = C1_SCOPE | {(line.namespace, line.key) for line in C2_LINES}


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

    rerun = FakeAsk(BIBLE_TWO, NO_SETUPS, NO_SETUPS)
    built = build(Store(tmp_path), chapters, rerun)

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**BIBLE_TWO, "chapter": 2}]
    assert [schema for _, schema in rerun.calls] == [SCHEMA, SETUP_SCHEMA, SETUP_SCHEMA]
    assert "Made-up chapter two line. Not game text." in _prompt(rerun, 0)


def test_build_stores_an_arc_that_continues_from_an_earlier_chapter(tmp_path: Path) -> None:
    store = Store(tmp_path)

    built = build(store, [(1, C1_LINES), (2, C2_LINES)], FakeAsk(BIBLE_ONE, BIBLE_TWO, NO_SETUPS, NO_SETUPS))

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**BIBLE_TWO, "chapter": 2}]
    assert len(store.finished()) == 4


def test_build_accepts_arc_character_who_speaks_in_a_later_passed_chapter(tmp_path: Path) -> None:
    answer = {**BIBLE_ONE, "arcs": [{**MADE_UP_ARC, "characters": ["Cass"], "beats": []}]}
    late = {"characters": [], "facts": [], "callbacks": [], "arcs": []}

    built = build(Store(tmp_path), [(1, C1_LINES), (2, LATE_SPEAKER_LINES)], FakeAsk(answer, late, NO_SETUPS, NO_SETUPS))

    assert built == [{**answer, "chapter": 1}, {**late, "chapter": 2}]


def test_build_drops_empty_speaker_from_arc_characters_and_stores_the_drop(tmp_path: Path) -> None:
    answer = {**BIBLE_ONE, "arcs": [{**MADE_UP_ARC, "characters": ["", "Ash"]}]}
    dropped = {**BIBLE_ONE, "arcs": [{**MADE_UP_ARC, "characters": ["Ash"]}]}

    built = build(Store(tmp_path), [(1, C1_LINES)], FakeAsk(answer, NO_SETUPS))

    assert built == [{**dropped, "chapter": 1}]
    reread = chapter(Store(tmp_path), 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, FakeAsk())
    assert reread == {**dropped, "chapter": 1}


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
            {**BIBLE_ONE, "arcs": [{**MADE_UP_ARC, "characters": ["Nobody"]}]},
            Rejected,
        ),
    ],
    ids=[
        "unfinished",
        "rejected",
        "unknown-speaker",
        "arc-unknown-character",
    ],
)
def test_failed_chapter_is_returned_stores_nothing_and_stays_unfinished(
    tmp_path: Path, answer: Any, failure: type[Unfinished] | type[Rejected]
) -> None:
    store = Store(tmp_path)

    result = chapter(store, 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, FakeAsk(answer))

    assert isinstance(result, failure)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_build_drops_out_of_scope_callback_depends_and_stores_the_drop(tmp_path: Path) -> None:
    in_scope = {"namespace": "ST_C01_001_01", "key": "1"}
    unknown = {"namespace": "ST_C09_001_01", "key": "1"}
    callback = {
        "line": {"namespace": "ST_C01_001_01", "key": "2"},
        "meaning": "Made-up callback meaning with an unknown dependent. Not game text.",
    }
    answer = {**BIBLE_ONE, "callbacks": [{**callback, "depends": [unknown, in_scope]}]}
    dropped = {**BIBLE_ONE, "callbacks": [{**callback, "depends": [in_scope]}]}

    built = build(Store(tmp_path), [(1, C1_LINES)], FakeAsk(answer, NO_SETUPS))

    assert built == [{**dropped, "chapter": 1}]
    reread = chapter(Store(tmp_path), 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, FakeAsk())
    assert reread == {**dropped, "chapter": 1}


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
    ask = FakeAsk(BIBLE_ONE, BIBLE_UNPLACED, NO_SETUPS, NO_SETUPS)

    built = build(Store(tmp_path), [(1, C1_LINES), (None, UNPLACED_LINES)], ask)

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**BIBLE_UNPLACED, "chapter": None}]
    assert len(ask.calls) == 4
    assert "Unplaced lines:\n" in _prompt(ask, 1)
    assert "Made-up unplaced line. Not game text." in _prompt(ask, 1)
    assert json.loads(_part(ask.calls[1][0], "Earlier bibles:\n")) == [{**BIBLE_ONE, "chapter": 1}]


def test_build_accepts_ref_to_a_line_of_a_later_passed_chapter(tmp_path: Path) -> None:
    built = build(Store(tmp_path), [(1, C1_LINES), (2, C2_LINES)], FakeAsk(BIBLE_LATER_REF, BIBLE_TWO, NO_SETUPS, NO_SETUPS))

    assert built == [{**BIBLE_LATER_REF, "chapter": 1}, {**BIBLE_TWO, "chapter": 2}]


def test_build_drops_out_of_scope_fact_callback_and_beat_refs_and_stores_the_drop(tmp_path: Path) -> None:
    # Made-up refs: an in-scope line of chapter one and a line naming no passed line. Not game text.
    in_scope_one = {"namespace": "ST_C01_001_01", "key": "1"}
    in_scope_two = {"namespace": "ST_C01_001_01", "key": "2"}
    unknown = {"namespace": "ST_C09_001_01", "key": "1"}
    beatless_arc = {
        "id": "made-up-beatless-arc",
        "characters": ["Ash"],
        "state": "Made-up arc state with no beat left. Not game text.",
        "beats": [{"line": unknown, "kind": "payoff", "earned": True}],
    }
    answer = {
        "characters": BIBLE_ONE["characters"],
        "facts": [
            {"fact": "Made-up fact beside an out-of-scope line. Not game text.", "lines": [unknown, in_scope_one]},
            {"fact": "Made-up fact whose only line is out of scope. Not game text.", "lines": [unknown]},
        ],
        "callbacks": [
            {"line": in_scope_two, "depends": [], "meaning": "Made-up callback meaning in scope. Not game text."},
            {"line": unknown, "depends": [], "meaning": "Made-up callback meaning out of scope. Not game text."},
        ],
        "arcs": [
            {
                **MADE_UP_ARC,
                "beats": [
                    {"line": unknown, "kind": "escalation", "earned": False},
                    {"line": in_scope_one, "kind": "setup", "earned": False},
                ],
            },
            beatless_arc,
        ],
    }
    dropped = {
        "characters": BIBLE_ONE["characters"],
        "facts": [{"fact": "Made-up fact beside an out-of-scope line. Not game text.", "lines": [in_scope_one]}],
        "callbacks": [{"line": in_scope_two, "depends": [], "meaning": "Made-up callback meaning in scope. Not game text."}],
        "arcs": [
            {**MADE_UP_ARC, "beats": [{"line": in_scope_one, "kind": "setup", "earned": False}]},
            {**beatless_arc, "beats": []},
        ],
    }

    built = build(Store(tmp_path), [(1, C1_LINES)], FakeAsk(answer, NO_SETUPS))

    assert built == [{**dropped, "chapter": 1}]
    rereads = FakeAsk()
    reread = chapter(Store(tmp_path), 1, C1_LINES, [], C1_SCOPE, C1_SPEAKERS, rereads)
    assert reread == {**dropped, "chapter": 1}
    assert rereads.calls == []


def test_setup_schema_requires_line_depends_and_meaning_with_no_extra_properties() -> None:
    assert SETUP_SCHEMA["additionalProperties"] is False
    assert SETUP_SCHEMA["required"] == ["setups"]
    setup = SETUP_SCHEMA["properties"]["setups"]["items"]

    assert set(setup["required"]) == {"line", "depends", "meaning"}
    assert setup["additionalProperties"] is False
    assert setup["properties"]["depends"]["minItems"] == 1
    assert setup["properties"]["meaning"] == {"type": "string"}


def test_build_asks_each_setups_with_only_later_forward_bibles_and_no_setups(tmp_path: Path) -> None:
    ask = ByLine(
        {C1_LINES[0].text: BIBLE_ONE, C2_LINES[0].text: BIBLE_TWO},
        {C1_LINES[0].text: SETUP_ONE, C2_LINES[0].text: SETUP_TWO},
    )

    build(Store(tmp_path), [(1, C1_LINES), (2, C2_LINES)], ask)

    assert [schema for _, schema in ask.calls] == [SCHEMA, SCHEMA, SETUP_SCHEMA, SETUP_SCHEMA]
    chapter_two = _setups_call(ask, C2_LINES[0].text)
    assert "Chapter 2 lines:\n" in chapter_two
    assert "Later bibles:\n[]" in chapter_two
    chapter_one = _setups_call(ask, C1_LINES[0].text)
    assert "Chapter 1 lines:\n" in chapter_one
    assert "Later bibles:\n" in chapter_one
    assert "Made-up callback meaning two. Not game text." in chapter_one
    assert "Made-up setup meaning two. Not game text." not in chapter_one


def test_build_appends_setups_after_forward_callbacks_and_touching_finds_them(tmp_path: Path) -> None:
    ask = ByLine(
        {C1_LINES[0].text: BIBLE_ONE, C2_LINES[0].text: BIBLE_TWO},
        {C1_LINES[0].text: SETUP_ONE, C2_LINES[0].text: SETUP_TWO},
    )
    built = build(Store(tmp_path), [(1, C1_LINES), (2, C2_LINES)], ask)

    assert isinstance(built, list)
    assert built == [
        {**BIBLE_ONE, "chapter": 1, "callbacks": [*BIBLE_ONE["callbacks"], *SETUP_ONE["setups"]]},
        {**BIBLE_TWO, "chapter": 2, "callbacks": [*BIBLE_TWO["callbacks"], *SETUP_TWO["setups"]]},
    ]
    assert touching(built, "ST_C01_001_01", "1") == [{**SETUP_ONE["setups"][0], "chapter": 1}]
    assert {**SETUP_ONE["setups"][0], "chapter": 1} in touching(built, "ST_C02_001_01", "1")


def test_setups_asks_with_setup_schema_and_later_bibles_and_returns_them(tmp_path: Path) -> None:
    ask = FakeAsk(SETUP_ONE)

    result = setups(Store(tmp_path), 1, C1_LINES, [{**BIBLE_TWO, "chapter": 2}], SCOPE_ALL, ask)

    assert result == SETUP_ONE["setups"]
    assert ask.calls[0][1] == SETUP_SCHEMA
    assert "Made-up first line. Not game text." in _prompt(ask, 0)
    assert "Made-up callback meaning two. Not game text." in _prompt(ask, 0)


def test_fresh_store_on_same_root_returns_stored_setups_without_asking(tmp_path: Path) -> None:
    later = [{**BIBLE_TWO, "chapter": 2}]
    setups(Store(tmp_path), 1, C1_LINES, later, SCOPE_ALL, FakeAsk(SETUP_ONE))
    ask = FakeAsk()

    result = setups(Store(tmp_path), 1, C1_LINES, later, SCOPE_ALL, ask)

    assert result == SETUP_ONE["setups"]
    assert ask.calls == []


class ByLine:
    """Answers a prompt by the made-up line it carries, so concurrent calls get their own answers in any order.

    A bible prompt gets the answer keyed by its line in answers. A setup prompt gets the answer keyed by its chapter's
    line in setup_answers, or no setups when none is keyed. A callable answer is called first, which lets a test hold
    a call. Counts the calls in flight at once. Not game text.
    """

    def __init__(self, answers: Mapping[str, Any], setup_answers: Mapping[str, Any] | None = None) -> None:
        self.answers = dict(answers)
        self.setup_answers = dict(setup_answers or {})
        self.calls: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []
        self.in_flight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, parts: Sequence[str], schema: Mapping[str, Any]) -> Any:
        with self._lock:
            self.calls.append((tuple(parts), schema))
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            prompt = "\n".join(parts)
            if schema is SETUP_SCHEMA:
                matches = [line for line in self.setup_answers if line in prompt]
                assert len(matches) <= 1, "setup prompt names more than one known line"
                if not matches:
                    return NO_SETUPS
                answer = self.setup_answers[matches[0]]
            else:
                matches = [line for line in self.answers if line in prompt]
                assert len(matches) == 1, "prompt names no single known line"
                answer = self.answers[matches[0]]
            return answer() if callable(answer) else answer
        finally:
            with self._lock:
                self.in_flight -= 1


def _part(parts: Sequence[str], prefix: str) -> str:
    return next(part for part in parts if part.startswith(prefix)).removeprefix(prefix)


def _bible_call(ask: ByLine, text: str) -> tuple[str, ...]:
    (parts,) = [parts for parts, schema in ask.calls if schema is SCHEMA and any(text in part for part in parts)]
    return parts


def _setups_call(ask: ByLine, text: str) -> str:
    (parts,) = [parts for parts, schema in ask.calls if schema is SETUP_SCHEMA and any(text in part for part in parts)]
    return "\n".join(parts)


def _meet(gate: threading.Barrier, answer: Any) -> Any:
    gate.wait()
    return answer


HOLD = 0.2  # Seconds a held setups call stays in flight, so a call past the concurrency limit would overlap it.


def _hold(answer: Any) -> Any:
    time.sleep(HOLD)
    return answer


def _earlier(parts: Sequence[str]) -> Any:
    return json.loads(_part(parts, "Earlier bibles:\n"))


def _unplaced(name: str) -> tuple[list[Line], dict[str, Any]]:
    # Made-up unplaced chunk, one line and one callback each. Not game text.
    lines = [Line(f"ST_Menu_{name}", "1", "Brin", f"Made-up unplaced line {name}. Not game text.")]
    bible = {
        "characters": [],
        "facts": [],
        "callbacks": [
            {
                "line": {"namespace": f"ST_Menu_{name}", "key": "1"},
                "depends": [{"namespace": "ST_C01_001_01", "key": "1"}],
                "meaning": f"Made-up callback meaning unplaced {name}. Not game text.",
            }
        ],
        "arcs": [],
    }
    return lines, bible


@pytest.fixture
def restore_concurrency() -> Iterator[None]:
    yield
    claude.set_concurrency(claude.DEFAULT_CONCURRENCY)


def test_unplaced_chunks_read_only_numbered_bibles_and_store_under_those_inputs(tmp_path: Path, restore_concurrency: None) -> None:
    claude.set_concurrency(2)
    store = Store(tmp_path)
    (lines_a, bible_a), (lines_b, bible_b) = _unplaced("A"), _unplaced("B")
    ask = ByLine({C1_LINES[0].text: BIBLE_ONE, lines_a[0].text: bible_a, lines_b[0].text: bible_b})
    numbered = [{**BIBLE_ONE, "chapter": 1}]

    built = build(store, [(1, C1_LINES), (None, lines_a), (None, lines_b)], ask)

    assert built == [numbered[0], {**bible_a, "chapter": None}, {**bible_b, "chapter": None}]
    for lines, other in ((lines_a, bible_b), (lines_b, bible_a)):
        parts = _bible_call(ask, lines[0].text)
        assert _earlier(parts) == numbered
        assert other["callbacks"][0]["meaning"] not in "\n".join(parts)
        inputs = {"chapter": None, "lines": [asdict(line) for line in lines], "earlier": numbered}
        assert store.read(key(STAGE, inputs, None)) is not None


def test_unplaced_chunks_overlap_and_never_exceed_the_configured_concurrency(tmp_path: Path, restore_concurrency: None) -> None:
    claude.set_concurrency(2)
    chunks = [_unplaced(name) for name in "ABCDE"]
    gate = threading.Barrier(2, timeout=10)

    def meet(bible: dict[str, Any]) -> dict[str, Any]:
        gate.wait()
        return bible

    answers: dict[str, Any] = {C1_LINES[0].text: BIBLE_ONE}
    for index, (lines, bible) in enumerate(chunks):
        answers[lines[0].text] = partial(meet, bible) if index < 2 else bible
    ask = ByLine(answers)

    built = build(Store(tmp_path), [(1, C1_LINES), *((None, lines) for lines, _ in chunks)], ask)

    assert built == [{**BIBLE_ONE, "chapter": 1}, *({**bible, "chapter": None} for _, bible in chunks)]
    assert ask.peak == 2


def test_output_order_is_chunk_order_whatever_order_chunks_finish(tmp_path: Path, restore_concurrency: None) -> None:
    claude.set_concurrency(2)
    (lines_a, bible_a), (lines_b, bible_b) = _unplaced("A"), _unplaced("B")
    b_asked = threading.Event()

    def late_a() -> dict[str, Any]:
        # Chunk A is held until chunk B has been asked, so B is in flight first. Not game text.
        if not b_asked.wait(timeout=10):
            raise TimeoutError("chunk B was never asked")
        return bible_a

    def early_b() -> dict[str, Any]:
        b_asked.set()
        return bible_b

    ask = ByLine({C1_LINES[0].text: BIBLE_ONE, lines_a[0].text: late_a, lines_b[0].text: early_b})

    built = build(Store(tmp_path), [(1, C1_LINES), (None, lines_a), (None, lines_b)], ask)

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**bible_a, "chapter": None}, {**bible_b, "chapter": None}]


def test_failed_chunk_returns_the_first_failure_in_chunk_order_and_stores_the_others(tmp_path: Path, restore_concurrency: None) -> None:
    claude.set_concurrency(3)
    store = Store(tmp_path)
    (lines_a, _), (lines_b, _), (lines_c, bible_c) = _unplaced("A"), _unplaced("B"), _unplaced("C")
    b_rejected = threading.Event()

    def reject_b() -> Rejected:
        b_rejected.set()
        return Rejected("Made-up rejected detail B. Not game text.")

    def unfinished_a() -> Unfinished:
        # Chunk A fails after chunk B has failed, so the chunk order, not the finish order, picks the failure. Not game text.
        if not b_rejected.wait(timeout=10):
            raise TimeoutError("chunk B was never rejected")
        return Unfinished("Made-up unfinished detail A. Not game text.")

    ask = ByLine({C1_LINES[0].text: BIBLE_ONE, lines_a[0].text: unfinished_a, lines_b[0].text: reject_b, lines_c[0].text: bible_c})

    built = build(store, [(1, C1_LINES), (None, lines_a), (None, lines_b), (None, lines_c)], ask)

    assert built == Unfinished("Made-up unfinished detail A. Not game text.")
    assert len(store.finished()) == 2
    assert len(store.unfinished()) == 2


def test_rerun_after_one_chunk_failed_asks_only_that_chunk(tmp_path: Path, restore_concurrency: None) -> None:
    claude.set_concurrency(3)
    (lines_a, bible_a), (lines_b, bible_b), (lines_c, bible_c) = _unplaced("A"), _unplaced("B"), _unplaced("C")
    chunks = [(1, C1_LINES), (None, lines_a), (None, lines_b), (None, lines_c)]
    first = ByLine(
        {
            C1_LINES[0].text: BIBLE_ONE,
            lines_a[0].text: bible_a,
            lines_b[0].text: Rejected("Made-up rejected detail B. Not game text."),
            lines_c[0].text: bible_c,
        }
    )
    assert build(Store(tmp_path), chunks, first) == Rejected("Made-up rejected detail B. Not game text.")

    rerun = ByLine({lines_b[0].text: bible_b})
    built = build(Store(tmp_path), chunks, rerun)

    assert built == [{**BIBLE_ONE, "chapter": 1}, {**bible_a, "chapter": None}, {**bible_b, "chapter": None}, {**bible_c, "chapter": None}]
    forward = [parts for parts, schema in rerun.calls if schema is SCHEMA]
    assert len(forward) == 1
    assert any(lines_b[0].text in part for part in forward[0])


OUTSIDE_LINE = {
    "setups": [
        {
            "line": {"namespace": "ST_C02_001_01", "key": "1"},
            "depends": [{"namespace": "ST_C01_001_01", "key": "2"}],
            "meaning": "Made-up setup outside its chapter. Not game text.",
        }
    ]
}


@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        (Unfinished("Made-up unfinished detail. Not game text."), Unfinished),
        (Rejected("Made-up rejected detail. Not game text."), Rejected),
        (OUTSIDE_LINE, Rejected),
    ],
    ids=["unfinished", "rejected", "line-outside-chapter"],
)
def test_failed_setups_are_returned_store_nothing_and_stay_unfinished(
    tmp_path: Path, answer: Any, failure: type[Unfinished] | type[Rejected]
) -> None:
    store = Store(tmp_path)

    result = setups(store, 1, C1_LINES, [{**BIBLE_TWO, "chapter": 2}], SCOPE_ALL, FakeAsk(answer))

    assert isinstance(result, failure)
    assert store.finished() == []
    assert len(store.unfinished()) == 1


def test_setups_drop_out_of_scope_depends_and_store_the_drop(tmp_path: Path) -> None:
    # Made-up setup whose depends mix an in-scope line of chapter two with an unknown line. Not game text.
    in_scope = {"namespace": "ST_C02_001_01", "key": "1"}
    unknown = {"namespace": "ST_C09_001_01", "key": "1"}
    answer = {
        "setups": [
            {
                "line": {"namespace": "ST_C01_001_01", "key": "1"},
                "depends": [unknown, in_scope],
                "meaning": "Made-up setup meaning with an unknown dependent. Not game text.",
            }
        ]
    }
    dropped = [{**answer["setups"][0], "depends": [in_scope]}]
    later = [{**BIBLE_TWO, "chapter": 2}]

    assert setups(Store(tmp_path), 1, C1_LINES, later, SCOPE_ALL, FakeAsk(answer)) == dropped

    ask = FakeAsk()
    assert setups(Store(tmp_path), 1, C1_LINES, later, SCOPE_ALL, ask) == dropped
    assert ask.calls == []


def test_setup_whose_depends_are_all_out_of_scope_is_kept_with_empty_depends(tmp_path: Path) -> None:
    # Made-up setup whose only depends name an unknown line. Not game text.
    setup = {
        "line": {"namespace": "ST_C01_001_01", "key": "1"},
        "depends": [{"namespace": "ST_C09_001_01", "key": "1"}],
        "meaning": "Made-up setup meaning with only unknown dependents. Not game text.",
    }

    result = setups(Store(tmp_path), 1, C1_LINES, [{**BIBLE_TWO, "chapter": 2}], SCOPE_ALL, FakeAsk({"setups": [setup]}))

    assert result == [{**setup, "depends": []}]


def test_setups_calls_for_two_chapters_overlap_and_never_exceed_the_configured_concurrency(
    tmp_path: Path, restore_concurrency: None
) -> None:
    claude.set_concurrency(2)
    gate = threading.Barrier(2, timeout=10)
    lines_a, bible_a = _unplaced("A")
    ask = ByLine(
        {C1_LINES[0].text: BIBLE_ONE, C2_LINES[0].text: BIBLE_TWO, lines_a[0].text: bible_a},
        {
            # The unplaced chunk's setups call is asked first and held, so a third call in flight would push the peak past 2.
            lines_a[0].text: partial(_hold, NO_SETUPS),
            C1_LINES[0].text: partial(_meet, gate, SETUP_ONE),
            C2_LINES[0].text: partial(_meet, gate, SETUP_TWO),
        },
    )

    built = build(Store(tmp_path), [(1, C1_LINES), (2, C2_LINES), (None, lines_a)], ask)

    assert built == [
        {**BIBLE_ONE, "chapter": 1, "callbacks": [*BIBLE_ONE["callbacks"], *SETUP_ONE["setups"]]},
        {**BIBLE_TWO, "chapter": 2, "callbacks": [*BIBLE_TWO["callbacks"], *SETUP_TWO["setups"]]},
        {**bible_a, "chapter": None},
    ]
    assert ask.peak == 2


def test_setups_store_under_the_same_inputs_and_a_rerun_asks_no_setups(tmp_path: Path) -> None:
    chapters = [(1, C1_LINES), (2, C2_LINES)]
    ask = ByLine(
        {C1_LINES[0].text: BIBLE_ONE, C2_LINES[0].text: BIBLE_TWO},
        {C1_LINES[0].text: SETUP_ONE, C2_LINES[0].text: SETUP_TWO},
    )
    built = build(Store(tmp_path), chapters, ask)

    store = Store(tmp_path)
    chapter_one = {"chapter": 1, "lines": [asdict(line) for line in C1_LINES], "later": [{**BIBLE_TWO, "chapter": 2}]}
    chapter_two = {"chapter": 2, "lines": [asdict(line) for line in C2_LINES], "later": []}
    assert store.read(key(SETUP_STAGE, chapter_one, None)) is not None
    assert store.read(key(SETUP_STAGE, chapter_two, None)) is not None

    rerun = FakeAsk()
    assert build(Store(tmp_path), chapters, rerun) == built
    assert rerun.calls == []


def test_failed_setups_return_the_highest_chapter_failure_whatever_order_they_finish(tmp_path: Path, restore_concurrency: None) -> None:
    claude.set_concurrency(2)
    one_unfinished = threading.Event()

    def unfinished_one() -> Unfinished:
        # Chapter one fails first, so the chapter order, not the finish order, picks the failure. Not game text.
        one_unfinished.set()
        return Unfinished("Made-up unfinished detail one. Not game text.")

    def reject_two() -> Rejected:
        if not one_unfinished.wait(timeout=10):
            raise TimeoutError("chapter one was never unfinished")
        return Rejected("Made-up rejected detail two. Not game text.")

    ask = ByLine(
        {C1_LINES[0].text: BIBLE_ONE, C2_LINES[0].text: BIBLE_TWO},
        {C1_LINES[0].text: unfinished_one, C2_LINES[0].text: reject_two},
    )

    built = build(Store(tmp_path), [(1, C1_LINES), (2, C2_LINES)], ask)

    assert built == Rejected("Made-up rejected detail two. Not game text.")


STORED_CHAPTERS = [(1, C1_LINES), (2, C2_LINES), (None, UNPLACED_LINES)]
STORED_BIBLE_ONE = {**BIBLE_ONE, "chapter": 1}
STORED_BIBLE_TWO = {**BIBLE_TWO, "chapter": 2}
STORED_BIBLE_UNPLACED = {**BIBLE_UNPLACED, "chapter": None}
# Record keys written by build for STORED_CHAPTERS, spelled out from the inputs build hashes. Not game text.
FORWARD_ONE = key(STAGE, {"chapter": 1, "lines": [asdict(line) for line in C1_LINES], "earlier": []}, None)
FORWARD_TWO = key(STAGE, {"chapter": 2, "lines": [asdict(line) for line in C2_LINES], "earlier": [STORED_BIBLE_ONE]}, None)
FORWARD_UNPLACED = key(
    STAGE,
    {"chapter": None, "lines": [asdict(line) for line in UNPLACED_LINES], "earlier": [STORED_BIBLE_ONE, STORED_BIBLE_TWO]},
    None,
)
SETUPS_ONE = key(
    SETUP_STAGE,
    {"chapter": 1, "lines": [asdict(line) for line in C1_LINES], "later": [STORED_BIBLE_TWO, STORED_BIBLE_UNPLACED]},
    None,
)
SETUPS_TWO = key(SETUP_STAGE, {"chapter": 2, "lines": [asdict(line) for line in C2_LINES], "later": [STORED_BIBLE_UNPLACED]}, None)


def _stored_by_build(root: Path) -> Any:
    ask = ByLine(
        {C1_LINES[0].text: BIBLE_ONE, C2_LINES[0].text: BIBLE_TWO, UNPLACED_LINES[0].text: BIBLE_UNPLACED},
        {C1_LINES[0].text: SETUP_ONE, C2_LINES[0].text: SETUP_TWO},
    )
    return build(Store(root), STORED_CHAPTERS, ask)


def test_stored_returns_what_build_returned_from_its_records_without_running_a_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _stored_by_build(tmp_path)
    assert isinstance(built, list)
    monkeypatch.setattr(Store, "run", lambda *_args, **_kwargs: pytest.fail("stored must not run a stage"))
    store = Store(tmp_path)
    before = (store.finished(), store.unfinished())

    assert stored(store, STORED_CHAPTERS) == built
    assert (store.finished(), store.unfinished()) == before


@pytest.mark.parametrize(
    ("removed", "detail"),
    [
        ([FORWARD_TWO], "chapter 2 bible is not stored"),
        ([FORWARD_UNPLACED], "unplaced bible is not stored"),
        ([SETUPS_ONE], "chapter 1 bible setups are not stored"),
        ([FORWARD_TWO, SETUPS_ONE], "chapter 2 bible is not stored"),
        ([SETUPS_ONE, SETUPS_TWO], "chapter 2 bible setups are not stored"),
    ],
    ids=[
        "forward-chapter-two",
        "forward-unplaced",
        "setups-chapter-one",
        "forward-before-setups",
        "setups-highest-chapter-first",
    ],
)
def test_stored_names_the_first_missing_record_in_build_order_and_writes_nothing(tmp_path: Path, removed: list[str], detail: str) -> None:
    _stored_by_build(tmp_path)
    for record in removed:
        (tmp_path / "results" / f"{record}.json").unlink()
    store = Store(tmp_path)
    before = (store.finished(), store.unfinished())

    assert stored(store, STORED_CHAPTERS) == Unfinished(detail)
    assert (store.finished(), store.unfinished()) == before
