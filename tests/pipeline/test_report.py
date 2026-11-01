import argparse
import json
import re
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.pipeline import report

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
DETAIL = r"  (reason [a-z0-9]+(?:-[a-z0-9]+)*|character [0-9]+|no speaker|gate [a-z0-9]+(?:[-_][a-z0-9]+)*): [0-9]+"
ARCS_LINE = r"arcs: [0-9]+, [0-9]+ with changes"
RECALL_LINE = r"recall [a-z0-9]+(?:-[a-z0-9]+)*: [0-9]+ disagreements, [0-9]+ confirmed"
LINE = re.compile(f"{SUMMARY}|{DETAIL}|{ARCS_LINE}|{RECALL_LINE}")

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
arcs: 3, 2 with changes
recall made-up-first-id: 4 disagreements, 1 confirmed
recall made-up-second-id: 0 disagreements, 0 confirmed
"""

# Made-up recall counts. Not game text.
RECALL_VALID = _bytes({FIRST: {"disagreements": 4, "confirmed": 1}, SECOND: {"disagreements": 0, "confirmed": 0}})
GOOD_ENTRY = {"disagreements": 1, "confirmed": 0}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    (root / "lenses").mkdir()
    (root / "lenses" / f"{LENS}.toml").write_text(LENS_TOML)
    monkeypatch.chdir(root)
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
arcs: 3, 2 with changes
recall made-up-first-id: 4 disagreements, 1 confirmed
recall made-up-second-id: 0 disagreements, 0 confirmed
"""
    )


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
    assert capsys.readouterr().out.splitlines()[-3:] == [
        "arcs: 3, 2 with changes",
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
    pytest.param(_lines(_record(reasons=["SENTINEL-REASON"])), "field reasons", id="reason-not-in-lens"),
    pytest.param(_lines(_record(outcome="SENTINEL-OUTCOME")), "field outcome", id="outcome-unknown"),
    pytest.param(_lines(_record(outcome="gate_failed", gate=None)), "field gate", id="gate-missing"),
    pytest.param(_lines(_record(outcome="gate_failed", gate=3)), "field gate", id="gate-type"),
    pytest.param(_lines(_record(outcome="gate_failed", gate="SENTINEL Bad Gate")), "field gate", id="gate-name"),
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
