"""Pipeline stages: Jev triage and the Opus screen per scene, then Opus intent, draft, scene judge and cross-scene judge and the voice render per line, each stored by its inputs and the lens text it reads; plus the per-batch Claude account and the injected scene, classifier and renderer interfaces."""

import atexit
import json
import re
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ffrd import find
from ffrd import scenes as scene_file
from ffrd.game import Fail, entries, vanilla
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.gates import Judgment
from ffrd.pipeline.lens import Lens, TriageQuestion
from ffrd.pipeline.store import Store
from ffrd.voice import classifier, remote
from ffrd.voice import render as voice_render
from ffrd.voice.evals import RENDER_SCORE_BAR


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
    # True when the scene was built from the game's scene table; not part of a scene's identity.
    table: bool = field(default=False, compare=False)


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


_SCENE_FAMILY = re.compile(r"ST_(?:DialogChoice|MainSummary|UnitDescription)(?:_|\Z)")


def _texts(lang: str) -> dict[tuple[str, str], str]:
    return {(namespace, key): text for namespace, key, text in entries(vanilla(lang))}


def _line(namespace: str, key: str, speaker: str | None, english: str, japanese: dict[tuple[str, str], str]) -> SceneLine:
    return SceneLine(
        namespace=namespace,
        key=key,
        speaker=speaker,
        english=english,
        japanese=japanese.get((namespace, key), ""),
        chapter=find.chapter(namespace),
    )


def _table_scenes(
    rows: Sequence[scene_file.SceneLine], english: dict[tuple[str, str], str], japanese: dict[tuple[str, str], str]
) -> list[Scene]:
    grouped: dict[str, list[SceneLine]] = {}
    for row in rows:
        text = english.get((row.namespace, row.key))
        if not text:
            raise Fail(f"scene table line {row.namespace}/{row.key} has no English text; run `ffrd scenes`")
        grouped.setdefault(row.scene, []).append(_line(row.namespace, row.key, row.speaker, text, japanese))
    return [Scene(key=key, lines=tuple(lines), table=True) for key, lines in grouped.items()]


def _family_scenes(
    english: dict[tuple[str, str], str], japanese: dict[tuple[str, str], str], table_refs: set[tuple[str, str]]
) -> list[Scene]:
    grouped: dict[str, list[SceneLine]] = {}
    for (namespace, key), text in english.items():
        if not text or (namespace, key) in table_refs or _SCENE_FAMILY.match(namespace) is None:
            continue
        grouped.setdefault(namespace, []).append(_line(namespace, key, None, text, japanese))
    return [Scene(key=namespace, lines=tuple(lines)) for namespace, lines in grouped.items()]


class _SceneTable:
    scene_table = True

    def scenes(self) -> list[Scene]:
        english = _texts("en")
        japanese = _texts("ja")
        rows = scene_file.load()
        found = _table_scenes(rows, english, japanese)
        table_refs = {(row.namespace, row.key) for row in rows}
        return found + _family_scenes(english, japanese, table_refs)


class _Classifier:
    threshold = RENDER_SCORE_BAR

    def __init__(self, stack: ExitStack) -> None:
        self._stack = stack
        self._scorer: classifier.Scorer | None = None
        self._lock = threading.Lock()

    def score(self, text: str) -> float:
        # The scorer talks over one pipe, so the open and each score run one at a time.
        with self._lock:
            if self._scorer is None:
                remote.sync()
                self._scorer = self._stack.enter_context(classifier.Scorer())
            return self._scorer.score(text)


class _Renderer:
    def __init__(self, stack: ExitStack) -> None:
        self._stack = stack
        self._render: Callable[[str, str], str] | None = None
        self._lock = threading.Lock()

    def render(self, speaker: str | None, text: str) -> str:
        if speaker is None:
            return text
        with self._lock:
            if self._render is None:
                self._render = self._stack.enter_context(voice_render.rendering())
            render = self._render
        return render(speaker, text)


def bindings() -> Bindings:
    stack = ExitStack()
    atexit.register(stack.close)
    return Bindings(scenes=_SceneTable(), classifier=_Classifier(stack), renderer=_Renderer(stack))


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
        self._lock = threading.Lock()

    def ask(self, model: str, parts: Sequence[str], schema: Mapping[str, Any]) -> dict[str, Any] | Unfinished | Rejected:
        with self._lock:
            if self._account is None:
                self._account = self._choose()
            account = self._account
        answered, answer = self._ask(account, model, parts, schema)
        with self._lock:
            self._account = answered
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


def _triage_lines(
    jev: Triage, lens: Lens, scene_key: str, lines: Sequence[SceneLine]
) -> dict[str, float] | RateLimited | Unfinished | Rejected:
    answer = jev(scene_text(Scene(scene_key, tuple(lines))), lens.triage)
    if not isinstance(answer, Rejected) or len(lines) < 2:
        return answer
    middle = len(lines) // 2
    first = _triage_lines(jev, lens, scene_key, lines[:middle])
    if not isinstance(first, dict):
        return first
    second = _triage_lines(jev, lens, scene_key, lines[middle:])
    if not isinstance(second, dict):
        return second
    return {question: max(first[question], second[question]) for question in first}


def triage(store: Store, lens: Lens, scene: Scene, jev: Triage) -> dict[str, float] | RateLimited | Unfinished | Rejected:
    def call() -> dict[str, float]:
        answer = _triage_lines(jev, lens, scene.key, scene.lines)
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


def _flagged(
    answer: Mapping[str, Any], lens: Lens, scene: Scene, eligible: Collection[tuple[str, str]] | None
) -> list[dict[str, Any]] | Rejected:
    known = {(line.namespace, line.key) for line in scene.lines}
    question_ids = [question.id for question in lens.triage]
    reasons_by_line: dict[tuple[str, str], set[str]] = {}
    for entry in answer["flagged"]:
        ref = (entry["namespace"], entry["key"])
        if ref not in known:
            return Rejected(f"flagged line {ref[0]}/{ref[1]} is not in scene {scene.key}")
        if eligible is not None and ref not in eligible:
            return Rejected(f"flagged line {ref[0]}/{ref[1]} is not eligible in scene {scene.key}")
        for reason in entry["reasons"]:
            if reason not in question_ids:
                return Rejected(f"reason {reason!r} is not a triage id in the lens")
        reasons_by_line.setdefault(ref, set()).update(entry["reasons"])
    return [
        {"namespace": namespace, "key": key, "reasons": [i for i in question_ids if i in reasons]}
        for (namespace, key), reasons in reasons_by_line.items()
    ]


def _screen_lines(scene: Scene) -> str:
    return json.dumps(
        [{"namespace": line.namespace, "key": line.key, "speaker": line.speaker, "english": line.english} for line in scene.lines],
        ensure_ascii=False,
        sort_keys=True,
    )


def screen(
    store: Store,
    lens: Lens,
    scene: Scene,
    cast: Sequence[Mapping[str, Any]],
    ask: Ask,
    *,
    model: str = claude.OPUS,
    eligible: Collection[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected:
    eligible_refs = None if eligible is None else [{"namespace": namespace, "key": key} for namespace, key in sorted(eligible)]
    parts = [
        SCREEN_INSTRUCTIONS,
        f"Screen criteria:\n{lens.stage_text('screen')}",
        f"Triage questions:\n{lens.stage_text('triage')}",
        f"Scene lines:\n{_screen_lines(scene)}",
        f"Cast:\n{_json(list(cast))}",
    ]
    if eligible_refs is not None:
        parts.append(f"Eligible lines, flag only these:\n{_json(eligible_refs)}")

    def call() -> list[dict[str, Any]]:
        answer = ask(model, parts, SCREEN_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        flagged = _flagged(answer, lens, scene, eligible)
        if isinstance(flagged, Rejected):
            raise _Unanswered(flagged)
        return flagged

    inputs = {**_inputs(scene), "cast": list(cast), "model": model}
    if eligible_refs is not None:
        inputs["eligible"] = eligible_refs
    lens_text = lens.stage_text("screen") + "\n" + lens.stage_text("triage")
    try:
        flagged = store.run("screen", inputs, lens_text, call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return {(entry["namespace"], entry["key"]): tuple(entry["reasons"]) for entry in flagged}


RESCREEN_INSTRUCTIONS = (
    "Flag the scene lines that answer yes to the one triage question under the screen criteria. Return JSON with "
    "flagged, one entry per flagged line, each with namespace and key. Refer to lines only by namespace and key."
)

RESCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "flagged": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"namespace": {"type": "string"}, "key": {"type": "string"}},
                "required": ["namespace", "key"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["flagged"],
    "additionalProperties": False,
}


def rescreen(
    store: Store,
    lens: Lens,
    scene: Scene,
    question: TriageQuestion,
    cast: Sequence[Mapping[str, Any]],
    ask: Ask,
) -> dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected:
    parts = [
        RESCREEN_INSTRUCTIONS,
        f"Screen criteria:\n{lens.stage_text('screen')}",
        f"Triage question:\n{_json({'id': question.id, 'question': question.question})}",
        f"Scene lines:\n{_screen_lines(scene)}",
        f"Cast:\n{_json(list(cast))}",
    ]

    def call() -> list[dict[str, str]]:
        answer = ask(claude.OPUS, parts, RESCREEN_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        known = {(line.namespace, line.key) for line in scene.lines}
        refs = list(dict.fromkeys((entry["namespace"], entry["key"]) for entry in answer["flagged"]))
        for namespace, key in refs:
            if (namespace, key) not in known:
                raise _Unanswered(Rejected(f"flagged line {namespace}/{key} is not in scene {scene.key}"))
        return [{"namespace": namespace, "key": key} for namespace, key in refs]

    inputs = {**_inputs(scene), "cast": list(cast), "question": question.id, "text": question.question}
    try:
        flagged = store.run("rescreen", inputs, lens.stage_text("screen"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return {(entry["namespace"], entry["key"]): (question.id,) for entry in flagged}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _line_ref(line: SceneLine) -> dict[str, str]:
    return {"namespace": line.namespace, "key": line.key}


def _line_dicts(lines: Sequence[SceneLine]) -> list[dict[str, Any]]:
    return [
        {"namespace": line.namespace, "key": line.key, "speaker": line.speaker, "english": line.english, "japanese": line.japanese}
        for line in lines
    ]


KEEP_REASONS = (
    "no-hit",
    "earned",
    "no-invented-motive",
    "gender-rule-narrow",
    "own-wish",
    "oaths-outside-romance",
    "protection",
    "plot",
    "none",
)

INTENT_INSTRUCTIONS = (
    "State the intent of the target line: what the rewritten line must do for the player in this scene, in one "
    "sentence. Follow the intent rule. Use the callbacks and the scene lines for context. Set keep to true when the "
    "intent rule leaves the target line unchanged, and to false when it calls for a rewrite. When keep is true, set "
    "keep_reason to the reason from the intent rule's keep list that the target line meets; otherwise set keep_reason "
    "to none. Return JSON with intent, keep and keep_reason. Refer to lines only by namespace and key."
)

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "keep": {"type": "boolean"},
        "keep_reason": {"type": "string", "enum": list(KEEP_REASONS)},
    },
    "required": ["intent", "keep", "keep_reason"],
    "additionalProperties": False,
}
# Bumped whenever the intent answer changes shape, so results stored under the old answer are not reused.
INTENT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Intent:
    text: str
    keep: bool
    keep_reason: str


def intent(
    store: Store,
    lens: Lens,
    scene: Scene,
    line: SceneLine,
    reasons: Sequence[str],
    callbacks: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
    ask: Ask,
    bucket: str | None = None,
) -> Intent | Unfinished | Rejected:
    target = {**_line_ref(line), "reasons": list(reasons)}
    if bucket is not None:
        target["bucket"] = bucket
    parts = [
        INTENT_INSTRUCTIONS,
        f"Intent rule:\n{lens.stage_text('intent')}",
        f"Callbacks:\n{_json(list(callbacks))}",
        f"Arc context:\n{_json(context)}",
        f"Scene lines:\n{_json(_line_dicts(scene.lines))}",
        f"Target line:\n{_json(target)}",
    ]

    def call() -> dict[str, Any]:
        answer = ask(claude.OPUS, parts, INTENT_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        return asdict(Intent(text=answer["intent"], keep=answer["keep"], keep_reason=answer["keep_reason"]))

    inputs = {
        **_inputs(scene),
        "line": _line_ref(line),
        "reasons": list(reasons),
        "callbacks": list(callbacks),
        "context": context,
        "schema": INTENT_SCHEMA_VERSION,
    }
    if bucket is not None:
        inputs["bucket"] = bucket
    try:
        return Intent(**store.run("intent", inputs, lens.stage_text("intent"), call).value)
    except _Unanswered as unanswered:
        return unanswered.failure


@dataclass(frozen=True)
class Target:
    line: SceneLine
    intent: str
    callbacks: tuple[Mapping[str, Any], ...]
    context: Mapping[str, Any]


DRAFT_INSTRUCTIONS = (
    "Write candidate rewrites of each target line that carry out its intent. Write all target lines together so they "
    "read as one scene. Return JSON with drafts, an array with one entry per target, each with namespace, key and "
    "drafts, an array of candidate English lines for that target. Keep every {...} placeholder and every <text ...>...</> "
    "tag from the target's English line, unchanged. Keep each English line's line breaks. Stay within 120% of each "
    "English line's length. Keep the speaker's character traits. Refer to lines only by namespace and key."
)

DRAFT_NOTE_INSTRUCTIONS = (
    "A reviewer read the earlier rewrite of this target line and named a problem in the reviewer note. Write candidates "
    "that fix that problem. Where the note says the original line is itself weak, you may rewrite more freely, but keep "
    "the line's point, its plot facts, its speaker and the speaker's voice."
)

SPEAKER_DRAFT_INSTRUCTIONS = (
    "The targets are one speaker's lines across these scenes. Write them together so the speaker stays consistent from scene to scene."
)

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "key": {"type": "string"},
                    "drafts": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["namespace", "key", "drafts"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["drafts"],
    "additionalProperties": False,
}

TRANS_DRAFT_INSTRUCTIONS = (
    "Decide for each of one speaker's lines whether a trans detail fits it. Add a light, natural trans detail only where "
    "the line allows it: a detail that the speaker is trans, or a nonbinary partner who uses they/them on a romance line. "
    'Otherwise keep the line unchanged. Never force a detail, and never write a "she\'s trans" aside. Return JSON with '
    "lines, an array with one entry per speaker line, each with namespace, key, keep, intent and drafts. Set keep to true, "
    "intent to an empty string and drafts to an empty array when the line stays unchanged. Otherwise set keep to false, "
    "give the intent of the change, and give one or more candidate English lines in drafts. Keep every {...} placeholder "
    "and every <text ...>...</> tag from the line's English, unchanged. Keep each English line's line breaks. Stay within "
    "120% of each English line's length. Refer to lines only by namespace and key."
)

TRANS_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "key": {"type": "string"},
                    "keep": {"type": "boolean"},
                    "intent": {"type": "string"},
                    "drafts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["namespace", "key", "keep", "intent", "drafts"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lines"],
    "additionalProperties": False,
}


def _target_dict(target: Target) -> dict[str, Any]:
    return {
        **_line_ref(target.line),
        "intent": target.intent,
        "callbacks": list(target.callbacks),
        "context": target.context,
    }


def draft(
    store: Store,
    lens: Lens,
    scene: Scene,
    targets: Sequence[Target],
    retry: str | None,
    ask: Ask,
    note: str | None = None,
) -> dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected:
    expected = sorted((target.line.namespace, target.line.key) for target in targets)
    parts = [
        DRAFT_INSTRUCTIONS,
        f"Intent rule:\n{lens.stage_text('intent')}",
        f"Scene lines:\n{_json(_line_dicts(scene.lines))}",
        f"Targets:\n{_json([_target_dict(target) for target in targets])}",
    ]
    if note is not None:
        parts.extend([DRAFT_NOTE_INSTRUCTIONS, f"Reviewer note:\n{note}"])
    if retry is not None:
        parts.append(f"The previous candidates failed the {retry} gate. Write candidates that pass it.")

    def call() -> list[dict[str, Any]]:
        answer = ask(claude.OPUS, parts, DRAFT_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        entries = answer["drafts"]
        if sorted((entry["namespace"], entry["key"]) for entry in entries) != expected:
            raise _Unanswered(Rejected(f"draft entries do not name each target of scene {scene.key} once"))
        return entries

    inputs = {
        **_inputs(scene),
        "targets": [_target_dict(target) for target in targets],
        "retry": retry,
    }
    if note is not None:
        inputs["note"] = note
    try:
        entries = store.run("draft", inputs, lens.stage_text("intent"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return {(entry["namespace"], entry["key"]): tuple(entry["drafts"]) for entry in entries}


def draft_speaker(
    store: Store,
    lens: Lens,
    groups: Sequence[tuple[Scene, Sequence[Target]]],
    ask: Ask,
) -> dict[tuple[str, str], tuple[str, ...]] | Unfinished | Rejected:
    expected = sorted((target.line.namespace, target.line.key) for _scene, targets in groups for target in targets)
    parts = [
        DRAFT_INSTRUCTIONS,
        SPEAKER_DRAFT_INSTRUCTIONS,
        f"Intent rule:\n{lens.stage_text('intent')}",
        "Scenes:\n"
        + _json(
            [
                {"scene": scene.key, "lines": _line_dicts(scene.lines), "targets": [_target_dict(target) for target in targets]}
                for scene, targets in groups
            ]
        ),
    ]

    def call() -> list[dict[str, Any]]:
        answer = ask(claude.OPUS, parts, DRAFT_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        entries = answer["drafts"]
        if sorted((entry["namespace"], entry["key"]) for entry in entries) != expected:
            raise _Unanswered(Rejected("draft entries do not name each target of the speaker's scenes once"))
        return entries

    inputs = {
        "scenes": [{**_inputs(scene), "targets": [_target_dict(target) for target in targets]} for scene, targets in groups],
    }
    try:
        entries = store.run("speaker_draft", inputs, lens.stage_text("intent"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return {(entry["namespace"], entry["key"]): tuple(entry["drafts"]) for entry in entries}


@dataclass(frozen=True)
class TransDraft:
    keep: bool
    intent: str
    drafts: tuple[str, ...]


def trans_draft(
    store: Store,
    lens: Lens,
    groups: Sequence[tuple[Scene, Sequence[SceneLine]]],
    ask: Ask,
) -> dict[tuple[str, str], TransDraft] | Unfinished | Rejected:
    expected = sorted((line.namespace, line.key) for _scene, lines in groups for line in lines)
    parts = [
        TRANS_DRAFT_INSTRUCTIONS,
        f"Intent rule:\n{lens.stage_text('intent')}",
        "Scenes:\n"
        + _json([{"scene": scene.key, "lines": _line_dicts(scene.lines), "speaker_lines": _line_dicts(lines)} for scene, lines in groups]),
    ]

    def call() -> list[dict[str, Any]]:
        answer = ask(claude.OPUS, parts, TRANS_DRAFT_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        entries = answer["lines"]
        if sorted((entry["namespace"], entry["key"]) for entry in entries) != expected:
            raise _Unanswered(Rejected("trans entries do not name each line of the speaker once"))
        for entry in entries:
            if entry["keep"] and entry["drafts"]:
                raise _Unanswered(Rejected("trans entry keeps a line but gives drafts for it"))
            if not entry["keep"] and not (entry["intent"] and entry["drafts"]):
                raise _Unanswered(Rejected("trans entry changes a line without an intent or drafts"))
        return entries

    inputs = {"scenes": [{**_inputs(scene), "targets": [_line_ref(line) for line in lines]} for scene, lines in groups]}
    try:
        entries = store.run("trans_draft", inputs, lens.stage_text("intent"), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
    return {
        (entry["namespace"], entry["key"]): TransDraft(keep=entry["keep"], intent=entry["intent"], drafts=tuple(entry["drafts"]))
        for entry in entries
    }


def render(store: Store, renderer: Renderer, speaker: str | None, text: str) -> str:
    def call() -> str:
        try:
            return renderer.render(speaker, text)
        except Exception as error:
            raise Fail(f"voice render failed ({type(error).__name__})") from error

    return store.run("render", {"speaker": speaker, "text": text}, None, call).value


JUDGE_INSTRUCTIONS = (
    "Judge a candidate rewrite of the target line. Return JSON with six booleans. meaning: the candidate keeps "
    "everything the judge rubric protects: the plot facts, who loves whom, the character traits and the beat. plot: "
    "the plot is unchanged. minimal_change: the candidate changes no more than it must; moving toward the Japanese "
    "line counts as minimal change. callback: every callback still holds. fixed: the candidate answers no to every "
    "line reason question. restores_japanese: the candidate restores the meaning of the Japanese line. The Japanese "
    "line is the meaning reference. The judge rubric overrides the Japanese line wherever they conflict. Refer to "
    "lines only by namespace and key."
)

JUDGE_VERDICTS = ("meaning", "plot", "minimal_change", "callback", "fixed", "restores_japanese")

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {verdict: {"type": "boolean"} for verdict in JUDGE_VERDICTS},
    "required": list(JUDGE_VERDICTS),
    "additionalProperties": False,
}


def _judge_lens_text(lens: Lens) -> str:
    return lens.stage_text("judge") + "\n" + lens.stage_text("triage")


def _reason_questions(lens: Lens, reasons: Sequence[str]) -> list[dict[str, str]]:
    return [{"id": question.id, "question": question.question} for question in lens.triage if question.id in reasons]


def judge(
    store: Store,
    lens: Lens,
    scene: Scene,
    line: SceneLine,
    text: str,
    callbacks: Sequence[Mapping[str, Any]],
    reasons: Sequence[str],
    intent: str,
    context: Mapping[str, Any],
    ask: Ask,
) -> Judgment | Unfinished | Rejected:
    parts = [
        JUDGE_INSTRUCTIONS,
        f"Judge rubric:\n{lens.stage_text('judge')}",
        f"Line reasons:\n{_json(_reason_questions(lens, reasons))}",
        f"Intent:\n{intent}",
        f"Target line:\n{_json({**_line_ref(line), 'english': line.english, 'japanese': line.japanese})}",
        f"Candidate:\n{text}",
        f"Callbacks:\n{_json(list(callbacks))}",
        f"Arc context:\n{_json(context)}",
        f"Scene lines:\n{_json(_line_dicts(scene.lines))}",
    ]

    def call() -> dict[str, bool]:
        answer = ask(claude.OPUS, parts, JUDGE_SCHEMA)
        if isinstance(answer, Unfinished | Rejected):
            raise _Unanswered(answer)
        return asdict(Judgment(**answer))

    inputs = {
        **_inputs(scene),
        "line": _line_ref(line),
        "text": text,
        "callbacks": list(callbacks),
        "reasons": list(reasons),
        "intent": intent,
        "context": context,
    }
    try:
        return Judgment(**store.run("judge", inputs, _judge_lens_text(lens), call).value)
    except _Unanswered as unanswered:
        return unanswered.failure


CROSS_INSTRUCTIONS = (
    "Decide whether the candidate rewrite of the target line stays consistent with every callback and with the "
    "related lines, which may come from any scene. Follow the judge rubric. The line reasons and the intent say what "
    "the rewrite must do. Return JSON with consistent, true only if the candidate is consistent with all of them. "
    "Refer to lines only by namespace and key."
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
    reasons: Sequence[str],
    intent: str,
    context: Mapping[str, Any],
    ask: Ask,
) -> bool | Unfinished | Rejected:
    parts = [
        CROSS_INSTRUCTIONS,
        f"Judge rubric:\n{lens.stage_text('judge')}",
        f"Line reasons:\n{_json(_reason_questions(lens, reasons))}",
        f"Intent:\n{intent}",
        f"Target line:\n{_json({**_line_ref(line), 'english': line.english})}",
        f"Candidate:\n{text}",
        f"Callbacks:\n{_json(list(callbacks))}",
        f"Arc context:\n{_json(context)}",
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
        "reasons": list(reasons),
        "intent": intent,
        "context": context,
    }
    try:
        return store.run("cross_judge", inputs, _judge_lens_text(lens), call).value
    except _Unanswered as unanswered:
        return unanswered.failure
