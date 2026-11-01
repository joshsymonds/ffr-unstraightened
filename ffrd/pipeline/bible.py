"""Story bible stage: per chapter, the characters, facts and callbacks the model reads from the script, checked and stored so reruns resume."""

import json
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import asdict, dataclass
from typing import Any

from ffrd.pipeline.claude import Rejected, Unfinished, fan_out
from ffrd.pipeline.store import Store

STAGE = "bible"

INSTRUCTIONS = (
    "Read this chapter's lines and the earlier chapter bibles. Return JSON with four lists. "
    "characters: one entry per speaker in this chapter, with speaker, notes and gender. "
    "facts: story facts this chapter establishes, each with the lines that show it. "
    "callbacks: lines later scenes rely on, each with its meaning, the lines of this chapter that "
    "depend on it (may be empty), and it may pay off a line from an earlier bible. "
    "arcs: the story arcs this chapter moves forward. Reuse an earlier bible's arc id when the arc continues, "
    "otherwise give a new id. Each arc names its characters by speaker id, a state of one sentence for this "
    "chapter, and beats, each with the line it sits on, its kind (setup, escalation, declaration, payoff or other) "
    "and whether it is earned. "
    "Refer to lines only by namespace and key."
)

REF: dict[str, Any] = {
    "type": "object",
    "properties": {"namespace": {"type": "string"}, "key": {"type": "string"}},
    "required": ["namespace", "key"],
    "additionalProperties": False,
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "notes": {"type": "string"},
                    "gender": {"type": "string"},
                },
                "required": ["speaker", "notes", "gender"],
                "additionalProperties": False,
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "lines": {"type": "array", "items": REF, "minItems": 1},
                },
                "required": ["fact", "lines"],
                "additionalProperties": False,
            },
        },
        "callbacks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": REF,
                    "depends": {"type": "array", "items": REF},
                    "meaning": {"type": "string"},
                },
                "required": ["line", "depends", "meaning"],
                "additionalProperties": False,
            },
        },
        "arcs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "characters": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "state": {"type": "string"},
                    "beats": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line": REF,
                                "kind": {"type": "string", "enum": ["setup", "escalation", "declaration", "payoff", "other"]},
                                "earned": {"type": "boolean"},
                            },
                            "required": ["line", "kind", "earned"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "characters", "state", "beats"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["characters", "facts", "callbacks", "arcs"],
    "additionalProperties": False,
}


SETUP_STAGE = "bible_setups"

SETUP_INSTRUCTIONS = (
    "Read this chapter's lines and the later bibles. Return JSON with one list, setups. "
    "setups: lines of this chapter that lines in the later bibles rely on, each with the line, "
    "the later lines that depend on it (at least one), and its meaning. "
    "Refer to lines only by namespace and key."
)

SETUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "setups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": REF,
                    "depends": {"type": "array", "items": REF, "minItems": 1},
                    "meaning": {"type": "string"},
                },
                "required": ["line", "depends", "meaning"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["setups"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Line:
    namespace: str
    key: str
    speaker: str
    text: str


Ask = Callable[[Sequence[str], Mapping[str, Any]], dict[str, Any] | Unfinished | Rejected]


class _Refused(Exception):
    """Carries a failed model answer out of the store's call so the store marks the chapter unfinished."""

    def __init__(self, failure: Unfinished | Rejected) -> None:
        super().__init__(failure.detail)
        self.failure = failure


def _refs(bible: Mapping[str, Any]) -> list[tuple[str, str]]:
    refs = [(ref["namespace"], ref["key"]) for fact in bible["facts"] for ref in fact["lines"]]
    for callback in bible["callbacks"]:
        refs.append((callback["line"]["namespace"], callback["line"]["key"]))
        refs.extend((ref["namespace"], ref["key"]) for ref in callback["depends"])
    refs.extend((beat["line"]["namespace"], beat["line"]["key"]) for arc in bible["arcs"] for beat in arc["beats"])
    return refs


def _label(number: int | None) -> str:
    return "unplaced" if number is None else f"chapter {number}"


def _heading(number: int | None) -> str:
    return "Unplaced lines:" if number is None else f"Chapter {number} lines:"


def _line_json(lines: Sequence[Line]) -> str:
    return json.dumps([asdict(line) for line in lines], sort_keys=True, ensure_ascii=False)


def _check(
    output: Mapping[str, Any], number: int | None, lines: Sequence[Line], scope: Set[tuple[str, str]], speakers: Set[str]
) -> Rejected | None:
    chapter_speakers = {line.speaker for line in lines}
    for character in output["characters"]:
        if character["speaker"] not in chapter_speakers:
            return Rejected(f"{_label(number)} character speaker {character['speaker']!r} speaks no line of this chapter")
    for arc in output["arcs"]:
        for speaker in arc["characters"]:
            if speaker and speaker not in speakers:
                return Rejected(f"{_label(number)} arc {arc['id']!r} character speaker {speaker!r} speaks no in-scope line")
    for namespace, key in _refs(output):
        if (namespace, key) not in scope:
            return Rejected(f"{_label(number)} ref {namespace}/{key} names no line passed to the story bible")
    return None


def chapter(
    store: Store,
    number: int | None,
    lines: Sequence[Line],
    earlier: Sequence[Mapping[str, Any]],
    scope: Set[tuple[str, str]],
    speakers: Set[str],
    ask: Ask,
) -> dict[str, Any] | Unfinished | Rejected:
    earlier_json = json.dumps(list(earlier), sort_keys=True, ensure_ascii=False)
    parts = [INSTRUCTIONS, f"Earlier bibles:\n{earlier_json}", f"{_heading(number)}\n{_line_json(lines)}"]

    def call() -> dict[str, Any]:
        output = ask(parts, SCHEMA)
        if isinstance(output, Unfinished | Rejected):
            raise _Refused(output)
        problem = _check(output, number, lines, scope, speakers)
        if problem is not None:
            raise _Refused(problem)
        arcs = [{**arc, "characters": [speaker for speaker in arc["characters"] if speaker]} for arc in output["arcs"]]
        return {**output, "arcs": arcs, "chapter": number}

    inputs = {"chapter": number, "lines": [asdict(line) for line in lines], "earlier": list(earlier)}
    try:
        return store.run(STAGE, inputs, None, call).value
    except _Refused as refused:
        return refused.failure


def _check_setups(output: Mapping[str, Any], number: int | None, lines: Sequence[Line], scope: Set[tuple[str, str]]) -> Rejected | None:
    chapter_refs = {(line.namespace, line.key) for line in lines}
    for setup in output["setups"]:
        namespace, key = setup["line"]["namespace"], setup["line"]["key"]
        if (namespace, key) not in chapter_refs:
            return Rejected(f"{_label(number)} setup line {namespace}/{key} is no line of this chapter")
        for ref in setup["depends"]:
            if (ref["namespace"], ref["key"]) not in scope:
                return Rejected(
                    f"{_label(number)} setup depends ref {ref['namespace']}/{ref['key']} names no line passed to the story bible"
                )
    return None


def setups(
    store: Store,
    number: int | None,
    lines: Sequence[Line],
    later: Sequence[Mapping[str, Any]],
    scope: Set[tuple[str, str]],
    ask: Ask,
) -> list[dict[str, Any]] | Unfinished | Rejected:
    later_json = json.dumps(list(later), sort_keys=True, ensure_ascii=False)
    parts = [SETUP_INSTRUCTIONS, f"Later bibles:\n{later_json}", f"{_heading(number)}\n{_line_json(lines)}"]

    def call() -> list[dict[str, Any]]:
        output = ask(parts, SETUP_SCHEMA)
        if isinstance(output, Unfinished | Rejected):
            raise _Refused(output)
        problem = _check_setups(output, number, lines, scope)
        if problem is not None:
            raise _Refused(problem)
        return output["setups"]

    inputs = {"chapter": number, "lines": [asdict(line) for line in lines], "later": list(later)}
    try:
        return store.run(SETUP_STAGE, inputs, None, call).value
    except _Refused as refused:
        return refused.failure


def build(store: Store, chapters: Sequence[tuple[int | None, Sequence[Line]]], ask: Ask) -> list[dict[str, Any]] | Unfinished | Rejected:
    scope = {(line.namespace, line.key) for _, lines in chapters for line in lines}
    speakers = {line.speaker for _, lines in chapters for line in lines if line.speaker}
    placed = [(number, lines) for number, lines in chapters if number is not None]
    loose = [(number, lines) for number, lines in chapters if number is None]
    ordered = [*placed, *loose]
    bibles: list[dict[str, Any]] = []
    for number, lines in placed:
        result = chapter(store, number, lines, bibles, scope, speakers, ask)
        if not isinstance(result, dict):
            return result
        bibles.append(result)
    placed_bibles = list(bibles)

    def unplaced(chunk: tuple[int | None, Sequence[Line]]) -> dict[str, Any] | Unfinished | Rejected:
        # Each unplaced chunk reads only the numbered chapter bibles, never another unplaced chunk's bible.
        return chapter(store, chunk[0], chunk[1], placed_bibles, scope, speakers, ask)

    for result in fan_out(unplaced, loose):
        if not isinstance(result, dict):
            return result
        bibles.append(result)
    found: list[list[dict[str, Any]]] = [[] for _ in bibles]
    for index in reversed(range(len(bibles))):
        number, lines = ordered[index]
        result = setups(store, number, lines, bibles[index + 1 :], scope, ask)
        if not isinstance(result, list):
            return result
        found[index] = result
    return [{**bible, "callbacks": [*bible["callbacks"], *found[index]]} for index, bible in enumerate(bibles)]


def touching(bibles: Sequence[Mapping[str, Any]], namespace: str, key: str) -> list[dict[str, Any]]:
    ref = {"namespace": namespace, "key": key}
    found: list[dict[str, Any]] = []
    for bible in bibles:
        for callback in bible["callbacks"]:
            if callback["line"] == ref or ref in callback["depends"]:
                found.append({**callback, "chapter": bible["chapter"]})
    return found
