import os
import subprocess
from pathlib import Path

import pytest

from ffrd import cli, deck
from ffrd.game import MOD_NAME, Fail

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


# Stands in for ssh for ssh_status: logs each argument on its own line, writes to both output streams
# (which the caller must not see), and exits with $FAKE_SSH_STATUS.
FAKE_SSH_ARGS = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_SSH_LOG"
echo noise-on-stdout
echo noise-on-stderr >&2
exit "$FAKE_SSH_STATUS"
"""


@pytest.fixture
def ssh_args_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "args-bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH_ARGS)
    ssh.chmod(0o755)
    log = tmp_path / "ssh-args.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FAKE_SSH_STATUS", "0")
    return log


@pytest.fixture
def no_ssh_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def expand_remote(path: str) -> bytes:
    # The remote shell is sh; printing the quoted word shows what it expands to here.
    return subprocess.run(["sh", "-c", f"printf %s {deck.remote(path)}"], capture_output=True, check=True).stdout


def test_remote_expands_leading_tilde_to_home_and_quotes_the_rest(deck_home: Path, tmp_path: Path) -> None:
    assert expand_remote("~/mods test/Deck's Paks") == str(deck_home / "mods test/Deck's Paks").encode()
    assert expand_remote(str(tmp_path / "abs dir")) == str(tmp_path / "abs dir").encode()


def test_ssh_returns_remote_stdout_and_runs_plain_ssh_host_command(ssh_log: Path) -> None:
    assert deck.ssh("synthetic-host", "printf 'remote-out'") == b"remote-out"
    assert ssh_calls(ssh_log) == [("2", "synthetic-host", "printf 'remote-out'")]


def test_ssh_streams_stdin_to_the_remote_command(ssh_log: Path, tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"synthetic payload")
    with payload.open("rb") as data:
        assert deck.ssh("synthetic-host", "cat", stdin=data) == b"synthetic payload"


def test_ssh_nonzero_exit_raises_fail_naming_host_and_status(ssh_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SSH_FAIL", "255")
    with pytest.raises(Fail) as failure:
        deck.ssh("synthetic-host", "true")
    assert str(failure.value) == "ssh to synthetic-host exited 255"


def test_ssh_without_ssh_on_path_raises_fail_naming_host(no_ssh_on_path: None) -> None:
    with pytest.raises(Fail) as failure:
        deck.ssh("synthetic-host", "true")
    assert str(failure.value) == "ssh not found on PATH; cannot reach synthetic-host"


@pytest.mark.parametrize("status", [0, 1, 255])
def test_ssh_status_returns_exit_status_without_raising_and_discards_output(
    status: int, ssh_args_log: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FAKE_SSH_STATUS", str(status))
    assert deck.ssh_status("synthetic-host", "pgrep -f synthetic") == status
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ssh_status_passes_batchmode_and_connect_timeout_before_host(ssh_args_log: Path) -> None:
    deck.ssh_status("synthetic-host", "pgrep -f synthetic")
    assert ssh_args_log.read_text().splitlines() == [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "synthetic-host",
        "pgrep -f synthetic",
    ]


def test_ssh_status_without_ssh_on_path_raises_fail_naming_host(no_ssh_on_path: None) -> None:
    with pytest.raises(Fail) as failure:
        deck.ssh_status("synthetic-host", "true")
    assert str(failure.value) == "ssh not found on PATH; cannot reach synthetic-host"


def test_put_writes_files_to_explicit_host_and_dest_independent_of_env(
    repo: Path, deck_home: Path, ssh_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FFR_DECK_HOST", "wrong-host")
    monkeypatch.setenv("FFR_DECK_PAKS", "~/wrong/Paks")
    dest = deck_home / "mods test/Deck's Paks/~mods"
    files = [repo / "build" / name for name in NAMES]

    deck.put("synthetic-host", "~/mods test/Deck's Paks/~mods", files)

    for name in NAMES:
        assert (dest / name).read_bytes() == (repo / "build" / name).read_bytes()
    assert not (deck_home / "wrong").exists()
    calls = ssh_calls(ssh_log)
    assert len(calls) == len(NAMES)
    assert {host for _, host, _ in calls} == {"synthetic-host"}


def test_delete_removes_named_files_from_explicit_host_and_dest_tolerating_absent(
    deck_home: Path, ssh_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FFR_DECK_HOST", "wrong-host")
    monkeypatch.setenv("FFR_DECK_PAKS", "~/wrong/Paks")
    dest = deck_home / "mods test/Deck's Paks/~mods"
    dest.mkdir(parents=True)
    (dest / NAMES[0]).write_bytes(b"installed")
    (dest / "Other.pak").write_bytes(b"unrelated")

    deck.delete("synthetic-host", "~/mods test/Deck's Paks/~mods", list(NAMES))
    deck.delete("synthetic-host", "~/no such dir", list(NAMES))

    assert not (dest / NAMES[0]).exists()
    assert (dest / "Other.pak").read_bytes() == b"unrelated"
    assert {host for _, host, _ in ssh_calls(ssh_log)} == {"synthetic-host"}
