import subprocess
from pathlib import Path

import pytest
from pylocres import Entry, LocresFile, Namespace

from ffrd import cli
from ffrd.game import LOCRES

# Made-up lines, not game text. EN_LINE and JA_LINE are long enough to be checked;
# SHORT_LINE is one character under the minimum and must never be counted.
EN_LINE = "A made-up sentence for the leak test."
JA_LINE = "これは漏えい検査のための合成された文章です。"
MINIMUM_LINE = "Exactly twenty chars"
SHORT_LINE = "Nineteen chars, ok!"

EN_TABLE = (
    ("ST_T01_001", "1", EN_LINE),
    ("ST_T01_001", "2", MINIMUM_LINE),
    ("ST_T01_001", "3", SHORT_LINE),
)
JA_TABLE = (("ST_T01_001", "1", JA_LINE),)


def write_table(path: Path, lines: tuple[tuple[str, str, str], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    namespaces: dict[str, Namespace] = {}
    for ns_name, key, text in lines:
        namespaces.setdefault(ns_name, Namespace(ns_name)).add(Entry(key, text, text, is_hash=False))
    locres = LocresFile()
    for ns in namespaces.values():
        locres.add(ns)
    locres.write(str(path))


@pytest.fixture
def vanilla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vanilla"
    write_table(root / "pak" / LOCRES / "en" / "Game.locres", EN_TABLE)
    write_table(root / "pak" / LOCRES / "ja" / "Game.locres", JA_TABLE)
    monkeypatch.setenv("FFR_VANILLA", str(root))
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    git(root, "init", "-q")
    monkeypatch.chdir(root)
    return root


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def leak_check_fails(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["leak-check"])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_clean_history_prints_zero_and_exits_0(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit(repo, "Initial", {"notes.txt": "Nothing to see here.\n"})
    cli.main(["leak-check"])
    captured = capsys.readouterr()
    assert captured.out == "0\n"
    assert captured.err == ""


def test_planted_line_in_committed_file_is_reported_by_commit_and_path(
    vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = commit(repo, "Add notes", {"dialog/notes.po": f"{EN_LINE}\n"})
    out, err = leak_check_fails(capsys)
    assert out == f"{planted}:dialog/notes.po en ST_T01_001,1\n"
    assert err == "ffrd: 1 vanilla strings in history\n"


def test_planted_line_in_commit_message_is_reported_as_message(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planted = commit(repo, f"Notes: {EN_LINE}", {})
    out, err = leak_check_fails(capsys)
    assert out == f"{planted}:message en ST_T01_001,1\n"
    assert err == "ffrd: 1 vanilla strings in history\n"


def test_line_from_a_non_english_table_is_found(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planted = commit(repo, "Add notes", {"notes.txt": f"{JA_LINE}\n"})
    out, _ = leak_check_fails(capsys)
    assert out == f"{planted}:notes.txt ja ST_T01_001,1\n"


def test_line_one_character_under_the_minimum_is_not_counted(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit(repo, "Add notes", {"notes.txt": f"{SHORT_LINE}\n"})
    cli.main(["leak-check"])
    captured = capsys.readouterr()
    assert captured.out == "0\n"
    assert captured.err == ""


def test_line_at_the_minimum_length_is_counted(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planted = commit(repo, "Add notes", {"notes.txt": f"{MINIMUM_LINE}\n"})
    out, _ = leak_check_fails(capsys)
    assert out == f"{planted}:notes.txt en ST_T01_001,2\n"


def test_planted_text_never_appears_in_output(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit(repo, f"Message {EN_LINE}", {"one.txt": f"{EN_LINE}\n", "two.txt": f"{JA_LINE}\n"})
    out, err = leak_check_fails(capsys)
    assert len(out.splitlines()) == 3
    assert EN_LINE not in out + err
    assert JA_LINE not in out + err


def test_text_in_a_tree_path_is_reported_with_the_text_masked(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planted = commit(repo, "Add notes", {f"notes/{EN_LINE}.txt": "clean\n"})
    out, err = leak_check_fails(capsys)
    assert out == f"{planted}:notes/{'*' * len(EN_LINE)}.txt en ST_T01_001,1\n"
    assert EN_LINE not in out + err
    assert err == "ffrd: 1 vanilla strings in history\n"


def test_blob_is_reported_once_at_its_oldest_commit_and_path(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    oldest = commit(repo, "Add a", {"a.txt": f"{EN_LINE}\n"})
    commit(repo, "Add b with the same text", {"b.txt": f"{EN_LINE}\n"})
    out, err = leak_check_fails(capsys)
    assert out == f"{oldest}:a.txt en ST_T01_001,1\n"
    assert err == "ffrd: 1 vanilla strings in history\n"


def test_text_removed_in_a_later_commit_is_still_reported(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    added = commit(repo, "Add notes", {"gone.txt": f"{EN_LINE}\n"})
    git(repo, "rm", "-q", "gone.txt")
    git(repo, "commit", "-q", "-m", "Remove notes")
    out, _ = leak_check_fails(capsys)
    assert out == f"{added}:gone.txt en ST_T01_001,1\n"


def test_tracked_work_file_is_reported_by_commit_and_path(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planted = commit(repo, "Add work", {"work/voice/x.jsonl": '{"made": "up"}\n'})
    out, err = leak_check_fails(capsys)
    assert out == f"{planted}:work/voice/x.jsonl work/\n"
    assert err == "ffrd: 1 tracked files under work/ in history\n"


def test_work_file_removed_in_a_later_commit_is_still_reported(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    added = commit(repo, "Add work", {"work/voice/x.jsonl": '{"made": "up"}\n'})
    git(repo, "rm", "-q", "work/voice/x.jsonl")
    git(repo, "commit", "-q", "-m", "Remove work")
    out, _ = leak_check_fails(capsys)
    assert out == f"{added}:work/voice/x.jsonl work/\n"


def test_work_path_seen_in_two_commits_is_reported_once_at_the_oldest(
    vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    oldest = commit(repo, "Add work", {"work/a.jsonl": '{"made": "up"}\n'})
    commit(repo, "Change work", {"work/a.jsonl": '{"made": "changed"}\n'})
    out, err = leak_check_fails(capsys)
    assert out == f"{oldest}:work/a.jsonl work/\n"
    assert err == "ffrd: 1 tracked files under work/ in history\n"


def test_path_named_work_below_another_directory_is_not_reported(vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commit(repo, "Add notes", {"notes/work/a.txt": "clean\n"})
    cli.main(["leak-check"])
    captured = capsys.readouterr()
    assert captured.out == "0\n"
    assert captured.err == ""


def test_vanilla_hit_and_work_file_are_reported_with_the_combined_message(
    vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = commit(repo, "Add notes", {"notes.txt": f"{EN_LINE}\n", "work/voice/x.jsonl": '{"made": "up"}\n'})
    out, err = leak_check_fails(capsys)
    assert out == f"{planted}:notes.txt en ST_T01_001,1\n{planted}:work/voice/x.jsonl work/\n"
    assert err == "ffrd: 1 vanilla strings and 1 tracked files under work/ in history\n"


def test_missing_vanilla_dir_exits_1_without_printing_zero(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-vanilla"
    monkeypatch.setenv("FFR_VANILLA", str(missing))
    commit(repo, "Initial", {"notes.txt": "Nothing to see here.\n"})
    out, err = leak_check_fails(capsys)
    assert out == ""
    assert err == f"ffrd: no vanilla text under {missing / 'pak' / LOCRES}; run `ffrd extract`\n"


def test_vanilla_dir_without_a_game_locres_exits_1_without_printing_zero(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty-vanilla"
    (empty / "pak" / LOCRES / "en").mkdir(parents=True)
    monkeypatch.setenv("FFR_VANILLA", str(empty))
    commit(repo, "Initial", {"notes.txt": "Nothing to see here.\n"})
    out, err = leak_check_fails(capsys)
    assert out == ""
    assert err == f"ffrd: no vanilla text under {empty / 'pak' / LOCRES}; run `ffrd extract`\n"


def test_leak_check_is_registered_with_its_minimum_in_help(
    vanilla: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert any(module.__name__ == "ffrd.leakcheck" for module in cli.COMMANDS)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["leak-check", "--help"])
    assert exit_info.value.code == 0
    assert "shorter than 20 characters" in capsys.readouterr().out


def test_leak_check_help_names_the_work_rule(
    vanilla: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["leak-check", "--help"])
    assert exit_info.value.code == 0
    assert "tracked file under the top-level work/ directory" in capsys.readouterr().out
