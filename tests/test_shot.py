"""Save guard tests against a fake gnomon filesystem. Made-up save bytes and names. Not game data."""

import fnmatch
import shlex
import subprocess
from pathlib import Path
from typing import IO

import pytest

from ffrd import deck, shot
from ffrd.game import Fail

REMOTE_HOME = "/remote"
STEAM_ID = "76561197969529126"
SAVE_ROOT = f"{REMOTE_HOME}/.local/share/Steam/steamapps/compatdata/3259780/pfx/drive_c/users/steamuser/AppData/Local/FFRS/Steam"
SAVE_FOLDER = f"{SAVE_ROOT}/{STEAM_ID}"
SHARED_CONFIG = f"{REMOTE_HOME}/.local/share/Steam/userdata/111/7/remote/sharedconfig.vdf"
DEDICATED = "GameSaveData10.sav"
BACKUP_ROOT = Path(".local/share/ffr-unstraightened/save-backups")
STAMP = "20260101T000000Z"


def config(cloud: str) -> bytes:
    # Made-up sharedconfig.vdf text with the app block keyed 3259780.
    return (
        '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n'
        '\t\t\t\t{\n\t\t\t\t\t"3259780"\n\t\t\t\t\t{\n\t\t\t\t\t\t"cloudenabled"\t\t"'
        + cloud
        + '"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
    ).encode()


class FakeRemote:
    """An in-memory gnomon. Files are keyed by absolute path; `~/` stands for REMOTE_HOME."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {SHARED_CONFIG: config("0")}
        self.mtimes: dict[str, float] = {}
        self.writes: list[str] = []
        self.stubborn: set[str] = set()  # writes to these paths are silently dropped

    def add_save(self, name: str, data: bytes, mtime: float = 0.0) -> None:
        self.files[f"{SAVE_FOLDER}/{name}"] = data
        self.mtimes[f"{SAVE_FOLDER}/{name}"] = mtime

    def children(self, root: str) -> list[str]:
        return [k for k in self.files if k.startswith(root + "/")]

    def ssh(self, host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
        argv = [REMOTE_HOME + t[1:] if t.startswith("~/") else t for t in shlex.split(command)]
        if argv[:2] == ["cat", ">"]:
            assert not isinstance(stdin, int)
            path = argv[2]
            self.writes.append(path)
            if path not in self.stubborn:
                self.files[path] = stdin.read()
                self.mtimes[path] = 1.0
            return b""
        if argv[0] == "cat":
            if argv[1] not in self.files:
                raise Fail(f"ssh to {host} exited 1")
            return self.files[argv[1]]
        if argv[0] == "find":
            root = argv[1]
            if not self.children(root):
                raise Fail(f"ssh to {host} exited 1")
            if argv[argv.index("-type") + 1] == "d":
                assert argv[-1] == "-print0"
                names = sorted({k[len(root) + 1 :].split("/")[0] for k in self.children(root) if "/" in k[len(root) + 1 :]})
                return b"".join(f"{root}/{n}".encode() + b"\0" for n in names)
            assert argv[argv.index("-printf") + 1] == "%T@ %f\\0"
            direct = [k for k in self.children(root) if k.rsplit("/", 1)[0] == root]
            return b"".join(f"{self.mtimes.get(k, 0.0):.1f} {k.rsplit('/', 1)[1]}".encode() + b"\0" for k in direct)
        if argv[0] == "ls":
            assert argv[1] == "-1"
            matches = sorted(k for k in self.files if fnmatch.fnmatchcase(k, argv[2]))
            if not matches:
                raise Fail(f"ssh to {host} exited 2")
            return "".join(m + "\n" for m in matches).encode()
        raise AssertionError(f"unexpected ssh command: {command}")


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRemote:
    fake = FakeRemote()
    monkeypatch.setattr(deck, "ssh", fake.ssh)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shot, "_stamp", lambda: STAMP)
    return fake


@pytest.fixture
def saves(remote: FakeRemote) -> dict[str, bytes]:
    originals = {
        "GameSaveData0.sav": b"synthetic autosave\x00\x01",
        "GameSaveData1.sav": b"synthetic slot one",
        "GameSaveData2.sav": b"synthetic slot two",
        "OptionSaveData.sav": b"synthetic options",
        "steam_autocloud.vdf": b"synthetic cloud state",
    }
    for i, (name, data) in enumerate(originals.items()):
        remote.add_save(name, data, mtime=float(i))
    return originals


def backup_dir(home: Path) -> Path:
    return home / BACKUP_ROOT / STAMP / STEAM_ID


def test_refuses_when_cloud_saves_are_on(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    remote.files[SHARED_CONFIG] = config("1")
    ran = False
    with pytest.raises(Fail, match="cloud"), shot.guard():
        ran = True
    assert not ran
    assert not (tmp_path / BACKUP_ROOT).exists()
    assert remote.writes == []


def test_refuses_when_cloud_value_is_not_zero(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    remote.files[SHARED_CONFIG] = config("false")
    with pytest.raises(Fail, match="cloud"), shot.guard():
        pass
    assert not (tmp_path / BACKUP_ROOT).exists()
    assert remote.writes == []


def test_refuses_when_sharedconfig_is_missing(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    del remote.files[SHARED_CONFIG]
    with pytest.raises(Fail), shot.guard():
        pass
    assert not (tmp_path / BACKUP_ROOT).exists()
    assert remote.writes == []


def test_refuses_when_the_app_block_is_missing(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    remote.files[SHARED_CONFIG] = (
        b'"UserLocalConfigStore"\n{\n\t"apps"\n\t{\n\t\t"570"\n\t\t{\n\t\t\t"cloudenabled"\t\t"0"\n\t\t}\n\t}\n}\n'
    )
    with pytest.raises(Fail, match="cloud"), shot.guard():
        pass
    assert not (tmp_path / BACKUP_ROOT).exists()
    assert remote.writes == []


def test_refuses_when_there_is_not_exactly_one_steam_id_folder(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    remote.files[f"{SAVE_ROOT}/76561190000000000/GameSaveData0.sav"] = b"second account"
    with pytest.raises(Fail, match="exactly one"), shot.guard():
        pass
    assert not (tmp_path / BACKUP_ROOT).exists()
    assert remote.writes == []


def test_backup_holds_exact_bytes_of_every_regular_file(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    with shot.guard():
        assert {p.name: p.read_bytes() for p in backup_dir(tmp_path).iterdir()} == saves


def test_backup_never_touches_an_earlier_backup(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    earlier = tmp_path / BACKUP_ROOT / "20250101T000000Z" / STEAM_ID
    earlier.mkdir(parents=True)
    (earlier / "GameSaveData0.sav").write_bytes(b"hand backup")
    with shot.guard():
        pass
    assert (earlier / "GameSaveData0.sav").read_bytes() == b"hand backup"
    assert (backup_dir(tmp_path) / "GameSaveData0.sav").read_bytes() == saves["GameSaveData0.sav"]


def test_backup_refuses_when_its_timestamp_folder_exists(remote: FakeRemote, saves: dict[str, bytes], tmp_path: Path) -> None:
    (tmp_path / BACKUP_ROOT / STAMP).mkdir(parents=True)
    ran = False
    with pytest.raises(Fail), shot.guard():
        ran = True
    assert not ran
    assert remote.writes == []


def test_seeds_dedicated_slot_from_newest_save_when_absent(remote: FakeRemote, tmp_path: Path) -> None:
    remote.add_save("GameSaveData0.sav", b"autosave, oldest", mtime=10.0)
    remote.add_save("GameSaveData1.sav", b"slot one, newest", mtime=30.0)
    remote.add_save("GameSaveData2.sav", b"slot two, middle", mtime=20.0)
    seen: list[bytes] = []
    with shot.guard():
        seen.append(remote.files[f"{SAVE_FOLDER}/{DEDICATED}"])
    assert seen == [b"slot one, newest"]


def test_refuses_to_seed_when_no_game_save_exists(remote: FakeRemote, tmp_path: Path) -> None:
    remote.add_save("OptionSaveData.sav", b"synthetic options", mtime=5.0)
    ran = False
    with pytest.raises(Fail, match="seed"), shot.guard():
        ran = True
    assert not ran
    assert remote.writes == []


def test_existing_dedicated_slot_is_left_alone_and_not_restored(remote: FakeRemote, saves: dict[str, bytes]) -> None:
    remote.add_save(DEDICATED, b"earlier run's slot", mtime=99.0)
    with shot.guard():
        assert remote.files[f"{SAVE_FOLDER}/{DEDICATED}"] == b"earlier run's slot"
        remote.files[f"{SAVE_FOLDER}/{DEDICATED}"] = b"game wrote this"
    assert remote.files[f"{SAVE_FOLDER}/{DEDICATED}"] == b"game wrote this"


def test_a_clean_run_writes_only_the_dedicated_slot(remote: FakeRemote, saves: dict[str, bytes]) -> None:
    with shot.guard():
        pass
    assert remote.writes == [f"{SAVE_FOLDER}/{DEDICATED}"]


def test_restores_every_other_file_after_body_changes_them(
    remote: FakeRemote, saves: dict[str, bytes], capsys: pytest.CaptureFixture[str]
) -> None:
    with shot.guard():
        remote.files[f"{SAVE_FOLDER}/GameSaveData0.sav"] = b"game rewrote the autosave"
        del remote.files[f"{SAVE_FOLDER}/OptionSaveData.sav"]
        remote.files[f"{SAVE_FOLDER}/{DEDICATED}"] = b"game wrote the dedicated slot"
    for name, data in saves.items():
        assert remote.files[f"{SAVE_FOLDER}/{name}"] == data
    assert remote.files[f"{SAVE_FOLDER}/{DEDICATED}"] == b"game wrote the dedicated slot"
    out = capsys.readouterr().out
    assert "GameSaveData0.sav" in out
    assert "OptionSaveData.sav" in out
    assert "GameSaveData1.sav" not in out
    assert DEDICATED not in out


def test_restores_after_body_raises_and_propagates_the_error(remote: FakeRemote, saves: dict[str, bytes]) -> None:
    with pytest.raises(RuntimeError, match="body failed"), shot.guard():
        remote.files[f"{SAVE_FOLDER}/GameSaveData1.sav"] = b"clobbered before the crash"
        raise RuntimeError("body failed")
    for name, data in saves.items():
        assert remote.files[f"{SAVE_FOLDER}/{name}"] == data


def test_a_file_still_differing_after_restore_raises_naming_it(remote: FakeRemote, saves: dict[str, bytes]) -> None:
    remote.stubborn.add(f"{SAVE_FOLDER}/GameSaveData2.sav")
    with pytest.raises(Fail, match=r"GameSaveData2\.sav"), shot.guard():
        remote.files[f"{SAVE_FOLDER}/GameSaveData2.sav"] = b"game rewrote it"


def test_every_mismatch_is_named_in_the_failure(remote: FakeRemote, saves: dict[str, bytes]) -> None:
    remote.stubborn.update({f"{SAVE_FOLDER}/GameSaveData0.sav", f"{SAVE_FOLDER}/OptionSaveData.sav"})
    with pytest.raises(Fail) as err, shot.guard():
        remote.files[f"{SAVE_FOLDER}/GameSaveData0.sav"] = b"x"
        remote.files[f"{SAVE_FOLDER}/OptionSaveData.sav"] = b"y"
    message = str(err.value)
    assert "GameSaveData0.sav" in message
    assert "OptionSaveData.sav" in message
    assert "GameSaveData1.sav" not in message
