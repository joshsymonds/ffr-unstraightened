"""Scene coherence check: one Opus pass reads a scene with its chosen rewrites applied, the arcs and the judge rubric, and names the changed lines it would revert; a recheck sees the same changes plus those proposed reverts. A line reverts only when both passes name it."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ffrd.pipeline import claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, Stage
from ffrd.pipeline.stages import Ask, Scene, _inputs, _json, _Unanswered
from ffrd.pipeline.store import Store

STAGE = "scene_coherence"

INSTRUCTIONS = (
    "Judge the scene with the rewrites applied against the judge rubric and the arcs. Return JSON with coherent, true "
    "only if the scene holds together with the rewrites applied, and revert, the changed lines that must revert for it "
    "to hold together. Refer to lines only by namespace and key."
)

RECHECK_INSTRUCTIONS = (
    "A first pass proposed these reverts. Return JSON with coherent, true only if the scene holds together with the "
    "rewrites applied, and revert, only the proposed lines that must revert. Refer to lines only by namespace and key."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "coherent": {"type": "boolean"},
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
    "required": ["coherent", "revert"],
    "additionalProperties": False,
}


def check(
    store: Store,
    lens: Lens,
    scene: Scene,
    rewrites: Mapping[tuple[str, str], str],
    arcs: Sequence[Mapping[str, Any]],
    ask: Ask,
    stage: str = STAGE,
    rubric: Stage = "judge",
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> frozenset[tuple[str, str]] | Unfinished | Rejected:
    first = _pass(store, lens, scene, rewrites, arcs, ask, INSTRUCTIONS, None, stage, rubric, base_rewrites)
    if isinstance(first, Unfinished | Rejected):
        return first
    first_coherent, proposed = first
    if first_coherent:
        return frozenset()

    recheck = _pass(store, lens, scene, rewrites, arcs, ask, RECHECK_INSTRUCTIONS, proposed, stage, rubric, base_rewrites)
    if isinstance(recheck, Unfinished | Rejected):
        return recheck
    recheck_coherent, named = recheck
    if recheck_coherent:
        return frozenset()
    return proposed & named


def _pass(
    store: Store,
    lens: Lens,
    scene: Scene,
    rewrites: Mapping[tuple[str, str], str],
    arcs: Sequence[Mapping[str, Any]],
    ask: Ask,
    instructions: str,
    proposed: frozenset[tuple[str, str]] | None,
    stage: str,
    rubric: Stage,
    base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> tuple[bool, frozenset[tuple[str, str]]] | Unfinished | Rejected:
    rubric_text = lens.stage_text(rubric)
    parts = [
        instructions,
        f"Judge rubric:\n{rubric_text}",
        f"Arcs:\n{_json(list(arcs))}",
        f"Scene lines:\n{_json(_scene_dicts(scene, rewrites))}",
    ]
    inputs: dict[str, Any] = {**_inputs(scene), "rewrites": _rewrite_list(rewrites), "arcs": list(arcs)}
    base = _base_rewrite_list(scene, base_rewrites)
    if base:
        parts.append(f"Base lens rewrites (the arcs describe these lines as they read before the base lens rewrote them):\n{_json(base)}")
        inputs["base_rewrites"] = base
    if proposed is not None:
        parts.append(f"Proposed reverts:\n{_json(_ref_list(proposed))}")
        inputs["proposed"] = _ref_list(proposed)

    def call() -> dict[str, Any]:
        answer = ask(claude.OPUS, parts, SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        unknown = _refs(answer) - set(rewrites)
        if unknown:
            raise _Unanswered(Rejected(f"revert names no rewritten line: {_json(_ref_list(unknown))}"))
        return answer

    try:
        answer = store.run(stage, inputs, rubric_text, call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return bool(answer["coherent"]), _refs(answer)


def _refs(answer: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset((ref["namespace"], ref["key"]) for ref in answer["revert"])


def _ref_list(refs: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"namespace": namespace, "key": key} for namespace, key in sorted(refs)]


def _rewrite_list(rewrites: Mapping[tuple[str, str], str]) -> list[dict[str, str]]:
    return [{"namespace": namespace, "key": key, "text": text} for (namespace, key), text in sorted(rewrites.items())]


def _base_rewrite_list(scene: Scene, base_rewrites: Mapping[tuple[str, str], Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if base_rewrites is None:
        return []
    listed: list[dict[str, Any]] = []
    for line in scene.lines:
        base = base_rewrites.get((line.namespace, line.key))
        if base is not None:
            listed.append(
                {
                    "namespace": line.namespace,
                    "key": line.key,
                    "lens": base["lens"],
                    "reasons": base["reasons"],
                }
            )
    return listed


def _scene_dicts(scene: Scene, rewrites: Mapping[tuple[str, str], str]) -> list[dict[str, Any]]:
    return [
        {
            "namespace": line.namespace,
            "key": line.key,
            "speaker": line.speaker,
            "english": line.english,
            "rewrite": rewrites.get((line.namespace, line.key)),
        }
        for line in scene.lines
    ]
