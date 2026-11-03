import re
from dataclasses import replace
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.pipeline.lens import Lens, ScenePattern, SceneSection, TriageQuestion, load_lens

# Made-up lens text. Not game text.
VALID = """\
[[triage]]
id = "made-up-id"
question = "Made-up triage question. Not game text."

[[triage]]
id = "second-id"
question = "Made-up second question."

[screen]
criteria = ["Made-up criterion one.", "Made-up criterion two."]

[intent]
rule = "Made-up intent rule, café."

[judge]
rubric = "Made-up judge rubric."
"""

EXPECTED_LENS = Lens(
    name="sample",
    triage=(
        TriageQuestion(id="made-up-id", question="Made-up triage question. Not game text."),
        TriageQuestion(id="second-id", question="Made-up second question."),
    ),
    screen_criteria=("Made-up criterion one.", "Made-up criterion two."),
    intent_rule="Made-up intent rule, café.",
    judge_rubric="Made-up judge rubric.",
)

# Made-up scene section. Not game text.
PATTERNS = 'patterns = [{ id = "made-up-pattern", question = "Made-up pattern question." }, { id = "second-pattern", question = "Made-up second pattern question, café." }]'
CRITERIA = 'criteria = ["Made-up scene criterion one.", "Made-up scene criterion two."]'
PLAN = 'plan = "Made-up scene plan."'
RUBRIC = 'rubric = "Made-up scene rubric."'
SCENE = "[scene]\n" + "\n".join((PATTERNS, CRITERIA, PLAN, RUBRIC)) + "\n"
VALID_WITH_SCENE = VALID + "\n" + SCENE

EXPECTED_SCENE = SceneSection(
    patterns=(
        ScenePattern(id="made-up-pattern", question="Made-up pattern question."),
        ScenePattern(id="second-pattern", question="Made-up second pattern question, café."),
    ),
    criteria=("Made-up scene criterion one.", "Made-up scene criterion two."),
    plan="Made-up scene plan.",
    rubric="Made-up scene rubric.",
)


def write_lens(tmp_path: Path, text: str, name: str = "sample") -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_lens_loads_into_typed_fields(tmp_path: Path) -> None:
    assert load_lens(write_lens(tmp_path, VALID)) == EXPECTED_LENS


def test_name_is_file_stem(tmp_path: Path) -> None:
    assert load_lens(write_lens(tmp_path, VALID, name="my-lens")).name == "my-lens"


def test_missing_file_raises_fail_naming_file(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    with pytest.raises(Fail, match=re.escape(str(missing))):
        load_lens(missing)


def test_directory_raises_fail_naming_file(tmp_path: Path) -> None:
    with pytest.raises(Fail, match=re.escape(str(tmp_path))):
        load_lens(tmp_path)


def test_undecodable_file_raises_fail_naming_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.toml"
    path.write_bytes(b"\xff\xfe[[triage]]\n")
    with pytest.raises(Fail, match=re.escape(str(path))):
        load_lens(path)


def test_invalid_toml_raises_fail_naming_file(tmp_path: Path) -> None:
    path = write_lens(tmp_path, "[[triage]\nid = ")
    with pytest.raises(Fail, match=re.escape(f"{path}: cannot read as TOML")):
        load_lens(path)


# Each case edits VALID and names the one problem it must report.
INVALID_CASES: list[tuple[str, str, str, str]] = [
    ("missing triage id", 'id = "made-up-id"\n', "", "triage[0].id is missing"),
    ("malformed id", 'id = "made-up-id"', 'id = "Made_Up"', "triage[0].id must be"),
    ("duplicate id", 'id = "second-id"', 'id = "made-up-id"', "triage[1].id duplicates triage[0].id"),
    ("empty triage question", 'question = "Made-up second question."', 'question = ""', "triage[1].question must be a non-empty string"),
    ("unknown triage key", 'id = "made-up-id"', 'id = "made-up-id"\nnote = "Made-up."', "triage[0].note is an unknown key"),
    ("missing screen", '[screen]\ncriteria = ["Made-up criterion one.", "Made-up criterion two."]\n', "", "screen is missing"),
    ("missing judge", '[judge]\nrubric = "Made-up judge rubric."\n', "", "judge is missing"),
    (
        "empty criteria",
        'criteria = ["Made-up criterion one.", "Made-up criterion two."]',
        "criteria = []",
        "screen.criteria must be a non-empty list",
    ),
    ("empty criterion", '"Made-up criterion two."]', '""]', "screen.criteria[1] must be a non-empty string"),
    ("criterion wrong type", '"Made-up criterion two."]', "5]", "screen.criteria[1] must be a non-empty string"),
    ("missing intent rule", 'rule = "Made-up intent rule, café."', "", "intent.rule is missing"),
    ("intent rule wrong type", 'rule = "Made-up intent rule, café."', "rule = 5", "intent.rule must be a non-empty string"),
    ("empty judge rubric", 'rubric = "Made-up judge rubric."', 'rubric = ""', "judge.rubric must be a non-empty string"),
    (
        "unknown judge key",
        'rubric = "Made-up judge rubric."',
        'rubric = "Made-up judge rubric."\nextra = "Made-up."',
        "judge.extra is an unknown key",
    ),
    ("unknown top-level table", "[judge]", '[extra]\nkey = "Made-up."\n\n[judge]', "extra is an unknown key"),
]


@pytest.mark.parametrize(
    ("label", "old", "new", "problem"),
    INVALID_CASES,
    ids=[case[0] for case in INVALID_CASES],
)
def test_invalid_lens_raises_fail_naming_file_and_field(tmp_path: Path, label: str, old: str, new: str, problem: str) -> None:
    assert VALID.count(old) == 1, label
    path = write_lens(tmp_path, VALID.replace(old, new))
    with pytest.raises(Fail, match=re.escape(f"{path}: {problem}")):
        load_lens(path)


def test_screen_not_a_table_raises_fail_naming_field(tmp_path: Path) -> None:
    # A top-level key must come before the first table header, so the bad value goes first.
    text = 'screen = "Made-up."\n' + VALID.replace('[screen]\ncriteria = ["Made-up criterion one.", "Made-up criterion two."]\n\n', "")
    path = write_lens(tmp_path, text)
    with pytest.raises(Fail, match=re.escape(f"{path}: screen must be a table")):
        load_lens(path)


def test_all_problems_reported_in_one_fail(tmp_path: Path) -> None:
    text = VALID.replace('rule = "Made-up intent rule, café."', "").replace('rubric = "Made-up judge rubric."', 'rubric = ""')
    path = write_lens(tmp_path, text)
    with pytest.raises(Fail) as raised:
        load_lens(path)
    message = str(raised.value)
    assert f"{path}: intent.rule is missing" in message
    assert f"{path}: judge.rubric must be a non-empty string" in message


def test_stage_text_is_exact_for_each_stage(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, VALID))
    assert lens.stage_text("triage") == (
        '[{"id": "made-up-id", "question": "Made-up triage question. Not game text."}, '
        '{"id": "second-id", "question": "Made-up second question."}]'
    )
    assert lens.stage_text("screen") == '{"criteria": ["Made-up criterion one.", "Made-up criterion two."]}'
    assert lens.stage_text("intent") == '{"rule": "Made-up intent rule, café."}'
    assert lens.stage_text("judge") == '{"rubric": "Made-up judge rubric."}'


def test_stage_text_is_equal_across_loads(tmp_path: Path) -> None:
    first = load_lens(write_lens(tmp_path, VALID, name="first"))
    second = load_lens(write_lens(tmp_path, VALID, name="second"))
    for stage in ("triage", "screen", "intent", "judge"):
        assert first.stage_text(stage) == second.stage_text(stage)


def test_editing_one_stage_changes_only_that_stage_text(tmp_path: Path) -> None:
    original = load_lens(write_lens(tmp_path, VALID, name="original"))
    edited = load_lens(write_lens(tmp_path, VALID.replace("Made-up judge rubric.", "Made-up edited rubric."), name="edited"))
    assert edited.stage_text("judge") != original.stage_text("judge")
    for stage in ("triage", "screen", "intent"):
        assert edited.stage_text(stage) == original.stage_text(stage)


def test_valid_lens_with_scene_loads_scene_section(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, VALID_WITH_SCENE))
    assert lens.scene == EXPECTED_SCENE
    assert lens.stage_text("triage") == (
        '[{"id": "made-up-id", "question": "Made-up triage question. Not game text."}, '
        '{"id": "second-id", "question": "Made-up second question."}]'
    )
    assert lens.stage_text("screen") == '{"criteria": ["Made-up criterion one.", "Made-up criterion two."]}'
    assert lens.stage_text("intent") == '{"rule": "Made-up intent rule, café."}'
    assert lens.stage_text("judge") == '{"rubric": "Made-up judge rubric."}'


def test_scene_stage_text_is_exact(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, VALID_WITH_SCENE))
    assert lens.stage_text("scene") == (
        '{"criteria": ["Made-up scene criterion one.", "Made-up scene criterion two."], '
        '"patterns": [{"id": "made-up-pattern", "question": "Made-up pattern question."}, '
        '{"id": "second-pattern", "question": "Made-up second pattern question, café."}], '
        '"plan": "Made-up scene plan.", "rubric": "Made-up scene rubric."}'
    )


def test_lens_without_scene_has_no_scene_section(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, VALID))
    assert lens == EXPECTED_LENS
    assert lens.scene is None
    with pytest.raises(Fail, match=re.escape("lens sample has no [scene] section")):
        lens.stage_text("scene")


# Each case edits SCENE and names the one problem it must report.
SCENE_INVALID_CASES: list[tuple[str, str, str, str]] = [
    ("unknown scene key", PLAN, PLAN + '\nnote = "Made-up."', "scene.note is an unknown key"),
    ("missing patterns", PATTERNS + "\n", "", "scene.patterns is missing"),
    ("missing criteria", CRITERIA + "\n", "", "scene.criteria is missing"),
    ("missing plan", PLAN + "\n", "", "scene.plan is missing"),
    ("missing rubric", RUBRIC + "\n", "", "scene.rubric is missing"),
    ("empty patterns", PATTERNS, "patterns = []", "scene.patterns must be one or more tables"),
    ("patterns not a list", PATTERNS, 'patterns = "Made-up."', "scene.patterns must be one or more tables"),
    (
        "pattern not a table",
        PATTERNS,
        'patterns = [{ id = "made-up-pattern", question = "Made-up pattern question." }, "Made-up."]',
        "scene.patterns[1] must be a table",
    ),
    (
        "unknown pattern key",
        'id = "made-up-pattern", ',
        'id = "made-up-pattern", note = "Made-up.", ',
        "scene.patterns[0].note is an unknown key",
    ),
    ("missing pattern id", 'id = "made-up-pattern", ', "", "scene.patterns[0].id is missing"),
    ("malformed pattern id", '"made-up-pattern"', '"Made_Up"', "scene.patterns[0].id must be"),
    (
        "duplicate pattern id",
        'id = "second-pattern"',
        'id = "made-up-pattern"',
        "scene.patterns[1].id duplicates scene.patterns[0].id",
    ),
    ("pattern id collides with triage", 'id = "second-pattern"', 'id = "made-up-id"', "scene.patterns[1].id duplicates triage[0].id"),
    (
        "empty pattern question",
        'question = "Made-up second pattern question, café."',
        'question = ""',
        "scene.patterns[1].question must be a non-empty string",
    ),
    ("empty scene criteria", CRITERIA, "criteria = []", "scene.criteria must be a non-empty list"),
    ("empty scene criterion", '"Made-up scene criterion two."]', '""]', "scene.criteria[1] must be a non-empty string"),
    ("scene criterion wrong type", '"Made-up scene criterion two."]', "5]", "scene.criteria[1] must be a non-empty string"),
    ("empty scene plan", PLAN, 'plan = ""', "scene.plan must be a non-empty string"),
    ("empty scene rubric", RUBRIC, 'rubric = ""', "scene.rubric must be a non-empty string"),
]


@pytest.mark.parametrize(
    ("label", "old", "new", "problem"),
    SCENE_INVALID_CASES,
    ids=[case[0] for case in SCENE_INVALID_CASES],
)
def test_invalid_scene_raises_fail_naming_file_and_field(tmp_path: Path, label: str, old: str, new: str, problem: str) -> None:
    assert SCENE.count(old) == 1, label
    path = write_lens(tmp_path, VALID + "\n" + SCENE.replace(old, new))
    with pytest.raises(Fail, match=re.escape(f"{path}: {problem}")):
        load_lens(path)


def test_scene_not_a_table_raises_fail_naming_field(tmp_path: Path) -> None:
    # A top-level key must come before the first table header, so the bad value goes first.
    path = write_lens(tmp_path, 'scene = "Made-up."\n' + VALID)
    with pytest.raises(Fail, match=re.escape(f"{path}: scene must be a table")):
        load_lens(path)


BASE_ID = "made-up-base-lens"
BASE_MESSAGE = "base must be a lens name of lowercase letters, digits and single hyphens, like made-up-id"


def test_declared_base_loads_into_base_field(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, f'base = "{BASE_ID}"\n' + VALID))
    assert lens == replace(EXPECTED_LENS, base=BASE_ID)


def test_lens_without_base_has_none(tmp_path: Path) -> None:
    assert load_lens(write_lens(tmp_path, VALID)).base is None


def test_base_does_not_change_stage_text(tmp_path: Path) -> None:
    plain = load_lens(write_lens(tmp_path, VALID_WITH_SCENE, name="plain"))
    based = load_lens(write_lens(tmp_path, f'base = "{BASE_ID}"\n' + VALID_WITH_SCENE, name="based"))
    for stage in ("triage", "screen", "intent", "judge", "scene"):
        assert based.stage_text(stage) == plain.stage_text(stage)


# Each case declares a base whose value has the wrong type or breaks the lens name pattern.
BASE_INVALID_VALUES: list[tuple[str, str]] = [
    ("uppercase", '"Made_Up"'),
    ("empty", '""'),
    ("double hyphen", '"made--base"'),
    ("trailing hyphen", '"made-base-"'),
    ("integer", "5"),
    ("array", '["made-up-base-lens"]'),
    ("boolean", "true"),
]


@pytest.mark.parametrize(
    ("label", "value"),
    BASE_INVALID_VALUES,
    ids=[case[0] for case in BASE_INVALID_VALUES],
)
def test_malformed_base_raises_fail_naming_file_and_field(tmp_path: Path, label: str, value: str) -> None:
    path = write_lens(tmp_path, f"base = {value}\n" + VALID)
    with pytest.raises(Fail, match=re.escape(f"{path}: {BASE_MESSAGE}")):
        load_lens(path)


def test_base_naming_the_lens_itself_raises_fail(tmp_path: Path) -> None:
    path = write_lens(tmp_path, f'base = "{BASE_ID}"\n' + VALID, name=BASE_ID)
    with pytest.raises(Fail, match=re.escape(f"{path}: base names the lens itself")):
        load_lens(path)


def test_base_problem_reported_with_other_problems_in_one_fail(tmp_path: Path) -> None:
    text = 'base = "Made_Up"\n' + VALID.replace('rule = "Made-up intent rule, café."', "")
    path = write_lens(tmp_path, text)
    with pytest.raises(Fail) as raised:
        load_lens(path)
    message = str(raised.value)
    assert f"{path}: {BASE_MESSAGE}" in message
    assert f"{path}: intent.rule is missing" in message


SELECTION_LINE = 'selection = "queering"\n'


def test_declared_selection_loads_into_selection_field(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, SELECTION_LINE + VALID))
    assert lens.selection == "queering"
    assert lens == replace(EXPECTED_LENS, selection="queering")


def test_lens_without_selection_has_none(tmp_path: Path) -> None:
    assert load_lens(write_lens(tmp_path, VALID)).selection is None


def test_selection_does_not_change_stage_text(tmp_path: Path) -> None:
    plain = load_lens(write_lens(tmp_path, VALID_WITH_SCENE, name="plain"))
    selected = load_lens(write_lens(tmp_path, SELECTION_LINE + VALID_WITH_SCENE, name="selected"))
    for stage in ("triage", "screen", "intent", "judge", "scene"):
        assert selected.stage_text(stage) == plain.stage_text(stage)


# Each case declares a selection that is not the queering mode.
SELECTION_INVALID_VALUES: list[tuple[str, str]] = [
    ("other string", '"other"'),
    ("empty", '""'),
    ("integer", "5"),
    ("table", '{ mode = "queering" }'),
]


@pytest.mark.parametrize(
    ("label", "value"),
    SELECTION_INVALID_VALUES,
    ids=[case[0] for case in SELECTION_INVALID_VALUES],
)
def test_malformed_selection_raises_fail_naming_file_and_field(tmp_path: Path, label: str, value: str) -> None:
    path = write_lens(tmp_path, f"selection = {value}\n" + VALID)
    with pytest.raises(Fail, match=re.escape(f'{path}: selection must be "queering"')):
        load_lens(path)


def test_selection_problem_reported_with_other_problems_in_one_fail(tmp_path: Path) -> None:
    text = 'selection = "other"\n' + VALID.replace('rule = "Made-up intent rule, café."', "")
    path = write_lens(tmp_path, text)
    with pytest.raises(Fail) as raised:
        load_lens(path)
    message = str(raised.value)
    assert f'{path}: selection must be "queering"' in message
    assert f"{path}: intent.rule is missing" in message


SCREEN_CRITERIA_LINE = 'criteria = ["Made-up criterion one.", "Made-up criterion two."]\n'


def with_screen_model(value: str) -> str:
    assert VALID.count(SCREEN_CRITERIA_LINE) == 1
    return VALID.replace(SCREEN_CRITERIA_LINE, SCREEN_CRITERIA_LINE + f"model = {value}\n")


def test_lens_without_screen_model_defaults_to_opus(tmp_path: Path) -> None:
    assert load_lens(write_lens(tmp_path, VALID)).screen_model == "opus"


@pytest.mark.parametrize("model", ["opus", "sonnet"])
def test_declared_screen_model_loads_into_screen_model_field(tmp_path: Path, model: str) -> None:
    lens = load_lens(write_lens(tmp_path, with_screen_model(f'"{model}"')))
    assert lens.screen_model == model
    assert lens == replace(EXPECTED_LENS, screen_model=model)


# Each case declares a screen model that is not one of the two Claude models.
SCREEN_MODEL_INVALID_VALUES: list[tuple[str, str]] = [
    ("other string", '"haiku"'),
    ("capitalised", '"Sonnet"'),
    ("empty", '""'),
    ("integer", "5"),
    ("array", '["opus"]'),
    ("boolean", "true"),
]


@pytest.mark.parametrize(
    ("label", "value"),
    SCREEN_MODEL_INVALID_VALUES,
    ids=[case[0] for case in SCREEN_MODEL_INVALID_VALUES],
)
def test_malformed_screen_model_raises_fail_naming_file_and_field(tmp_path: Path, label: str, value: str) -> None:
    path = write_lens(tmp_path, with_screen_model(value))
    with pytest.raises(Fail, match=re.escape(f'{path}: screen.model must be "opus" or "sonnet"')):
        load_lens(path)


def test_screen_model_problem_reported_with_other_problems_in_one_fail(tmp_path: Path) -> None:
    text = with_screen_model('"haiku"').replace('rule = "Made-up intent rule, café."', "")
    path = write_lens(tmp_path, text)
    with pytest.raises(Fail) as raised:
        load_lens(path)
    message = str(raised.value)
    assert f'{path}: screen.model must be "opus" or "sonnet"' in message
    assert f"{path}: intent.rule is missing" in message


def test_screen_model_does_not_change_screen_stage_text(tmp_path: Path) -> None:
    plain = load_lens(write_lens(tmp_path, VALID, name="plain"))
    modelled = load_lens(write_lens(tmp_path, with_screen_model('"sonnet"'), name="modelled"))
    assert modelled.stage_text("screen") == plain.stage_text("screen")
    assert modelled.stage_text("screen") == '{"criteria": ["Made-up criterion one.", "Made-up criterion two."]}'


def test_lens_without_shares_has_default_shares(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, VALID))
    assert lens.romance_share == 15
    assert lens.world_share == 10
    assert lens.nonbinary_share == 5
    assert lens.trans_share == 5
    assert lens == EXPECTED_LENS


SHARE_KEYS = ["romance_share", "world_share", "nonbinary_share", "trans_share"]
# Each case declares a share as TOML text, with the value the field should load. 0 and 100 are the bounds, both allowed.
SHARE_VALID_VALUES: list[tuple[str, int]] = [
    ("0", 0),
    ("42", 42),
    ("100", 100),
]


@pytest.mark.parametrize("key", SHARE_KEYS)
@pytest.mark.parametrize(("text", "value"), SHARE_VALID_VALUES, ids=[case[0] for case in SHARE_VALID_VALUES])
def test_declared_share_loads_into_its_field(tmp_path: Path, key: str, text: str, value: int) -> None:
    lens = load_lens(write_lens(tmp_path, f"{key} = {text}\n" + VALID))
    assert getattr(lens, key) == value


def test_all_four_shares_declared_load_together(tmp_path: Path) -> None:
    lens = load_lens(write_lens(tmp_path, "romance_share = 0\nworld_share = 100\nnonbinary_share = 7\ntrans_share = 50\n" + VALID))
    assert lens == replace(EXPECTED_LENS, romance_share=0, world_share=100, nonbinary_share=7, trans_share=50)


# Each case declares a share that is not a whole number from 0 to 100. A bool is not a number here, and "all" is no longer a value.
SHARE_INVALID_VALUES: list[tuple[str, str]] = [
    ("negative", "-1"),
    ("over 100", "101"),
    ("float", "1.5"),
    ("boolean", "true"),
    ("all", '"all"'),
    ("other string", '"x"'),
]


@pytest.mark.parametrize("key", SHARE_KEYS)
@pytest.mark.parametrize(("label", "value"), SHARE_INVALID_VALUES, ids=[case[0] for case in SHARE_INVALID_VALUES])
def test_malformed_share_raises_fail_naming_file_and_field(tmp_path: Path, key: str, label: str, value: str) -> None:
    path = write_lens(tmp_path, f"{key} = {value}\n" + VALID)
    with pytest.raises(Fail, match=re.escape(f"{path}: {key} must be a whole number from 0 to 100")):
        load_lens(path)


def test_share_problem_reported_with_other_problems_in_one_fail(tmp_path: Path) -> None:
    text = "trans_share = 101\n" + VALID.replace('rule = "Made-up intent rule, café."', "")
    path = write_lens(tmp_path, text)
    with pytest.raises(Fail) as raised:
        load_lens(path)
    message = str(raised.value)
    assert f"{path}: trans_share must be a whole number from 0 to 100" in message
    assert f"{path}: intent.rule is missing" in message


@pytest.mark.parametrize("key", ["queer_count", "trans_count"])
def test_retired_count_keys_are_unknown_keys(tmp_path: Path, key: str) -> None:
    path = write_lens(tmp_path, f"{key} = 10\n" + VALID)
    with pytest.raises(Fail, match=re.escape(f"{path}: {key} is an unknown key")):
        load_lens(path)


def test_shares_do_not_change_stage_text(tmp_path: Path) -> None:
    plain = load_lens(write_lens(tmp_path, VALID_WITH_SCENE, name="plain"))
    shared = load_lens(write_lens(tmp_path, "romance_share = 0\ntrans_share = 100\n" + VALID_WITH_SCENE, name="shared"))
    for stage in ("triage", "screen", "intent", "judge", "scene"):
        assert shared.stage_text(stage) == plain.stage_text(stage)


README = Path(__file__).resolve().parents[2] / "lenses" / "README.md"
FENCED_TOML = re.compile(r"^```toml\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def test_readme_example_is_a_valid_lens(tmp_path: Path) -> None:
    blocks = FENCED_TOML.findall(README.read_text(encoding="utf-8"))
    assert len(blocks) == 1
    lens = load_lens(write_lens(tmp_path, blocks[0], name="readme-example"))
    assert [question.id for question in lens.triage] == ["made-up-first-id", "made-up-second-id"]
    assert lens.base is None
    assert lens.selection is None
