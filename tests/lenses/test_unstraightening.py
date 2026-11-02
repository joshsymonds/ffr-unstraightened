import hashlib
import re
from pathlib import Path

import pytest

from ffrd.pipeline.lens import Lens, SceneSection, Stage, TriageQuestion, load_lens
from ffrd.pipeline.scene_record import SCENE_KEEP_REASONS

LENS = Path(__file__).resolve().parents[2] / "lenses" / "unstraightening.toml"
DOC = Path(__file__).resolve().parents[2] / "docs" / "lenses" / "unstraightening.md"
LABEL = re.compile(r"\[([a-z0-9]+(?:-[a-z0-9]+)*)\]")
U4 = "u4-stake-through-other"

# DL23's seven triage questions, verbatim, in id order u1..u7.
TRIAGE: tuple[TriageQuestion, ...] = (
    TriageQuestion(
        id="u1-presumed-opposite-sex",
        question="A character in this scene assumes that another character's partner or future partner is of the opposite sex, or treats closeness between two characters of the same sex as a joke, an insult or a threat.",
    ),
    TriageQuestion(
        id="u2-romance-outranks",
        question="A character in this scene says or implies that a romantic feeling matters more than a duty, a friendship, their own life, or the fate of other people.",
    ),
    TriageQuestion(
        id="u3-devotion-as-vow",
        question="A character in this scene describes devotion to a romantic partner as a sacred vow, fate, or giving up their own self.",
    ),
    TriageQuestion(
        id="u4-stake-through-other",
        question="A character in this scene explains their own goal, fear or worth in terms of what another specific character thinks of them or needs from them.",
    ),
    TriageQuestion(
        id="u5-rivalry-over-one-person",
        question="Two characters in this scene compete or quarrel over the affection or attention of one other character.",
    ),
    TriageQuestion(
        id="u6-overwrought-declaration",
        question="A character in this scene declares romantic love or longing in extreme, breathless or life-or-death terms.",
    ),
    TriageQuestion(
        id="u7-gender-rule",
        question="A line in this scene states what men or women may do, must do, or are like.",
    ),
)

RULES: tuple[str, ...] = tuple(q.id for q in TRIAGE)
PROTECTIONS: tuple[str, ...] = (
    "protect-love-and-couples",
    "protect-mutual-support",
    "protect-plot",
    "protect-traits",
    "protect-operatic-stakes",
    "protect-earned-declarations",
    "protect-affectionate-teasing",
)
EXCLUSIONS: tuple[str, ...] = ("exclude-queer-additions", "exclude-clumsy-lines")

BOUND_STAGES: tuple[Stage, ...] = ("screen", "intent", "judge")
# The label table: which bracketed labels each stage's text must carry.
BINDING: dict[Stage, frozenset[str]] = {
    "screen": frozenset(
        {
            *RULES,
            "first-person-desire",
            "romantic-partner-only",
            "gender-rule-narrow",
            "romance-as-default",
            "all-characters",
            "act-on-clear-hits",
            *PROTECTIONS,
            *EXCLUSIONS,
        }
    ),
    "intent": frozenset(
        {
            *RULES,
            "earned",
            "no-invented-motive",
            "act-on-clear-hits",
            "minimal-fix",
            "leave-for-plot",
            *PROTECTIONS,
            *EXCLUSIONS,
        }
    ),
    "judge": frozenset(
        {
            *RULES,
            "earned",
            "first-person-desire",
            "romantic-partner-only",
            "no-invented-motive",
            "gender-rule-narrow",
            "minimal-fix",
            "leave-for-plot",
            *PROTECTIONS,
            *EXCLUSIONS,
        }
    ),
}


@pytest.fixture
def lens() -> Lens:
    return load_lens(LENS)


def labels(text: str) -> set[str]:
    return set(LABEL.findall(text))


def test_triage_is_the_seven_dl23_questions_in_order(lens: Lens) -> None:
    assert lens.triage == TRIAGE


@pytest.mark.parametrize("stage", BOUND_STAGES)
def test_stage_carries_exactly_its_bound_labels(lens: Lens, stage: Stage) -> None:
    assert labels(lens.stage_text(stage)) == BINDING[stage]


@pytest.mark.parametrize("rule", RULES)
def test_rule_is_carried_by_each_stage_it_is_bound_to(lens: Lens, rule: str) -> None:
    carrying = {stage for stage in BOUND_STAGES if rule in labels(lens.stage_text(stage))}
    assert carrying == {stage for stage in BOUND_STAGES if rule in BINDING[stage]}


def labelled(lens: Lens, stage: Stage, label: str) -> list[str]:
    """The paragraphs of a stage's text that start with the given label."""
    match stage:
        case "screen":
            paragraphs = list(lens.screen_criteria)
        case "intent":
            paragraphs = lens.intent_rule.split("\n\n")
        case _:
            paragraphs = lens.judge_rubric.split("\n\n")
    return [p.strip() for p in paragraphs if p.strip().startswith(f"[{label}]")]


@pytest.mark.parametrize("stage", BOUND_STAGES)
def test_u4_is_a_womans_stake_routed_through_a_man(lens: Lens, stage: Stage) -> None:
    (text,) = labelled(lens, stage, U4)
    assert re.search(r"\bwoman\b", text)
    assert re.search(r"\bman\b", text)


@pytest.mark.parametrize("stage", BOUND_STAGES)
def test_mutual_support_wins_over_every_rule(lens: Lens, stage: Stage) -> None:
    (text,) = labelled(lens, stage, "protect-mutual-support")
    assert "every rule" in text
    assert U4 in text


U7_LABELS: tuple[tuple[Stage, str], ...] = tuple(
    (stage, label) for stage in BOUND_STAGES for label in ("u7-gender-rule", "gender-rule-narrow") if label in BINDING[stage]
)


@pytest.mark.parametrize(("stage", "label"), U7_LABELS)
def test_u7_covers_a_plainly_implied_gender_rule(lens: Lens, stage: Stage, label: str) -> None:
    (text,) = labelled(lens, stage, label)
    assert "plainly implies" in text


# The scene pass: the [scene] section, its labels, and the doc page that describes it.
SCENE_PATTERNS: tuple[str, ...] = ("s1-manhood-performance", "s2-conquest-fixation", "s3-bechdel-fail")
SCENE_CRITERIA: frozenset[str] = frozenset(
    {
        *SCENE_PATTERNS,
        "recurring",
        "own-stance",
        "presence-not-hit",
        "plot-actor-exempt",
        "friendship-exempt",
        "flirt-trait",
        "coaching",
        "minor-flirt",
        "rejection-gag",
        "own-goal-exempt",
    }
)
SCENE_PLAN: frozenset[str] = frozenset(
    {
        "plan-budget",
        "required-optional",
        "line-pass-final",
        "keep-facts",
        "no-invented-topic",
        "named-keep",
        "cut-density",
        "her-own-line",
    }
)
SCENE_RUBRIC: frozenset[str] = frozenset({"reduce-pattern", "keep-flirt-beat", "protections-hold", "no-eye-roll-fill"})
SCENE_LABELS: frozenset[str] = SCENE_CRITERIA | SCENE_PLAN | SCENE_RUBRIC
# The words each label's paragraph must use to state its rule.
CRITERION_PHRASES: tuple[tuple[str, str], ...] = (
    ("s1-manhood-performance", "insufficiently manly"),
    ("s3-bechdel-fail", "mainly about"),
    ("s3-bechdel-fail", "never by one line"),
    ("friendship-exempt", "Male friendship"),
    ("friendship-exempt", "affection"),
    ("friendship-exempt", "camaraderie"),
    ("friendship-exempt", "brotherhood"),
    ("flirt-trait", "characterization"),
    ("coaching", "how to get women"),
    ("minor-flirt", "a minor"),
    ("minor-flirt", "child-looking"),
    ("minor-flirt", "an s2 hit"),
    ("minor-flirt", "without recurring"),
    ("rejection-gag", "slapstick rejection"),
    ("rejection-gag", "only part"),
    ("own-goal-exempt", "her own goal"),
)
PLAN_PHRASES: tuple[tuple[str, str], ...] = (
    ("cut-density", "keep the trait"),
    ("cut-density", "cut density"),
    ("her-own-line", "drawn from the scene's existing content"),
    ("her-own-line", "never a new topic"),
    ("named-keep", "not-recurring never keeps a minor-flirt hit"),
)
RUBRIC_PHRASES: tuple[tuple[str, str], ...] = (
    ("no-eye-roll-fill", "Women Are Wiser"),
    ("no-eye-roll-fill", "eye-rolling"),
)
# The words each pattern question must use to state its refined trigger.
QUESTION_PHRASES: tuple[tuple[str, str], ...] = (
    ("s1-manhood-performance", "insufficiently manly"),
    ("s1-manhood-performance", "recurs"),
    ("s2-conquest-fixation", "coach"),
    ("s2-conquest-fixation", "minor"),
    ("s2-conquest-fixation", "child-looking"),
    ("s2-conquest-fixation", "slapstick rejection"),
    ("s3-bechdel-fail", "mainly about"),
)
# SHA-256 of each existing stage_text at the base commit. The scene pass must not change them.
PINNED_SHA256: dict[Stage, str] = {
    "triage": "881bf79ed55b88ee4321b7950b9f9d8bbd95c3779dcbf8469a8e5f4ef30de2e4",
    "screen": "d26bdd84896a642b871557a0178afabaf827502d5b9285cdfccace590842f036",
    "intent": "e7133724bd7fbca5bdc8cd47e64fb3f07e61ab5fb42ab31177830b5b7b721fab",
    "judge": "8ace1d38978261cd4757a01c4b7f25dd229224c8e47da0e3a2b9786a86a93e84",
}


@pytest.fixture
def scene(lens: Lens) -> SceneSection:
    assert lens.scene is not None, "the lens has no [scene] section"
    return lens.scene


def leading_label(paragraph: str) -> str:
    match = re.match(r"\[([a-z0-9]+(?:-[a-z0-9]+)*)\]", paragraph)
    assert match is not None, "paragraph must start with a [label]"
    return match.group(1)


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def scene_paragraph(text: str, label: str) -> str:
    (found,) = [p for p in paragraphs(text) if p.startswith(f"[{label}]")]
    return found


def criterion(scene: SceneSection, label: str) -> str:
    (found,) = [c for c in scene.criteria if c.startswith(f"[{label}]")]
    return found


def pattern_question(scene: SceneSection, pattern_id: str) -> str:
    (found,) = [p.question for p in scene.patterns if p.id == pattern_id]
    return found


@pytest.mark.parametrize("stage", tuple(PINNED_SHA256))
def test_existing_stage_texts_are_unchanged(lens: Lens, stage: Stage) -> None:
    digest = hashlib.sha256(lens.stage_text(stage).encode("utf-8")).hexdigest()
    assert digest == PINNED_SHA256[stage]


def test_scene_patterns_are_the_three_in_order(scene: SceneSection) -> None:
    assert tuple(p.id for p in scene.patterns) == SCENE_PATTERNS


def test_scene_pattern_questions_are_phrased_like_triage(scene: SceneSection) -> None:
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


def test_plan_budget_says_four(scene: SceneSection) -> None:
    assert re.search(r"\b4\b", scene_paragraph(scene.plan, "plan-budget"))


@pytest.mark.parametrize("reason", SCENE_KEEP_REASONS)
def test_named_keep_names_every_keep_reason(scene: SceneSection, reason: str) -> None:
    text = scene_paragraph(scene.plan, "named-keep")
    assert re.search(rf"(?<![\w-]){re.escape(reason)}(?![\w-])", text)


@pytest.mark.parametrize("protection", (*PROTECTIONS, *EXCLUSIONS))
def test_protections_hold_names_each_protection_unbracketed(scene: SceneSection, protection: str) -> None:
    text = scene_paragraph(scene.rubric, "protections-hold")
    assert re.search(rf"(?<![\[\w-]){re.escape(protection)}(?![\]\w-])", text)


def test_doc_has_a_scene_pass_section() -> None:
    assert "\n## Scene pass\n" in DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("label", sorted(SCENE_LABELS))
def test_scene_label_appears_in_doc_in_backticks(label: str) -> None:
    assert f"`{label}`" in DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize(("pattern_id", "phrase"), QUESTION_PHRASES)
def test_scene_pattern_question_states_its_refined_trigger(scene: SceneSection, pattern_id: str, phrase: str) -> None:
    assert phrase in pattern_question(scene, pattern_id)


@pytest.mark.parametrize(("label", "phrase"), CRITERION_PHRASES)
def test_scene_criterion_states_its_rule(scene: SceneSection, label: str, phrase: str) -> None:
    assert phrase in criterion(scene, label)


@pytest.mark.parametrize(("label", "phrase"), PLAN_PHRASES)
def test_scene_plan_paragraph_states_its_rule(scene: SceneSection, label: str, phrase: str) -> None:
    assert phrase in scene_paragraph(scene.plan, label)


@pytest.mark.parametrize(("label", "phrase"), RUBRIC_PHRASES)
def test_scene_rubric_paragraph_states_its_rule(scene: SceneSection, label: str, phrase: str) -> None:
    assert phrase in scene_paragraph(scene.rubric, label)
