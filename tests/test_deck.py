import os
from pathlib import Path

import pytest

from ffrd import cli
from ffrd.game import MOD_NAME

EXTS = ("pak", "utoc", "ucas")
NAMES = tuple(f"{MOD_NAME}.{ext}" for ext in EXTS)
DEFAULT_PAKS = "~/.local/share/Steam/steamapps/common/FINAL FANTASY RESONANCE/FFRS/Content/Paks"
DEFAULT_MODS_REL = ".local/share/Steam/steamapps/common/FINAL FANTASY RESONANCE/FFRS/Content/Paks/~mods"

# Stands in for ssh: logs one tab-separated line per call (argc, host, remote command), then runs the
# remote command with sh in this machine's HOME, which the test points at a temp directory.
FAKE_SSH = """#!/bin/sh
printf '%s\\t%s\\t%s\\n' "$#" "$1" "$2" >> "$FAKE_SSH_LOG"
if [ -n "$FAKE_SSH_FAIL" ]; then exit "$FAKE_SSH_FAIL"; fi
exec sh -c "$2"
"""


def make_repo(root: Path) -> Path:
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    build = root / "build"
    build.mkdir()
    for name in NAMES:
        (build / name).write_bytes(f"synthetic {name}".encode())
    return root


@pytest.fixture(autouse=True)
def no_local_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FFR_GAME", str(tmp_path / "no-game"))
    monkeypatch.delenv("FFR_DECK_HOST", raising=False)
    monkeypatch.delenv("FFR_DECK_PAKS", raising=False)
    monkeypatch.delenv("FAKE_SSH_FAIL", raising=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def deck_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "deck-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def ssh_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return log


def parse_call(line: str) -> tuple[str, str, str]:
    argc, host, command = line.split("\t", 2)
    return argc, host, command


def ssh_calls(log: Path) -> list[tuple[str, str, str]]:
    if not log.exists():
        return []
    return [parse_call(line) for line in log.read_text().splitlines()]


def test_install_deck_with_default_env_lands_files_in_default_mods_dir(
    repo: Path, deck_home: Path, ssh_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = deck_home / DEFAULT_MODS_REL
    assert not dest.exists()
    cli.main(["install", "--deck"])
    for name in NAMES:
        assert (dest / name).read_bytes() == (repo / "build" / name).read_bytes()
    calls = ssh_calls(ssh_log)
    assert len(calls) == len(EXTS)
    assert all(argc == "2" and host == "deck" for argc, host, _ in calls)
    assert capsys.readouterr().out == f"installed to deck:{DEFAULT_PAKS}/~mods\n"


def test_install_and_uninstall_deck_with_configured_host_and_paks_containing_space_and_quote(
    repo: Path, deck_home: Path, ssh_log: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paks = "~/mods test/Deck's Paks"
    monkeypatch.setenv("FFR_DECK_HOST", "synthetic-deck")
    monkeypatch.setenv("FFR_DECK_PAKS", paks)
    dest = deck_home / "mods test/Deck's Paks/~mods"
    assert not dest.exists()

    cli.main(["install", "--deck"])
    for name in NAMES:
        assert (dest / name).read_bytes() == (repo / "build" / name).read_bytes()
    assert capsys.readouterr().out == f"installed to synthetic-deck:{paks}/~mods\n"

    (dest / "Other.pak").write_bytes(b"unrelated")
    cli.main(["uninstall", "--deck"])
    for name in NAMES:
        assert not (dest / name).exists()
    assert (dest / "Other.pak").read_bytes() == b"unrelated"
    assert capsys.readouterr().out == f"removed {MOD_NAME} from synthetic-deck:{paks}/~mods\n"
    assert {host for _, host, _ in ssh_calls(ssh_log)} == {"synthetic-deck"}


def test_uninstall_deck_tolerates_absent_files_and_mods_dir(
    repo: Path, deck_home: Path, ssh_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = deck_home / DEFAULT_MODS_REL
    assert not dest.exists()
    cli.main(["uninstall", "--deck"])
    assert capsys.readouterr().out == f"removed {MOD_NAME} from deck:{DEFAULT_PAKS}/~mods\n"

    dest.mkdir(parents=True)
    (dest / f"{MOD_NAME}.pak").write_bytes(b"installed")
    cli.main(["uninstall", "--deck"])
    assert not (dest / f"{MOD_NAME}.pak").exists()
    assert capsys.readouterr().out == f"removed {MOD_NAME} from deck:{DEFAULT_PAKS}/~mods\n"


def test_install_deck_with_missing_local_files_exits_1_and_never_calls_ssh(
    repo: Path, deck_home: Path, ssh_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "build" / f"{MOD_NAME}.utoc").unlink()
    (repo / "build" / f"{MOD_NAME}.ucas").unlink()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["install", "--deck"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: missing ['{MOD_NAME}.utoc', '{MOD_NAME}.ucas']; run `ffrd build`\n"
    assert ssh_calls(ssh_log) == []


@pytest.mark.parametrize("command", [["install", "--deck"], ["uninstall", "--deck"]])
def test_deck_command_with_nonzero_ssh_exit_exits_1_naming_host(
    command: list[str], repo: Path, deck_home: Path, ssh_log: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FFR_DECK_HOST", "synthetic-deck")
    monkeypatch.setenv("FAKE_SSH_FAIL", "255")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(command)
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: ssh to synthetic-deck exited 255\n"


@pytest.mark.parametrize("command", [["install", "--deck"], ["uninstall", "--deck"]])
def test_deck_command_without_ssh_on_path_exits_1_naming_host(
    command: list[str], repo: Path, deck_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("FFR_DECK_HOST", "synthetic-deck")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(command)
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: ssh not found on PATH; cannot reach synthetic-deck\n"


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_help_documents_deck_flag_and_both_variables(
    command: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--help"])
    assert exit_info.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "--deck" in out
    assert "FFR_DECK_HOST" in out
    assert "default deck" in out
    assert "FFR_DECK_PAKS" in out
