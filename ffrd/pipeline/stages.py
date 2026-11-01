"""Pipeline stages: Jev triage and the Sonnet screen per scene, then Opus intent, draft, scene judge and cross-scene judge and the voice render per line, each stored by its inputs and the lens text it reads; plus the per-batch Claude account and the injected scene, classifier and renderer interfaces."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from ffrd.game import Fail
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Judgment
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.store import Store


@dataclass(frozen=True)
class SceneLine:
    namespace: str
    key: str
    speaker: str | None
    english: str
    japanese: str
    chapter: int | None


@dataclass(frozen=True)
class Scene:
    key: str
    lines: tuple[SceneLine, ...]


class SceneSource(Protocol):
    # True when the scenes come from the game's scene table (lines referenced by event assets), False otherwise.
    scene_table: bool

    def scenes(self) -> Sequence[Scene]: ...


class Classifier(Protocol):
    threshold: float

    def score(self, text: str) -> float: ...


class Renderer(Protocol):
    def render(self, speaker: str | None, text: str) -> str: ...


@dataclass(frozen=True)
class Bindings:
    scenes: SceneSource
    classifier: Classifier
    renderer: Renderer


def bindings() -> Bindings:
    raise Fail("no scene source, classifier and renderer are bound yet")


Ask = Callable[[str, Sequence[str], Mapping[str, Any]], dict[str, Any] | Unfinished | Rejected]
Triage = Callable[[str, Sequence[TriageQuestion]], dict[str, float] | RateLimited | Unfinished | Rejected]


def _choose_account() -> int:
    return accounts.choose().number


class Batch:
    """One Claude account for a whole batch: chosen on the first ask, then kept as each call returns it."""

    def __init__(
        self,
        *,
        choose: Callable[[], int] = _choose_account,
        ask: Callable[
            [int, str, Sequence[str], Mapping[str, Any]],
            tuple[int, dict[str, Any] | Unfinished | Rejected],
        ] = claude.ask,
    ) -> None:
        self._choose = choose
        self._ask = ask
        self._account: int | None = None

    def ask(self, model: str, parts: Sequence[str], schema: Mapping[str, Any]) -> dict[str, Any] | Unfinished | Rejected:
        if self._account is None:
            self._account = self._choose()
        account, answer = self._ask(self._account, model, parts, schema)
        self._account = account
        return answer


def scene_text(scene: Scene) -> str:
    return "\n".join(line.english if line.speaker is None else f"{line.speaker}: {line.english}" for line in scene.lines)


class _Refused(Exception):
    """Carries a failed triage answer out of the store's call so the store marks the scene unfinished and stores nothing."""

    def __init__(self, failure: RateLimited | Unfinished | Rejected) -> None:
        super().__init__(failure.detail)
        self.failure = failure


class _Unanswered(Exception):
    """Carries a failed model answer out of the store's call so the store marks the stage unfinished and stores nothing."""

    def __init__(self, failure: Unfinished | Rejected) -> None:
        super().__init__(failure.detail)
        self.failure = failure


def _inputs(scene: Scene) -> dict[str, Any]:
    return {"scene": scene.key, "lines": [asdict(line) for line in scene.lines]}


def triage(store: Store, lens: Lens, scene: Scene, jev: Triage) -> dict[str, float] | RateLimited | Unfinished | Rejected:
    def call() -> dict[str, float]:
        answer = jev(scene_text(scene), lens.triage)
        if isinstance(answer, RateLimited | Unfinished | Rejected):
            raise _Refused(answer)
        return answer

    try:
        return store.run("triage", _inputs(scene), lens.stage_text("triage"), call).value
    except _Refused as refused:
        return refused.failure


SCREEN_INSTRUCTIONS = (
    "Flag the scene lines that meet the screen criteria. For each flagged line, list reasons: the ids of the "
    "triage questions it answers yes to, taken only from the triage list. Return JSON with flagged, one entry "
    "per flagged line, each with namespace, key and reasons. Refer to lines only by namespace and key."
)

SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "flagged": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "key": {"type": "string"},
                    "reasons": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["namespace", "key", "reasons"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["flagged"],
    "additionalProperties": False,
}


def _flagged(answer: Mapping[str, Any], lens: Lens, scene: Scene) -> list[dict[str, Any]] | Rejected:
    known = {(line.namespace, line.key) for line in scene.lines}
    question_ids = [question.id for question in lens.triage]
    reasons_by_line: dict[tuple[str, str], set[str]] = {}
    for entry in answer["flagged"]:
        ref = (entry["namespace"], entry["key"])
        if ref not in known:
            return Rejected(f"flagged line {ref[0]}/{ref[1]} is not in scene {scene.key}")
        for reason in entry["reasons"]:
            if reason not in question_ids:
                return Rejected(f"reason {reason!r} is not a triage id in the lens")
        reasons_by_line.setdefault(ref, set()).update(entry["reasons"])
    return [
        {"namespace": namespace, "key": key, "reasons": [i for i in question_ids if i in reasons]}
        for (namespace, key), reasons in reasons_by_line.items()
    ]


def screen(store: Store, lens: Lens, scene: Scene, ask: Ask) -> dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected:
    lines = json.dumps(
        [{"namespace": line.namespace, "key": line.key, "speaker": line.speaker, "english": line.english} for line in scene.lines],
        ensure_ascii=False,
        sort_keys=True,
    )
    parts = [
        SCREEN_INSTRUCTIONS,
        f"Screen criteria:\n{lens.stage_text('screen')}",
        f"Triage questions:\n{lens.stage_text('triage')}",
        f"Scene lines:\n{lines}",
    ]

    def call() -> list[dict[str, Any]]:
        answer = ask(claude.SONNET, parts, SCREEN_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        flagged = _flagged(answer, lens, scene)
        if isinstance(flagged, Rejected):
            raise _Unanswered(flagged)
        return flagged

    lens_text = lens.stage_text("screen") + "\n" + lens.stage_text("triage")
    try:
        flagged = store.run("screen", _inputs(scene), lens_text, call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return {(entry["namespace"], entry["key"]): tuple(entry["reasons"]) for entry in flagged}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _line_ref(line: SceneLine) -> dict[str, str]:
    return {"namespace": line.namespace, "key": line.key}


def _line_dicts(lines: Sequence[SceneLine]) -> list[dict[str, Any]]:
    return [
        {"namespace": line.namespace, "key": line.key, "speaker": line.speaker, "english": line.english, "japanese": line.japanese}
        for line in lines
    ]


INTENT_INSTRUCTIONS = (
    "State the intent of the target line: what the rewritten line must do for the player in this scene, in one "
    "sentence. Follow the intent rule. Use the callbacks and the scene lines for context. Return JSON with intent. "
    "Refer to lines only by namespace and key."
)

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"intent": {"type": "string"}},
    "required": ["intent"],
    "additionalProperties": False,
}


def intent(
    store: Store,
    lens: Lens,
    scene: Scene,
    line: SceneLine,
    reasons: Sequence[str],
    callbacks: Sequence[Mapping[str, Any]],
    ask: Ask,
) -> str | Unfinished | Rejected:
    parts = [
        INTENT_INSTRUCTIONS,
        f"Intent rule:\n{lens.stage_text('intent')}",
        f"Callbacks:\n{_json(list(callbacks))}",
        f"Scene lines:\n{_json(_line_dicts(scene.lines))}",
        f"Target line:\n{_json({**_line_ref(line), 'reasons': list(reasons)})}",
    ]

    def call() -> str:
        answer = ask(claude.OPUS, parts, INTENT_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        return answer["intent"]

    inputs = {**_inputs(scene), "line": _line_ref(line), "reasons": list(reasons), "callbacks": list(callbacks)}
    try:
        return store.run("intent", inputs, lens.stage_text("intent"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure


DRAFT_INSTRUCTIONS = (
    "Write candidate rewrites of the target line that carry out its intent. Return JSON with drafts, an array of "
    "candidate English lines. Keep every {...} placeholder and every <text ...>...</> tag from the English line, "
    "unchanged. Keep the English line's line breaks. Stay within 120% of the English line's length. Keep the "
    "speaker's character traits. Refer to lines only by namespace and key."
)

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"drafts": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
    "required": ["drafts"],
    "additionalProperties": False,
}


def draft(
    store: Store,
    lens: Lens,
    scene: Scene,
    line: SceneLine,
    intent: str,
    callbacks: Sequence[Mapping[str, Any]],
    retry: str | None,
    ask: Ask,
) -> tuple[str, ...] | Unfinished | Rejected:
    parts = [
        DRAFT_INSTRUCTIONS,
        f"Intent rule:\n{lens.stage_text('intent')}",
        f"Intent:\n{intent}",
        f"Callbacks:\n{_json(list(callbacks))}",
        f"Scene lines:\n{_json(_line_dicts(scene.lines))}",
        f"Target line:\n{_json(_line_ref(line))}",
    ]
    if retry is not None:
        parts.append(f"The previous candidates failed the {retry} gate. Write candidates that pass it.")

    def call() -> list[str]:
        answer = ask(claude.OPUS, parts, DRAFT_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        return list(answer["drafts"])

    inputs = {**_inputs(scene), "line": _line_ref(line), "intent": intent, "callbacks": list(callbacks), "retry": retry}
    try:
        return tuple(store.run("draft", inputs, lens.stage_text("intent"), call).value)
    except _Unanswered as unanswered:
        return unanswered.failure


def render(store: Store, renderer: Renderer, speaker: str | None, text: str) -> str:
    return store.run("render", {"speaker": speaker, "text": text}, None, lambda: renderer.render(speaker, text)).value


JUDGE_INSTRUCTIONS = (
    "Judge a candidate rewrite of the target line. Return JSON with five booleans. meaning: the candidate keeps the "
    "meaning of the English line. plot: the plot is unchanged. minimal_change: the candidate changes no more than it "
    "must; moving toward the Japanese line counts as minimal change. callback: every callback still holds. "
    "restores_japanese: the candidate restores the meaning of the Japanese line. The Japanese line is the meaning "
    "reference. Refer to lines only by namespace and key."
)

JUDGE_VERDICTS = ("meaning", "plot", "minimal_change", "callback", "restores_japanese")

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {verdict: {"type": "boolean"} for verdict in JUDGE_VERDICTS},
    "required": list(JUDGE_VERDICTS),
    "additionalProperties": False,
}


def judge(
    store: Store,
    lens: Lens,
    scene: Scene,
    line: SceneLine,
    text: str,
    callbacks: Sequence[Mapping[str, Any]],
    ask: Ask,
) -> Judgment | Unfinished | Rejected:
    parts = [
        JUDGE_INSTRUCTIONS,
        f"Judge rubric:\n{lens.stage_text('judge')}",
        f"Target line:\n{_json({**_line_ref(line), 'english': line.english, 'japanese': line.japanese})}",
        f"Candidate:\n{text}",
        f"Callbacks:\n{_json(list(callbacks))}",
        f"Scene lines:\n{_json(_line_dicts(scene.lines))}",
    ]

    def call() -> dict[str, bool]:
        answer = ask(claude.OPUS, parts, JUDGE_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        return asdict(Judgment(**answer))

    inputs = {**_inputs(scene), "line": _line_ref(line), "text": text, "callbacks": list(callbacks)}
    try:
        return Judgment(**store.run("judge", inputs, lens.stage_text("judge"), call).value)
    except _Unanswered as unanswered:
        return unanswered.failure


CROSS_INSTRUCTIONS = (
    "Decide whether the candidate rewrite of the target line stays consistent with every callback and with the "
    "related lines, which may come from any scene. Follow the judge rubric. Return JSON with consistent, true only "
    "if the candidate is consistent with all of them. Refer to lines only by namespace and key."
)

CROSS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"consistent": {"type": "boolean"}},
    "required": ["consistent"],
    "additionalProperties": False,
}


def cross_judge(
    store: Store,
    lens: Lens,
    line: SceneLine,
    text: str,
    callbacks: Sequence[Mapping[str, Any]],
    related: Sequence[SceneLine],
    ask: Ask,
) -> bool | Unfinished | Rejected:
    parts = [
        CROSS_INSTRUCTIONS,
        f"Judge rubric:\n{lens.stage_text('judge')}",
        f"Target line:\n{_json({**_line_ref(line), 'english': line.english})}",
        f"Candidate:\n{text}",
        f"Callbacks:\n{_json(list(callbacks))}",
        f"Related lines:\n{_json(_line_dicts(related))}",
    ]

    def call() -> bool:
        answer = ask(claude.OPUS, parts, CROSS_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        return answer["consistent"]

    inputs = {
        "line": {**_line_ref(line), "english": line.english},
        "text": text,
        "callbacks": list(callbacks),
        "related": _line_dicts(related),
    }
    try:
        return store.run("cross_judge", inputs, lens.stage_text("judge"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
