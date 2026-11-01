import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import polib
import pytest
from pylocres import Entry, LocresFile, Namespace

from ffrd import cli, extract, install, probe, scenes
from ffrd.game import LOCRES, MOD_NAME, vanilla_hash
from ffrd.pipeline import smoke
from ffrd.scenes import SceneLine
from ffrd.voice import cli as voice_cli
from ffrd.voice import pairs

EXTS = ("pak", "utoc", "ucas")
WORKSPACE = Path(__file__).resolve().parents[1]


def make_repo(root: Path) -> Path:
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    build = root / "build"
    build.mkdir()
    for ext in EXTS:
        (build / f"{MOD_NAME}.{ext}").write_bytes(f"synthetic {ext}".encode())
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def recorded_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "progress"
    path.write_text("3\n")
    monkeypatch.setenv("FFR_PROGRESS", str(path))
    return path


def mods_dir(game: Path) -> Path:
    return game / "FFRS/Content/Paks/~mods"


def test_install_copies_built_files_and_creates_mods_dir(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    assert not dest.exists()
    cli.main(["install"])
    for ext in EXTS:
        name = f"{MOD_NAME}.{ext}"
        assert (dest / name).read_bytes() == (repo / "build" / name).read_bytes()
    assert capsys.readouterr().out == f"installed to {dest}\n"


def test_install_with_missing_files_exits_1(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "build" / f"{MOD_NAME}.utoc").unlink()
    (repo / "build" / f"{MOD_NAME}.ucas").unlink()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["install"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: missing ['{MOD_NAME}.utoc', '{MOD_NAME}.ucas']; run `ffrd build`\n"
    assert not mods_dir(fake_game).exists()


def test_uninstall_removes_the_three_files_and_keeps_others(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    dest.mkdir(parents=True)
    for ext in EXTS:
        (dest / f"{MOD_NAME}.{ext}").write_bytes(b"installed")
    (dest / "Other.pak").write_bytes(b"unrelated")
    cli.main(["uninstall"])
    for ext in EXTS:
        assert not (dest / f"{MOD_NAME}.{ext}").exists()
    assert (dest / "Other.pak").read_bytes() == b"unrelated"
    assert capsys.readouterr().out == f"removed {MOD_NAME} from {dest}\n"


def test_uninstall_tolerates_absent_files(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    dest.mkdir(parents=True)
    (dest / f"{MOD_NAME}.pak").write_bytes(b"installed")
    cli.main(["uninstall"])
    assert not (dest / f"{MOD_NAME}.pak").exists()
    assert capsys.readouterr().out == f"removed {MOD_NAME} from {dest}\n"


def test_uninstall_tolerates_missing_mods_dir(fake_game: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dest = mods_dir(fake_game)
    assert not dest.exists()
    cli.main(["uninstall"])
    assert capsys.readouterr().out == f"removed {MOD_NAME} from {dest}\n"


def test_commands_register_install_and_uninstall(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert install in cli.COMMANDS
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage: ffrd" in out
    assert "Build a FINAL FANTASY RESONANCE dialogue mod from dialog/**/*.toml edits." in out
    assert "copy the built mod into the game's Paks/~mods" in out
    assert "remove the mod from Paks/~mods" in out


def test_main_reports_fail_on_stderr_and_exits_1(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-game"
    monkeypatch.setenv("FFR_GAME", str(missing))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["install"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == f"ffrd: no game install at {missing} (set FFR_GAME)\n"


def test_python_m_ffrd_runs_cli_main(repo: Path, tmp_path: Path) -> None:
    missing = tmp_path / "no-game"
    env = {**os.environ, "FFR_GAME": str(missing), "PYTHONPATH": str(WORKSPACE)}
    result = subprocess.run(
        [sys.executable, "-m", "ffrd", "install"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == f"ffrd: no game install at {missing} (set FFR_GAME)\n"


STUB_TWO = '["ST_C01_001_01".2]\nvanilla = "dcc4be08fef05464"\ntext = "Synthetic line two."\n\n'
STUB_TEN = '["ST_C01_001_01".10]\nvanilla = "49c8ad88d95de4f8"\ntext = "Synthetic line ten."\n\n'
STUB_SAME = '["ST_C03_002_01".1]\nvanilla = "6ff2fc2682a12379"\ntext = "Same in both."\n\n'


@pytest.mark.usefixtures("recorded_progress")
def test_find_without_arguments_prints_every_non_empty_english_stub_in_scene_order(
    fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["find"])
    captured = capsys.readouterr()
    assert captured.out == STUB_TWO + STUB_TEN + STUB_SAME
    assert captured.err == "# 3 shown, 0 withheld\n"


@pytest.mark.usefixtures("recorded_progress")
def test_find_pattern_matches_text_case_insensitively(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "SYNTHETIC LINE TWO"])
    captured = capsys.readouterr()
    assert captured.out == STUB_TWO
    assert captured.err == "# 1 shown, 0 withheld\n"


@pytest.mark.usefixtures("recorded_progress")
def test_find_pattern_matches_key(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "^10$"])
    captured = capsys.readouterr()
    assert captured.out == STUB_TEN
    assert captured.err == "# 1 shown, 0 withheld\n"


@pytest.mark.usefixtures("recorded_progress")
def test_find_ns_option_filters_namespaces_by_regex(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "--ns", "^ST_C03_"])
    captured = capsys.readouterr()
    assert captured.out == STUB_SAME
    assert captured.err == "# 1 shown, 0 withheld\n"


@pytest.mark.usefixtures("recorded_progress")
def test_find_does_not_read_japanese_text(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (fake_vanilla / "pak" / LOCRES / "ja" / "Game.locres").unlink()
    cli.main(["find", "--ns", "^ST_C03_"])
    captured = capsys.readouterr()
    assert captured.out == STUB_SAME
    assert captured.err == "# 1 shown, 0 withheld\n"


@pytest.mark.usefixtures("recorded_progress")
def test_find_stubs_parse_back_to_the_exact_english_text_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = (
        ('ST_C01_"T"\\01', 'key "quoted" 1', 'Quote " and backslash \\ here.'),
        ('ST_C01_"T"\\01', "plain_key-9", "line one\nline two\rthree\tfour"),
        ("ST_C02_001", "del\x7fchar", "del\x7f, ctrl\x01 and é 日本語 ✓"),
    )
    root = tmp_path / "vanilla"
    path = root / "pak" / LOCRES / "en" / "Game.locres"
    path.parent.mkdir(parents=True)
    locres_file(lines).write(str(path))
    monkeypatch.setenv("FFR_VANILLA", str(root))
    cli.main(["find"])
    captured = capsys.readouterr()
    expected: dict[str, dict[str, dict[str, str]]] = {}
    for ns, key, text in lines:
        expected.setdefault(ns, {})[key] = {"vanilla": vanilla_hash(text), "text": text}
    assert tomllib.loads(captured.out) == expected
    assert captured.err == "# 3 shown, 0 withheld\n"


@pytest.mark.usefixtures("recorded_progress")
def test_find_skips_empty_english_lines(fake_vanilla: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["find", "--ns", "^ST_C01_001_02$"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "# 0 shown, 0 withheld\n"


@pytest.mark.usefixtures("recorded_progress")
def test_find_without_vanilla_text_exits_1(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FFR_VANILLA", str(tmp_path / "no-vanilla"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["find"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no vanilla text yet; run `ffrd extract`\n"


def test_find_is_registered_with_its_help(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert any(module.__name__ == "ffrd.find" for module in cli.COMMANDS)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "print vanilla entries as TOML stubs, ready to paste into dialog/" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["find", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "regex matched against text or key (case-insensitive)" in out
    assert "regex the namespace must match, e.g. '^ST_C01_023_'" in out


BASE_EN = (
    ("ST_T01_001", "1", "Synthetic base one."),
    ("ST_T01_001", "2", "Synthetic base two."),
    ("ST_T02_001", "1", ""),
    ("ST_T03_001", "1", "Same in both."),
)
BASE_JA = (
    ("ST_T01_001", "1", "合成基礎一"),
    ("ST_T01_001", "2", "合成基礎二"),
    ("ST_T02_001", "1", "空欄"),
    ("ST_T03_001", "1", "Same in both."),
)
# A patch pak ships a whole replacement Game.locres, so each patch repeats the base lines.
PATCH_TWO_EN = (BASE_EN[0], ("ST_T01_001", "2", "Synthetic patch two from pak two."), BASE_EN[2], BASE_EN[3])
PATCH_TEN_EN = (BASE_EN[0], ("ST_T01_001", "2", "Synthetic patch two from pak ten."), BASE_EN[2], BASE_EN[3])


def locres_file(lines: tuple[tuple[str, str, str], ...]) -> LocresFile:
    namespaces: dict[str, Namespace] = {}
    for ns_name, key, text in lines:
        namespaces.setdefault(ns_name, Namespace(ns_name)).add(Entry(key, text, text, is_hash=False))
    locres = LocresFile()
    for ns in namespaces.values():
        locres.add(ns)
    return locres


def stage_locres(stage: Path, lang: str, lines: tuple[tuple[str, str, str], ...]) -> None:
    path = stage / LOCRES / lang / "Game.locres"
    path.parent.mkdir(parents=True, exist_ok=True)
    locres_file(lines).write(str(path))


def pack(stage: Path, pak: Path) -> None:
    subprocess.run(
        ["repak", "pack", "-q", "--version", "V11", "--compression", "Zlib", "-m", "../../../", str(stage), str(pak)],
        check=True,
    )


@pytest.fixture
def game_paks(fake_game: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FFR_VANILLA", str(tmp_path / "vanilla"))
    paks = fake_game / "FFRS/Content/Paks"
    stages = tmp_path / "stages"

    base = stages / "base"
    stage_locres(base, "en", BASE_EN)
    stage_locres(base, "ja", BASE_JA)
    pack(base, paks / "FFRS-Windows.pak")

    patch_two = stages / "patch-two"
    stage_locres(patch_two, "en", PATCH_TWO_EN)
    pack(patch_two, paks / "FFRS-Windows_2.pak")

    patch_ten = stages / "patch-ten"
    stage_locres(patch_ten, "en", PATCH_TEN_EN)
    pack(patch_ten, paks / "FFRS-Windows_10.pak")

    no_text = stages / "no-text"
    (no_text / "FFRS/Content/Textures").mkdir(parents=True)
    (no_text / "FFRS/Content/Textures/synthetic.txt").write_text("synthetic texture")
    pack(no_text, paks / "FFRS-Windows_5.pak")
    return paks


def test_extract_removes_stale_vanilla_contents(game_paks: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vanilla = tmp_path / "vanilla"
    (vanilla / "pak/stale").mkdir(parents=True)
    (vanilla / "pak/stale/Old.locres").write_bytes(b"stale")
    (vanilla / "stale.po").write_text("stale")
    cli.main(["extract"])
    assert not (vanilla / "pak/stale").exists()
    assert not (vanilla / "stale.po").exists()
    assert (vanilla / "en.po").is_file()


def test_extract_unpacks_english_and_japanese_text_with_later_patch_paks_winning(
    game_paks: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["extract"])
    lang_dir = tmp_path / "vanilla/pak" / LOCRES
    en = LocresFile()
    en.read(str(lang_dir / "en/Game.locres"))
    ja = LocresFile()
    ja.read(str(lang_dir / "ja/Game.locres"))
    assert en["ST_T01_001"]["2"].translation == "Synthetic patch two from pak ten."
    assert ja["ST_T01_001"]["2"].translation == "合成基礎二"


def test_extract_skips_paks_without_text(game_paks: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["extract"])
    assert "FFRS-Windows_5.pak" not in capsys.readouterr().out
    assert not (tmp_path / "vanilla/pak/FFRS/Content/Textures").exists()


def test_extract_writes_build_id_from_manifest(game_paks: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["extract"])
    assert (tmp_path / "vanilla/BUILDID").read_text() == "424242\n"


def test_extract_writes_en_po_with_ja_comments_and_build_header(
    game_paks: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["extract"])
    po = polib.pofile(str(tmp_path / "vanilla/en.po"))
    assert po.metadata["X-Game-Build"] == "424242"
    assert [(e.msgctxt, e.msgid, e.msgstr, e.comment) for e in po] == [
        ("ST_T01_001,1", "Synthetic base one.", "", "ja: 合成基礎一"),
        ("ST_T01_001,2", "Synthetic patch two from pak ten.", "", "ja: 合成基礎二"),
        ("ST_T03_001,1", "Same in both.", "", ""),
    ]


def test_extract_prints_each_text_pak_and_the_build_summary(game_paks: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["extract"])
    assert capsys.readouterr().out == (
        "extracted text from FFRS-Windows.pak\n"
        "extracted text from FFRS-Windows_2.pak\n"
        "extracted text from FFRS-Windows_10.pak\n"
        "build 424242: 3 English entries -> vanilla/en.po\n"
    )


def test_extract_is_registered_with_its_help(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert extract in cli.COMMANDS
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "pull the installed game's text into vanilla/" in capsys.readouterr().out


def test_build_is_registered_between_find_and_install_with_its_help(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert [module.__name__ for module in cli.COMMANDS] == [
        "ffrd.extract",
        "ffrd.progress",
        "ffrd.find",
        "ffrd.build",
        "ffrd.install",
        "ffrd.leakcheck",
        "ffrd.pipeline.report",
        "ffrd.pipeline.diff",
        "ffrd.pipeline.run",
        "ffrd.scenes",
        "ffrd.voice.cli",
        "ffrd.pipeline.smoke",
        "ffrd.probe",
    ]
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "apply dialog/**/*.toml edits to vanilla text and pack the mod into build/" in capsys.readouterr().out


def test_progress_is_registered_after_extract_with_its_help(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "record the chapter you are on, or print the recorded one" in capsys.readouterr().out


def test_report_and_diff_are_registered_with_their_help(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "count a lens run's outcomes per chapter, reason, character and gate" in out
    assert "show dialog/<lens> rewrites beside their vanilla English, up to recorded progress" in out


def test_diff_without_recorded_progress_exits_1_through_the_cli(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FFR_PROGRESS", str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["diff", "--lens", "made-up-lens"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no progress recorded; run `ffrd progress <chapter>`\n"


def test_report_without_a_lens_file_exits_1_through_the_cli(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["report", "--lens", "made-up-lens"])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("ffrd: ")
    assert "made-up-lens.toml" in err
    assert err.endswith("\n")


# Made-up lens text. Not game text.
RUN_LENS_TOML = """\
[[triage]]
id = "made-up-first-id"
question = "Made-up first question. Not game text."

[screen]
criteria = ["Made-up criterion. Not game text."]

[intent]
rule = "Made-up intent rule. Not game text."

[judge]
rubric = "Made-up judge rubric. Not game text."
"""
RUN_LENS = "made-up-lens"


@pytest.fixture
def made_up_lens(repo: Path) -> Path:
    path = repo / "lenses" / f"{RUN_LENS}.toml"
    path.parent.mkdir()
    path.write_text(RUN_LENS_TOML, encoding="utf-8")
    return path


def test_run_is_registered_last_with_its_help_and_no_tune_command(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert (
        "run a lens's scenes through the screen, the Jev recall check, drafts, gates and selection, then write its outcomes and dialog"
        in out
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--help"])
    assert exit_info.value.code == 0
    assert "skip the Jev recall check" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["tune", "--lens", RUN_LENS])
    assert exit_info.value.code == 2
    assert "invalid choice: 'tune'" in capsys.readouterr().err


def test_run_no_jev_through_the_cli_exits_1_before_any_work_is_written(
    repo: Path, made_up_lens: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FFR_VANILLA", str(tmp_path / "no-vanilla"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--lens", RUN_LENS, "--no-jev"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no vanilla text yet; run `ffrd extract`\n"
    assert not (repo / "work").exists()
    assert not (repo / "dialog").exists()


def test_smoke_is_registered_just_before_probe_with_its_help(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert cli.COMMANDS[-2] is smoke
    assert cli.COMMANDS[-1] is probe
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "run one scene through every stage under work/smoke/ and fail when a stage produced no result" in capsys.readouterr().out


def test_probe_is_registered_last_with_its_help(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert cli.COMMANDS[-1] is probe
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "run the UE4SS probe on the gnomon host to see which text shape FFR honours" in capsys.readouterr().out


def test_voice_is_registered_with_its_help(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert voice_cli in cli.COMMANDS
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "build the voice models' training data and evaluate them" in capsys.readouterr().out


def test_voice_lists_pairs_with_its_help(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["voice", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "pairs" in out
    assert "write four paraphrases per story line to work/voice/, or print split and call counts" in out


def test_voice_without_a_subcommand_exits_2(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["voice"])
    assert exit_info.value.code == 2
    assert "required" in capsys.readouterr().err


def test_voice_subcommands_are_pairs_only() -> None:
    assert (pairs,) == voice_cli.SUBCOMMANDS


def test_voice_pairs_check_through_the_cli_prints_counts_only(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Made-up scenes and text. Every line shares one text, so the one held-out scene's line is excluded.
    lines = [
        SceneLine(scene=f"made-up-scene-{i:02d}", order=0, namespace="ST_Made_Up", key=f"{i:02d}", speaker="SENTINEL-SPEAKER", voice=None)
        for i in range(10)
    ]
    texts = {(line.namespace, line.key): "Made-up SENTINEL-TEXT line. Not game text." for line in lines}
    root = tmp_path / "work"
    root.mkdir()
    calls = [
        {"scene": "made-up-scene-00", "slot": 0, "model": "made-up", "account": 3, "outcome": "ok"},
        {"scene": "made-up-scene-01", "slot": 0, "model": "made-up", "account": 3, "outcome": "ok"},
        {"scene": "made-up-scene-02", "slot": 1, "model": "made-up", "account": 7, "outcome": "ok"},
    ]
    (root / pairs.CALLS).write_text("".join(json.dumps(row) + "\n" for row in calls), encoding="utf-8")
    monkeypatch.setattr(scenes, "load", lambda: lines)
    monkeypatch.setattr(pairs, "english", lambda: texts)
    monkeypatch.setattr(pairs, "work_dir", lambda lens: root)
    cli.main(["voice", "pairs", "--check"])
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "scenes 10",
        "train scenes 9",
        "heldout scenes 1",
        "train lines 9",
        "heldout lines 0",
        "excluded lines 1",
        "account 3 calls 2",
        "account 7 calls 1",
    ]
    assert "SENTINEL" not in out
