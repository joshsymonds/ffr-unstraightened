import json
import tomllib
from pathlib import Path
from typing import Literal

import pytest

from ffrd.game import Fail, vanilla_hash
from ffrd.pipeline import report
from ffrd.pipeline.run import Change, Outcome, write_dialog, write_outcomes

LENS = "made-up-lens"
RUN = "made-up-run-id"
TRIAGE_ORDER = ["made-up-first-id", "made-up-second-id"]
TRIAGE = frozenset(TRIAGE_ORDER)
OUTCOME_KEYS = {"namespace", "key", "chapter", "speaker", "reasons", "outcome", "gate", "writer"}
FIELD_ORDER = ["vanilla", "text", "lens", "reason", "writer", "verdict", "run"]

# Made-up lines. Not game text.
EN_ONE = "Made-up English one. Not game text."
EN_TWO = "Made-up English two. Not game text."
TEXT_ONE = "Made-up rewrite one. Not game text."
TEXT_TWO = "Made-up rewrite two. Not game text."
TEXT_TRICKY = 'Made-up "quoted" \\ line.\nSecond\tline. Made-up ünïcode. Not game text.'
EN_TRICKY = "Made-up English tricky. Not game text."

OUTCOMES = [
    Outcome("made-up-scene-a", "1", 1, "made-up-speaker-2", ("made-up-first-id",), "changed", None, "voice"),
    Outcome("made-up-scene-a", "2", 1, "made-up-speaker-10", ("made-up-first-id", "made-up-second-id"), "changed", None, "opus"),
    Outcome("made-up-scene-a", "3", 1, "made-up-speaker-2", ("made-up-second-id",), "kept_for_plot", None, None),
    Outcome("made-up-scene-a", "4", 1, "made-up-speaker-2", ("made-up-first-id",), "gate_failed", "made-up-gate", "voice"),
    Outcome("made-up-scene-b", "5", None, None, (), "untouched", None, None),
    Outcome("made-up-scene-b", "6", 2, "made-up-speaker-10", ("made-up-second-id",), "unfinished", None, None),
]

EXPECTED_SUMMARY = """\
chapter 1: 2 changed, 1 kept for plot, 1 gate failed, 0 unfinished, 0 untouched
  reason made-up-first-id: 2
  reason made-up-second-id: 1
  character 1: 1
  character 2: 1
  gate made-up-gate: 1
chapter 2: 0 changed, 0 kept for plot, 0 gate failed, 1 unfinished, 0 untouched
unattributed: 0 changed, 0 kept for plot, 0 gate failed, 0 unfinished, 1 untouched
total: 2 changed, 1 kept for plot, 1 gate failed, 1 unfinished, 1 untouched
  reason made-up-first-id: 2
  reason made-up-second-id: 1
  character 1: 1
  character 2: 1
  gate made-up-gate: 1
"""


def _change(
    namespace: str = "made-up-scene-a",
    key: str = "1",
    chapter: int | None = 1,
    vanilla: str | None = None,
    text: str = TEXT_ONE,
    reason: str = "made-up-first-id",
    writer: Literal["voice", "opus"] = "voice",
) -> Change:
    # Made-up change. Not game text.
    return Change(
        namespace=namespace,
        key=key,
        chapter=chapter,
        vanilla=vanilla_hash(EN_ONE) if vanilla is None else vanilla,
        text=text,
        lens=LENS,
        reason=reason,
        writer=writer,
        run=RUN,
    )


def _dialog_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_outcomes_parse_through_report_records_and_render_counts(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    write_outcomes(path, OUTCOMES)
    records = report._records(path, TRIAGE)
    assert [r.outcome for r in records] == [o.outcome for o in OUTCOMES]
    assert "\n".join(report.render(records, TRIAGE_ORDER)) + "\n" == EXPECTED_SUMMARY


def test_each_outcome_object_has_exactly_the_dl31_keys_and_no_text(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    write_outcomes(path, OUTCOMES)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(OUTCOMES)
    for line, outcome in zip(lines, OUTCOMES, strict=True):
        obj = json.loads(line)
        assert set(obj) == OUTCOME_KEYS
        assert obj["key"] == outcome.key
        assert obj["reasons"] == list(outcome.reasons)
        assert obj["writer"] == outcome.writer
        assert obj["gate"] == outcome.gate


def test_outcomes_are_written_in_the_given_order_and_no_temp_file_remains(tmp_path: Path) -> None:
    parent = tmp_path / "work" / LENS
    path = parent / "outcomes.jsonl"
    write_outcomes(path, list(reversed(OUTCOMES)))
    keys = [json.loads(line)["key"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert keys == [o.key for o in reversed(OUTCOMES)]
    assert _dialog_files(parent) == ["outcomes.jsonl"]


def test_outcomes_replace_an_earlier_file(tmp_path: Path) -> None:
    path = tmp_path / "work" / LENS / "outcomes.jsonl"
    write_outcomes(path, OUTCOMES)
    write_outcomes(path, OUTCOMES[:1])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_outcomes_oserror_fails_naming_the_path_and_reason(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    path = blocker / "work" / LENS / "outcomes.jsonl"
    with pytest.raises(Fail) as raised:
        write_outcomes(path, OUTCOMES)
    assert str(path) in str(raised.value)
    assert "Not a directory" in str(raised.value)


def test_dialog_entries_round_trip_through_tomllib(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(
        directory,
        [
            _change(namespace="made-up-scene-a", key="2", reason="made-up-second-id", writer="opus"),
            _change(namespace="made-up-scene-b", key='made-up "quoted" key', chapter=12, vanilla=vanilla_hash(EN_TRICKY), text=TEXT_TRICKY),
        ],
    )
    first = tomllib.loads((directory / "chapter-01.toml").read_text(encoding="utf-8"))
    assert list(first["made-up-scene-a"]["2"]) == FIELD_ORDER
    assert first["made-up-scene-a"]["2"] == {
        "vanilla": vanilla_hash(EN_ONE),
        "text": TEXT_ONE,
        "lens": LENS,
        "reason": "made-up-second-id",
        "writer": "opus",
        "verdict": "passed",
        "run": RUN,
    }
    twelve = tomllib.loads((directory / "chapter-12.toml").read_text(encoding="utf-8"))
    assert list(twelve["made-up-scene-b"]['made-up "quoted" key']) == FIELD_ORDER
    assert twelve["made-up-scene-b"]['made-up "quoted" key'] == {
        "vanilla": vanilla_hash(EN_TRICKY),
        "text": TEXT_TRICKY,
        "lens": LENS,
        "reason": "made-up-first-id",
        "writer": "voice",
        "verdict": "passed",
        "run": RUN,
    }


def test_dialog_never_contains_the_vanilla_line(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(directory, [_change(vanilla=vanilla_hash(EN_ONE), text=TEXT_ONE)])
    content = (directory / "chapter-01.toml").read_text(encoding="utf-8")
    assert EN_ONE not in content
    assert TEXT_ONE in content


def test_chapter_and_unattributed_placement_and_table_order(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(
        directory,
        [
            _change(namespace="made-up-scene-10", key="2"),
            _change(namespace="made-up-scene-2", key="10"),
            _change(namespace="made-up-scene-2", key="2"),
            _change(namespace="made-up-scene-3", key='made-up "quoted" key', chapter=12),
            _change(namespace="made-up-scene-4", key="1", chapter=None, reason="made-up-second-id", writer="opus"),
        ],
    )
    assert _dialog_files(directory) == ["chapter-01.toml", "chapter-12.toml", "unattributed.toml"]
    first = tomllib.loads((directory / "chapter-01.toml").read_text(encoding="utf-8"))
    assert list(first) == ["made-up-scene-2", "made-up-scene-10"]
    assert list(first["made-up-scene-2"]) == ["2", "10"]
    twelve = tomllib.loads((directory / "chapter-12.toml").read_text(encoding="utf-8"))
    assert list(twelve) == ["made-up-scene-3"]
    unattributed = tomllib.loads((directory / "unattributed.toml").read_text(encoding="utf-8"))
    assert list(unattributed) == ["made-up-scene-4"]
    assert unattributed["made-up-scene-4"]["1"]["writer"] == "opus"


def test_creates_the_directory_even_with_no_changes(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    write_dialog(directory, [])
    assert directory.is_dir()
    assert _dialog_files(directory) == []


def test_stale_chapter_and_unattributed_files_are_removed_and_others_kept(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    directory.mkdir(parents=True)
    # Made-up stale files. Not game text.
    (directory / "chapter-01.toml").write_text("# Made-up old chapter one. Not game text.\n")
    (directory / "chapter-03.toml").write_text("# Made-up old chapter three. Not game text.\n")
    (directory / "unattributed.toml").write_text("# Made-up old unattributed. Not game text.\n")
    (directory / "chapter-3.toml").write_text("# Made-up other shape. Not game text.\n")
    (directory / "notes.toml").write_text("# Made-up notes. Not game text.\n")
    (directory / "README.md").write_text("Made-up readme. Not game text.\n")

    write_dialog(directory, [_change(chapter=1)])

    assert _dialog_files(directory) == ["README.md", "chapter-01.toml", "chapter-3.toml", "notes.toml"]
    assert (directory / "chapter-3.toml").read_text() == "# Made-up other shape. Not game text.\n"
    assert (directory / "notes.toml").read_text() == "# Made-up notes. Not game text.\n"
    assert (directory / "README.md").read_text() == "Made-up readme. Not game text.\n"
    assert TEXT_ONE in (directory / "chapter-01.toml").read_text(encoding="utf-8")


def test_unedited_change_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    directory.mkdir(parents=True)
    # Made-up stale file. Not game text.
    (directory / "chapter-01.toml").write_text("# Made-up old chapter one. Not game text.\n")
    changes = [
        _change(namespace="made-up-scene-a", key="1", chapter=1),
        _change(namespace="made-up-scene-z", key="9", chapter=2, vanilla=vanilla_hash(EN_TWO), text=EN_TWO),
    ]
    with pytest.raises(Fail) as raised:
        write_dialog(directory, changes)
    message = str(raised.value)
    assert "made-up-scene-z 9" in message
    assert EN_TWO not in message
    assert _dialog_files(directory) == ["chapter-01.toml"]
    assert (directory / "chapter-01.toml").read_text() == "# Made-up old chapter one. Not game text.\n"


def test_unedited_change_with_no_directory_creates_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    with pytest.raises(Fail):
        write_dialog(directory, [_change(vanilla=vanilla_hash(EN_ONE), text=EN_ONE)])
    assert not directory.exists()


def test_duplicate_namespace_and_key_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    directory = tmp_path / "dialog" / LENS
    with pytest.raises(Fail) as raised:
        write_dialog(directory, [_change(key="7"), _change(key="7", text=TEXT_TWO)])
    assert "made-up-scene-a 7" in str(raised.value)
    assert not directory.exists()


def test_dialog_oserror_fails_naming_the_path_and_reason(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    directory = blocker / "dialog" / LENS
    with pytest.raises(Fail) as raised:
        write_dialog(directory, [_change()])
    assert str(directory) in str(raised.value)
    assert "Not a directory" in str(raised.value)
