"""Arc context: story-bible arcs merged across chapters, the character notes a scene's stages receive, and the arc judge: one Opus pass reads an arc's changed lines in story order against the judge rubric and names the lines it would revert; a recheck sees the same lines plus those proposed reverts. A line reverts only when both passes name it."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens
from ffrd.pipeline.stages import Ask, Scene, SceneLine, _json, _Unanswered
from ffrd.pipeline.store import Store

STAGE = "arc_judge"

INSTRUCTIONS = (
    "Judge the arc's lines in story order, each vanilla English beside its rewrite where one is given, against the judge "
    "rubric. Return JSON with holds, true only if the arc holds together with the rewrites applied, and revert, the "
    "rewritten lines that must revert for it to hold together. Refer to lines only by namespace and key."
)

RECHECK_INSTRUCTIONS = (
    "A first pass proposed these reverts. Return JSON with holds, true only if the arc holds together with the rewrites "
    "applied, and revert, only the proposed lines that must revert. Refer to lines only by namespace and key."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "holds": {"type": "boolean"},
        "revert": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"namespace": {"type": "string"}, "key": {"type": "string"}},
                "required": ["namespace", "key"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["holds", "revert"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Verdicts:
    reverted: frozenset[tuple[str, str]]
    unfinished: frozenset[tuple[str, str]]


def consolidate(bibles: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One record per arc id, in order of first appearance across the bibles."""
    records: dict[str, dict[str, Any]] = {}
    for bible in bibles:
        chapter = bible["chapter"]
        for arc in bible["arcs"]:
            record = records.setdefault(arc["id"], {"id": arc["id"], "characters": [], "states": [], "beats": []})
            for speaker in arc["characters"]:
                if speaker not in record["characters"]:
                    record["characters"].append(speaker)
            record["states"].append({"chapter": chapter, "state": arc["state"]})
            for beat in arc["beats"]:
                record["beats"].append(
                    {
                        "line": dict(beat["line"]),
                        "kind": beat["kind"],
                        "earned": beat["earned"],
                        "chapter": chapter,
                    }
                )
    return list(records.values())


def context(
    arcs: Sequence[Mapping[str, Any]],
    bibles: Sequence[Mapping[str, Any]],
    speakers: Collection[str | None],
    speaker: str | None,
) -> dict[str, Any]:
    """Arcs involving any scene speaker, and the target speaker's character notes from every bible."""
    scene = {name for name in speakers if name is not None}
    return {
        "arcs": [arc for arc in arcs if any(name in scene for name in arc["characters"])],
        "characters": _notes(bibles, speaker) if speaker is not None else [],
    }


def cast(bibles: Sequence[Mapping[str, Any]], speakers: Collection[str | None]) -> list[dict[str, Any]]:
    """Every character entry, in bible order, whose speaker is in the scene."""
    return [
        {**entry, "chapter": bible["chapter"]}
        for bible in bibles
        for entry in bible["characters"]
        if entry["speaker"] is not None and entry["speaker"] in speakers
    ]


def _notes(bibles: Sequence[Mapping[str, Any]], speaker: str) -> list[dict[str, Any]]:
    return [
        {**{key: value for key, value in entry.items() if key != "speaker"}, "chapter": bible["chapter"]}
        for bible in bibles
        for entry in bible["characters"]
        if entry["speaker"] == speaker
    ]


def lines(arc: Mapping[str, Any], scenes: Sequence[Scene], rewrites: Mapping[tuple[str, str], str]) -> list[SceneLine]:
    """The arc's judged lines: the first occurrence of each beat line, and each rewritten line its characters speak, in story order."""
    beat_refs = {(beat["line"]["namespace"], beat["line"]["key"]) for beat in arc["beats"]}
    characters = set(arc["characters"])
    first: dict[tuple[str, str], SceneLine] = {}
    for scene in scenes:
        for line in scene.lines:
            first.setdefault(_ref(line), line)
    chosen = [line for ref, line in first.items() if ref in beat_refs or (ref in rewrites and line.speaker in characters)]
    return sorted(chosen, key=_story_position)


def with_changes(arcs: Sequence[Mapping[str, Any]], scenes: Sequence[Scene], rewrites: Mapping[tuple[str, str], str]) -> int:
    """How many arcs have a rewritten key among their judged lines."""
    return sum(1 for arc in arcs if any(_ref(line) in rewrites for line in lines(arc, scenes, rewrites)))


def judge(
    store: Store,
    lens: Lens,
    arcs: Sequence[Mapping[str, Any]],
    scenes: Sequence[Scene],
    rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
) -> Verdicts:
    reverted: set[tuple[str, str]] = set()
    unfinished: set[tuple[str, str]] = set()
    standing = dict(rewrites)
    for arc in arcs:
        arc_lines = lines(arc, scenes, standing)
        changed = frozenset(_ref(line) for line in arc_lines if _ref(line) in standing)
        if not changed:
            continue
        outcome = _arc(store, lens, arc, arc_lines, standing, ask)
        if isinstance(outcome, Unfinished | Rejected):
            unfinished |= changed
            dropped = changed
        else:
            reverted |= outcome
            dropped = outcome
        for ref in dropped:
            del standing[ref]
    return Verdicts(frozenset(reverted), frozenset(unfinished))


def _arc(
    store: Store,
    lens: Lens,
    arc: Mapping[str, Any],
    arc_lines: Sequence[SceneLine],
    rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
) -> frozenset[tuple[str, str]] | Unfinished | Rejected:
    first = _pass(store, lens, arc, arc_lines, rewrites, ask, INSTRUCTIONS, None)
    if isinstance(first, Unfinished | Rejected):
        return first
    first_holds, proposed = first
    if first_holds:
        return frozenset()

    recheck = _pass(store, lens, arc, arc_lines, rewrites, ask, RECHECK_INSTRUCTIONS, proposed)
    if isinstance(recheck, Unfinished | Rejected):
        return recheck
    recheck_holds, named = recheck
    if recheck_holds:
        return frozenset()
    return proposed & named


def _pass(
    store: Store,
    lens: Lens,
    arc: Mapping[str, Any],
    arc_lines: Sequence[SceneLine],
    rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
    instructions: str,
    proposed: frozenset[tuple[str, str]] | None,
) -> tuple[bool, frozenset[tuple[str, str]]] | Unfinished | Rejected:
    line_dicts = _line_dicts(arc_lines, rewrites)
    parts = [
        instructions,
        f"Judge rubric:\n{lens.stage_text('judge')}",
        f"Arc:\n{_json(arc)}",
        f"Arc lines:\n{_json(line_dicts)}",
    ]
    inputs: dict[str, Any] = {"arc": arc, "lines": line_dicts}
    if proposed is not None:
        parts.append(f"Proposed reverts:\n{_json(_ref_list(proposed))}")
        inputs["proposed"] = _ref_list(proposed)
    changed = {_ref(line) for line in arc_lines if _ref(line) in rewrites}

    def call() -> dict[str, Any]:
        answer = ask(claude.OPUS, parts, SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        unknown = _refs(answer) - changed
        if unknown:
            raise _Unanswered(Rejected(f"revert names no changed line of this arc: {_json(_ref_list(unknown))}"))
        return answer

    try:
        answer = store.run(STAGE, inputs, lens.stage_text("judge"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return bool(answer["holds"]), _refs(answer)


def _ref(line: SceneLine) -> tuple[str, str]:
    return (line.namespace, line.key)


def _story_position(line: SceneLine) -> tuple[bool, int]:
    return (line.chapter is None, line.chapter or 0)


def _refs(answer: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset((ref["namespace"], ref["key"]) for ref in answer["revert"])


def _ref_list(refs: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"namespace": namespace, "key": key} for namespace, key in sorted(refs)]


def _line_dicts(arc_lines: Sequence[SceneLine], rewrites: Mapping[tuple[str, str], str]) -> list[dict[str, Any]]:
    return [
        {
            "namespace": line.namespace,
            "key": line.key,
            "speaker": line.speaker,
            "chapter": line.chapter,
            "english": line.english,
            "rewrite": rewrites.get(_ref(line)),
        }
        for line in arc_lines
    ]
