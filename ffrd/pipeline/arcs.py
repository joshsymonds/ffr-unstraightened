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

BASE_LABEL = "Base lens rewrites (the arc describes these lines as they read before the base lens rewrote them):"

INSTRUCTIONS = (
    "Judge the arc's lines in story order, each vanilla English beside its rewrite where one is given, against the judge "
    "rubric. Return JSON with holds, true only if the arc holds together with the rewrites applied, and revert, the "
    "rewritten lines that must revert for it to hold together. Refer to lines only by namespace and key."
)

RECHECK_INSTRUCTIONS = (
    "A first pass proposed these reverts. Return JSON with holds, true only if the arc holds together with the rewrites "
    "applied, and revert, only the proposed lines that must revert. Refer to lines only by namespace and key."
)

SCENE_STAGE = "scene_arc"

SCENE_INSTRUCTIONS = (
    "Judge the arc's lines in story order, each vanilla English beside its rewrite where one is given, against the judge "
    "rubric and the scene rubric. The scene rubric takes precedence over the judge rubric for the scene-pass lines, and "
    "only scene-pass lines may revert. Return JSON with holds, true only if the arc holds together with the rewrites "
    "applied, and revert, the scene-pass lines that must revert for it to hold together. Refer to lines only by namespace "
    "and key."
)

SCENE_RECHECK_INSTRUCTIONS = (
    "A first pass proposed these reverts. The scene rubric takes precedence over the judge rubric for the scene-pass lines, "
    "and only scene-pass lines may revert. Return JSON with holds, true only if the arc holds together with the rewrites "
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


@dataclass(frozen=True)
class _Stage:
    """One arc judge pass for one arc: its store stage and lens text, its prompt parts, the judged lines the base lens rewrote, and the refs it may revert."""

    stage: str
    lens_text: str
    instructions: str
    recheck: str
    rubric: tuple[str, ...]
    allowed: frozenset[tuple[str, str]]
    base: list[dict[str, Any]]
    after_lines: tuple[str, ...]
    extra_inputs: Mapping[str, Any]


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


def line_context(
    arcs: Sequence[Mapping[str, Any]],
    bibles: Sequence[Mapping[str, Any]],
    speakers: Collection[str | None],
    line: SceneLine,
) -> dict[str, Any]:
    """Arcs of the line's speaker (any scene speaker when the line has none), windowed to the line's chapter, and the speaker's character notes for it."""
    if line.speaker is None:
        names = {name for name in speakers if name is not None}
        involved = [arc for arc in arcs if any(name in names for name in arc["characters"])]
    else:
        involved = [arc for arc in arcs if line.speaker in arc["characters"]]
    chapters = [] if line.chapter is None else [line.chapter]
    return {
        "arcs": [_windowed(arc, chapters) for arc in involved],
        "characters": _line_notes(bibles, line.speaker, line.chapter) if line.speaker is not None else [],
    }


def scene_arcs(arcs: Sequence[Mapping[str, Any]], scene: Scene) -> list[dict[str, Any]]:
    """Arcs of the scene's speakers, windowed over every placed chapter among its lines; with none placed, each arc's last listed state."""
    names = {line.speaker for line in scene.lines if line.speaker is not None}
    chapters = {line.chapter for line in scene.lines if line.chapter is not None}
    return [_windowed(arc, chapters) for arc in arcs if any(name in names for name in arc["characters"])]


def _windowed(arc: Mapping[str, Any], chapters: Collection[int]) -> dict[str, Any]:
    """The arc's beats of each chapter and the one before, and one state per chapter; with no chapter, the last listed state and no beats."""
    if chapters:
        window = {number for chapter in chapters for number in (chapter - 1, chapter)}
        beats = [beat for beat in arc["beats"] if beat["chapter"] in window]
        picked = {state["chapter"] for chapter in chapters if (state := _state(arc["states"], chapter)) is not None}
        states = [state for state in arc["states"] if state["chapter"] in picked]
    else:
        beats = []
        states = arc["states"][-1:]
    return {"id": arc["id"], "characters": list(arc["characters"]), "states": states, "beats": beats}


def _state(states: Sequence[Mapping[str, Any]], chapter: int) -> Mapping[str, Any] | None:
    """The state recorded at the chapter, else the latest numbered state before it."""
    at_chapter = next((state for state in states if state["chapter"] == chapter), None)
    if at_chapter is not None:
        return at_chapter
    earlier = [state for state in states if state["chapter"] is not None and state["chapter"] < chapter]
    return max(earlier, key=lambda state: state["chapter"], default=None)


def _line_notes(bibles: Sequence[Mapping[str, Any]], speaker: str, chapter: int | None) -> list[dict[str, Any]]:
    """The speaker's entries from the chapter's bibles, preceded by the latest earlier bible holding one; unplaced, the last bible in list order holding one."""
    if chapter is None:
        chosen = [bible for bible in bibles if _entries(bible, speaker)][-1:]
    else:
        earlier = [bible for bible in bibles if bible["chapter"] is not None and bible["chapter"] < chapter and _entries(bible, speaker)]
        chosen = [
            *sorted(earlier, key=lambda bible: bible["chapter"])[-1:],
            *(bible for bible in bibles if bible["chapter"] == chapter),
        ]
    return [entry for bible in chosen for entry in _entries(bible, speaker)]


def _notes(bibles: Sequence[Mapping[str, Any]], speaker: str) -> list[dict[str, Any]]:
    return [entry for bible in bibles for entry in _entries(bible, speaker)]


def _entries(bible: Mapping[str, Any], speaker: str) -> list[dict[str, Any]]:
    return [
        {**{key: value for key, value in entry.items() if key != "speaker"}, "chapter": bible["chapter"]}
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
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> Verdicts:
    verdicts = claude.fan_out(
        lambda arc: _judge_arc(store, lens, arc, scenes, rewrites, ask, base_rewrites),
        arcs,
    )
    return _combine(verdicts)


def judge_scene(
    store: Store,
    lens: Lens,
    arcs: Sequence[Mapping[str, Any]],
    scenes: Sequence[Scene],
    rewrites: Mapping[tuple[str, str], str],
    scene_refs: Collection[tuple[str, str]],
    ask: Ask,
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> Verdicts:
    verdicts = claude.fan_out(
        lambda arc: _judge_scene_arc(store, lens, arc, scenes, rewrites, scene_refs, ask, base_rewrites),
        arcs,
    )
    return _combine(verdicts)


def _combine(verdicts: Sequence[Verdicts]) -> Verdicts:
    return Verdicts(
        frozenset(ref for verdict in verdicts for ref in verdict.reverted),
        frozenset(ref for verdict in verdicts for ref in verdict.unfinished),
    )


def _judge_arc(
    store: Store,
    lens: Lens,
    arc: Mapping[str, Any],
    scenes: Sequence[Scene],
    rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> Verdicts:
    arc_lines = lines(arc, scenes, rewrites)
    changed = frozenset(_ref(line) for line in arc_lines if _ref(line) in rewrites)
    if not changed:
        return Verdicts(frozenset(), frozenset())
    judge_text = lens.stage_text("judge")
    stage = _Stage(
        stage=STAGE,
        lens_text=judge_text,
        instructions=INSTRUCTIONS,
        recheck=RECHECK_INSTRUCTIONS,
        rubric=(f"Judge rubric:\n{judge_text}",),
        allowed=changed,
        base=_base_lines(arc_lines, base_rewrites),
        after_lines=(),
        extra_inputs={},
    )
    return _verdicts(store, stage, arc, arc_lines, rewrites, ask)


def _judge_scene_arc(
    store: Store,
    lens: Lens,
    arc: Mapping[str, Any],
    scenes: Sequence[Scene],
    rewrites: Mapping[tuple[str, str], str],
    scene_refs: Collection[tuple[str, str]],
    ask: Ask,
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> Verdicts:
    arc_lines = lines(arc, scenes, rewrites)
    scene_pass = frozenset(_ref(line) for line in arc_lines if _ref(line) in scene_refs)
    if not scene_pass:
        return Verdicts(frozenset(), frozenset())
    judge_text = lens.stage_text("judge")
    scene_text = lens.stage_text("scene")
    refs = _ref_list(scene_pass)
    stage = _Stage(
        stage=SCENE_STAGE,
        lens_text=f"{judge_text}\n{scene_text}",
        instructions=SCENE_INSTRUCTIONS,
        recheck=SCENE_RECHECK_INSTRUCTIONS,
        rubric=(f"Judge rubric:\n{judge_text}", f"Scene rubric:\n{scene_text}"),
        allowed=scene_pass,
        base=_base_lines(arc_lines, base_rewrites),
        after_lines=(f"Scene-pass lines:\n{_json(refs)}",),
        extra_inputs={"scene_refs": refs},
    )
    return _verdicts(store, stage, arc, arc_lines, rewrites, ask)


def _verdicts(
    store: Store,
    stage: _Stage,
    arc: Mapping[str, Any],
    arc_lines: Sequence[SceneLine],
    rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
) -> Verdicts:
    outcome = _arc(store, stage, arc, arc_lines, rewrites, ask)
    if isinstance(outcome, Unfinished | Rejected):
        return Verdicts(frozenset(), stage.allowed)
    return Verdicts(outcome, frozenset())


def _arc(
    store: Store,
    stage: _Stage,
    arc: Mapping[str, Any],
    arc_lines: Sequence[SceneLine],
    rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
) -> frozenset[tuple[str, str]] | Unfinished | Rejected:
    first = _pass(store, stage, arc, arc_lines, rewrites, ask, None)
    if isinstance(first, Unfinished | Rejected):
        return first
    first_holds, proposed = first
    if first_holds:
        return frozenset()

    recheck = _pass(store, stage, arc, arc_lines, rewrites, ask, proposed)
    if isinstance(recheck, Unfinished | Rejected):
        return recheck
    recheck_holds, named = recheck
    if recheck_holds:
        return frozenset()
    return proposed & named


def _pass(
    store: Store,
    stage: _Stage,
    arc: Mapping[str, Any],
    arc_lines: Sequence[SceneLine],
    rewrites: Mapping[tuple[str, str], str],
    ask: Ask,
    proposed: frozenset[tuple[str, str]] | None,
) -> tuple[bool, frozenset[tuple[str, str]]] | Unfinished | Rejected:
    line_dicts = _line_dicts(arc_lines, rewrites)
    instructions = stage.instructions if proposed is None else stage.recheck
    base_parts = [f"{BASE_LABEL}\n{_json(stage.base)}"] if stage.base else []
    parts = [
        instructions,
        *stage.rubric,
        f"Arc:\n{_json(arc)}",
        f"Arc lines:\n{_json(line_dicts)}",
        *base_parts,
        *stage.after_lines,
    ]
    inputs: dict[str, Any] = {"arc": arc, "lines": line_dicts, **stage.extra_inputs}
    if stage.base:
        inputs["base_rewrites"] = stage.base
    if proposed is not None:
        parts.append(f"Proposed reverts:\n{_json(_ref_list(proposed))}")
        inputs["proposed"] = _ref_list(proposed)

    def call() -> dict[str, Any]:
        answer = ask(claude.OPUS, parts, SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        unknown = _refs(answer) - stage.allowed
        if unknown:
            raise _Unanswered(Rejected(f"revert names no changed line of this arc: {_json(_ref_list(unknown))}"))
        return answer

    try:
        answer = store.run(stage.stage, inputs, stage.lens_text, call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return bool(answer["holds"]), _refs(answer)


def _ref(line: SceneLine) -> tuple[str, str]:
    return (line.namespace, line.key)


def _base_lines(
    arc_lines: Sequence[SceneLine],
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The judged lines the base lens rewrote, in arc-line order, each with the base lens and its reasons."""
    if not base_rewrites:
        return []
    return [
        {
            "namespace": line.namespace,
            "key": line.key,
            "lens": base_rewrites[_ref(line)]["lens"],
            "reasons": base_rewrites[_ref(line)]["reasons"],
        }
        for line in arc_lines
        if _ref(line) in base_rewrites
    ]


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
