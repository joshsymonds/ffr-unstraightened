import tomllib
from pathlib import Path

import pytest

from ffrd import cli, progress
from ffrd.game import Fail, vanilla_hash

NO_PROGRESS = "ffrd: no progress recorded; run `ffrd progress <chapter>`\n"

# Made-up gate lines (see GATE_LINES in tests/conftest.py). Not game text.
CHAPTER_ONE = "Made-up gate line chapter one. Not game text."
CHAPTER_TWO = "Made-up gate line chapter two. Not game text."
SUB_EVENT_TWO = "Made-up gate sub-event line chapter two. Not game text."
LATER_CHAPTER = "Made-up later sentinel chapter three. Not game text."
LATER_SUB_EVENT = "Made-up later sentinel sub-event chapter three. Not game text."
CHAPTERLESS = "Made-up gate line without a chapter. Not game text."
NON_CHAPTER_SEGMENT = "Made-up gate line with a non-chapter segment. Not game text."
SHOWN_TEXT = {
    "ST_C01_001_01": CHAPTER_ONE,
    "ST_C02_001_01": CHAPTER_TWO,
    "ST_SubEv_004_C2_01": SUB_EVENT_TWO,
    "ST_C03_001_01": LATER_CHAPTER,
    "ST_SubEv_004_C3_01": LATER_SUB_EVENT,
}
ALL_CHAPTERED = {"ST_C01_001_01", "ST_C02_001_01", "ST_SubEv_004_C2_01", "ST_C03_001_01", "ST_SubEv_004_C3_01"}
CHAPTERLESS_NAMESPACES = ("ST_Misc_001", "ST_Scene_C1x_001")


@pytest.fixture(autouse=True)
def progress_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    path = tmp_path / "progress"
    monkeypatch.setenv("FFR_PROGRESS", str(path))
    return path


def test_record_then_read(progress_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["progress", "12"])
    assert capsys.readouterr().out == "chapter 12\n"
    assert progress_path.read_text() == "12\n"
    cli.main(["progress"])
    assert capsys.readouterr().out == "chapter 12\n"


def test_rerecording_replaces_including_lower(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["progress", "12"])
    cli.main(["progress", "3"])
    assert capsys.readouterr().out == "chapter 12\nchapter 3\n"
    cli.main(["progress"])
    assert capsys.readouterr().out == "chapter 3\n"
    assert progress.recorded() == 3


def test_reading_with_nothing_recorded_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert progress.recorded() is None
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["progress"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == NO_PROGRESS


@pytest.mark.parametrize("chapter", ["0", "-3", "abc", "1.5", "", "+4", pytest.param("9" * 5000, id="5000-digits")])
def test_invalid_chapter_exits_1_without_writing(chapter: str, progress_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["progress", chapter])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: chapter must be a positive integer, got {chapter!r}\n"
    assert not progress_path.exists()


def test_invalid_chapter_keeps_recorded_value(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["progress", "5"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["progress", "0"])
    capsys.readouterr()
    assert progress.recorded() == 5


@pytest.mark.parametrize("content", [b"abc\n", b"0\n", b"-2\n", b"1.5\n", b"", b"\n", b"\xff\xfe"])
def test_invalid_content_is_a_fail_not_a_traceback(content: bytes, progress_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    progress_path.write_bytes(content)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["progress"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: progress file {progress_path} does not hold a positive integer\n"
    with pytest.raises(Fail):
        progress.recorded()


def test_parent_directories_are_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state" / "ffrd" / "deep" / "progress"
    monkeypatch.setenv("FFR_PROGRESS", str(path))
    cli.main(["progress", "7"])
    assert path.read_text() == "7\n"


def test_unreadable_progress_path_is_a_fail(progress_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    progress_path.mkdir()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["progress"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.startswith(f"ffrd: cannot read progress from {progress_path}: ")


def test_unwritable_progress_path_is_a_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    path = blocker / "progress"
    monkeypatch.setenv("FFR_PROGRESS", str(path))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["progress", "4"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.startswith(f"ffrd: cannot write progress to {path}: ")


@pytest.mark.parametrize(
    ("ffr", "xdg", "expected"),
    [
        ("explicit", "xdg", "explicit"),
        ("explicit", None, "explicit"),
        ("", "xdg", "xdg/ffrd/progress"),
        (None, "xdg", "xdg/ffrd/progress"),
        ("", "", "home/.local/state/ffrd/progress"),
        (None, "", "home/.local/state/ffrd/progress"),
        (None, None, "home/.local/state/ffrd/progress"),
    ],
)
def test_progress_file_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffr: str | None, xdg: str | None, expected: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if ffr is None:
        monkeypatch.delenv("FFR_PROGRESS")
    else:
        monkeypatch.setenv("FFR_PROGRESS", str(tmp_path / ffr) if ffr else ffr)
    if xdg is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / xdg) if xdg else xdg)
    assert progress.progress_file() == tmp_path / expected


@pytest.mark.parametrize(
    ("progress_chapter", "shown", "withheld"),
    [
        (1, {"ST_C01_001_01"}, 6),
        (2, {"ST_C01_001_01", "ST_C02_001_01", "ST_SubEv_004_C2_01"}, 4),
        (3, ALL_CHAPTERED, 2),
        (99, ALL_CHAPTERED, 2),
    ],
)
def test_find_shows_chapters_at_or_below_progress(
    progress_chapter: int, shown: set[str], withheld: int, progress_path: Path, gate_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    progress_path.write_text(f"{progress_chapter}\n")
    cli.main(["find"])
    captured = capsys.readouterr()
    parsed = tomllib.loads(captured.out)
    assert set(parsed) == shown
    for ns in shown:
        assert parsed[ns] == {"1": {"vanilla": vanilla_hash(SHOWN_TEXT[ns]), "text": SHOWN_TEXT[ns]}}
    assert captured.err == f"# {len(shown)} shown, {withheld} withheld\n"


def test_later_chapter_text_and_namespaces_appear_in_neither_stream(
    progress_path: Path, gate_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    progress_path.write_text("2\n")
    cli.main(["find"])
    captured = capsys.readouterr()
    for hidden in (
        LATER_CHAPTER,
        LATER_SUB_EVENT,
        CHAPTERLESS,
        NON_CHAPTER_SEGMENT,
        "ST_C03_001_01",
        "ST_SubEv_004_C3_01",
        *CHAPTERLESS_NAMESPACES,
    ):
        assert hidden not in captured.out
        assert hidden not in captured.err


def test_pattern_is_never_matched_against_withheld_entries(
    progress_path: Path, gate_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    progress_path.write_text("2\n")
    cli.main(["find", "sentinel"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "# 0 shown, 4 withheld\n"


def test_pattern_narrows_shown_entries_and_withheld_stays_gate_only(
    progress_path: Path, gate_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    progress_path.write_text("2\n")
    cli.main(["find", "gate line chapter"])
    captured = capsys.readouterr()
    assert set(tomllib.loads(captured.out)) == {"ST_C01_001_01", "ST_C02_001_01"}
    assert captured.err == "# 2 shown, 4 withheld\n"


def test_ns_option_is_applied_after_the_gate(progress_path: Path, gate_vanilla: Path, capsys: pytest.CaptureFixture[str]) -> None:
    progress_path.write_text("2\n")
    cli.main(["find", "--ns", "^ST_C03_"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "# 0 shown, 4 withheld\n"


@pytest.mark.parametrize("args", [["--ns", "^ST_(Misc|Scene)_"], ["without a chapter"]])
def test_chapterless_namespaces_are_refused_even_at_high_progress(
    args: list[str], progress_path: Path, gate_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    progress_path.write_text("99\n")
    cli.main(["find", *args])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "# 0 shown, 2 withheld\n"


@pytest.mark.parametrize("args", [[], ["sentinel"], ["--ns", "^ST_C01_"]])
def test_nothing_recorded_refuses_everything_with_exit_1(
    args: list[str], progress_path: Path, gate_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not progress_path.exists()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["find", *args])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == NO_PROGRESS


def test_progress_is_read_before_vanilla(
    progress_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FFR_VANILLA", str(tmp_path / "no-vanilla"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["find"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == NO_PROGRESS
