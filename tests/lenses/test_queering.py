import re
import tomllib
from pathlib import Path

import pytest

from ffrd.pipeline.lens import Lens, Stage, TriageQuestion, load_lens

LENS = Path(__file__).resolve().parents[2] / "lenses" / "queering.toml"
DOC = Path(__file__).resolve().parents[2] / "docs" / "lenses" / "queering.md"
LABEL = re.compile(r"\[([a-z0-9]+(?:-[a-z0-9]+)*)\]")

# The three triage questions, verbatim, in the order the lens lists them.
TRIAGE: tuple[TriageQuestion, ...] = (
    TriageQuestion(
        id="romance",
        question="A character outside the core cast in this scene refers to their own partner or a romantic interest of any sort: a partner, a crush, someone they are attracted to, flirting or romantic pursuit.",
    ),
    TriageQuestion(
        id="gender-roles",
        question="A line in this scene is about what men or women are, may do or should do.",
    ),
    TriageQuestion(
        id="flirt-at-woman",
        question="The party member whose running gag is flirting aims a running-gag flirt in this scene only at a woman or at women.",
    ),
)

TRIAGE_IDS: frozenset[str] = frozenset(q.id for q in TRIAGE)
SCREEN: frozenset[str] = frozenset({"romance", "gender-roles", "not-core-cast"})
INTENT: frozenset[str] = frozenset(
    {
        "minimal-fix",
        "same-partner",
        "queer-bucket",
        "trans-bucket",
        "already-queer",
        "flirt-redirect",
        "generalize-when-reaction",
        "protect-sincere-thread",
        "protect-minor-target",
        "protect-core-cast",
        "no-gender-joke-or-menace",
        "no-explaining",
        "keep-voice",
        "no-new-detail",
        "no-contradiction",
        "protect-plot",
    }
)
JUDGE: frozenset[str] = INTENT | {"meaning"}
BOUND_STAGES: tuple[Stage, ...] = ("screen", "intent", "judge")
# The label table: which bracketed labels each stage's text must carry.
BINDING: dict[Stage, frozenset[str]] = {"screen": SCREEN, "intent": INTENT, "judge": JUDGE}

# Each pinned phrase must appear in the paragraph that starts with its label.
PINNED_PHRASES: tuple[tuple[Stage, str, str], ...] = (
    (
        "screen",
        "romance",
        "Flag romance when a character outside the core cast refers to their own partner or a romantic interest of any sort",
    ),
    ("screen", "romance", "Any character outside the core cast qualifies, a bigger recurring one as much as a passer-by."),
    ("screen", "romance", "Flag only the speaker's own lines."),
    ("screen", "gender-roles", "Flag gender-roles when a line is about what men or women are, may do or should do."),
    ("screen", "gender-roles", "Tracking only: a gender-roles flag never selects a speaker or a line."),
    ("screen", "not-core-cast", "Never flag a core-cast line, with one exception"),
    ("screen", "not-core-cast", '"flirt": true'),
    ("screen", "not-core-cast", "Flag the flirt's running-gag flirts aimed only at a woman or women under flirt-at-woman."),
    ("screen", "not-core-cast", "Never flag the flirt's sincere thread."),
    ("screen", "not-core-cast", "Never flag a flirt aimed at a minor or a child-looking character."),
    ("intent", "minimal-fix", "Change only the words the edit needs"),
    ("intent", "same-partner", "the same partner in every line"),
    ("intent", "queer-bucket", "the speaker's romantic target becomes the same sex"),
    ("intent", "queer-bucket", "Swap the gender of the partner or crush so the pairing is same-sex"),
    ("intent", "queer-bucket", "For a man or woman speaker, the pursuit target changes gender too."),
    ("intent", "queer-bucket", "Where the line can carry both a same-sex and an opposite-sex reading, a bi reading is fine."),
    ("intent", "queer-bucket", "Keep everything else"),
    ("intent", "trans-bucket", "For a trans pick, every line of the speaker is drafted together."),
    ("intent", "trans-bucket", "Where a line naturally allows it, add a light, natural trans detail:"),
    ("intent", "trans-bucket", "Where it does not, keep the line unchanged as no-hit."),
    ("intent", "trans-bucket", "The detail is never forced."),
    ("intent", "trans-bucket", "a detail that the speaker is trans, or, on a romance line, a nonbinary partner who uses they/them"),
    ("intent", "trans-bucket", 'Never write a "she\'s trans" aside.'),
    ("intent", "already-queer", "A line whose pairing is already same-sex stays as written"),
    ("intent", "already-queer", "a line whose speaker, partner or crush is already trans or nonbinary"),
    ("intent", "already-queer", "Keep these lines unchanged as no-hit."),
    ("intent", "flirt-redirect", "decided before drafting, by key hash"),
    ("intent", "flirt-redirect", "redirect the flirt to a man present in the scene"),
    ("intent", "generalize-when-reaction", "When the woman's reaction is in the same scene"),
    ("intent", "generalize-when-reaction", "generalize the address instead, for example to all genders"),
    ("intent", "protect-sincere-thread", "Never touch the flirt's sincere thread."),
    ("intent", "protect-minor-target", "Never touch a flirt aimed at a minor or a child-looking character."),
    (
        "intent",
        "protect-core-cast",
        "Do not change the core cast's sexuality or romances beyond the flirt's split.",
    ),
    ("intent", "no-gender-joke-or-menace", "The queerness is never the joke or the threat."),
    ("judge", "meaning", "meaning protects the bond, not the gender"),
    (
        "judge",
        "no-gender-joke-or-menace",
        "Reject a rewrite whose humor or menace rests on the target's or partner's gender or queerness.",
    ),
    ("judge", "no-explaining", "Reject a rewrite that explains the change or adds a tokenistic detail."),
    ("judge", "keep-voice", "Reject a rewrite that breaks the speaker's voice."),
    (
        "judge",
        "no-new-detail",
        "Reject a rewrite that adds a name, object or motive the original line lacks.",
    ),
    ("judge", "no-contradiction", "Reject a rewrite that contradicts the speaker's other lines."),
    ("judge", "minimal-fix", "Reject a rewrite that changes more than its edit needs."),
    ("judge", "queer-bucket", "Reject a rewrite that changes more than the target's gender"),
    ("judge", "queer-bucket", "unless the line carries a bi reading"),
    ("judge", "trans-bucket", 'Reject a "she\'s trans" aside.'),
    ("judge", "trans-bucket", "Reject a rewrite that forces a trans detail the line does not naturally allow."),
    (
        "judge",
        "trans-bucket",
        "is the bucket's edit: a detail that the speaker is trans, or a nonbinary partner who uses they/them on a romance line",
    ),
    ("judge", "already-queer", "Reject a rewrite that changes a line whose pairing is already same-sex"),
    ("judge", "already-queer", "or whose speaker, partner or crush is already trans or nonbinary."),
    ("judge", "flirt-redirect", "unless generalize-when-reaction applies"),
)

# Judge rules that each carry a "Reject a rewrite that ..." sentence for their rule.
REJECT_RULES: tuple[str, ...] = (
    "same-partner",
    "queer-bucket",
    "trans-bucket",
    "already-queer",
    "flirt-redirect",
    "generalize-when-reaction",
    "protect-sincere-thread",
    "protect-minor-target",
    "protect-core-cast",
    "protect-plot",
)

DOC_SECTIONS: tuple[str, ...] = (
    "## Aims",
    "## The marker",
    "## Buckets",
    "## The flirt",
    "## Judge rubric",
    "## What the lens never does",
    "## How it is checked",
)
DOC_SENTENCES: tuple[str, ...] = (
    "The queerness is never the joke or the threat.",
    'The trans bucket never adds a "she\'s trans" aside.',
    "The core cast's sexuality and romances stay as written, except the flirt's split.",
    "The lens chains onto the unstraightening rewrites: it starts from their output in `dialog/unstraightening/`, and the build applies the lenses in order.",
    "The lens runs under the queering selection: an Opus marker, a seeded refill selection of the romance characters as queer and the eligible characters as trans, one draft call per pick, and no Jev.",
)
BUCKET_PHRASES: tuple[str, ...] = (
    "queer_share",
    "trans_share",
    "round(queer_share% of the romance pool",
    "round(trans_share% of every eligible character)",
    "refilled from the order until the target or the pool runs out",
    "gender-roles marks never select",
)
HOW_IT_IS_CHECKED: tuple[str, ...] = (
    "used only for counts",
    "no Jev recheck",
    "reads the lines of characters outside the core cast who are not antagonists, and the flirt's",
)


@pytest.fixture
def lens() -> Lens:
    return load_lens(LENS)


def labels(text: str) -> set[str]:
    return set(LABEL.findall(text))


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


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_lens_loads_without_a_scene_section(lens: Lens) -> None:
    assert lens.name == "queering"
    assert lens.scene is None


def test_lens_declares_its_base_and_selection(lens: Lens) -> None:
    assert lens.base == "unstraightening"
    assert lens.selection == "queering"


def test_lens_declares_its_shares(lens: Lens) -> None:
    assert lens.queer_share == 15
    assert lens.trans_share == 5


def test_declarations_are_the_only_top_level_keys_before_the_first_table() -> None:
    head = LENS.read_text(encoding="utf-8").split("[[triage]]", 1)[0]
    assert tomllib.loads(head) == {"base": "unstraightening", "selection": "queering", "queer_share": 15, "trans_share": 5}


def test_stage_texts_are_unchanged_by_the_declarations(lens: Lens, tmp_path: Path) -> None:
    lines = LENS.read_text(encoding="utf-8").splitlines(keepends=True)
    bare = tmp_path / "queering.toml"
    bare.write_text(
        "".join(line for line in lines if not line.startswith(("base = ", "selection = ", "queer_share = ", "trans_share = "))),
        encoding="utf-8",
    )
    without = load_lens(bare)
    for stage in ("triage", "screen", "intent", "judge"):
        assert lens.stage_text(stage) == without.stage_text(stage)


def test_triage_is_the_three_questions_in_order(lens: Lens) -> None:
    assert lens.triage == TRIAGE


@pytest.mark.parametrize("stage", BOUND_STAGES)
def test_stage_carries_exactly_its_bound_labels(lens: Lens, stage: Stage) -> None:
    assert labels(lens.stage_text(stage)) == BINDING[stage]


@pytest.mark.parametrize(("stage", "label", "phrase"), PINNED_PHRASES)
def test_pinned_phrase_is_in_the_paragraph_that_starts_with_its_label(lens: Lens, stage: Stage, label: str, phrase: str) -> None:
    (paragraph,) = labelled(lens, stage, label)
    assert phrase in paragraph


@pytest.mark.parametrize("label", REJECT_RULES)
def test_judge_rule_is_a_reject_sentence(lens: Lens, label: str) -> None:
    (paragraph,) = labelled(lens, "judge", label)
    assert "Reject a rewrite that " in paragraph


INTENT_PREAMBLE_PHRASES: tuple[str, ...] = (
    "every romance line of a speaker chosen queer, drafted together",
    "a line in the flirt's redirect half",
    "every line of a trans pick, drafted together",
    "no-hit when the line is not the shape its label names",
    "protection when a protect rule below covers it",
    "plot when switching would contradict a partner or fact shown on screen, or would change the plot",
)


@pytest.mark.parametrize("phrase", INTENT_PREAMBLE_PHRASES)
def test_intent_preamble_covers_the_flirt_and_names_the_keep_list(lens: Lens, phrase: str) -> None:
    preamble = lens.intent_rule.strip().split("\n\n", 1)[0]
    assert phrase in preamble


def test_doc_opens_with_the_title_and_the_no_game_text_promise() -> None:
    doc = " ".join(doc_text().split())
    assert doc_text().startswith("# Queering lens\n")
    assert "It holds no game text." in doc
    assert "It names no characters, places or story events." in doc
    assert 'It says "the game" and "the flirt" instead of names.' in doc


def test_doc_has_its_sections_in_order() -> None:
    assert re.findall(r"^## .+$", doc_text(), re.M) == list(DOC_SECTIONS)


@pytest.mark.parametrize("label", sorted(TRIAGE_IDS | SCREEN | INTENT | JUDGE))
def test_label_appears_in_doc_in_backticks(label: str) -> None:
    assert f"`{label}`" in doc_text()


@pytest.mark.parametrize("sentence", DOC_SENTENCES)
def test_doc_carries_its_pinned_sentence(sentence: str) -> None:
    assert sentence in " ".join(doc_text().split())


@pytest.mark.parametrize("phrase", BUCKET_PHRASES)
def test_doc_buckets_describe_the_shares_and_the_refill(phrase: str) -> None:
    section = doc_text().split("\n## Buckets\n", 1)[1].split("\n## ", 1)[0]
    assert phrase in " ".join(section.split())


def test_doc_how_it_is_checked_says_what_the_checks_are() -> None:
    section = doc_text().split("\n## How it is checked\n", 1)[1]
    normalized = " ".join(section.split())
    for phrase in HOW_IT_IS_CHECKED:
        assert phrase in normalized
