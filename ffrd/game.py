"""Game install, vanilla text and .po helpers shared by the ffrd commands."""

import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path

import polib
from pylocres import LocresFile

MOD_NAME = "FFRS-Dialog_P"
APP_ID = "3259780"
LOCRES = Path("FFRS/Content/Localization/Game")
DEFAULT_GAME = Path.home() / ".local/share/Steam/steamapps/common/FINAL FANTASY RESONANCE"


class Fail(Exception):
    pass


def repo_root() -> Path:
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "devenv.nix").is_file() and (d / "ffrd").is_dir():
            return d
    raise Fail("run ffrd inside the ffr-dialog repository")


def game_dir() -> Path:
    g = Path(os.environ.get("FFR_GAME", DEFAULT_GAME))
    if not (g / "FFRS/Content/Paks").is_dir():
        raise Fail(f"no game install at {g} (set FFR_GAME)")
    return g


def paks_dir() -> Path:
    return game_dir() / "FFRS/Content/Paks"


def build_id() -> str:
    manifest = game_dir().parent.parent / f"appmanifest_{APP_ID}.acf"
    m = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text())
    if not m:
        raise Fail(f"no buildid in {manifest}")
    return m.group(1)


def vanilla_dir() -> Path:
    env = os.environ.get("FFR_VANILLA")
    return Path(env) if env else repo_root() / "vanilla"


def natural(s: str) -> list[int | str]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def read_locres(path: Path) -> LocresFile:
    f = LocresFile()
    f.read(str(path))
    return f


def vanilla(lang: str = "en") -> LocresFile:
    path = vanilla_dir() / "pak" / LOCRES / lang / "Game.locres"
    if not path.exists():
        raise Fail("no vanilla text yet; run `ffrd extract`")
    return read_locres(path)


def entries(locres: LocresFile) -> Iterator[tuple[str, str, str]]:
    """(namespace, key, text) in scene order."""
    for ns in sorted(locres, key=lambda n: natural(n.name)):
        for key in sorted(ns.entrys, key=natural):
            yield ns.name, key, ns[key].translation


def po_entry(ns: str, key: str, text: str, ja: str | None) -> polib.POEntry:
    e = polib.POEntry(msgctxt=f"{ns},{key}", msgid=text, msgstr="")
    if ja is not None and ja != text:
        e.comment = f"ja: {ja}"
    return e


def vanilla_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
