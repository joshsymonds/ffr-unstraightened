import subprocess
import tomllib
from pathlib import Path

import pytest
from pylocres import Entry, LocresFile, Namespace

from ffrd import cli
from ffrd.build import PAK_SHAPES, pack, pak_files
from ffrd.game import LOCRES, MOD_NAME, vanilla, vanilla_hash

# Made-up lines. Not game text. They match SYNTHETIC_LINES in tests/conftest.py.
TEN = "Synthetic line ten."
TWO = "Synthetic line two."
OUT = "{pak,utoc,ucas}"
TARGET_LOCRES = "FFRS/Content/Localization/Unstraightened/en/Unstraightened.locres"
USER_GAME_INI = "FFRS/Config/UserGame.ini"
GAME_LOCRES = f"{LOCRES}/en/Game.locres"
PARTIAL_LOCRES = f"{LOCRES}/en/Unstraightened.locres"
ANCHORS_INI = (
    "[Internationalization]\n"
    "-LocalizationPaths=%GAMEDIR%Content/Localization/Game\n"
    "+LocalizationPaths=%GAMEDIR%Content/Localization/Unstraightened\n"
    "+LocalizationPaths=%GAMEDIR%Content/Localization/Game\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    monkeypatch.chdir(root)
    return root


def write_edits(repo: Path, name: str, body: str) -> None:
    path = repo / "dialog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def edit(namespace: str, key: str, vanilla_line: str, text: str) -> str:
    return f'["{namespace}".{key}]\nvanilla = "{vanilla_hash(vanilla_line)}"\ntext = "{text}"\n\n'


def write_english(vanilla: Path, lines: tuple[tuple[str, str, str], ...]) -> None:
    namespaces: dict[str, Namespace] = {}
    for ns_name, key, text in lines:
        namespaces.setdefault(ns_name, Namespace(ns_name)).add(Entry(key, text, text, is_hash=False))
    locres = LocresFile()
    for ns in namespaces.values():
        locres.add(ns)
    path = vanilla / "pak" / LOCRES / "en/Game.locres"
    path.parent.mkdir(parents=True, exist_ok=True)
    locres.write(str(path))


def packed_bytes(repo: Path, path_in_pak: str) -> bytes:
    """One file's bytes out of the built pak, read with repak get."""
    pak = repo / "build" / f"{MOD_NAME}.pak"
    return subprocess.run(["repak", "get", str(pak), path_in_pak], check=True, capture_output=True).stdout


def packed_locres(repo: Path, path_in_pak: str, tmp_path: Path) -> LocresFile:
    path = tmp_path / Path(path_in_pak).name
    path.write_bytes(packed_bytes(repo, path_in_pak))
    locres = LocresFile()
    locres.read(str(path))
    return locres


def contents(locres: LocresFile) -> dict[tuple[str, str], tuple[str, int]]:
    """(namespace, key) -> (translation, source-string hash) for every entry."""
    return {(ns.name, entry.key): (entry.translation, entry.hash) for ns in locres for entry in ns}


def pak_listing(repo: Path) -> set[str]:
    pak = repo / "build" / f"{MOD_NAME}.pak"
    listing = subprocess.run(["repak", "list", str(pak)], check=True, capture_output=True, text=True).stdout
    return set(listing.splitlines())


def test_build_applies_toml_edits_from_nested_dialog_folders(
    fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "scenes/one.toml", edit("ST_C01_001_01", "10", TEN, "Edited ten."))
    write_edits(repo, "scenes/deep/two.toml", edit("ST_C01_001_01", "2", TWO, "Edited two."))
    cli.main(["build", "--full"])
    build = repo / "build"
    original = contents(vanilla())
    assert contents(packed_locres(repo, GAME_LOCRES, tmp_path)) == {
        **original,
        ("ST_C01_001_01", "10"): ("Edited ten.", original[("ST_C01_001_01", "10")][1]),
        ("ST_C01_001_01", "2"): ("Edited two.", original[("ST_C01_001_01", "2")][1]),
    }
    for ext in ("utoc", "ucas"):
        assert (build / f"{MOD_NAME}.{ext}").read_bytes() == (fake_game / "FFRS/Content/Paks" / f"global.{ext}").read_bytes()
    assert capsys.readouterr().out == f"2 lines overridden -> build/{MOD_NAME}.{OUT}\n"


def test_default_build_packs_exactly_the_edited_locres_and_the_user_game_ini(
    fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "scenes/one.toml", edit("ST_C01_001_01", "10", TEN, "Edited ten."))
    write_edits(repo, "scenes/deep/two.toml", edit("ST_C01_001_01", "2", TWO, "Edited two."))
    cli.main(["build"])
    assert pak_listing(repo) == {TARGET_LOCRES, USER_GAME_INI}
    original = contents(vanilla())
    assert contents(packed_locres(repo, TARGET_LOCRES, tmp_path)) == {
        ("ST_C01_001_01", "10"): ("Edited ten.", original[("ST_C01_001_01", "10")][1]),
        ("ST_C01_001_01", "2"): ("Edited two.", original[("ST_C01_001_01", "2")][1]),
    }
    assert packed_bytes(repo, USER_GAME_INI).decode("utf-8") == ANCHORS_INI
    assert capsys.readouterr().out == f"2 lines overridden -> build/{MOD_NAME}.{OUT}\n"


def test_partial_build_packs_only_the_edited_entries_into_unstraightened_locres(
    fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path
) -> None:
    write_edits(repo, "scenes/one.toml", edit("ST_C01_001_01", "10", TEN, "Edited ten."))
    cli.main(["build", "--partial"])
    listing = pak_listing(repo)
    assert PARTIAL_LOCRES in listing
    assert GAME_LOCRES not in listing
    assert contents(packed_locres(repo, PARTIAL_LOCRES, tmp_path)) == {
        ("ST_C01_001_01", "10"): ("Edited ten.", contents(vanilla())[("ST_C01_001_01", "10")][1]),
    }


def test_full_build_packs_the_whole_english_table_with_edits_applied(
    fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path
) -> None:
    write_edits(repo, "scenes/one.toml", edit("ST_C01_001_01", "10", TEN, "Edited ten."))
    write_edits(repo, "scenes/deep/two.toml", edit("ST_C01_001_01", "2", TWO, "Edited two."))
    cli.main(["build", "--full"])
    listing = pak_listing(repo)
    assert GAME_LOCRES in listing
    assert TARGET_LOCRES not in listing
    assert USER_GAME_INI not in listing
    expected = contents(vanilla())
    expected[("ST_C01_001_01", "10")] = ("Edited ten.", expected[("ST_C01_001_01", "10")][1])
    expected[("ST_C01_001_01", "2")] = ("Edited two.", expected[("ST_C01_001_01", "2")][1])
    assert contents(packed_locres(repo, GAME_LOCRES, tmp_path)) == expected


def test_full_and_partial_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build", "--full", "--partial"])
    assert exit_info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["build"], ["build", "--full"], ["build", "--partial"]], ids=["default", "full", "partial"])
def test_build_writes_the_unstraightened_mod_files_in_every_shape(
    argv: list[str], fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "scenes/one.toml", edit("ST_C01_001_01", "10", TEN, "Edited ten."))
    cli.main(argv)
    build = repo / "build"
    assert (build / "FFRS-Unstraightened_P.pak").is_file()
    for ext in ("utoc", "ucas"):
        assert (build / f"FFRS-Unstraightened_P.{ext}").read_bytes() == (fake_game / "FFRS/Content/Paks" / f"global.{ext}").read_bytes()
    assert capsys.readouterr().out == f"1 lines overridden -> build/FFRS-Unstraightened_P.{OUT}\n"


def test_zero_edits_default_build_packs_an_empty_target_locres_and_the_ini(
    fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["build"])
    assert capsys.readouterr().out == f"0 lines overridden -> build/{MOD_NAME}.{OUT}\n"
    assert pak_listing(repo) == {TARGET_LOCRES, USER_GAME_INI}
    assert contents(packed_locres(repo, TARGET_LOCRES, tmp_path)) == {}
    assert packed_bytes(repo, USER_GAME_INI).decode("utf-8") == ANCHORS_INI


def test_zero_edits_full_build_packs_the_vanilla_english_table(
    fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["build", "--full"])
    assert capsys.readouterr().out == f"0 lines overridden -> build/{MOD_NAME}.{OUT}\n"
    assert contents(packed_locres(repo, GAME_LOCRES, tmp_path)) == contents(vanilla())


def test_pak_files_names_the_pak_paths_of_each_shape(fake_vanilla: Path, fake_game: Path) -> None:
    overrides = {("ST_C01_001_01", "10"): "Edited ten."}
    assert PAK_SHAPES == ("target", "full", "partial")
    assert set(pak_files("target", vanilla(), overrides)) == {Path(TARGET_LOCRES), Path(USER_GAME_INI)}
    assert set(pak_files("full", vanilla(), overrides)) == {Path(GAME_LOCRES)}
    assert set(pak_files("partial", vanilla(), overrides)) == {Path(PARTIAL_LOCRES)}


def test_pack_stages_the_files_under_out_and_returns_the_pak_path(tmp_path: Path) -> None:
    out = tmp_path / "out"
    pak = pack({Path(USER_GAME_INI): ANCHORS_INI.encode("utf-8")}, out, "Probe_P")
    assert pak == out / "Probe_P.pak"
    assert (out / "stage" / USER_GAME_INI).read_bytes() == ANCHORS_INI.encode("utf-8")
    got = subprocess.run(["repak", "get", str(pak), USER_GAME_INI], check=True, capture_output=True).stdout
    assert got == ANCHORS_INI.encode("utf-8")


@pytest.mark.parametrize("make_empty_dialog", [False, True], ids=["missing", "empty"])
def test_build_with_no_dialog_edits_packs_an_empty_unstraightened_locres(
    make_empty_dialog: bool, fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if make_empty_dialog:
        (repo / "dialog").mkdir()
    cli.main(["build", "--partial"])
    assert capsys.readouterr().out == f"0 lines overridden -> build/{MOD_NAME}.{OUT}\n"
    assert PARTIAL_LOCRES in pak_listing(repo)
    assert contents(packed_locres(repo, PARTIAL_LOCRES, tmp_path)) == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            edit("ST_C01_999", "1", TEN, "Changed."),
            "dialog/bad.toml: ST_C01_999,1 is not in the game's text",
        ),
        (
            edit("ST_C01_001_01", "99", TEN, "Changed."),
            "dialog/bad.toml: ST_C01_001_01,99 is not in the game's text",
        ),
        (
            edit("ST_C01_001_01", "10", "Old wording.", "Changed."),
            "dialog/bad.toml: ST_C01_001_01,10 changed in the game",
        ),
        (
            edit("ST_C01_001_01", "10", TEN, TEN),
            "dialog/bad.toml: ST_C01_001_01,10 is unedited; text matches vanilla",
        ),
        (
            edit("ST_C01_001_01", "10", TEN, "Uses {name}."),
            "dialog/bad.toml: ST_C01_001_01,10 uses placeholders the game won't fill: ['{name}']",
        ),
        (
            edit("ST_C01_001_01", "10", TEN, "<text>Open only."),
            "dialog/bad.toml: ST_C01_001_01,10 has unbalanced <text ...>...</> tags",
        ),
        (
            '["ST_C01_001_01".10]\ntext = "Changed."\n\n',
            'dialog/bad.toml: ST_C01_001_01,10 needs a string "vanilla"',
        ),
        (
            f'["ST_C01_001_01".10]\nvanilla = "{vanilla_hash(TEN)}"\n\n',
            'dialog/bad.toml: ST_C01_001_01,10 needs a string "text"',
        ),
        (
            '["ST_C01_001_01".10]\nvanilla = 7\ntext = "Changed."\n\n',
            'dialog/bad.toml: ST_C01_001_01,10 needs a string "vanilla"',
        ),
        (
            f'["ST_C01_001_01".10]\nvanilla = "{vanilla_hash(TEN)}"\ntext = 7\n\n',
            'dialog/bad.toml: ST_C01_001_01,10 needs a string "text"',
        ),
        (
            'ST_C01_001_01 = "Changed."\n',
            "dialog/bad.toml: ST_C01_001_01 must be a table of edits",
        ),
        (
            '["ST_C01_001_01"]\n10 = "Changed."\n',
            "dialog/bad.toml: ST_C01_001_01,10 must be a table with vanilla and text",
        ),
    ],
)
def test_build_rejects_each_invalid_edit_with_its_message_and_never_prints_vanilla(
    body: str, message: str, fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "bad.toml", body)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert err == f"ffrd: {message}\n"
    assert TEN not in err
    assert TWO not in err


def test_build_rejects_the_same_key_edited_in_two_files_naming_both(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "a.toml", edit("ST_C01_001_01", "10", TEN, "First."))
    write_edits(repo, "b/b.toml", edit("ST_C01_001_01", "10", TEN, "Second."))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: dialog/b/b.toml: ST_C01_001_01,10 is also edited in dialog/a.toml\n"


def test_build_rejects_a_file_that_is_not_valid_toml_and_names_it(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = '["ST_C01_001_01".10]\nvanilla = \n'
    write_edits(repo, "broken.toml", broken)
    with pytest.raises(tomllib.TOMLDecodeError) as decode_error:
        tomllib.loads(broken)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: dialog/broken.toml: cannot read as TOML: {decode_error.value}\n"


def test_build_warns_on_stderr_when_an_edit_drops_a_placeholder_and_still_builds(
    fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_english(fake_vanilla, (("ST_C01_001_01", "10", "Hello {name}, line ten."),))
    write_edits(repo, "warn.toml", edit("ST_C01_001_01", "10", "Hello {name}, line ten.", "Hello there, line ten."))
    cli.main(["build"])
    captured = capsys.readouterr()
    assert captured.err == "warning: dialog/warn.toml: ST_C01_001_01,10 drops placeholders ['{name}']\n"
    assert captured.out == f"1 lines overridden -> build/{MOD_NAME}.{OUT}\n"
    assert packed_locres(repo, TARGET_LOCRES, tmp_path)["ST_C01_001_01"]["10"].translation == "Hello there, line ten."


def test_build_without_a_buildid_exits_1(fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (fake_vanilla / "BUILDID").unlink()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no vanilla text yet; run `ffrd extract`\n"


def test_build_with_a_game_build_mismatch_exits_1(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (fake_vanilla / "BUILDID").write_text("111111\n")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: the game updated to build 424242; run `ffrd extract` and rebuild\n"
