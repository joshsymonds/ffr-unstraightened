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
