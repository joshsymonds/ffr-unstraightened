"""Story bible stage: per chapter, the characters, facts and callbacks the model reads from the script, checked and stored so reruns resume."""

import json
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import asdict, dataclass
from typing import Any

from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.store import Store

STAGE = "bible"

INSTRUCTIONS = (
    "Read this chapter's lines and the earlier chapter bibles. Return JSON with three lists. "
    "characters: one entry per speaker in this chapter, with speaker and notes. "
    "facts: story facts this chapter establishes, each with the lines that show it. "
    "callbacks: lines later scenes rely on, each with its meaning, the lines of this chapter that "
    "depend on it (may be empty), and it may pay off a line from an earlier bible. "
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
                "properties": {"speaker": {"type": "string"}, "notes": {"type": "string"}},
                "required": ["speaker", "notes"],
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
    },
    "required": ["characters", "facts", "callbacks"],
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
    return refs


def _label(number: int | None) -> str:
    return "unplaced" if number is None else f"chapter {number}"


def _check(output: Mapping[str, Any], number: int | None, lines: Sequence[Line], scope: Set[tuple[str, str]]) -> Rejected | None:
    speakers = {line.speaker for line in lines}
    for character in output["characters"]:
        if character["speaker"] not in speakers:
            return Rejected(f"{_label(number)} character speaker {character['speaker']!r} speaks no line of this chapter")
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
    ask: Ask,
) -> dict[str, Any] | Unfinished | Rejected:
    line_json = json.dumps([asdict(line) for line in lines], sort_keys=True, ensure_ascii=False)
    earlier_json = json.dumps(list(earlier), sort_keys=True, ensure_ascii=False)
    heading = "Unplaced lines:" if number is None else f"Chapter {number} lines:"
    parts = [INSTRUCTIONS, f"Earlier bibles:\n{earlier_json}", f"{heading}\n{line_json}"]

    def call() -> dict[str, Any]:
        output = ask(parts, SCHEMA)
        if isinstance(output, Unfinished | Rejected):
            raise _Refused(output)
        problem = _check(output, number, lines, scope)
        if problem is not None:
            raise _Refused(problem)
        return {**output, "chapter": number}

    inputs = {"chapter": number, "lines": [asdict(line) for line in lines], "earlier": list(earlier)}
    try:
        return store.run(STAGE, inputs, None, call).value
    except _Refused as refused:
        return refused.failure


def build(store: Store, chapters: Sequence[tuple[int | None, Sequence[Line]]], ask: Ask) -> list[dict[str, Any]] | Unfinished | Rejected:
    scope = {(line.namespace, line.key) for _, lines in chapters for line in lines}
    bibles: list[dict[str, Any]] = []
    for number, lines in chapters:
        result = chapter(store, number, lines, bibles, scope, ask)
        if not isinstance(result, dict):
            return result
        bibles.append(result)
    return bibles


def touching(bibles: Sequence[Mapping[str, Any]], namespace: str, key: str) -> list[dict[str, Any]]:
    ref = {"namespace": namespace, "key": key}
    found: list[dict[str, Any]] = []
    for bible in bibles:
        for callback in bible["callbacks"]:
            if callback["line"] == ref or ref in callback["depends"]:
                found.append({**callback, "chapter": bible["chapter"]})
    return found
