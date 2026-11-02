import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from pylocres import Entry, LocresFile, Namespace

from ffrd import cli
from ffrd.build import PAK_SHAPES, edits_text, pack, pak_files
from ffrd.game import LOCRES, MOD_NAME, vanilla, vanilla_hash

WORKSPACE = Path(__file__).resolve().parents[1]
UE4SS_MOD = WORKSPACE / "ue4ss/UnstraightenedText/Scripts/main.lua"
UE4SS_SRC = Path("ue4ss/UnstraightenedText/Scripts/main.lua")
UE4SS_OUT = "build/UnstraightenedText"

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


def edit(namespace: str, key: str, vanilla_line: str, text: str, base_text: str | None = None) -> str:
    base = "" if base_text is None else f'base = "{vanilla_hash(base_text)}"\n'
    return f'["{namespace}".{key}]\nvanilla = "{vanilla_hash(vanilla_line)}"\ntext = "{text}"\n{base}\n'


# A made-up lens, the example in lenses/README.md with a base line before the first table. Not game text.
LENS = """\
# Made-up example lens. Not game text.
{base_line}
[[triage]]
id = "made-up-first-id"
question = "Made-up question one. Does the line mention a made-up thing?"

[[triage]]
id = "made-up-second-id"
question = "Made-up question two. Is the line a made-up greeting?"

[screen]
criteria = ["Made-up criterion one.", "Made-up criterion two."]

[intent]
rule = "Made-up intent rule."

[judge]
rubric = "Made-up judge rubric."
"""


def write_lens(repo: Path, name: str, base: str | None = None) -> None:
    path = repo / "lenses" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    base_line = "" if base is None else f'base = "{base}"'
    path.write_text(LENS.format(base_line=base_line))


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


@pytest.mark.parametrize(
    ("base_dir", "child_dir"),
    [("alpha-base", "zeta-child"), ("zeta-base", "alpha-child")],
    ids=["base-sorts-first", "child-sorts-first"],
)
def test_chained_lens_wins_its_shared_key_whichever_directory_sorts_first(
    base_dir: str, child_dir: str, fake_vanilla: Path, fake_game: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, f"{base_dir}/base.toml", edit("ST_C01_001_01", "10", TEN, "Base ten."))
    write_lens(repo, child_dir, base=base_dir)
    write_edits(repo, f"{child_dir}/child.toml", edit("ST_C01_001_01", "10", TEN, "Child ten.", base_text="Base ten."))
    cli.main(["build"])
    assert packed_locres(repo, TARGET_LOCRES, tmp_path)["ST_C01_001_01"]["10"].translation == "Child ten."
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("child_edit", "message"),
    [
        (
            edit("ST_C01_001_01", "10", TEN, "Child ten.", base_text="Other base."),
            "dialog/alpha-child/child.toml: ST_C01_001_01,10 does not match its base in dialog/zeta-base/base.toml",
        ),
        (
            edit("ST_C01_001_01", "10", TEN, "Child ten."),
            "dialog/alpha-child/child.toml: ST_C01_001_01,10 does not match its base in dialog/zeta-base/base.toml",
        ),
    ],
    ids=["hash-differs", "base-missing"],
)
def test_chained_lens_with_a_base_that_does_not_match_fails_naming_the_key(
    child_edit: str, message: str, fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "zeta-base/base.toml", edit("ST_C01_001_01", "10", TEN, "Base ten."))
    write_lens(repo, "alpha-child", base="zeta-base")
    write_edits(repo, "alpha-child/child.toml", child_edit)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: {message}\n"


def test_unchained_lens_editing_a_key_another_lens_set_still_fails_naming_both(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "alpha-base/base.toml", edit("ST_C01_001_01", "10", TEN, "Base ten."))
    write_lens(repo, "zeta-child", base="gamma-other")
    write_edits(repo, "zeta-child/child.toml", edit("ST_C01_001_01", "10", TEN, "Child ten.", base_text="Base ten."))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: dialog/zeta-child/child.toml: ST_C01_001_01,10 is also edited in dialog/alpha-base/base.toml\n"


def test_edit_with_a_base_and_no_earlier_lens_editing_the_key_fails(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_lens(repo, "alpha-child", base="zeta-base")
    write_edits(repo, "alpha-child/child.toml", edit("ST_C01_001_01", "10", TEN, "Child ten.", base_text="Base ten."))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: dialog/alpha-child/child.toml: ST_C01_001_01,10 has a base but no earlier lens edits it\n"


def test_edit_with_a_non_string_base_fails_naming_the_key(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(
        repo,
        "a.toml",
        f'["ST_C01_001_01".10]\nvanilla = "{vanilla_hash(TEN)}"\ntext = "Child ten."\nbase = 7\n',
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == 'ffrd: dialog/a.toml: ST_C01_001_01,10 needs a string "base"\n'


def test_base_cycle_between_two_lens_files_fails_naming_both_lenses(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_lens(repo, "alpha-lens", base="beta-lens")
    write_lens(repo, "beta-lens", base="alpha-lens")
    write_edits(repo, "alpha-lens/a.toml", edit("ST_C01_001_01", "10", TEN, "Alpha ten."))
    write_edits(repo, "beta-lens/b.toml", edit("ST_C01_001_01", "2", TWO, "Beta two."))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: base cycle between lenses: alpha-lens -> beta-lens -> alpha-lens\n"


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


def test_edits_text_escapes_and_sorts_synthetic_edits() -> None:
    overrides = {
        ("ST_Synth_002", "1"): "Second\ttabbed",
        ("ST_Synth_001", "2"): "Line\nbreak, back\\slash and\rreturn.",
        ("ST_Synth_001", "10"): "Plain.",
    }
    assert edits_text(overrides) == (
        "ST_Synth_001\t10\tPlain.\nST_Synth_001\t2\tLine\\nbreak, back\\\\slash and\\rreturn.\nST_Synth_002\t1\tSecond\\ttabbed\n"
    )


def test_edits_text_is_empty_without_edits() -> None:
    assert edits_text({}) == ""


def test_ue4ss_build_writes_the_text_mod_folder_and_no_pak(
    fake_vanilla: Path, fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_edits(repo, "scenes/one.toml", edit("ST_C01_001_01", "10", TEN, "Edited ten."))
    write_edits(repo, "scenes/deep/two.toml", edit("ST_C01_001_01", "2", TWO, "Edited two."))
    mod_source = repo / UE4SS_SRC
    mod_source.parent.mkdir(parents=True)
    mod_source.write_bytes(b"-- synthetic mod\n")
    cli.main(["build", "--ue4ss"])
    build = repo / UE4SS_OUT
    assert {p.relative_to(build).as_posix() for p in build.rglob("*") if p.is_file()} == {
        "Scripts/main.lua",
        "enabled.txt",
        "edits.txt",
    }
    assert (build / "Scripts/main.lua").read_bytes() == b"-- synthetic mod\n"
    assert (build / "enabled.txt").read_bytes() == b""
    assert (build / "edits.txt").read_text(encoding="utf-8") == ("ST_C01_001_01\t10\tEdited ten.\nST_C01_001_01\t2\tEdited two.\n")
    assert capsys.readouterr().out == f"2 lines overridden -> {UE4SS_OUT}/\n"


@pytest.mark.parametrize("shape_flag", ["--full", "--partial"])
def test_ue4ss_is_exclusive_with_the_other_shape_flags(shape_flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["build", "--ue4ss", shape_flag])
    assert exit_info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


# Made-up (namespace, key, text) edits for the text mod. Not game text.
MOD_OVERRIDES = {
    ("ST_Synth_001", "1"): "Line one\twith a tab.",
    ("ST_Synth_001", "2"): "Line two\nwith a newline, a back\\slash and a\rreturn.",
    ("ST_Synth_002", "9"): "Plain line nine.",
}


@dataclass(frozen=True)
class Registration:
    tick: int
    namespace: str
    key: str
    culture: str
    category: str
    minimal_patch: str
    native: str
    cached: str


# Stubs the game globals the text mod reads. The stub records each PolyglotDataToText call as hex-encoded
# fields so the text never reaches stdout, and it stops the loop after the mod returns true.
TEXT_STUB_HEAD = """
local tick = 0
local out = assert(io.open(RECORD, "w"))
local function hex(s) return (tostring(s):gsub(".", function(c) return string.format("%02x", c:byte()) end)) end
local function ready() return tick >= READY_AT end
local KTL = {
  IsValid = function() return ready() end,
  PolyglotDataToText = function(_, args)
    local cached = args.CachedText and args.CachedText.text or "<none>"
    out:write(table.concat({
      tostring(tick), hex(args.Namespace), hex(args.Key), hex(args.NativeCulture),
      tostring(args.Category), tostring(args.bIsMinimalPatch), hex(args.NativeString), hex(cached),
    }, " "), "\\n")
  end,
}
local KIL = {
  IsValid = function() return ready() end,
  GetCurrentCulture = function() return { ToString = function() return CULTURE end } end,
}
function StaticFindObject(path)
  if path == "/Script/Engine.Default__KismetTextLibrary" then return KTL end
  if path == "/Script/Engine.Default__KismetInternationalizationLibrary" then return KIL end
  error("unexpected object " .. path)
end
function FText(s) return { text = s } end
function ExecuteInGameThread(f) f() end
function LoopAsync(ms, f)
  assert(ms == 1000, "text loop does not retry each second")
  repeat
    tick = tick + 1
    assert(tick <= 1000, "text loop did not stop")
  until f()
end
"""


def unhex(s: str) -> str:
    return bytes.fromhex(s).decode("utf-8")


def run_text_mod(root: Path, edits: str, culture: str, ready_at: int = 1) -> tuple[list[Registration], int, str, dict[str, bytes]]:
    """Run the shipped text mod under plain Lua with the game globals stubbed.

    Returns the registrations, the tick the loop stopped on, the stdout, and the mod folder's files as they were after the run.
    """
    mod_dir = root / "UnstraightenedText"
    scripts = mod_dir / "Scripts"
    scripts.mkdir(parents=True)
    copied = scripts / "main.lua"
    shutil.copyfile(UE4SS_MOD, copied)
    (mod_dir / "enabled.txt").write_bytes(b"")
    (mod_dir / "edits.txt").write_bytes(edits.encode("utf-8"))
    record = root / "record.txt"
    prelude = f"RECORD = {lua_string(str(record))}\nCULTURE = {lua_string(culture)}\nREADY_AT = {ready_at}\n"
    epilogue = f'dofile({lua_string(str(copied))})\nout:write("end " .. tick .. "\\n")\nout:close()\n'
    stub = root / "stub.lua"
    stub.write_text(prelude + TEXT_STUB_HEAD + epilogue, encoding="utf-8")
    result = subprocess.run(["lua", str(stub)], capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    registrations: list[Registration] = []
    final_tick = -1
    for text_line in record.read_text(encoding="utf-8").splitlines():
        parts = text_line.split(" ")
        if parts[0] == "end":
            final_tick = int(parts[1])
            continue
        registrations.append(
            Registration(
                tick=int(parts[0]),
                namespace=unhex(parts[1]),
                key=unhex(parts[2]),
                culture=unhex(parts[3]),
                category=parts[4],
                minimal_patch=parts[5],
                native=unhex(parts[6]),
                cached=unhex(parts[7]),
            )
        )
    files = {p.relative_to(mod_dir).as_posix(): p.read_bytes() for p in mod_dir.rglob("*") if p.is_file()}
    return registrations, final_tick, result.stdout, files


def lua_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@pytest.mark.parametrize("culture", ["en", "en-US", "en-GB"])
def test_text_mod_registers_each_edit_once_for_english_cultures(culture: str, tmp_path: Path) -> None:
    edits = edits_text(MOD_OVERRIDES)
    registrations, final_tick, _, _ = run_text_mod(tmp_path, edits, culture)
    assert registrations == [
        Registration(1, "ST_Synth_001", "1", "en", "0", "false", "Line one\twith a tab.", ""),
        Registration(1, "ST_Synth_001", "2", "en", "0", "false", "Line two\nwith a newline, a back\\slash and a\rreturn.", ""),
        Registration(1, "ST_Synth_002", "9", "en", "0", "false", "Plain line nine.", ""),
    ]
    assert final_tick == 2


@pytest.mark.parametrize("culture", ["de", "ja", "eng"])
def test_text_mod_registers_nothing_outside_english_cultures(culture: str, tmp_path: Path) -> None:
    registrations, final_tick, _, _ = run_text_mod(tmp_path, edits_text(MOD_OVERRIDES), culture)
    assert registrations == []
    assert final_tick == 2


def test_text_mod_retries_each_second_until_the_libraries_are_valid_then_registers_once(tmp_path: Path) -> None:
    registrations, final_tick, _, _ = run_text_mod(tmp_path, edits_text(MOD_OVERRIDES), "en", ready_at=4)
    assert [r.tick for r in registrations] == [4, 4, 4]
    assert len(registrations) == len(MOD_OVERRIDES)
    assert final_tick == 5


def test_text_mod_writes_no_file_and_prints_no_text(tmp_path: Path) -> None:
    edits = edits_text(MOD_OVERRIDES)
    registrations, _, stdout, files = run_text_mod(tmp_path, edits, "en")
    assert registrations
    assert stdout == ""
    assert files == {"Scripts/main.lua": UE4SS_MOD.read_bytes(), "enabled.txt": b"", "edits.txt": edits.encode("utf-8")}
