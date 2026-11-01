import re
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.pipeline.lens import Lens, TriageQuestion, load_lens

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
