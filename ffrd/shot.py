"""Save guard for gnomon launches: refuses unless FFR cloud saves are off, backs up the save folder, seeds the dedicated slot and restores the rest afterwards."""

import contextlib
import re
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ffrd import deck, gnomon
from ffrd.game import APP_ID, Fail

SAVE_ROOT = f"~/.local/share/Steam/steamapps/compatdata/{APP_ID}/pfx/drive_c/users/steamuser/AppData/Local/FFRS/Steam"
CLOUD_CONFIGS = "~/.local/share/Steam/userdata/*/7/remote/sharedconfig.vdf"
BACKUP_ROOT = Path(".local/share/ffr-unstraightened/save-backups")
DEDICATED = "GameSaveData10.sav"
SEED_CANDIDATE = re.compile(r"GameSaveData\d+\.sav")
CLOUD_BLOCK = re.compile(rf'"{APP_ID}"\s*\{{([^{{}}]*)\}}')
CLOUD_VALUE = re.compile(r'"cloudenabled"\s+"([^"]*)"')


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _lines(out: bytes) -> list[str]:
    return out.decode("utf-8", errors="surrogateescape").splitlines()


def _nul_split(out: bytes) -> list[str]:
    return [part.decode("utf-8", errors="surrogateescape") for part in out.split(b"\0") if part]


def _cloud_off(text: str) -> bool:
    block = CLOUD_BLOCK.search(text)
    if block is None:
        return False
    return CLOUD_VALUE.findall(block.group(1)) == ["0"]


def _require_cloud_off(host: str) -> None:
    paths = _lines(deck.ssh(host, f"ls -1 {CLOUD_CONFIGS}"))
    if not paths:
        raise Fail(f"no sharedconfig.vdf on {host}; cannot confirm FFR cloud saves are off")
    for path in paths:
        text = deck.ssh(host, f"cat {deck.remote(path)}").decode("utf-8", errors="replace")
        if not _cloud_off(text):
            raise Fail(f"FFR cloud saves are not off on {host}; refusing to launch")


def _save_folder(host: str) -> str:
    out = deck.ssh(host, f"find {deck.remote(SAVE_ROOT)} -mindepth 1 -maxdepth 1 -type d -print0")
    ids = [Path(p).name for p in _nul_split(out)]
    if len(ids) != 1:
        raise Fail(f"expected exactly one Steam ID folder under the FFRS save root on {host}, found {len(ids)}")
    return f"{SAVE_ROOT}/{ids[0]}"


def _save_files(host: str, folder: str) -> dict[str, float]:
    out = deck.ssh(host, f"find {deck.remote(folder)} -mindepth 1 -maxdepth 1 -type f -printf '%T@ %f\\0'")
    mtimes: dict[str, float] = {}
    for record in _nul_split(out):
        mtime, name = record.split(" ", 1)
        mtimes[name] = float(mtime)
    return mtimes


def _fetch(host: str, folder: str, name: str) -> bytes:
    return deck.ssh(host, f"cat {deck.remote(f'{folder}/{name}')}")


def _fetch_or_none(host: str, folder: str, name: str) -> bytes | None:
    try:
        return _fetch(host, folder, name)
    except Fail:
        return None


def _write(host: str, folder: str, name: str, data: bytes) -> None:
    with tempfile.TemporaryFile() as stdin:
        stdin.write(data)
        stdin.seek(0)
        deck.ssh(host, f"cat > {deck.remote(f'{folder}/{name}')}", stdin)


def _backup(steam_id: str, files: dict[str, bytes]) -> None:
    stamp_dir = Path.home() / BACKUP_ROOT / _stamp()
    try:
        stamp_dir.mkdir(parents=True)
    except FileExistsError as e:
        raise Fail(f"backup folder {stamp_dir.name} already exists; refusing to reuse it") from e
    dest = stamp_dir / steam_id
    dest.mkdir()
    for name, data in files.items():
        (dest / name).write_bytes(data)
        if (dest / name).read_bytes() != data:
            raise Fail(f"backup copy of {name} does not match the save folder")


def _seed(host: str, folder: str, mtimes: dict[str, float], files: dict[str, bytes]) -> None:
    if DEDICATED in mtimes:
        return
    candidates = [name for name in mtimes if SEED_CANDIDATE.fullmatch(name)]
    if not candidates:
        raise Fail(f"no GameSaveData<n>.sav on {host} to seed {DEDICATED} from")
    newest = max(candidates, key=lambda name: (mtimes[name], name))
    _write(host, folder, DEDICATED, files[newest])


def _restore(host: str, folder: str, files: dict[str, bytes]) -> None:
    restorable = {name: data for name, data in files.items() if name != DEDICATED}
    failed: list[str] = []
    for name, data in restorable.items():
        try:
            if _fetch_or_none(host, folder, name) != data:
                print(f"changed by the game: {name}")
                _write(host, folder, name, data)
        except Fail:
            failed.append(name)
    for name, data in restorable.items():
        if name in failed:
            continue
        try:
            if _fetch(host, folder, name) != data:
                failed.append(name)
        except Fail:
            failed.append(name)
    if failed:
        raise Fail("save restore did not verify byte for byte: " + ", ".join(sorted(set(failed))))


@contextlib.contextmanager
def guard() -> Iterator[None]:
    host = gnomon.host()
    _require_cloud_off(host)
    folder = _save_folder(host)
    mtimes = _save_files(host, folder)
    files = {name: _fetch(host, folder, name) for name in mtimes}
    _backup(folder.rsplit("/", 1)[1], files)
    _seed(host, folder, mtimes, files)
    try:
        yield
    finally:
        _restore(host, folder, files)
