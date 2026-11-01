from pathlib import Path

import pytest
from pylocres import Entry, LocresFile, Namespace

LOCRES = Path("FFRS/Content/Localization/Game")

# Made-up (namespace, key, English, Japanese) lines. Not game text.
SYNTHETIC_LINES: tuple[tuple[str, str, str, str], ...] = (
    ("ST_C01_001_01", "10", "Synthetic line ten.", "合成テキスト十"),
    ("ST_C01_001_01", "2", "Synthetic line two.", "合成テキスト二"),
    ("ST_C01_001_02", "1", "", "空欄の行"),
    ("ST_C03_002_01", "1", "Same in both.", "Same in both."),
)


def _locres(lang_index: int) -> LocresFile:
    namespaces: dict[str, Namespace] = {}
    for line in SYNTHETIC_LINES:
        ns_name, key, english = line[0], line[1], line[2]
        ns = namespaces.setdefault(ns_name, Namespace(ns_name))
        ns.add(Entry(key, line[2 + lang_index], english, is_hash=False))
    locres = LocresFile()
    for ns in namespaces.values():
        locres.add(ns)
    return locres


@pytest.fixture
def fake_vanilla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vanilla"
    for lang, index in (("en", 0), ("ja", 1)):
        path = root / "pak" / LOCRES / lang / "Game.locres"
        path.parent.mkdir(parents=True)
        _locres(index).write(path)
    (root / "BUILDID").write_text("424242\n")
    monkeypatch.setenv("FFR_VANILLA", str(root))
    return root


@pytest.fixture
def fake_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    steamapps = tmp_path / "steamapps"
    game = steamapps / "common" / "Synthetic Game"
    paks = game / "FFRS/Content/Paks"
    paks.mkdir(parents=True)
    (paks / "global.utoc").write_bytes(b"synthetic utoc")
    (paks / "global.ucas").write_bytes(b"synthetic ucas")
    (steamapps / "appmanifest_3259780.acf").write_text('"AppState"\n{\n\t"appid"\t\t"3259780"\n\t"buildid"\t\t"424242"\n}\n')
    monkeypatch.setenv("FFR_GAME", str(game))
    return game
