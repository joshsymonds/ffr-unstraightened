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
        "world-bucket",
        "nonbinary-bucket",
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
    ("intent", "minimal-fix", "The world, nonbinary and trans buckets' own edit is allowed here."),
    ("intent", "same-partner", "the same partner in every line"),
    ("intent", "queer-bucket", "the speaker's romantic target becomes the same sex"),
    ("intent", "queer-bucket", "Swap the gender of the partner or crush so the pairing is same-sex"),
    ("intent", "queer-bucket", "For a man or woman speaker, the pursuit target changes gender too."),
    ("intent", "queer-bucket", "Where the line can carry both a same-sex and an opposite-sex reading, a bi reading is fine."),
    ("intent", "queer-bucket", "Keep everything else"),
    (
        "intent",
        "world-bucket",
        "For a world pick, change one candidate line of the speaker",
    ),
    ("intent", "world-bucket", "a short same-sex partner mention"),
    ("intent", "world-bucket", "worked into a line already about the speaker's home, family, work or daily life"),
    (
        "intent",
        "world-bucket",
        "The partner must carry the line's point, so that deleting the mention would lose something.",
    ),
    ("intent", "world-bucket", "Put it in one short clause, with no labels and no explaining."),
    ("intent", "nonbinary-bucket", "For a nonbinary pick, change one candidate line of the speaker"),
    ("intent", "nonbinary-bucket", "a short mention of a specific partner"),
    ("intent", "nonbinary-bucket", 'either "my partner" or a name the line already allows'),
    ("intent", "nonbinary-bucket", "consistently called they/them wherever gender would otherwise show"),
    ("intent", "nonbinary-bucket", "worked into a line already about the speaker's home, family, work or daily life"),
    ("intent", "nonbinary-bucket", "a line where the partner's gender would otherwise show"),
    ("intent", "nonbinary-bucket", "The partner must carry the line's point, so that deleting the mention would lose something."),
    ("intent", "nonbinary-bucket", "Put it in one short clause, with no labels and no explaining."),
    ("intent", "nonbinary-bucket", "Change no other line."),
    ("intent", "trans-bucket", "For a trans pick, change at most one candidate line of the speaker."),
    ("intent", "trans-bucket", "Where the line naturally allows it, add one light, natural trans detail of one of four shapes:"),
    ("intent", "trans-bucket", "a family member's acceptance, possibly in progress"),
    ("intent", "trans-bucket", "someone who backs the speaker"),
    ("intent", "trans-bucket", "a name the speaker chose and likes"),
    ("intent", "trans-bucket", "ordinary present life with the speaker's gender a given"),
    ("intent", "trans-bucket", "The detail must be recognizable as trans and must answer the line's own topic."),
    ("intent", "trans-bucket", "Where the line does not allow it, keep it unchanged as no-hit."),
    ("intent", "trans-bucket", "The detail is never forced."),
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
    ("intent", "no-explaining", "Do not explain the change in the line, and do not add a tokenistic detail."),
    ("intent", "no-explaining", "The world, nonbinary and trans buckets' own edit is neither an explanation nor a tokenistic detail."),
    (
        "intent",
        "no-new-detail",
        "Add no name, object or motive the original line lacks, except the world, nonbinary and trans buckets' own edit.",
    ),
    (
        "intent",
        "protect-plot",
        "A pick's partner or crush gender is the bucket's edit, never a plot fact by itself.",
    ),
    (
        "intent",
        "protect-plot",
        "Keep a line unchanged for plot only when that partner appears or is named as a character elsewhere in the given context, or when the change would alter what happens.",
    ),
    ("judge", "meaning", "meaning protects the bond, not the gender"),
    (
        "judge",
        "no-gender-joke-or-menace",
        "Reject a rewrite whose humor or menace rests on the target's or partner's gender or queerness.",
    ),
    ("judge", "no-explaining", "Reject a rewrite that explains the change or adds a tokenistic detail."),
    ("judge", "no-explaining", "The world, nonbinary and trans buckets' own edit is neither an explanation nor a tokenistic detail."),
    ("judge", "keep-voice", "Reject a rewrite that breaks the speaker's voice."),
    (
        "judge",
        "no-new-detail",
        "Reject a rewrite that adds a name, object or motive the original line lacks, except the world, nonbinary and trans buckets' own edit.",
    ),
    ("judge", "no-contradiction", "Reject a rewrite that contradicts the speaker's other lines."),
    ("judge", "minimal-fix", "Reject a rewrite that changes more than its edit needs."),
    ("judge", "minimal-fix", "The world, nonbinary and trans buckets' own edit is allowed here."),
    ("judge", "queer-bucket", "Reject a rewrite that changes more than the target's gender"),
    ("judge", "queer-bucket", "unless the line carries a bi reading"),
    ("judge", "world-bucket", "Reject a rewrite that adds a same-sex partner mention which could be deleted without loss."),
    ("judge", "world-bucket", "Reject a rewrite that changes more than one line of a world, nonbinary or trans pick."),
    (
        "judge",
        "nonbinary-bucket",
        "Reject a rewrite whose they/them reads as merely unspecified gender",
    ),
    ("judge", "nonbinary-bucket", "the partner is not a specific person"),
    (
        "judge",
        "nonbinary-bucket",
        "Reject a rewrite that uses the word nonbinary or any modern label (trans, transgender, transition, deadname) in the setting.",
    ),
    ("judge", "nonbinary-bucket", "Reject a rewrite whose partner mention could be deleted without loss."),
    ("judge", "nonbinary-bucket", "Reject a rewrite that is not one short clause, or that puts a label or an explanation in the line."),
    ("judge", "trans-bucket", 'Reject a "she\'s trans" aside.'),
    ("judge", "trans-bucket", "Reject a rewrite that forces a trans detail the line does not naturally allow."),
    ("judge", "trans-bucket", "is the bucket's edit only when it is one of the four shapes"),
    ("judge", "trans-bucket", "Reject a rewrite whose trans detail is too vague to read as trans."),
    ("judge", "trans-bucket", "Reject a rewrite that states an old name."),
    ("judge", "trans-bucket", "Reject a rewrite that volunteers transition history to a stranger."),
    ("judge", "trans-bucket", "Reject a rewrite with body or medical talk."),
    ("judge", "trans-bucket", "Reject a rewrite where pain is the whole story."),
    ("judge", "trans-bucket", "Reject a rewrite that frames being trans as a secret or concealment."),
    (
        "judge",
        "trans-bucket",
        "Reject a rewrite that uses a modern label (trans, transgender, transition, nonbinary, deadname) in the setting.",
    ),
    ("judge", "trans-bucket", "Reject a rewrite that makes the player the validator or the rescuer."),
    ("judge", "trans-bucket", "Reject a rewrite that makes a joke at the speaker's expense."),
    ("judge", "already-queer", "Reject a rewrite that changes a line whose pairing is already same-sex"),
    ("judge", "already-queer", "or whose speaker, partner or crush is already trans or nonbinary."),
    ("judge", "flirt-redirect", "unless generalize-when-reaction applies"),
    ("judge", "protect-plot", "Reject a rewrite that changes a plot fact, or that changes the scene's meaning to fit a label."),
    ("judge", "protect-plot", "A pick's partner or crush gender is not a plot fact by itself"),
    ("judge", "protect-plot", "unless that partner appears or is named as a character elsewhere in the given context."),
)

# Judge rules that each carry a "Reject a rewrite that ..." sentence for their rule.
REJECT_RULES: tuple[str, ...] = (
    "same-partner",
    "queer-bucket",
    "world-bucket",
    "nonbinary-bucket",
    "trans-bucket",
    "already-queer",
    "flirt-redirect",
    "generalize-when-reaction",
    "protect-sincere-thread",
    "protect-minor-target",
    "protect-core-cast",
    "protect-plot",
)

# Wording that the brief retires from the lens and the doc: the plot rule's screen clause.
RETIRED_PHRASES: tuple[str, ...] = ("shown on screen",)
# Nonbinary-partner wording that the romance and trans buckets never carry: the nonbinary edit is its own bucket.
NONBINARY_PARTNER_PHRASES: tuple[str, ...] = ("nonbinary partner", "they/them")
# The doc entries that carry the nonbinary bucket's legibility rule, under their section heading.
NONBINARY_DOC_PHRASES: tuple[tuple[str, str], ...] = (
    ("## Buckets", 'a short mention of a specific partner, "my partner" or a name the line already allows'),
    ("## Buckets", "consistently called they/them wherever gender would otherwise show"),
    ("## Buckets", "a line where the partner's gender would otherwise show"),
    ("## Judge rubric", "rejects a rewrite whose they/them reads as merely unspecified gender"),
    ("## Judge rubric", "the word nonbinary or any modern label"),
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
    "The lens runs under the queering selection: an Opus marker, a Haiku candidate pass, four disjoint seeded refill sides (romance, world, nonbinary and trans), one draft call per pick, and no Jev.",
)
BUCKET_PHRASES: tuple[str, ...] = (
    "romance_share of the romance pool",
    "world_share of the eligible characters outside the romance pool",
    "nonbinary_share of the eligible characters outside the romance pool",
    "trans_share of all eligible characters",
    "the world pool is the eligible speakers outside the romance pool with an insertable mark",
    "the nonbinary pool is the eligible speakers outside the romance pool with an insertable mark",
    "the trans pool is the eligible speakers with a trans-candidate mark, minus the other sides' picks",
    "Speakerless lines are never trans picks",
    "a share of 100 targets the whole denominator, only the romance pool equals its denominator, and a share of 0 draws nothing",
    "refilled from the order until the target or the pool runs out",
    "gender-roles mark never puts a unit in the queer pool",
)
HOW_IT_IS_CHECKED: tuple[str, ...] = (
    "used only for counts",
    "no Jev recheck",
    "reads the lines of characters outside the core cast who are not antagonists, and the flirt's",
    "`marks.jsonl` with no line text",
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
    assert lens.romance_share == 15
    assert lens.world_share == 10
    assert lens.nonbinary_share == 5
    assert lens.trans_share == 5


def test_declarations_are_the_only_top_level_keys_before_the_first_table() -> None:
    head = LENS.read_text(encoding="utf-8").split("[[triage]]", 1)[0]
    assert tomllib.loads(head) == {
        "base": "unstraightening",
        "selection": "queering",
        "romance_share": 15,
        "world_share": 10,
        "nonbinary_share": 5,
        "trans_share": 5,
    }


def test_stage_texts_are_unchanged_by_the_declarations(lens: Lens, tmp_path: Path) -> None:
    lines = LENS.read_text(encoding="utf-8").splitlines(keepends=True)
    bare = tmp_path / "queering.toml"
    bare.write_text(
        "".join(
            line
            for line in lines
            if not line.startswith(
                ("base = ", "selection = ", "romance_share = ", "world_share = ", "nonbinary_share = ", "trans_share = ")
            )
        ),
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
    "one candidate line of a world pick",
    "one candidate line of a nonbinary pick",
    "one candidate line of a trans pick",
    "no-hit when the line is not the shape its label names",
    "protection when a protect rule below covers it",
    "plot when the partner appears or is named as a character elsewhere in the given context, or when the change would alter what happens",
)


@pytest.mark.parametrize("phrase", INTENT_PREAMBLE_PHRASES)
def test_intent_preamble_covers_the_four_line_kinds_and_names_the_keep_list(lens: Lens, phrase: str) -> None:
    preamble = lens.intent_rule.strip().split("\n\n", 1)[0]
    assert phrase in preamble


@pytest.mark.parametrize("phrase", RETIRED_PHRASES)
def test_retired_wording_is_gone_from_the_lens_and_the_doc(phrase: str) -> None:
    assert phrase not in LENS.read_text(encoding="utf-8")
    assert phrase not in doc_text()


@pytest.mark.parametrize("phrase", NONBINARY_PARTNER_PHRASES)
@pytest.mark.parametrize("stage", ("intent", "judge"))
@pytest.mark.parametrize("bucket", ("queer-bucket", "trans-bucket"))
def test_romance_and_trans_bucket_paragraphs_carry_no_nonbinary_partner_wording(lens: Lens, bucket: str, stage: Stage, phrase: str) -> None:
    (paragraph,) = labelled(lens, stage, bucket)
    assert phrase not in paragraph


@pytest.mark.parametrize(("heading", "phrase"), NONBINARY_DOC_PHRASES)
def test_nonbinary_doc_entry_carries_the_legibility_rule(heading: str, phrase: str) -> None:
    section = doc_text().split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]
    entry = section.split("- `nonbinary-bucket`", 1)[1].split("- `trans-bucket`", 1)[0]
    assert phrase in " ".join(entry.split())


@pytest.mark.parametrize("phrase", NONBINARY_PARTNER_PHRASES)
def test_trans_bucket_doc_entry_carries_no_nonbinary_partner_wording(phrase: str) -> None:
    section = doc_text().split("\n## Buckets\n", 1)[1].split("\n## ", 1)[0]
    entry = section.split("- `trans-bucket`", 1)[1].split("- `same-partner`", 1)[0]
    assert phrase not in " ".join(entry.split())


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
