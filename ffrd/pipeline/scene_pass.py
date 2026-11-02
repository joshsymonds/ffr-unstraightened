"""Scene pass: an Opus screen names each scene pattern a scene hits with the lines that carry it, then an Opus plan keeps the scene for a listed reason or plans at most four line rewrites under the scene plan rule, and an Opus scene judge reverts a rewrite only when both passes of the coherence check name it."""

import hashlib
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from ffrd.game import Fail
from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.coherence import check
from ffrd.pipeline.lens import Lens, ScenePattern
from ffrd.pipeline.scene_record import SCENE_KEEP_REASONS
from ffrd.pipeline.stages import Ask, Scene, _inputs, _json, _line_dicts, _screen_lines, _Unanswered
from ffrd.pipeline.store import Store

SCREEN_STAGE = "scene_screen"
PLAN_STAGE = "scene_plan"
JUDGE_STAGE = "scene_judge"
MAX_PLANNED = 4

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
        answer = ask(claude.OPUS, parts, SCREEN_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        hits = _hits(answer, patterns, scene)
        if isinstance(hits, Rejected):
            raise _Unanswered(hits)
        return hits

    try:
        hits = store.run(SCREEN_STAGE, {**_inputs(scene), "cast": list(cast), "model": claude.OPUS}, lens.stage_text("scene"), call).value
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


PLAN_INSTRUCTIONS = (
    "Plan the scene's fix under the scene plan rule. Either keep the scene: set keep to true, keep_reason to the reason "
    "from the keep list that the scene meets, and lines to an empty list. Or set keep to false, keep_reason to none, and "
    "list the lines to change, at most four, each with namespace, key, intent, one sentence on what its rewrite must do, "
    "carrier, the words of that line that carry the flagged pattern, quoted exactly from that line, and required, true "
    "when the fix fails without that line. Never plan a final line or an excluded line. Return JSON "
    "with keep, keep_reason and lines. Refer to lines only by namespace and key."
)

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "keep_reason": {"type": "string", "enum": [*SCENE_KEEP_REASONS, "none"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "key": {"type": "string"},
                    "intent": {"type": "string"},
                    "carrier": {"type": "string"},
                    "required": {"type": "boolean"},
                },
                "required": ["namespace", "key", "intent", "carrier", "required"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["keep", "keep_reason", "lines"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class PlannedLine:
    namespace: str
    key: str
    intent: str
    required: bool


@dataclass(frozen=True)
class Plan:
    id: str | None
    keep_reason: str | None
    lines: tuple[PlannedLine, ...]


def plan(
    store: Store,
    lens: Lens,
    scene: Scene,
    cast: Sequence[Mapping[str, Any]],
    hits: Mapping[str, Sequence[tuple[str, str]]],
    final: Collection[tuple[str, str]],
    excluded: Collection[tuple[str, str]],
    ask: Ask,
) -> Plan | Unfinished | Rejected:
    hit_list = [
        {"id": pattern_id, "carriers": [{"namespace": namespace, "key": key} for namespace, key in carriers]}
        for pattern_id, carriers in hits.items()
    ]
    final_list = _line_refs(final)
    excluded_list = _line_refs(excluded)
    parts = [
        PLAN_INSTRUCTIONS,
        f"Scene rules:\n{lens.stage_text('scene')}",
        f"Scene lines:\n{_json(_line_dicts(scene.lines))}",
        f"Cast:\n{_json(list(cast))}",
        f"Patterns hit:\n{_json(hit_list)}",
        f"Final lines:\n{_json(final_list)}",
        f"Excluded lines:\n{_json(excluded_list)}",
    ]

    def call() -> dict[str, Any]:
        answer = ask(claude.OPUS, parts, PLAN_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        planned = _plan_answer(answer, scene, final, excluded)
        if isinstance(planned, Rejected):
            raise _Unanswered(planned)
        return asdict(planned)

    inputs = {
        **_inputs(scene),
        "cast": list(cast),
        "hits": hit_list,
        "final": final_list,
        "excluded": excluded_list,
    }
    try:
        stored = store.run(PLAN_STAGE, inputs, lens.stage_text("scene"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return Plan(
        id=stored["id"],
        keep_reason=stored["keep_reason"],
        lines=tuple(PlannedLine(**line) for line in stored["lines"]),
    )


def plan_id(scene_key: str, refs: Iterable[tuple[str, str]]) -> str:
    lines = [[namespace, key] for namespace, key in sorted(refs)]
    canonical = _json({"scene": scene_key, "lines": lines})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _line_refs(refs: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"namespace": namespace, "key": key} for namespace, key in sorted(refs)]


def _plan_answer(
    answer: Mapping[str, Any],
    scene: Scene,
    final: Collection[tuple[str, str]],
    excluded: Collection[tuple[str, str]],
) -> Plan | Rejected:
    if answer["keep"]:
        if answer["keep_reason"] == "none":
            return Rejected("a kept scene must name a keep reason")
        if answer["lines"]:
            return Rejected("a kept scene must plan no lines")
        return Plan(id=None, keep_reason=answer["keep_reason"], lines=())
    if answer["keep_reason"] != "none":
        return Rejected("a planned scene must set keep_reason to none")
    if not answer["lines"]:
        return Rejected("a planned scene must plan at least one line")
    if len(answer["lines"]) > MAX_PLANNED:
        return Rejected(f"plan names {len(answer['lines'])} lines, more than {MAX_PLANNED}")
    in_scene = {(line.namespace, line.key): line.english for line in scene.lines}
    final_refs = set(final)
    excluded_refs = set(excluded)
    seen: set[tuple[str, str]] = set()
    planned: list[PlannedLine] = []
    for entry in answer["lines"]:
        namespace, key = entry["namespace"], entry["key"]
        ref = (namespace, key)
        if ref not in in_scene:
            return Rejected(f"planned line {namespace}/{key} is not in scene {scene.key}")
        carrier = entry["carrier"]
        if not carrier.strip():
            return Rejected(f"planned line {namespace}/{key} has an empty carrier")
        if carrier not in in_scene[ref]:
            return Rejected(f"planned line {namespace}/{key} carrier is not in its line")
        if ref in final_refs:
            return Rejected(f"planned line {namespace}/{key} is a line-pass line")
        if ref in excluded_refs:
            return Rejected(f"planned line {namespace}/{key} is excluded")
        if ref in seen:
            return Rejected(f"planned line {namespace}/{key} is planned twice")
        seen.add(ref)
        planned.append(PlannedLine(namespace=namespace, key=key, intent=entry["intent"], required=entry["required"]))
    return Plan(id=plan_id(scene.key, seen), keep_reason=None, lines=tuple(planned))


def judge(
    store: Store,
    lens: Lens,
    scene: Scene,
    rewrites: Mapping[tuple[str, str], str],
    arcs: Sequence[Mapping[str, Any]],
    ask: Ask,
) -> frozenset[tuple[str, str]] | Unfinished | Rejected:
    return check(store, lens, scene, rewrites, arcs, ask, stage=JUDGE_STAGE, rubric="scene")
