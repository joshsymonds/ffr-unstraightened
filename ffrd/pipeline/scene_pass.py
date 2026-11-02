"""Scene screen: one Sonnet pass reads the lens scene patterns, the scene lines and the cast, and names each scene pattern the scene hits with the lines that carry it."""

from collections.abc import Mapping, Sequence
from typing import Any

from ffrd.game import Fail
from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, ScenePattern
from ffrd.pipeline.stages import Ask, Scene, _inputs, _json, _screen_lines, _Unanswered
from ffrd.pipeline.store import Store

SCREEN_STAGE = "scene_screen"

SCREEN_INSTRUCTIONS = (
    "Name the scene patterns the scene hits under the scene criteria and, for each pattern hit, the lines that carry it. "
    "Return JSON with patterns, one entry per pattern hit, each with id, taken only from the scene pattern list, and "
    "carriers, the lines that carry it, each with namespace and key. Return an empty patterns list when the scene hits "
    "none. Refer to lines only by namespace and key."
)

SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "carriers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"namespace": {"type": "string"}, "key": {"type": "string"}},
                            "required": ["namespace", "key"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                    },
                },
                "required": ["id", "carriers"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["patterns"],
    "additionalProperties": False,
}


def screen(
    store: Store, lens: Lens, scene: Scene, cast: Sequence[Mapping[str, Any]], ask: Ask
) -> dict[str, tuple[tuple[str, str], ...]] | Unfinished | Rejected:
    patterns = _patterns(lens)
    parts = [
        SCREEN_INSTRUCTIONS,
        f"Scene rules:\n{lens.stage_text('scene')}",
        f"Scene lines:\n{_screen_lines(scene)}",
        f"Cast:\n{_json(list(cast))}",
    ]

    def call() -> dict[str, list[dict[str, str]]]:
        answer = ask(claude.SONNET, parts, SCREEN_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        hits = _hits(answer, patterns, scene)
        if isinstance(hits, Rejected):
            raise _Unanswered(hits)
        return hits

    try:
        hits = store.run(SCREEN_STAGE, {**_inputs(scene), "cast": list(cast)}, lens.stage_text("scene"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return {pattern.id: tuple((ref["namespace"], ref["key"]) for ref in hits[pattern.id]) for pattern in patterns if pattern.id in hits}


def _patterns(lens: Lens) -> tuple[ScenePattern, ...]:
    if lens.scene is None:
        raise Fail(f"lens {lens.name} has no [scene] section")
    return lens.scene.patterns


def _hits(answer: Mapping[str, Any], patterns: Sequence[ScenePattern], scene: Scene) -> dict[str, list[dict[str, str]]] | Rejected:
    pattern_ids = {pattern.id for pattern in patterns}
    known = {(line.namespace, line.key) for line in scene.lines}
    carriers_by_pattern: dict[str, list[dict[str, str]]] = {}
    for entry in answer["patterns"]:
        pattern_id = entry["id"]
        if pattern_id not in pattern_ids:
            return Rejected(f"pattern {pattern_id!r} is not a scene pattern id in the lens")
        carriers = carriers_by_pattern.setdefault(pattern_id, [])
        for ref in entry["carriers"]:
            namespace, key = ref["namespace"], ref["key"]
            if (namespace, key) not in known:
                return Rejected(f"carrier line {namespace}/{key} is not in scene {scene.key}")
            if {"namespace": namespace, "key": key} not in carriers:
                carriers.append({"namespace": namespace, "key": key})
    return carriers_by_pattern
