"""shot: screenshot a changed line in game on the gnomon host, before (--vanilla) or after the mod.

`ffrd shot <namespace> <key>` brings the scene that holds the line on screen with the UnstraightenedShot mod: through
the map that starts the scene on load at a story progress when the scene table names one, else by playing the scene's
events in order. The mod logs the on-screen line's hash; the game window is focused and captured with niri, and the
PNG is kept in shots/ only when the hash matches the expected text both before and after the capture. `--title`
captures the title screen with the title tag. Every launch runs inside the save guard: it refuses unless FFR cloud
saves are off, backs up the save folder, reseeds the dedicated slot from the newest save, and restores every other
save file afterwards. Output names keys and hashes only.
"""

import argparse
import contextlib
import json
import re
import secrets
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ffrd import deck, gnomon, scenes
from ffrd.build import load_overrides
from ffrd.game import APP_ID, MOD_NAME, Fail, repo_root, vanilla, vanilla_hash
from ffrd.install import EXTS
from ffrd.probe import PROBE_KEY

SAVE_ROOT = f"~/.local/share/Steam/steamapps/compatdata/{APP_ID}/pfx/drive_c/users/steamuser/AppData/Local/FFRS/Steam"
CLOUD_CONFIGS = "~/.local/share/Steam/userdata/*/7/remote/sharedconfig.vdf"
BACKUP_ROOT = Path(".local/share/ffr-unstraightened/save-backups")
DEDICATED = "GameSaveData10.sav"
DEDICATED_SLOT = 10
MOD_DIR = "ue4ss/UnstraightenedShot"
LOG = "shot.log"
SHOTS_DIR = "shots"
WINDOW_APP = f"steam_app_{APP_ID}"
# niri's IPC socket and the Wayland display of the desktop session, for commands run over ssh.
DESKTOP = "export XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-1 NIRI_SOCKET=$(ls /run/user/$(id -u)/niri.wayland-*.sock | head -1); "
DONE_TIMEOUT = 2400
SETTLE = 3.0
CAPTURE_WAIT = 10.0
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
    # Every run starts the dedicated slot afresh: a run can leave it on another map or story progress.
    candidates = [name for name in mtimes if SEED_CANDIDATE.fullmatch(name) and name != DEDICATED]
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


def _desktop(host: str, command: str) -> bytes:
    return deck.ssh(host, DESKTOP + command)


def _game_window(host: str) -> int | None:
    windows = json.loads(_desktop(host, "niri msg --json windows") or b"[]")
    return next((w["id"] for w in windows if w.get("app_id") == WINDOW_APP), None)


def _focused(host: str) -> int | None:
    window = json.loads(_desktop(host, "niri msg --json focused-window") or b"null")
    return None if window is None else window.get("id")


def _focus(host: str, window: int) -> None:
    _desktop(host, f"niri msg action focus-window --id {window}")


def _capture(host: str, window: int) -> bytes:
    """The window's pixels as PNG. niri also copies a window screenshot to the clipboard, so a text clipboard is put back."""
    types = _desktop(host, "wl-paste --list-types 2>/dev/null || true").decode().split()
    saved = _desktop(host, "wl-paste --no-newline --type text/plain") if "text/plain" in types else None
    path = f"/tmp/ffrd-shot-{secrets.token_hex(4)}.png"
    try:
        _desktop(host, f"niri msg action screenshot-window --id {window} --write-to-disk true --show-pointer false --path {path}")
        deadline = time.monotonic() + CAPTURE_WAIT
        while deck.ssh_status(host, f"test -s {path}") != 0:
            if time.monotonic() >= deadline:
                raise Fail(f"niri wrote no window screenshot on {host}")
            time.sleep(0.5)
        return deck.ssh(host, f"cat {path}")
    finally:
        deck.ssh(host, f"rm -f {path}")
        if saved is not None:
            with tempfile.TemporaryFile() as stdin:
                stdin.write(saved)
                stdin.seek(0)
                deck.ssh(host, "cat > /tmp/ffrd-clip", stdin)
            # wl-copy stays running to serve the clipboard, so it is detached from the ssh session.
            _desktop(host, "setsid -f sh -c 'wl-copy < /tmp/ffrd-clip; rm -f /tmp/ffrd-clip' >/dev/null 2>&1 </dev/null")


class Watcher:
    """Follows the mod's log: focuses the game window so it renders, captures on snap and releases the mod's hold."""

    def __init__(self, host: str, mod_dir: str) -> None:
        self.host = host
        self.mod_dir = mod_dir
        self.previous: int | None = None
        self.window: int | None = None
        self.seen = 0
        self.snap: str | None = None
        self.held: str | None = None
        self.png: bytes | None = None

    def __call__(self, lines: list[str]) -> None:
        if self.window is None:
            self.window = _game_window(self.host)
            if self.window is not None:
                self.previous = _focused(self.host)
                _focus(self.host, self.window)
        for line in lines[self.seen :]:
            word, _, value = line.partition(" ")
            if word == "snap" and self.window is not None:
                self.snap = value
                _focus(self.host, self.window)
                time.sleep(SETTLE)
                self.png = _capture(self.host, self.window)
                deck.ssh(self.host, f"touch {deck.remote(self.mod_dir + '/go')}")
            elif word == "held":
                self.held = value
        self.seen = len(lines)

    def release(self) -> None:
        if self.previous is not None:
            _focus(self.host, self.previous)


def _expected(namespace: str, key: str, before: bool) -> str:
    en = vanilla()
    entry = en[namespace][key] if en[namespace] else None
    if entry is None:
        raise Fail(f"{namespace},{key} is not in the vanilla text")
    if before:
        return vanilla_hash(entry.translation)
    overrides, errors, _warnings = load_overrides(repo_root(), en)
    if errors:
        raise Fail("\n".join(errors))
    if (namespace, key) not in overrides:
        raise Fail(f"{namespace},{key} is not changed by the build")
    return vanilla_hash(overrides[(namespace, key)])


def target_files(expected: str, events: list[str], starts: list[tuple[int, int]], title: bool) -> dict[str, bytes]:
    files = {"slot.txt": f"{DEDICATED_SLOT}\n".encode()}
    if title:
        files["title.txt"] = f"{expected}\n".encode()
    else:
        ups = ";".join(f"{map_id}:{progress}" for map_id, progress in starts)
        files["target.txt"] = f"{expected}\t{','.join(events)}\t{ups}\n".encode()
    return files


def verdict(expected: str, watcher: Watcher) -> bool:
    return watcher.snap == expected and watcher.held == expected and bool(watcher.png)


def _build_files() -> list[Path]:
    files = [repo_root() / "build" / f"{MOD_NAME}.{ext}" for ext in EXTS]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise Fail(f"build/{', '.join(missing)} missing; run `ffrd build`")
    return files


def cmd_shot(args: argparse.Namespace) -> None:
    if args.title == (args.namespace is not None):
        raise Fail("name a namespace and key, or pass --title")
    namespace, key = PROBE_KEY if args.title else (args.namespace, args.key)
    if key is None:
        raise Fail("name the key after the namespace")
    expected = _expected(namespace, key, args.vanilla)
    events = [] if args.title else scenes.line_events(namespace, key)
    if not args.title and not events:
        raise Fail(f"no scene in the scene table plays {namespace},{key}")
    starts = list(dict.fromkeys(start for event in events for start in scenes.startups(event)))
    files = target_files(expected, events, starts, args.title)
    build = [] if args.vanilla else _build_files()
    host = gnomon.host()
    mod = repo_root() / MOD_DIR
    watcher = Watcher(host, f"{gnomon.ue4ss()}/Mods/{mod.name}")
    dest = f"{gnomon.paks()}/~mods"
    with guard():
        if build:
            deck.put(host, dest, build)
        try:
            gnomon.run_mod(mod, files, LOG, offscreen=False, watch=watcher, done_timeout=DONE_TIMEOUT)
        finally:
            if build:
                deck.delete(host, dest, [f.name for f in build])
            watcher.release()
    side = "before" if args.vanilla else "after"
    line = f"{namespace},{key} {side} expected {expected} snap {watcher.snap or 'none'} held {watcher.held or 'none'}"
    if not verdict(expected, watcher):
        raise Fail(line + ": not captured")
    out = repo_root() / SHOTS_DIR
    out.mkdir(exist_ok=True)
    path = out / f"{namespace}.{key}.{side}.png"
    path.write_bytes(watcher.png or b"")
    print(f"{line}: {path.relative_to(repo_root())}")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "shot",
        help="screenshot a changed line in game on the gnomon host, before (--vanilla) or after the mod",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("namespace", nargs="?", help="namespace of the line, such as ST_C07_701_01")
    p.add_argument("key", nargs="?", help="key of the line")
    p.add_argument("--vanilla", action="store_true", help="capture the game's own line, without the built mod")
    p.add_argument("--title", action="store_true", help="capture the title screen and its title tag")
    p.set_defaults(fn=cmd_shot)
