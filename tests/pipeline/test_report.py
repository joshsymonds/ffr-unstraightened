import argparse
import json
import re
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.pipeline import report
from ffrd.pipeline.scene_record import SCENES, SceneRecord, write_scenes

LENS = "made-up-lens"
FIRST = "made-up-first-id"
SECOND = "made-up-second-id"
SPEAKER_TWO = "made-up-speaker-2"
SPEAKER_TEN = "made-up-speaker-10"

# Made-up lens text. Not game text.
LENS_TOML = """\
[[triage]]
id = "made-up-first-id"
question = "Made-up first question. Not game text."

[[triage]]
id = "made-up-second-id"
question = "Made-up second question. Not game text."

[screen]
criteria = ["Made-up criterion. Not game text."]

[intent]
rule = "Made-up intent rule. Not game text."

[judge]
rubric = "Made-up judge rubric. Not game text."
"""

# Sentinels stand in for namespace, key and line text. None may reach output.
SENTINELS = ("SENTINEL-NAMESPACE", "SENTINEL-KEY", "SENTINEL-TEXT", "Made-up line text.")

SUMMARY = (
    r"(chapter [0-9]+|unattributed|total): [0-9]+ changed, [0-9]+ kept for plot, [0-9]+ gate failed, [0-9]+ unfinished, [0-9]+ untouched"
)
DETAIL = r"  (reason [a-z0-9]+(?:-[a-z0-9]+)*|kept [a-z0-9]+(?:-[a-z0-9]+)*|character [0-9]+|no speaker|gate [a-z0-9]+(?:[-_][a-z0-9]+)*): [0-9]+"
ARCS_LINE = r"arcs: [0-9]+, [0-9]+ with changes"
REDO_LINE = r"redo: [0-9]+ listed, [0-9]+ changed, [0-9]+ gate failed"
QUEERING_LINE = (
    r"queering (marked: [0-9]+|bucket (queer|trans|untouched): [0-9]+|flirt marked: [0-9]+, redirect half: [0-9]+"
    r"|changed (queer|trans|redirect): [0-9]+|gate failed (queer|trans|redirect): [0-9]+)"
)
RECALL_LINE = r"recall [a-z0-9]+(?:-[a-z0-9]+)*: [0-9]+ disagreements, [0-9]+ confirmed"
SCENE_DETAIL = (
    r"  scene (eligible: [0-9]+|plans: [0-9]+|replanned: [0-9]+|reverted lines: [0-9]+|changed lines: [0-9]+"
    r"|planned lines: [0-9]+ \(required [0-9]+\)|changed per scene: max [0-9]+, median [0-9]+(?:\.[0-9])?"
    r"|flagged [a-z0-9]+(?:-[a-z0-9]+)*: [0-9]+|kept [a-z0-9]+(?:-[a-z0-9]+)*: [0-9]+"
    r"|gate [a-z0-9]+(?:[-_][a-z0-9]+)*: [0-9]+|character [0-9]+: [0-9]+)"
)
SCENE_LINE = r"scenes: [0-9]+ of [0-9]+ from the scene table, [0-9]+ outside it"
LINE = re.compile(f"{SUMMARY}|{DETAIL}|{SCENE_DETAIL}|{SCENE_LINE}|{ARCS_LINE}|{REDO_LINE}|{QUEERING_LINE}|{RECALL_LINE}")

# Made-up scene table build. Not game text.
BUILD = "424242"

# Made-up arcs totals. Not game text.
ARCS_VALID = b'{"arcs": 3, "with_changes": 2}\n'


def _record(**fields: object) -> dict[str, object]:
    # Made-up outcome record. Not game text.
    record: dict[str, object] = {
        "namespace": "SENTINEL-NAMESPACE",
        "key": "SENTINEL-KEY",
        "chapter": 1,
        "speaker": SPEAKER_TWO,
        "reasons": [],
        "outcome": "untouched",
        "gate": None,
        "keep_reason": None,
        "plan": None,
        "writer": "made-up-writer",
        "text": "SENTINEL-TEXT Made-up line text. Not game text.",
    }
    record.update(fields)
    return record


def _lines(*records: dict[str, object]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")


def _without(field: str) -> bytes:
    record = {k: v for k, v in _record().items() if k != field}
    return json.dumps(record).encode("utf-8") + b"\n"


def _bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8") + b"\n"


MULTI = (
    _record(chapter=1, speaker=SPEAKER_TWO, reasons=[FIRST], outcome="changed"),
    _record(chapter=1, speaker=SPEAKER_TEN, reasons=[FIRST, SECOND], outcome="changed"),
    _record(chapter=1, speaker=SPEAKER_TWO, reasons=[SECOND], outcome="kept_for_plot"),
    _record(chapter=1, speaker=SPEAKER_TWO, reasons=[FIRST], outcome="gate_failed", gate="made-up-gate"),
    _record(chapter=1, speaker=None, outcome="untouched"),
    _record(chapter=2, speaker=SPEAKER_TEN, reasons=[SECOND], outcome="changed"),
    _record(chapter=2, speaker=SPEAKER_TEN, outcome="unfinished"),
    _record(chapter=None, speaker=None, reasons=[FIRST], outcome="changed"),
)

EXPECTED_MULTI = """\
chapter 1: 2 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 1 untouched
  reason made-up-first-id: 2
  reason made-up-second-id: 1
  character 1: 1
  character 2: 1
  gate made-up-gate: 1
chapter 2: 1 changed, 0 kept for plot, 0 gate failed, 1 unfinished, 0 untouched
  reason made-up-second-id: 1
  character 2: 1
unattributed: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched
  reason made-up-first-id: 1
  no speaker: 1
total: 4 changed, 1 kept for plot, 1 gate failed, 1 unfinished, 1 untouched
  reason made-up-first-id: 3
  reason made-up-second-id: 2
  character 1: 1
  character 2: 2
  no speaker: 1
  gate made-up-gate: 1
scenes: 1 of 1 from the scene table, 0 outside it
arcs: 3, 2 with changes
redo: 0 listed, 0 changed, 0 gate failed
recall made-up-first-id: 4 disagreements, 1 confirmed
recall made-up-second-id: 0 disagreements, 0 confirmed
"""

# Made-up recall counts. Not game text.
RECALL_VALID = _bytes({FIRST: {"disagreements": 4, "confirmed": 1}, SECOND: {"disagreements": 0, "confirmed": 0}})
GOOD_ENTRY = {"disagreements": 1, "confirmed": 0}


def _scene_row(scene: str, namespace: str, key: str, order: int = 1) -> dict[str, object]:
    # Made-up scene table row. Not game text.
    return {"scene": scene, "order": order, "namespace": namespace, "key": key, "speaker": None, "voice": None}


DEFAULT_SCENE_ROWS = [_scene_row("made-up-scene", "SENTINEL-NAMESPACE", "SENTINEL-KEY")]


def _write_scene_table(repo: Path, rows: list[dict[str, object]]) -> Path:
    folder = repo / "vanilla"
    folder.mkdir(exist_ok=True)
    (folder / "BUILDID").write_text(f"{BUILD}\n")
    path = folder / "scenes.json"
    path.write_text(json.dumps({"build": BUILD, "lines": rows}), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    (root / "lenses").mkdir()
    (root / "lenses" / f"{LENS}.toml").write_text(LENS_TOML)
    monkeypatch.chdir(root)
    monkeypatch.setenv("FFR_VANILLA", str(root / "vanilla"))
    _write_scene_table(root, DEFAULT_SCENE_ROWS)
    return root


def _write(repo: Path, content: bytes) -> Path:
    path = repo / "work" / LENS / "outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path.resolve()


def _write_arcs(repo: Path, content: bytes = ARCS_VALID) -> Path:
    path = repo / "work" / LENS / "arcs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path.resolve()


def _write_recall(repo: Path, content: bytes = RECALL_VALID) -> Path:
    path = repo / "work" / LENS / "recall.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path.resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ffrd")
    report.register(parser.add_subparsers(required=True))
    return parser


def _invoke() -> None:
    args = _parser().parse_args(["report", "--lens", LENS])
    args.fn(args)


def test_prints_exact_counts_per_chapter(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    assert capsys.readouterr().out == EXPECTED_MULTI


def test_every_line_matches_grammar_and_shows_no_text_keys_or_speakers(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert LINE.fullmatch(line), line
    for leaked in (*SENTINELS, SPEAKER_TWO, SPEAKER_TEN):
        assert leaked not in out


def test_character_numbers_do_not_move_when_an_outcome_changes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # SPEAKER_TWO appears only as untouched, so it is numbered 1 but not printed; SPEAKER_TEN is numbered 2.
    _write_arcs(repo)
    _write_recall(repo)
    _write(
        repo,
        _lines(
            _record(speaker=SPEAKER_TWO, outcome="untouched"),
            _record(speaker=SPEAKER_TEN, reasons=[FIRST], outcome="changed"),
        ),
    )
    _invoke()
    before = capsys.readouterr().out.splitlines()
    assert "  character 2: 1" in before
    assert not any(line.startswith("  character 1:") for line in before)

    # Flipping SPEAKER_TWO's line to changed must leave SPEAKER_TEN at number 2.
    _write(
        repo,
        _lines(
            _record(speaker=SPEAKER_TWO, reasons=[FIRST], outcome="changed"),
            _record(speaker=SPEAKER_TEN, reasons=[FIRST], outcome="changed"),
        ),
    )
    _invoke()
    after = capsys.readouterr().out.splitlines()
    assert "  character 1: 1" in after
    assert "  character 2: 1" in after


KEPT = (
    _record(chapter=1, speaker=SPEAKER_TWO, outcome="untouched", keep_reason="plot"),
    _record(chapter=1, speaker=SPEAKER_TEN, outcome="untouched", keep_reason="earned"),
    _record(chapter=1, speaker=None, outcome="untouched", keep_reason="none"),
    _record(chapter=2, speaker=None, outcome="untouched", keep_reason="earned"),
)

EXPECTED_KEPT = """\
chapter 1: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 3 untouched
  kept earned: 1
  kept plot: 1
  kept none: 1
chapter 2: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched
  kept earned: 1
total: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 4 untouched
  kept earned: 2
  kept plot: 1
  kept none: 1
scenes: 1 of 1 from the scene table, 0 outside it
arcs: 3, 2 with changes
redo: 0 listed, 0 changed, 0 gate failed
recall made-up-first-id: 4 disagreements, 1 confirmed
recall made-up-second-id: 0 disagreements, 0 confirmed
"""


def test_kept_lines_print_per_block_and_total_in_enum_order_and_only_when_nonzero(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _lines(*KEPT))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    assert capsys.readouterr().out == EXPECTED_KEPT


def test_record_without_keep_reason_is_accepted_and_counts_no_kept_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = {k: v for k, v in _record(outcome="untouched").items() if k != "keep_reason"}
    _write(repo, _lines(record))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    assert not any(line.startswith("  kept ") for line in capsys.readouterr().out.splitlines())


@pytest.mark.parametrize(
    "value",
    [
        None,
        "no-hit",
        "earned",
        "no-invented-motive",
        "gender-rule-narrow",
        "own-wish",
        "oaths-outside-romance",
        "protection",
        "plot",
        "none",
    ],
)
def test_keep_reason_accepts_null_and_every_listed_reason(repo: Path, value: str | None) -> None:
    _write(repo, _lines(_record(outcome="untouched", keep_reason=value)))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()


@pytest.mark.parametrize("value", ["made-up-plan-one", None])
def test_plan_string_or_null_parses_into_the_record(tmp_path: Path, value: str | None) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_bytes(_lines(_record(plan=value)))
    (record,) = report._records(path, frozenset({FIRST, SECOND}))
    assert record.plan == value


def test_absent_plan_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_bytes(_without("plan"))
    (record,) = report._records(path, frozenset({FIRST, SECOND}))
    assert record.plan is None


@pytest.mark.parametrize("value", [True, False])
def test_redo_true_or_false_parses_into_the_record(tmp_path: Path, value: bool) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_bytes(_lines(_record(redo=value)))
    (record,) = report._records(path, frozenset({FIRST, SECOND}))
    assert record.redo is value


def test_absent_redo_reads_as_false(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_bytes(_without("redo"))
    (record,) = report._records(path, frozenset({FIRST, SECOND}))
    assert record.redo is False


@pytest.mark.parametrize("value", ["queer", "trans", "untouched", "redirect", "not-redirected", None])
def test_bucket_null_or_listed_selection_parses_into_the_record(tmp_path: Path, value: str | None) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_bytes(_lines(_record(bucket=value)))
    (record,) = report._records(path, frozenset({FIRST, SECOND}))
    assert record.bucket == value


def test_absent_bucket_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_bytes(_without("bucket"))
    (record,) = report._records(path, frozenset({FIRST, SECOND}))
    assert record.bucket is None


# Made-up queering lens: the queering selection declared above the lens tables. Not game text.
QUEERING_LENS_TOML = 'selection = "queering"\n' + LENS_TOML

# Made-up outcomes for the queering block: eight records, the last one unmarked. Not game text.
QUEERING_RECORDS = (
    _record(chapter=1, speaker=SPEAKER_TWO, reasons=[FIRST], outcome="changed", bucket="queer"),
    _record(chapter=1, speaker=SPEAKER_TEN, reasons=[FIRST], outcome="changed", bucket="trans"),
    _record(chapter=1, speaker=SPEAKER_TWO, outcome="untouched", bucket="untouched"),
    _record(chapter=1, speaker=SPEAKER_TWO, reasons=[FIRST], outcome="changed", bucket="redirect"),
    _record(chapter=2, speaker=SPEAKER_TEN, outcome="gate_failed", gate="made-up-gate", bucket="queer"),
    _record(chapter=2, speaker=SPEAKER_TEN, outcome="gate_failed", gate="made-up-gate", bucket="redirect"),
    _record(chapter=2, speaker=SPEAKER_TEN, outcome="untouched", bucket="not-redirected"),
    _record(chapter=2, speaker=SPEAKER_TEN, outcome="untouched"),
)

EXPECTED_QUEERING = [
    "queering marked: 7",
    "queering bucket queer: 2",
    "queering bucket trans: 1",
    "queering bucket untouched: 1",
    "queering flirt marked: 3, redirect half: 2",
    "queering changed queer: 1",
    "queering changed trans: 1",
    "queering changed redirect: 1",
    "queering gate failed queer: 1",
    "queering gate failed trans: 0",
    "queering gate failed redirect: 1",
]


def test_queering_block_prints_exact_counts_after_redo_and_before_recall(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "lenses" / f"{LENS}.toml").write_text(QUEERING_LENS_TOML)
    _write(repo, _lines(*QUEERING_RECORDS))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert LINE.fullmatch(line), line
    lines = out.splitlines()
    redo = lines.index("redo: 0 listed, 0 changed, 0 gate failed")
    assert lines[redo + 1 :] == [
        *EXPECTED_QUEERING,
        "recall made-up-first-id: 4 disagreements, 1 confirmed",
        "recall made-up-second-id: 0 disagreements, 0 confirmed",
    ]


def test_lens_without_selection_prints_no_queering_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _lines(*QUEERING_RECORDS))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    assert not any(line.startswith("queering ") for line in capsys.readouterr().out.splitlines())


# Made-up outcomes for the redo line: four listed (one changed, two gate failed, one untouched),
# one listed as false, one with no redo field. Not game text.
REDO_RECORDS = (
    _record(outcome="changed", reasons=[FIRST], redo=True),
    _record(outcome="gate_failed", gate="redo", redo=True),
    _record(outcome="gate_failed", gate="made-up-gate", redo=True),
    _record(outcome="untouched", redo=True),
    _record(outcome="changed", reasons=[FIRST], redo=False),
    _record(outcome="changed", reasons=[FIRST]),
)


def test_redo_line_counts_listed_changed_and_gate_failed_right_after_arcs(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _lines(*REDO_RECORDS))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert LINE.fullmatch(line), line
    assert "SENTINEL" not in out
    lines = out.splitlines()
    arcs = lines.index("arcs: 3, 2 with changes")
    assert lines[arcs + 1] == "redo: 4 listed, 1 changed, 2 gate failed"
    assert lines[arcs + 2].startswith("recall made-up-first-id: ")


@pytest.mark.parametrize("value", ["made-up-reason", "Plot", 3])
def test_keep_reason_outside_the_listed_reasons_fails_naming_the_field(repo: Path, value: object) -> None:
    _write(repo, _lines(_record(outcome="untouched", keep_reason=value)))
    _write_arcs(repo)
    _write_recall(repo)
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert "keep_reason" in message
    assert "SENTINEL" not in message


SCENE_PATTERN = "made-up-scene-pattern"
# Made-up lens with a scene section. Not game text.
SCENE_LENS_TOML = (
    LENS_TOML
    + f"""
[scene]
patterns = [{{ id = "{SCENE_PATTERN}", question = "Made-up scene question. Not game text." }}]
criteria = ["Made-up scene criterion. Not game text."]
plan = "Made-up scene plan. Not game text."
rubric = "Made-up scene rubric. Not game text."
"""
)


def test_scene_pattern_id_is_an_accepted_reason_when_the_lens_has_a_scene(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "lenses" / f"{LENS}.toml").write_text(SCENE_LENS_TOML)
    _write(repo, _lines(_record(outcome="changed", reasons=[SCENE_PATTERN])))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    assert "total: 1 changed" in capsys.readouterr().out


def test_scene_pattern_id_is_rejected_when_the_lens_has_no_scene(repo: Path) -> None:
    _write(repo, _lines(_record(outcome="changed", reasons=[SCENE_PATTERN])))
    _write_arcs(repo)
    _write_recall(repo)
    with pytest.raises(Fail) as raised:
        _invoke()
    assert "field reasons has an id that is not a triage or scene pattern id of the lens" in str(raised.value)


def test_unknown_reason_still_fails_when_the_lens_has_a_scene(repo: Path) -> None:
    (repo / "lenses" / f"{LENS}.toml").write_text(SCENE_LENS_TOML)
    _write(repo, _lines(_record(outcome="changed", reasons=["SENTINEL-REASON"])))
    _write_arcs(repo)
    _write_recall(repo)
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert "field reasons has an id that is not a triage or scene pattern id of the lens" in message
    assert "SENTINEL" not in message


def test_missing_outcomes_file_fails_naming_the_file(repo: Path) -> None:
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert "outcomes.jsonl" in message
    assert "SENTINEL" not in message


def test_gate_lines_name_scene_and_arc_in_chapter_and_total_blocks(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Made-up records. Not game text.
    _write(
        repo,
        _lines(
            _record(chapter=1, speaker=None, outcome="gate_failed", gate="scene"),
            _record(chapter=1, speaker=None, outcome="gate_failed", gate="arc"),
            _record(chapter=2, speaker=None, outcome="gate_failed", gate="arc"),
        ),
    )
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    assert (
        capsys.readouterr().out
        == """\
chapter 1: 0 changed, 0 kept for plot, 2 gate failed, 0 unfinished, 0 untouched
  gate arc: 1
  gate scene: 1
chapter 2: 0 changed, 0 kept for plot, 1 gate failed, 0 unfinished, 0 untouched
  gate arc: 1
total: 0 changed, 0 kept for plot, 3 gate failed, 0 unfinished, 0 untouched
  gate arc: 2
  gate scene: 1
scenes: 1 of 1 from the scene table, 0 outside it
arcs: 3, 2 with changes
redo: 0 listed, 0 changed, 0 gate failed
recall made-up-first-id: 4 disagreements, 1 confirmed
recall made-up-second-id: 0 disagreements, 0 confirmed
"""
    )


# Made-up scene table: alpha (two lines), beta and gamma. Not game text.
SCENE_ROWS = [
    _scene_row("made-up-scene-alpha", "ST_ALPHA_001_01", "1", order=1),
    _scene_row("made-up-scene-alpha", "ST_ALPHA_001_01", "2", order=2),
    _scene_row("made-up-scene-beta", "ST_BETA_001_01", "1"),
    _scene_row("made-up-scene-gamma", "ST_GAMMA_001_01", "1"),
]

# Made-up outcomes: alpha and beta are covered, gamma is not. Two namespaces lie outside the table.
SCENE_RECORDS = (
    _record(namespace="ST_ALPHA_001_01", key="2", outcome="untouched"),
    _record(namespace="ST_BETA_001_01", key="1", outcome="untouched"),
    _record(namespace="ST_OUT_001_01", key="1", outcome="untouched"),
    _record(namespace="ST_OUT_001_01", key="2", outcome="untouched"),
    _record(namespace="ST_OUT_001_01", key="3", speaker=None, reasons=[FIRST], outcome="changed"),
    _record(namespace="ST_ELSE_003_01", key="1", outcome="untouched"),
)

EXPECTED_SCENES = """\
chapter 1: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 5 untouched
  reason made-up-first-id: 1
  no speaker: 1
total: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 5 untouched
  reason made-up-first-id: 1
  no speaker: 1
scenes: 2 of 3 from the scene table, 2 outside it
arcs: 3, 2 with changes
redo: 0 listed, 0 changed, 0 gate failed
recall made-up-first-id: 4 disagreements, 1 confirmed
recall made-up-second-id: 0 disagreements, 0 confirmed
"""


def test_scene_line_counts_covered_scenes_and_outside_namespaces_after_total(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_scene_table(repo, SCENE_ROWS)
    _write(repo, _lines(*SCENE_RECORDS))
    _write_arcs(repo)
    _write_recall(repo)
    _invoke()
    assert capsys.readouterr().out == EXPECTED_SCENES


def test_missing_scene_table_fails_naming_the_scene_table(repo: Path) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    _write_recall(repo)
    (repo / "vanilla" / "scenes.json").unlink()
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert "scene table" in message
    assert "SENTINEL" not in message


def test_arcs_line_accepts_zero_totals(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo, b'{"arcs": 0, "with_changes": 0}\n')
    _write_recall(repo)
    _invoke()
    assert "arcs: 0, 0 with changes" in capsys.readouterr().out.splitlines()


def test_missing_arcs_file_fails_naming_the_file(repo: Path) -> None:
    _write(repo, _lines(*MULTI))
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert "arcs.json" in message
    assert "SENTINEL" not in message


def test_recall_lines_follow_arcs_in_lens_order(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    # Made-up recall counts with the second id first in the file. Not game text.
    _write_recall(repo, _bytes({SECOND: {"disagreements": 2, "confirmed": 2}, FIRST: {"disagreements": 5, "confirmed": 3}}))
    _invoke()
    assert capsys.readouterr().out.splitlines()[-4:] == [
        "arcs: 3, 2 with changes",
        "redo: 0 listed, 0 changed, 0 gate failed",
        "recall made-up-first-id: 5 disagreements, 3 confirmed",
        "recall made-up-second-id: 2 disagreements, 2 confirmed",
    ]


def test_missing_recall_file_fails_naming_the_file(repo: Path) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert "recall.json" in message
    assert "SENTINEL" not in message


BAD_RECALL = [
    pytest.param(b"SENTINEL-NOT-JSON {\n", "is not valid JSON", id="not-json"),
    pytest.param(b'["SENTINEL-ARRAY"]\n', "is not a JSON object", id="not-object"),
    pytest.param(_bytes({FIRST: GOOD_ENTRY}), f"triage id {SECOND} is missing", id="id-missing"),
    pytest.param(
        _bytes({FIRST: GOOD_ENTRY, SECOND: GOOD_ENTRY, "SENTINEL-UNKNOWN": GOOD_ENTRY}),
        "has a key that is not a triage id of the lens",
        id="id-unknown",
    ),
    pytest.param(_bytes({FIRST: "SENTINEL-ENTRY", SECOND: GOOD_ENTRY}), f"triage id {FIRST} is not an object", id="entry-type"),
    pytest.param(
        _bytes({FIRST: {"disagreements": 1}, SECOND: GOOD_ENTRY}),
        f"triage id {FIRST}: field confirmed is missing",
        id="field-missing",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": 1, "confirmed": 0, "SENTINEL-EXTRA": 1}, SECOND: GOOD_ENTRY}),
        f"triage id {FIRST}: has a field that is not disagreements or confirmed",
        id="field-extra",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": "SENTINEL-COUNT", "confirmed": 0}, SECOND: GOOD_ENTRY}),
        "field disagreements must be a non-negative integer",
        id="disagreements-type",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": 1.5, "confirmed": 0}, SECOND: GOOD_ENTRY}),
        "field disagreements must be a non-negative integer",
        id="disagreements-float",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": True, "confirmed": 0}, SECOND: GOOD_ENTRY}),
        "field disagreements must be a non-negative integer",
        id="disagreements-bool",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": -1, "confirmed": 0}, SECOND: GOOD_ENTRY}),
        "field disagreements must be a non-negative integer",
        id="disagreements-negative",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": 1, "confirmed": "SENTINEL-COUNT"}, SECOND: GOOD_ENTRY}),
        "field confirmed must be a non-negative integer",
        id="confirmed-type",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": 1, "confirmed": -1}, SECOND: GOOD_ENTRY}),
        "field confirmed must be a non-negative integer",
        id="confirmed-negative",
    ),
    pytest.param(
        _bytes({FIRST: {"disagreements": 1, "confirmed": 2}, SECOND: GOOD_ENTRY}),
        "field confirmed must not exceed disagreements",
        id="confirmed-above-disagreements",
    ),
]


@pytest.mark.parametrize(("bad", "fragment"), BAD_RECALL)
def test_invalid_recall_fails_naming_file_and_field_but_not_the_value(repo: Path, bad: bytes, fragment: str) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    path = _write_recall(repo, bad)
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert str(path) in message
    assert fragment in message
    assert "SENTINEL" not in message


BAD_ARCS = [
    pytest.param(b"SENTINEL-NOT-JSON {\n", "is not valid JSON", id="not-json"),
    pytest.param(b'["SENTINEL-ARRAY"]\n', "is not a JSON object", id="not-object"),
    pytest.param(b'{"with_changes": 0}\n', "field arcs is missing", id="arcs-missing"),
    pytest.param(b'{"arcs": 3}\n', "field with_changes is missing", id="with-changes-missing"),
    pytest.param(b'{"arcs": "SENTINEL-ARCS", "with_changes": 0}\n', "field arcs must be a non-negative integer", id="arcs-type"),
    pytest.param(b'{"arcs": 2.5, "with_changes": 0}\n', "field arcs must be a non-negative integer", id="arcs-float"),
    pytest.param(b'{"arcs": true, "with_changes": 0}\n', "field arcs must be a non-negative integer", id="arcs-bool"),
    pytest.param(b'{"arcs": -1, "with_changes": 0}\n', "field arcs must be a non-negative integer", id="arcs-negative"),
    pytest.param(
        b'{"arcs": 3, "with_changes": "SENTINEL-CHANGES"}\n', "field with_changes must be a non-negative integer", id="with-changes-type"
    ),
    pytest.param(b'{"arcs": 3, "with_changes": -1}\n', "field with_changes must be a non-negative integer", id="with-changes-negative"),
    pytest.param(b'{"arcs": 3, "with_changes": 4}\n', "field with_changes must not exceed arcs", id="with-changes-above-arcs"),
]


@pytest.mark.parametrize(("bad", "fragment"), BAD_ARCS)
def test_invalid_arcs_fails_naming_file_and_field_but_not_the_value(repo: Path, bad: bytes, fragment: str) -> None:
    _write(repo, _lines(*MULTI))
    path = _write_arcs(repo, bad)
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert str(path) in message
    assert fragment in message
    assert "SENTINEL" not in message


BAD_LINES = [
    pytest.param(b"SENTINEL-NOT-JSON {\n", "is not valid JSON", id="not-json"),
    pytest.param(b'["SENTINEL-ARRAY"]\n', "is not a JSON object", id="not-object"),
    pytest.param(b"\xff\xfe\n", "is not valid UTF-8", id="not-utf-8"),
    pytest.param(_lines(_record(namespace=7)), "field namespace", id="namespace-type"),
    pytest.param(_without("key"), "field key", id="key-missing"),
    pytest.param(_lines(_record(chapter="SENTINEL-CHAPTER")), "field chapter", id="chapter-type"),
    pytest.param(_lines(_record(chapter=0)), "field chapter", id="chapter-zero"),
    pytest.param(_lines(_record(speaker=["SENTINEL-SPEAKER"])), "field speaker", id="speaker-type"),
    pytest.param(_lines(_record(reasons="SENTINEL-REASONS")), "field reasons", id="reasons-type"),
    pytest.param(
        _lines(_record(reasons=["SENTINEL-REASON"])),
        "field reasons has an id that is not a triage or scene pattern id of the lens",
        id="reason-not-in-lens",
    ),
    pytest.param(_lines(_record(plan=["SENTINEL-PLAN"])), "field plan must be a string or null", id="plan-type"),
    pytest.param(_lines(_record(plan=3)), "field plan must be a string or null", id="plan-number"),
    pytest.param(_lines(_record(outcome="SENTINEL-OUTCOME")), "field outcome", id="outcome-unknown"),
    pytest.param(_lines(_record(outcome="gate_failed", gate=None)), "field gate", id="gate-missing"),
    pytest.param(_lines(_record(outcome="gate_failed", gate=3)), "field gate", id="gate-type"),
    pytest.param(_lines(_record(outcome="gate_failed", gate="SENTINEL Bad Gate")), "field gate", id="gate-name"),
    pytest.param(_lines(_record(redo="SENTINEL-REDO")), "field redo must be true or false", id="redo-type"),
    pytest.param(_lines(_record(redo=1)), "field redo must be true or false", id="redo-number"),
    pytest.param(_lines(_record(redo=None)), "field redo must be true or false", id="redo-null"),
    pytest.param(
        _lines(_record(bucket="SENTINEL-BUCKET")),
        "field bucket is not a queering bucket or null",
        id="bucket-unknown",
    ),
    pytest.param(_lines(_record(bucket=3)), "field bucket is not a queering bucket or null", id="bucket-number"),
]


@pytest.mark.parametrize(("bad", "fragment"), BAD_LINES)
def test_invalid_line_fails_naming_file_line_and_field_but_not_the_value(repo: Path, bad: bytes, fragment: str) -> None:
    # Line 1 is valid, so the bad line is line 2.
    path = _write(repo, _lines(_record()) + bad)
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert f"{path}:2" in message
    assert fragment in message
    assert "SENTINEL" not in message


def test_lens_is_required_and_has_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as missing:
        _parser().parse_args(["report"])
    assert missing.value.code == 2
    assert "--lens" in capsys.readouterr().err

    with pytest.raises(SystemExit) as helped:
        _parser().parse_args(["report", "--help"])
    assert helped.value.code == 0
    assert "--lens" in capsys.readouterr().out


OTHER_PATTERN = "made-up-other-pattern"
TRIAGE_IDS = [FIRST, SECOND]
PLAN = "made-up-plan"
SCENE_NAMES = ("made-up-scene-a", "made-up-scene-b", "made-up-scene-c", "made-up-scene-d", "made-up-scene-e")

# Made-up lens with two scene patterns. Not game text.
SCENE_TWO_LENS_TOML = (
    LENS_TOML
    + f"""
[scene]
patterns = [{{ id = "{SCENE_PATTERN}", question = "Made-up scene question. Not game text." }}, {{ id = "{OTHER_PATTERN}", question = "Made-up other question. Not game text." }}]
criteria = ["Made-up scene criterion. Not game text."]
plan = "Made-up scene plan. Not game text."
rubric = "Made-up scene rubric. Not game text."
"""
)


def _outcome(speaker: str | None, outcome: str, chapter: int | None = 1, plan: str | None = None) -> report.Record:
    # Made-up outcome record. Not game text.
    return report.Record(chapter=chapter, speaker=speaker, reasons=frozenset(), outcome=outcome, gate=None, keep_reason=None, plan=plan)


def _scene(
    scene: str,
    chapter: int | None,
    *,
    eligible: bool = True,
    patterns: tuple[str, ...] = (),
    plan: str | None = None,
    keep_reason: str | None = None,
    planned: int = 0,
    required: int = 0,
    changed: int = 0,
    reverted: int = 0,
    replanned: int = 0,
    gate_failures: dict[str, int] | None = None,
) -> SceneRecord:
    # Made-up scene record. Not game text.
    return SceneRecord(
        scene=scene,
        chapter=chapter,
        eligible=eligible,
        patterns=patterns,
        plan=plan,
        keep_reason=keep_reason,
        planned=planned,
        required=required,
        changed=changed,
        reverted=reverted,
        replanned=replanned,
        gate_failures=gate_failures or {},
    )


SCENE_OUTCOMES = (
    _record(chapter=1, speaker=SPEAKER_TWO, outcome="changed", plan="made-up-plan-one"),
    _record(chapter=1, speaker=SPEAKER_TEN, outcome="changed", plan="made-up-plan-two"),
    _record(chapter=1, speaker=SPEAKER_TWO, outcome="untouched"),
    _record(chapter=2, speaker=SPEAKER_TEN, outcome="changed", plan=None),
    _record(chapter=None, speaker=None, outcome="untouched"),
)

SCENE_FIXTURES = (
    _scene(
        "made-up-scene-a",
        1,
        patterns=(SCENE_PATTERN,),
        plan="made-up-plan-one",
        planned=3,
        required=2,
        changed=2,
        reverted=1,
        replanned=1,
        gate_failures={"made-up-gate": 1},
    ),
    _scene(
        "made-up-scene-b",
        1,
        patterns=(SCENE_PATTERN, OTHER_PATTERN),
        plan="made-up-plan-two",
        keep_reason="plot",
        planned=2,
        required=2,
        changed=1,
    ),
    _scene("made-up-scene-c", 1, eligible=False, keep_reason="trait"),
    _scene(
        "made-up-scene-d",
        2,
        patterns=(OTHER_PATTERN,),
        plan="made-up-plan-three",
        planned=1,
        changed=1,
        gate_failures={"made-up-gate": 2},
    ),
    _scene("made-up-scene-e", None),
)

# Made-up counts. Not game text.
CHAPTER_ONE_SCENE_LINES = (
    "  scene eligible: 2",
    "  scene flagged made-up-scene-pattern: 2",
    "  scene flagged made-up-other-pattern: 1",
    "  scene plans: 2",
    "  scene kept plot: 1",
    "  scene kept trait: 1",
    "  scene planned lines: 5 (required 4)",
    "  scene changed lines: 3",
    "  scene replanned: 1",
    "  scene reverted lines: 1",
    "  scene gate made-up-gate: 1",
    "  scene changed per scene: max 2, median 1.5",
    "  scene character 1: 1",
    "  scene character 2: 1",
)

EXPECTED_SCENE_REPORT = """\
chapter 1: 2 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched
  character 1: 1
  character 2: 1
  scene eligible: 2
  scene flagged made-up-scene-pattern: 2
  scene flagged made-up-other-pattern: 1
  scene plans: 2
  scene kept plot: 1
  scene kept trait: 1
  scene planned lines: 5 (required 4)
  scene changed lines: 3
  scene replanned: 1
  scene reverted lines: 1
  scene gate made-up-gate: 1
  scene changed per scene: max 2, median 1.5
  scene character 1: 1
  scene character 2: 1
chapter 2: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched
  character 2: 1
  scene eligible: 1
  scene flagged made-up-scene-pattern: 0
  scene flagged made-up-other-pattern: 1
  scene plans: 1
  scene planned lines: 1 (required 0)
  scene changed lines: 1
  scene replanned: 0
  scene reverted lines: 0
  scene gate made-up-gate: 2
  scene changed per scene: max 1, median 1
unattributed: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched
  scene eligible: 1
  scene flagged made-up-scene-pattern: 0
  scene flagged made-up-other-pattern: 0
  scene plans: 0
  scene planned lines: 0 (required 0)
  scene changed lines: 0
  scene replanned: 0
  scene reverted lines: 0
  scene changed per scene: max 0, median 0
total: 3 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 2 untouched
  character 1: 1
  character 2: 2
  scene eligible: 4
  scene flagged made-up-scene-pattern: 2
  scene flagged made-up-other-pattern: 2
  scene plans: 3
  scene kept plot: 1
  scene kept trait: 1
  scene planned lines: 6 (required 4)
  scene changed lines: 4
  scene replanned: 1
  scene reverted lines: 1
  scene gate made-up-gate: 3
  scene changed per scene: max 2, median 1
  scene character 1: 1
  scene character 2: 1
scenes: 1 of 1 from the scene table, 0 outside it
arcs: 3, 2 with changes
redo: 0 listed, 0 changed, 0 gate failed
recall made-up-first-id: 4 disagreements, 1 confirmed
recall made-up-second-id: 0 disagreements, 0 confirmed
"""


def _write_scenes(repo: Path, scenes: list[SceneRecord]) -> Path:
    path = repo / "work" / LENS / SCENES
    path.parent.mkdir(parents=True, exist_ok=True)
    write_scenes(path, scenes)
    return path.resolve()


def _scene_report(repo: Path) -> None:
    (repo / "lenses" / f"{LENS}.toml").write_text(SCENE_TWO_LENS_TOML)
    _write(repo, _lines(*SCENE_OUTCOMES))
    _write_arcs(repo)
    _write_recall(repo)
    _write_scenes(repo, list(SCENE_FIXTURES))


def test_scene_counts_per_chapter_unattributed_and_total(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _scene_report(repo)
    _invoke()
    assert capsys.readouterr().out == EXPECTED_SCENE_REPORT


def test_scene_report_lines_match_grammar_and_show_no_text_keys_speakers_or_scene_ids(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scene_report(repo)
    _invoke()
    out = capsys.readouterr().out
    assert any(line.startswith("  scene ") for line in out.splitlines())
    for line in out.splitlines():
        assert LINE.fullmatch(line), line
    for leaked in (*SENTINELS, SPEAKER_TWO, SPEAKER_TEN, *SCENE_NAMES):
        assert leaked not in out


def test_scene_pattern_id_outside_the_lens_fails_naming_file_line_and_field(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "lenses" / f"{LENS}.toml").write_text(SCENE_TWO_LENS_TOML)
    _write(repo, _lines(*SCENE_OUTCOMES))
    _write_arcs(repo)
    _write_recall(repo)
    path = _write_scenes(
        repo,
        [_scene("made-up-scene-a", 1), _scene("made-up-scene-b", 1, patterns=("made-up-unknown-pattern",))],
    )
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert f"{path}:2: field patterns has an id that is not a scene pattern id of the lens" in message
    assert "made-up-unknown-pattern" not in message
    assert capsys.readouterr().out == ""


def test_render_without_scenes_is_the_outcome_report_alone() -> None:
    records = [_outcome(SPEAKER_TWO, "changed")]
    lines = report.render(records, TRIAGE_IDS)
    assert lines == [
        "chapter 1: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched",
        "  character 1: 1",
        "total: 1 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched",
        "  character 1: 1",
    ]
    assert report.render(records, TRIAGE_IDS, scene_ids=(SCENE_PATTERN,)) == lines


def test_render_with_chapter_one_scenes_gives_the_provisional_block() -> None:
    records = [
        _outcome(SPEAKER_TWO, "changed", plan="made-up-plan-one"),
        _outcome(SPEAKER_TEN, "changed", plan="made-up-plan-two"),
        _outcome(SPEAKER_TWO, "untouched"),
    ]
    lines = report.render(records, TRIAGE_IDS, scenes=list(SCENE_FIXTURES[:3]), scene_ids=(SCENE_PATTERN, OTHER_PATTERN))
    summary = "2 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched"
    assert lines == [
        f"chapter 1: {summary}",
        "  character 1: 1",
        "  character 2: 1",
        *CHAPTER_ONE_SCENE_LINES,
        f"total: {summary}",
        "  character 1: 1",
        "  character 2: 1",
        *CHAPTER_ONE_SCENE_LINES,
    ]


def test_scene_only_chapters_and_unattributed_get_blocks() -> None:
    lines = report.render([], TRIAGE_IDS, scenes=[_scene("made-up-scene-x", 3), _scene("made-up-scene-y", None)])
    assert [line for line in lines if not line.startswith("  ")] == [
        "chapter 3: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched",
        "unattributed: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched",
        "total: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 0 untouched",
    ]


def _planned(*changed: int) -> list[SceneRecord]:
    return [_scene(f"made-up-scene-{index}", 1, plan=PLAN, changed=value) for index, value in enumerate(changed)]


MEDIAN_CASES = [
    pytest.param([], "max 0, median 0", id="no-scenes"),
    pytest.param([_scene("made-up-scene-a", 1, changed=5)], "max 0, median 0", id="no-plan-not-counted"),
    pytest.param(_planned(3), "max 3, median 3", id="whole-one-scene"),
    pytest.param(_planned(1, 2), "max 2, median 1.5", id="half"),
    pytest.param(_planned(4, 1, 2), "max 4, median 2", id="whole-odd-count"),
]


@pytest.mark.parametrize(("scenes", "expected"), MEDIAN_CASES)
def test_changed_per_scene_median_prints_whole_or_one_decimal(scenes: list[SceneRecord], expected: str) -> None:
    lines = report.render([], TRIAGE_IDS, scenes=scenes)
    assert f"  scene changed per scene: {expected}" in lines


def test_scene_character_numbers_are_the_outcome_numbers() -> None:
    # SPEAKER_TWO's only changed line has no plan, so it is numbered 1 and printed for outcomes but not for scenes.
    records = [
        _outcome(SPEAKER_TWO, "untouched"),
        _outcome(SPEAKER_TWO, "changed"),
        _outcome(SPEAKER_TEN, "changed", plan=PLAN),
    ]
    lines = report.render(records, TRIAGE_IDS, scenes=[_scene("made-up-scene-a", 1, plan=PLAN, changed=1)])
    assert "  character 1: 1" in lines
    assert "  scene character 2: 1" in lines
    assert not any(line.startswith("  scene character 1:") for line in lines)


# Made-up usage run ids. Not game text. Ids sort as text, so run-9 is the latest although run-10 is numerically greater.
RUN_LATEST = "made-up-run-9"
RUN_EARLIER = "made-up-run-10"
STAGE_A = "made-up-stage-a"
STAGE_B = "made-up-stage-b"
MODEL_X = "made-up-model-x"
MODEL_Y = "made-up-model-y"


def _usage_line(**fields: object) -> dict[str, object]:
    # Made-up usage entry. Not game text.
    entry: dict[str, object] = {
        "run": RUN_LATEST,
        "stage": STAGE_A,
        "model": MODEL_X,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "duration_ms": 0,
        "cost_usd": 0.0,
    }
    entry.update(fields)
    return entry


def _usage_without(field: str) -> bytes:
    return _bytes({k: v for k, v in _usage_line().items() if k != field})


def _write_usage(repo: Path, content: bytes) -> Path:
    path = repo / "work" / LENS / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path.resolve()


USAGE = (
    _usage_line(
        run=RUN_EARLIER,
        stage=STAGE_A,
        model=MODEL_X,
        input_tokens=999,
        output_tokens=999,
        cache_read_tokens=999,
        cache_creation_tokens=999,
        duration_ms=999,
        cost_usd=99.0,
    ),
    _usage_line(
        run=RUN_LATEST,
        stage=STAGE_B,
        model=MODEL_Y,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=2,
        cache_creation_tokens=1,
        duration_ms=100,
        cost_usd=1.5,
    ),
    _usage_line(
        run=RUN_LATEST,
        stage=STAGE_A,
        model=MODEL_X,
        input_tokens=7,
        output_tokens=3,
        cache_read_tokens=0,
        cache_creation_tokens=4,
        duration_ms=50,
        cost_usd=0.25,
    ),
    _usage_line(
        run=RUN_LATEST,
        stage=STAGE_A,
        model=MODEL_X,
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=3,
        cache_creation_tokens=0,
        duration_ms=5,
        cost_usd=0.5,
    ),
    _usage_line(
        run=RUN_EARLIER,
        stage=STAGE_B,
        model=MODEL_Y,
        input_tokens=999,
        output_tokens=999,
        cache_read_tokens=999,
        cache_creation_tokens=999,
        duration_ms=999,
        cost_usd=99.0,
    ),
)

# Sums for the latest run only. Not game text.
EXPECTED_USAGE = (
    f"usage run {RUN_LATEST}\n"
    f"usage {STAGE_A} {MODEL_X}: 2 calls, 8 input, 5 output, 3 cache read, 4 cache write, $0.75\n"
    f"usage {STAGE_B} {MODEL_Y}: 1 calls, 10 input, 5 output, 2 cache read, 1 cache write, $1.50\n"
    "usage total: 3 calls, 18 input, 10 output, 5 cache read, 5 cache write, $2.25\n"
)


def test_usage_block_after_recall_prints_latest_run_only_with_sums_per_stage_and_model(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    _write_recall(repo)
    _write_usage(repo, _lines(*USAGE))
    _invoke()
    assert capsys.readouterr().out == EXPECTED_MULTI + EXPECTED_USAGE


@pytest.mark.parametrize("usage", [pytest.param(None, id="no-file"), pytest.param(b"", id="no-entry")])
def test_no_usage_file_or_entry_prints_the_same_output_as_before(
    repo: Path, capsys: pytest.CaptureFixture[str], usage: bytes | None
) -> None:
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    _write_recall(repo)
    if usage is not None:
        _write_usage(repo, usage)
    _invoke()
    assert capsys.readouterr().out == EXPECTED_MULTI


BAD_USAGE = [
    pytest.param(b"SENTINEL-NOT-JSON {\n", "is not valid JSON", id="not-json"),
    pytest.param(b'["SENTINEL-ARRAY"]\n', "is not a JSON object", id="not-object"),
    pytest.param(b'"\xff"\n', "is not valid UTF-8", id="not-utf8"),
    pytest.param(_usage_without("run"), "field run is missing", id="run-missing"),
    pytest.param(_usage_without("cost_usd"), "field cost_usd is missing", id="cost-missing"),
    pytest.param(_bytes(_usage_line(stage=3)), "field stage must be a string", id="stage-number"),
    pytest.param(_bytes(_usage_line(model=None)), "field model must be a string", id="model-null"),
    pytest.param(_bytes(_usage_line(input_tokens=-1)), "field input_tokens must be a non-negative integer", id="input-negative"),
    pytest.param(_bytes(_usage_line(output_tokens=1.5)), "field output_tokens must be a non-negative integer", id="output-float"),
    pytest.param(
        _bytes(_usage_line(cache_read_tokens=True)), "field cache_read_tokens must be a non-negative integer", id="cache-read-bool"
    ),
    pytest.param(
        _bytes(_usage_line(cache_creation_tokens="SENTINEL-TOKENS")),
        "field cache_creation_tokens must be a non-negative integer",
        id="cache-write-string",
    ),
    pytest.param(_bytes(_usage_line(duration_ms=-5)), "field duration_ms must be a non-negative integer", id="duration-negative"),
    pytest.param(_bytes(_usage_line(cost_usd="SENTINEL-COST")), "field cost_usd must be a non-negative number", id="cost-string"),
    pytest.param(_bytes(_usage_line(cost_usd=True)), "field cost_usd must be a non-negative number", id="cost-bool"),
    pytest.param(_bytes(_usage_line(cost_usd=-0.5)), "field cost_usd must be a non-negative number", id="cost-negative"),
    pytest.param(_bytes(_usage_line(cost_usd=float("nan"))), "field cost_usd must be a non-negative number", id="cost-nan"),
]


@pytest.mark.parametrize(("bad", "fragment"), BAD_USAGE)
def test_invalid_usage_line_fails_naming_file_line_and_field_but_not_the_value(repo: Path, bad: bytes, fragment: str) -> None:
    # Line 1 is valid, so the bad line is line 2.
    path = _write_usage(repo, _lines(_usage_line()) + bad)
    _write(repo, _lines(*MULTI))
    _write_arcs(repo)
    _write_recall(repo)
    with pytest.raises(Fail) as raised:
        _invoke()
    message = str(raised.value)
    assert f"{path}:2" in message
    assert fragment in message
    assert "SENTINEL" not in message
