import re
from pathlib import Path

import pytest

from ffrd.pipeline.lens import Lens, Stage, TriageQuestion, load_lens

LENS = Path(__file__).resolve().parents[2] / "lenses" / "unstraightening.toml"
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
