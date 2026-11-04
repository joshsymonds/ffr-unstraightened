import re
import tomllib
from pathlib import Path

import pytest

from ffrd.pipeline.claude import OPUS
from ffrd.pipeline.lens import Lens, ScenePattern, SceneSection, Stage, TriageQuestion, load_lens
from ffrd.pipeline.scene_record import SCENE_KEEP_REASONS
from ffrd.pipeline.stages import KEEP_REASONS

LENS = Path(__file__).resolve().parents[2] / "lenses" / "panic.toml"
LABEL = re.compile(r"\[([a-z0-9]+(?:-[a-z0-9]+)*)\]")

# The three triage questions, verbatim, in the order the lens lists them.
TRIAGE: tuple[TriageQuestion, ...] = (
    TriageQuestion(
        id="p1-panic",
        question="A character in this scene reacts with panic, disgust, fear or ridicule to same-sex attraction or advance, or to someone being gay, bi or trans, whether the moment is played for laughs, for sympathy or straight.",
    ),
    TriageQuestion(
        id="p2-mistaken-identity",
        question="A character in this scene panics on finding out, or fearing, that someone they flirted with, kissed or were drawn to is the same sex or trans.",
    ),
    TriageQuestion(
        id="p3-crossdressing-gag",
        question="A man in women's clothes, a woman in men's clothes, or a gender-ambiguous character in this scene is made a joke, a trick or a \"trap\".",
    ),
)

# The scene pattern, verbatim.
SCENE_PATTERNS: tuple[ScenePattern, ...] = (
    ScenePattern(
        id="v1-queer-coded-menace",
        question="A villain or threat in this scene is made menacing through queer coding: camp or effeminate mannerisms used as menace, predatory same-sex interest, or gender ambiguity framed as creepy.",
    ),
)

TRIAGE_IDS: frozenset[str] = frozenset(q.id for q in TRIAGE)
SCREEN: frozenset[str] = frozenset(
    {
        "p1-panic",
        "p2-mistaken-identity",
        "p3-crossdressing-gag",
        "on-screen-at-all",
        "neutral-presence",
        "villain-coding-is-scene",
    }
)
INTENT: frozenset[str] = frozenset(
    {
        "act-on-clear-hits",
        "minimal-fix",
        "p1-panic",
        "p2-mistaken-identity",
        "p3-crossdressing-gag",
        "condemned-in-scene",
        "protect-queer-presence",
        "protect-villainy",
        "protect-plot",
        "keep-voice",
        "no-new-detail",
        "no-explaining",
    }
)
JUDGE: frozenset[str] = (INTENT - {"act-on-clear-hits"}) | {"meaning", "fixed", "keep-point"}
BOUND_STAGES: tuple[Stage, ...] = ("screen", "intent", "judge")
BINDING: dict[Stage, frozenset[str]] = {"screen": SCREEN, "intent": INTENT, "judge": JUDGE}

SCENE_CRITERIA: frozenset[str] = frozenset({"v1-queer-coded-menace", "menace-not-presence", "scene-not-line"})
SCENE_PLAN: frozenset[str] = frozenset(
    {"plan-budget", "carries-pattern", "line-pass-final", "keep-villainy", "keep-queerness", "no-invented-topic", "named-keep"}
)
SCENE_RUBRIC: frozenset[str] = frozenset(
    {
        "reduce-pattern",
        "keep-villainy",
        "keep-queerness",
        "protections-hold",
        "carries-pattern",
        "no-added-content",
        "grammatical",
        "keeps-subject",
        "no-new-detail",
        "not-a-no-op",
    }
)

# The keep list the intent stage may name, and the reasons the scene pass may name.
KEEP_LIST: tuple[str, ...] = ("no-hit", "condemned-in-scene", "protection", "plot")
NAMED_KEEP: tuple[str, ...] = ("protection", "plot")

# Each pinned phrase must appear in the paragraph that starts with its label.
PINNED_PHRASES: tuple[tuple[Stage, str, str], ...] = (
    (
        "screen",
        "p1-panic",
        "Flag p1-panic when a character reacts with panic, disgust, fear or ridicule to same-sex attraction or advance, or to someone being gay, bi or trans.",
    ),
    ("screen", "p1-panic", "This holds whether the moment is played for laughs, for sympathy or straight."),
    ("screen", "p2-mistaken-identity", "Flag p2-mistaken-identity when a character panics on finding out, or fearing"),
    (
        "screen",
        "p3-crossdressing-gag",
        "Flag p3-crossdressing-gag when a man in women's clothes, a woman in men's clothes, or a gender-ambiguous character is made a joke",
    ),
    ("screen", "p3-crossdressing-gag", "Clothes or an ambiguous gender the scene treats with respect is never a hit."),
    ("screen", "on-screen-at-all", "Flag panic shown at all, including when the scene condemns it."),
    ("screen", "on-screen-at-all", "The intent stage decides keeps"),
    ("screen", "neutral-presence", "Never flag neutral or positive queer presence, including a camp but harmless character."),
    ("screen", "villain-coding-is-scene", "Queer-coded villain menace is the scene pattern's job"),
    ("screen", "villain-coding-is-scene", "A character's fear of a villain's threat or predation is not p1-panic"),
    ("intent", "act-on-clear-hits", "Act only on a clear hit."),
    ("intent", "act-on-clear-hits", "Keep a line unchanged only for a reason from this keep list"),
    ("intent", "minimal-fix", "Change only the words the edit needs."),
    ("intent", "p1-panic", "For a p1-panic line, remove the panic and keep the beat."),
    ("intent", "p2-mistaken-identity", "For a p2-mistaken-identity line, remove the panic and keep the beat."),
    ("intent", "p2-mistaken-identity", "A disguise the plot needs stays."),
    ("intent", "p2-mistaken-identity", "The discovery stays in the scene, and the panic or fear at finding out"),
    ("intent", "p3-crossdressing-gag", "For a p3-crossdressing-gag line, remove the gag and keep the beat."),
    ("intent", "p3-crossdressing-gag", "A disguise the plot needs stays."),
    ("intent", "condemned-in-scene", "keep the line unchanged as condemned-in-scene"),
    ("intent", "protect-queer-presence", "Never make anyone straight-acting or less queer"),
    ("intent", "protect-villainy", "A villain keeps villainy, motive and plot role."),
    ("intent", "protect-plot", "Keep every plot fact: what happens, who does what, and the decisions made."),
    ("intent", "keep-voice", "Keep the speaker's voice"),
    ("intent", "no-new-detail", "Add no name, object or motive the original line lacks."),
    ("intent", "no-explaining", "Do not explain the change in the line, and do not add a tokenistic detail."),
    ("judge", "fixed", "A rewrite with no change to the panic or gag is not fixed."),
    ("judge", "keep-point", "Removing the panic or gag never removes the point."),
)

# Each pinned scene phrase must appear in the paragraph of its part that starts with its label.
SCENE_PHRASES: tuple[tuple[str, str, str], ...] = (
    (
        "criteria",
        "v1-queer-coded-menace",
        "Flag v1-queer-coded-menace for the scene when a villain or threat is made menacing through queer coding",
    ),
    ("criteria", "menace-not-presence", "Camp but harmless characters are not the pattern"),
    ("criteria", "menace-not-presence", "queer villains whose menace does not rest on queerness are not the pattern"),
    ("criteria", "scene-not-line", "never by a single line"),
    ("plan", "plan-budget", "Plan at most 4 changed lines per conversation."),
    ("plan", "keep-villainy", "villainy, motive and plot role"),
    ("plan", "keep-queerness", "the villain's queerness stays, and only its use as the source of menace changes"),
    ("rubric", "keep-queerness", "Removing queer coding's use as the source of menace is the fix, not a loss of queerness."),
    ("plan", "named-keep", "name one reason from exactly these two: protection or plot."),
    ("plan", "named-keep", "A scene's importance to the story is never a reason to keep it."),
    ("rubric", "keep-villainy", "Reject a plan that removes or softens the villain's villainy, motive or plot role"),
    ("rubric", "protections-hold", "protect-villainy, protect-queer-presence and protect-plot"),
)


@pytest.fixture
def lens() -> Lens:
    return load_lens(LENS)


@pytest.fixture
def scene(lens: Lens) -> SceneSection:
    assert lens.scene is not None
    return lens.scene


def labels(text: str) -> set[str]:
    return set(LABEL.findall(text))


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def leading_label(paragraph: str) -> str:
    match = LABEL.match(paragraph)
    assert match is not None
    return match.group(1)


def labelled(lens: Lens, stage: Stage, label: str) -> list[str]:
    """The paragraphs of a stage's text that start with the given label."""
    match stage:
        case "screen":
            found = list(lens.screen_criteria)
        case "intent":
            found = paragraphs(lens.intent_rule)
        case _:
            found = paragraphs(lens.judge_rubric)
    return [p for p in found if p.startswith(f"[{label}]")]


def scene_paragraphs(scene: SceneSection, part: str) -> list[str]:
    match part:
        case "criteria":
            return list(scene.criteria)
        case "plan":
            return paragraphs(scene.plan)
        case _:
            return paragraphs(scene.rubric)


def scene_labelled(scene: SceneSection, part: str, label: str) -> list[str]:
    return [p for p in scene_paragraphs(scene, part) if p.startswith(f"[{label}]")]


def mentions(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text) is not None


def test_lens_loads_with_its_base_leave_out_and_opus_screen(lens: Lens) -> None:
    assert lens.name == "panic"
    assert lens.base == "unstraightening"
    assert lens.leave_out == ("queering",)
    assert lens.selection is None
    assert lens.screen_model == OPUS
    assert lens.scene is not None


def test_declarations_are_the_only_top_level_keys_before_the_first_table() -> None:
    head = LENS.read_text(encoding="utf-8").split("[[triage]]", 1)[0]
    assert tomllib.loads(head) == {"base": "unstraightening", "leave_out": ["queering"]}


def test_screen_model_is_opus() -> None:
    data = tomllib.loads(LENS.read_text(encoding="utf-8"))
    assert data["screen"]["model"] == "opus"


def test_triage_is_the_three_questions_in_order(lens: Lens) -> None:
    assert lens.triage == TRIAGE


@pytest.mark.parametrize("stage", BOUND_STAGES)
def test_stage_carries_exactly_its_bound_labels(lens: Lens, stage: Stage) -> None:
    assert labels(lens.stage_text(stage)) == BINDING[stage]


@pytest.mark.parametrize(("stage", "label", "phrase"), PINNED_PHRASES)
def test_pinned_phrase_is_in_the_paragraph_that_starts_with_its_label(lens: Lens, stage: Stage, label: str, phrase: str) -> None:
    (paragraph,) = labelled(lens, stage, label)
    assert phrase in paragraph


@pytest.mark.parametrize("label", sorted(JUDGE))
def test_judge_paragraph_says_what_to_reject(lens: Lens, label: str) -> None:
    (paragraph,) = labelled(lens, "judge", label)
    assert "Reject a rewrite that " in paragraph


@pytest.mark.parametrize("reason", KEEP_LIST)
def test_every_intent_keep_reason_is_in_keep_reasons(reason: str) -> None:
    assert reason in KEEP_REASONS


def test_intent_keep_list_names_exactly_its_four_reasons(lens: Lens) -> None:
    (paragraph,) = labelled(lens, "intent", "act-on-clear-hits")
    assert {reason for reason in KEEP_REASONS if mentions(paragraph, reason)} == set(KEEP_LIST)


def test_scene_patterns_are_the_one_pattern_verbatim(scene: SceneSection) -> None:
    assert scene.patterns == SCENE_PATTERNS


def test_scene_pattern_question_is_phrased_like_triage(scene: SceneSection) -> None:
    for pattern in scene.patterns:
        assert "in this scene" in pattern.question


def test_scene_criteria_carry_exactly_their_labels(scene: SceneSection) -> None:
    assert sorted(leading_label(c) for c in scene.criteria) == sorted(SCENE_CRITERIA)
    assert labels("\n".join(scene.criteria)) == SCENE_CRITERIA


def test_scene_plan_carries_exactly_its_labels(scene: SceneSection) -> None:
    assert sorted(leading_label(p) for p in paragraphs(scene.plan)) == sorted(SCENE_PLAN)
    assert labels(scene.plan) == SCENE_PLAN


def test_scene_rubric_carries_exactly_its_labels(scene: SceneSection) -> None:
    assert sorted(leading_label(p) for p in paragraphs(scene.rubric)) == sorted(SCENE_RUBRIC)
    assert labels(scene.rubric) == SCENE_RUBRIC


def test_scene_rubric_paragraphs_each_say_what_to_reject(scene: SceneSection) -> None:
    for paragraph in paragraphs(scene.rubric):
        assert "Reject a plan that " in paragraph or "Reject a rewrite that " in paragraph


@pytest.mark.parametrize(("part", "label", "phrase"), SCENE_PHRASES)
def test_scene_paragraph_carries_its_pinned_phrase(scene: SceneSection, part: str, label: str, phrase: str) -> None:
    (paragraph,) = scene_labelled(scene, part, label)
    assert phrase in paragraph


def test_plan_budget_says_four(scene: SceneSection) -> None:
    (paragraph,) = scene_labelled(scene, "plan", "plan-budget")
    assert re.search(r"\b4\b", paragraph)


@pytest.mark.parametrize("reason", NAMED_KEEP)
def test_named_keep_reason_is_a_scene_keep_reason(reason: str) -> None:
    assert reason in SCENE_KEEP_REASONS


def test_named_keep_names_exactly_protection_and_plot(scene: SceneSection) -> None:
    (paragraph,) = scene_labelled(scene, "plan", "named-keep")
    assert {reason for reason in SCENE_KEEP_REASONS if mentions(paragraph, reason)} == set(NAMED_KEEP)


def test_named_keep_never_names_condemned_in_scene(scene: SceneSection) -> None:
    (paragraph,) = scene_labelled(scene, "plan", "named-keep")
    assert "condemned" not in paragraph
