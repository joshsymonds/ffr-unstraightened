import os
import subprocess
import sys
from pathlib import Path

import pytest

from ffrd import cli, install
from ffrd.game import MOD_NAME

EXTS = ("pak", "utoc", "ucas")
WORKSPACE = Path(__file__).resolve().parents[1]


def make_repo(root: Path) -> Path:
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    build = root / "build"
    build.mkdir()
    for ext in EXTS:
        (build / f"{MOD_NAME}.{ext}").write_bytes(f"synthetic {ext}".encode())
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    return root


def mods_dir(game: Path) -> Path:
    return game / "FFRS/Content/Paks/~mods"


def test_install_copies_built_files_and_creates_mods_dir(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    assert not dest.exists()
    cli.main(["install"])
    for ext in EXTS:
        name = f"{MOD_NAME}.{ext}"
        assert (dest / name).read_bytes() == (repo / "build" / name).read_bytes()
    assert capsys.readouterr().out == f"installed to {dest}\n"


def test_install_with_missing_files_exits_1(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "build" / f"{MOD_NAME}.utoc").unlink()
    (repo / "build" / f"{MOD_NAME}.ucas").unlink()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["install"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: missing ['{MOD_NAME}.utoc', '{MOD_NAME}.ucas']; run `ffrd build`\n"
    assert not mods_dir(fake_game).exists()


def test_uninstall_removes_the_three_files_and_keeps_others(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    dest.mkdir(parents=True)
    for ext in EXTS:
        (dest / f"{MOD_NAME}.{ext}").write_bytes(b"installed")
    (dest / "Other.pak").write_bytes(b"unrelated")
    cli.main(["uninstall"])
    for ext in EXTS:
        assert not (dest / f"{MOD_NAME}.{ext}").exists()
    assert (dest / "Other.pak").read_bytes() == b"unrelated"
    assert capsys.readouterr().out == f"removed {MOD_NAME} from {dest}\n"


def test_uninstall_tolerates_absent_files(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    dest.mkdir(parents=True)
    (dest / f"{MOD_NAME}.pak").write_bytes(b"installed")
    cli.main(["uninstall"])
    assert not (dest / f"{MOD_NAME}.pak").exists()
    assert capsys.readouterr().out == f"removed {MOD_NAME} from {dest}\n"


def test_uninstall_tolerates_missing_mods_dir(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    assert not dest.exists()
    cli.main(["uninstall"])
    assert capsys.readouterr().out == f"removed {MOD_NAME} from {dest}\n"


def test_commands_register_install_and_uninstall(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert install in cli.COMMANDS
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage: ffrd" in out
    assert "Build a FINAL FANTASY RESONANCE dialogue mod from .po overrides." in out
    assert "copy the built mod into the game's Paks/~mods" in out
    assert "remove the mod from Paks/~mods" in out


def test_main_reports_fail_on_stderr_and_exits_1(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-game"
    monkeypatch.setenv("FFR_GAME", str(missing))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["install"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: no game install at {missing} (set FFR_GAME)\n"


def test_python_m_ffrd_runs_cli_main(repo: Path, tmp_path: Path) -> None:
    missing = tmp_path / "no-game"
    env = {**os.environ, "FFR_GAME": str(missing), "PYTHONPATH": str(WORKSPACE)}
    result = subprocess.run(
        [sys.executable, "-m", "ffrd", "install"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == f"ffrd: no game install at {missing} (set FFR_GAME)\n"


ENTRY_TWO = '#. ja: 合成テキスト二\nmsgctxt "ST_C01_001_01,2"\nmsgid "Synthetic line two."\nmsgstr ""\n\n'
ENTRY_TEN = '#. ja: 合成テキスト十\nmsgctxt "ST_C01_001_01,10"\nmsgid "Synthetic line ten."\nmsgstr ""\n\n'
ENTRY_SAME = 'msgctxt "ST_C03_002_01,1"\nmsgid "Same in both."\nmsgstr ""\n\n'


def test_find_without_arguments_prints_every_non_empty_english_entry_in_scene_order(
    fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["find"])
    captured = capsys.readouterr()
    assert captured.out == ENTRY_TWO + ENTRY_TEN + ENTRY_SAME
    assert captured.err == "# 3 entries\n"


def test_find_pattern_matches_text_case_insensitively(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "SYNTHETIC LINE TWO"])
    captured = capsys.readouterr()
    assert captured.out == ENTRY_TWO
    assert captured.err == "# 1 entries\n"


def test_find_pattern_matches_key(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "^10$"])
    captured = capsys.readouterr()
    assert captured.out == ENTRY_TEN
    assert captured.err == "# 1 entries\n"


def test_find_ns_option_filters_namespaces_by_regex(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "--ns", "^ST_C03_"])
    captured = capsys.readouterr()
    assert captured.out == ENTRY_SAME
    assert captured.err == "# 1 entries\n"


def test_find_skips_empty_english_lines(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "--ns", "^ST_C01_001_02$"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "# 0 entries\n"


def test_find_without_vanilla_text_exits_1(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FFR_VANILLA", str(tmp_path / "no-vanilla"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["find"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no vanilla text yet; run `ffrd extract`\n"


def test_find_is_registered_with_its_help(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert any(module.__name__ == "ffrd.find" for module in cli.COMMANDS)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "print vanilla entries as .po, ready to paste into dialog/" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["find", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "regex matched against text or key (case-insensitive)" in out
    assert "regex the namespace must match, e.g. '^ST_C01_023_'" in out
