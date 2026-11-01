from pathlib import Path

import pytest

from ffrd import cli, progress
from ffrd.game import Fail

NO_PROGRESS = "ffrd: no progress recorded; run `ffrd progress <chapter>`\n"


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
