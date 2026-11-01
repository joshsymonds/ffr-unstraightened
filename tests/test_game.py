import re
from pathlib import Path

import pytest
from pylocres import Entry, LocresFile, Namespace

from ffrd import game
from ffrd.game import Fail


def make_repo(root: Path) -> Path:
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    return root


def test_repo_root_walks_up_to_devenv_and_ffrd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path / "repo")
    nested = repo / "ffrd" / "deep"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert game.repo_root() == repo


def test_repo_root_requires_devenv_nix_and_ffrd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_package = tmp_path / "no-package"
    no_package.mkdir()
    (no_package / "devenv.nix").write_text("{ }\n")
    monkeypatch.chdir(no_package)
    with pytest.raises(Fail, match="run ffrd inside the ffr-dialog repository"):
        game.repo_root()


def test_repo_root_ignores_old_flake_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_layout = tmp_path / "old-layout"
    (old_layout / "dialog").mkdir(parents=True)
    (old_layout / "flake.nix").write_text("{ }\n")
    monkeypatch.chdir(old_layout)
    with pytest.raises(Fail):
        game.repo_root()


def test_game_dir_honours_ffr_game(fake_game: Path) -> None:
    assert game.game_dir() == fake_game


def test_game_dir_defaults_to_default_game(fake_game: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FFR_GAME")
    monkeypatch.setattr(game, "DEFAULT_GAME", fake_game)
    assert game.game_dir() == fake_game


def test_game_dir_without_paks_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "no-game"
    monkeypatch.setenv("FFR_GAME", str(missing))
    with pytest.raises(Fail, match=re.escape(f"no game install at {missing} (set FFR_GAME)")):
        game.game_dir()


def test_paks_dir(fake_game: Path) -> None:
    assert game.paks_dir() == fake_game / "FFRS/Content/Paks"


def test_build_id_reads_manifest(fake_game: Path) -> None:
    assert game.build_id() == "424242"


def test_build_id_without_buildid_raises(fake_game: Path) -> None:
    manifest = fake_game.parent.parent / "appmanifest_3259780.acf"
    manifest.write_text('"AppState"\n{\n\t"appid"\t\t"3259780"\n}\n')
    with pytest.raises(Fail, match="no buildid in"):
        game.build_id()


def test_vanilla_dir_honours_ffr_vanilla(fake_vanilla: Path) -> None:
    assert game.vanilla_dir() == fake_vanilla


def test_vanilla_dir_defaults_to_repo_vanilla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("FFR_VANILLA", raising=False)
    assert game.vanilla_dir() == repo / "vanilla"


def test_vanilla_reads_english_and_japanese(fake_vanilla: Path) -> None:
    assert game.vanilla()["ST_C01_001_01"]["2"].translation == "Synthetic line two."
    assert game.vanilla("ja")["ST_C01_001_01"]["2"].translation == "合成テキスト二"


def test_vanilla_without_extract_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FFR_VANILLA", str(tmp_path / "empty"))
    with pytest.raises(Fail, match=re.escape("no vanilla text yet; run `ffrd extract`")):
        game.vanilla()


def test_entries_yields_scene_order_with_natural_keys(fake_vanilla: Path) -> None:
    assert list(game.entries(game.vanilla())) == [
        ("ST_C01_001_01", "2", "Synthetic line two."),
        ("ST_C01_001_01", "10", "Synthetic line ten."),
        ("ST_C01_001_02", "1", ""),
        ("ST_C03_002_01", "1", "Same in both."),
    ]


def test_entries_orders_namespaces_naturally() -> None:
    locres = LocresFile()
    for name in ("ST_C10_001_01", "ST_C2_001_01"):
        ns = Namespace(name)
        ns.add(Entry("1", "x", "x", is_hash=False))
        locres.add(ns)
    assert [ns for ns, _, _ in game.entries(locres)] == ["ST_C2_001_01", "ST_C10_001_01"]


def test_natural_sorts_digit_runs_numerically() -> None:
    assert sorted(["10", "9", "2"], key=game.natural) == ["2", "9", "10"]
    assert game.natural("ab10c") == ["ab", 10, "c"]


def test_po_entry_comments_japanese_when_it_differs() -> None:
    e = game.po_entry("ST_C01_001_01", "2", "Synthetic line two.", "合成テキスト二")
    assert e.msgctxt == "ST_C01_001_01,2"
    assert e.msgid == "Synthetic line two."
    assert e.msgstr == ""
    assert e.comment == "ja: 合成テキスト二"


def test_po_entry_has_no_comment_when_ja_matches_or_missing() -> None:
    same = game.po_entry("ST_C03_002_01", "1", "Same in both.", "Same in both.")
    missing = game.po_entry("ST_C01_001_01", "2", "Synthetic line two.", None)
    assert not same.comment
    assert not missing.comment


def test_vanilla_hash_is_first_16_hex_of_sha256() -> None:
    assert game.vanilla_hash("abc") == "ba7816bf8f01cfea"


def test_vanilla_hash_encodes_utf8() -> None:
    assert game.vanilla_hash("合成テキスト二") == "0033fc2a8dff117a"
