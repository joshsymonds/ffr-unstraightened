"""scenes: the scene table written by `ffrd scenes`, read by load(), and checked by `ffrd scenes --check`.

Made-up scene, speaker, sheet, cue and text names. Not game text. The extractor is a stub.
"""

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pylocres import Entry, LocresFile, Namespace

from ffrd import cli, scenes
from ffrd.game import LOCRES, Fail, repo_root
from ffrd.probe import USMAP_NAME
from ffrd.scenes import SceneLine, Voice

# Made-up (namespace, key, English) lines. Not game text.
ENGLISH: tuple[tuple[str, str, str], ...] = (
    ("ST_Syn_001_01", "10", "Made-up line ten. Not game text."),
    ("ST_Syn_001_01", "2", "Made-up line two. Not game text."),
    ("ST_Syn_001_01", "Mid_Run", "Made-up line with a run of segments. Not game text."),
    ("ST_Syn_001_02", "1", ""),
    ("ST_Syn_002_01", "1", "Made-up line in the second scene. Not game text."),
    ("ST_Syn_002_01", "2", "Made-up line two in the second scene. Not game text."),
)

# Made-up extractor output. Not game text.
# SynScene10 keeps 10, 2 and Mid_Run; its empty and unknown lines are dropped. SynScene2 keeps both lines.
# SynUnreferenced has no reference, so it is not a scene. SynSheetC is not referenced by any table.
RAW: dict[str, Any] = {
    "tables": [
        {
            "id": "SynScene10",
            "lines": [
                {"namespace": "ST_Syn_001_01", "key": "10", "speaker": "SynSpeakerA", "group": 0},
                {"namespace": "ST_Syn_001_02", "key": "1", "speaker": "SynSpeakerB", "group": 0},
                {"namespace": "ST_Syn_001_01", "key": "2", "speaker": None, "group": 0},
                {"namespace": "ST_Syn_999_01", "key": "1", "speaker": "SynSpeakerA", "group": 1},
                {"namespace": "ST_Syn_001_01", "key": "Mid_Run", "speaker": "SynSpeakerA", "group": 1},
            ],
        },
        {
            "id": "SynScene2",
            "lines": [
                {"namespace": "ST_Syn_002_01", "key": "1", "speaker": "SynSpeakerB", "group": -1},
                {"namespace": "ST_Syn_002_01", "key": "2", "speaker": "SynSpeakerA", "group": -1},
            ],
        },
        {
            "id": "SynUnreferenced",
            "lines": [{"namespace": "ST_Syn_002_01", "key": "1", "speaker": "SynSpeakerA", "group": -1}],
        },
    ],
    "references": [
        {"table": "SynScene10", "sheet": "SynSheetA", "event": "SynEvent10_A", "args": {"id": "0"}},
        {"table": "SynScene10", "sheet": None, "event": "SynEvent10_B", "args": {"id": "1"}},
        {"table": "SynScene2", "sheet": "SynSheetB", "event": "SynEvent2", "args": {}},
        {"table": "SynScene10", "sheet": None, "event": "SynEvent10_A", "args": {"id": "0"}},
    ],
    "maps": [
        {"map": 900, "level": "/Game/Map/Town/Syn/Syn_01/Town_Syn_01_PL.Town_Syn_01_PL"},
        {"map": 901, "level": "/Game/Map/Town/Syn/Town_Syn_PL.Town_Syn_PL"},
    ],
    "placements": [
        {
            "level": "FFRS/Content/Map/Town/Syn/Syn_01/EV/Town_Syn_01_EV",
            "class": "BP_MapUnit_NPC_C",
            "unit": 70,
            "x": 10.0,
            "y": -20.0,
            "z": 30.0,
            "events": [{"event": "SynEvent10_B", "condition": "{progress:28}==30"}],
        },
        {
            "level": "FFRS/Content/Map/Town/Syn/EV/Town_Syn_EV",
            "class": "CPP_MapGimmick_EventTrigger",
            "unit": None,
            "x": 1.0,
            "y": 2.0,
            "z": 3.0,
            "events": [{"event": "SynEvent2", "condition": ""}],
        },
        {
            "level": "FFRS/Content/Map/Dng/Elsewhere/EV/Dng_Elsewhere_EV",
            "class": "BP_MapUnit_NPC_C",
            "unit": 71,
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "events": [{"event": "SynEvent2", "condition": ""}],
        },
    ],
    "startup": [
        {"event": "SynEvent10_B", "map": 900, "condition": "{progress:0}==1500"},
        {"event": "SynEvent10_B", "map": 901, "condition": "{progress:0}>=1500"},
        {"event": "SynEvent2", "map": 902, "condition": "{progress:0}==2500"},
    ],
    "sheets": [
        {
            "name": "SynSheetA",
            "cues": ["Vo_Syn_10_Take1", "Vo_Syn_2_Take1", "Vo_Syn_MID_run_Take1", "Vo_Syn_Mid_Other_Run", "Vo_Syn_2_Take2"],
        },
        {"name": "SynSheetB", "cues": ["Vo_Syn_1_Take1"]},
        {"name": "SynSheetC", "cues": ["Vo_Syn_10_Take2"]},
    ],
}

# What the RAW output must become: "10" has one cue, "2" has two (ambiguous, so null), "Mid_Run" matches one
# cue case-insensitively, and SynScene2's "2" has no cue. Scenes sort by natural order, so SynScene2 comes first.
EXPECTED_LINES = [
    SceneLine("SynScene2", 0, "ST_Syn_002_01", "1", "SynSpeakerB", Voice("SynSheetB", 0)),
    SceneLine("SynScene2", 1, "ST_Syn_002_01", "2", "SynSpeakerA", None),
    SceneLine("SynScene10", 0, "ST_Syn_001_01", "10", "SynSpeakerA", Voice("SynSheetA", 0)),
    SceneLine("SynScene10", 1, "ST_Syn_001_01", "2", None, None),
    SceneLine("SynScene10", 2, "ST_Syn_001_01", "Mid_Run", "SynSpeakerA", Voice("SynSheetA", 2)),
]

EXPECTED_TABLE: dict[str, Any] = {
    "build": "424242",
    "lines": [
        {
            "scene": "SynScene2",
            "order": 0,
            "namespace": "ST_Syn_002_01",
            "key": "1",
            "speaker": "SynSpeakerB",
            "voice": {"sheet": "SynSheetB", "index": 0},
        },
        {"scene": "SynScene2", "order": 1, "namespace": "ST_Syn_002_01", "key": "2", "speaker": "SynSpeakerA", "voice": None},
        {
            "scene": "SynScene10",
            "order": 0,
            "namespace": "ST_Syn_001_01",
            "key": "10",
            "speaker": "SynSpeakerA",
            "voice": {"sheet": "SynSheetA", "index": 0},
        },
        {"scene": "SynScene10", "order": 1, "namespace": "ST_Syn_001_01", "key": "2", "speaker": None, "voice": None},
        {
            "scene": "SynScene10",
            "order": 2,
            "namespace": "ST_Syn_001_01",
            "key": "Mid_Run",
            "speaker": "SynSpeakerA",
            "voice": {"sheet": "SynSheetA", "index": 2},
        },
    ],
    "events": {
        "SynScene2": [{"event": "SynEvent2", "args": {}}],
        "SynScene10": [{"event": "SynEvent10_A", "args": {"id": "0"}}, {"event": "SynEvent10_B", "args": {"id": "1"}}],
    },
    "groups": {
        "SynScene10": {
            "ST_Syn_001_01 10": 0,
            "ST_Syn_001_02 1": 0,
            "ST_Syn_001_01 2": 0,
            "ST_Syn_999_01 1": 1,
            "ST_Syn_001_01 Mid_Run": 1,
        },
        "SynScene2": {"ST_Syn_002_01 1": -1, "ST_Syn_002_01 2": -1},
    },
    "startup": [
        {"event": "SynEvent10_B", "map": 900, "condition": "{progress:0}==1500"},
        {"event": "SynEvent10_B", "map": 901, "condition": "{progress:0}>=1500"},
        {"event": "SynEvent2", "map": 902, "condition": "{progress:0}==2500"},
    ],
    "maps": RAW["maps"],
    "placements": RAW["placements"],
}

COUNTS = "story lines 5\nscenes 2\nattributed 4\nunattributed 1\nwith cue 3\nattributed 80.0%\n"


class FakeExtractor:
    """Stands in for subprocess.run: writes RAW to the out.json argument, or raises the configured error."""

    def __init__(self) -> None:
        self.raw: dict[str, Any] = copy.deepcopy(RAW)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.error: BaseException | None = None
        self.writes = True

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((cmd, kwargs))
        if self.error is not None:
            raise self.error
        if self.writes:
            Path(cmd[-1]).write_text(json.dumps(self.raw), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)


@pytest.fixture
def scene_vanilla(fake_vanilla: Path, fake_game: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    namespaces: dict[str, Namespace] = {}
    for ns_name, key, text in ENGLISH:
        namespaces.setdefault(ns_name, Namespace(ns_name)).add(Entry(key, text, text, is_hash=False))
    locres = LocresFile()
    for ns in namespaces.values():
        locres.add(ns)
    locres.write(str(fake_vanilla / "pak" / LOCRES / "en" / "Game.locres"))
    (fake_vanilla / USMAP_NAME).write_bytes(b"synthetic usmap")
    monkeypatch.setenv("FFR_OODLE", "/synthetic/oodle.so")
    return fake_vanilla


@pytest.fixture
def extractor(scene_vanilla: Path, monkeypatch: pytest.MonkeyPatch) -> FakeExtractor:
    fake = FakeExtractor()
    monkeypatch.setattr(scenes.subprocess, "run", fake)
    return fake


def write_table(lines: list[dict[str, Any]], build: str = "424242") -> None:
    scenes.table_path().write_text(json.dumps({"build": build, "lines": lines}), encoding="utf-8")


def row(order: int, speaker: str | None, scene: str = "SynScene2") -> dict[str, Any]:
    return {"scene": scene, "order": order, "namespace": "ST_Syn_002_01", "key": "1", "speaker": speaker, "voice": None}


def test_writes_the_table_by_the_rules(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["scenes"])
    assert json.loads(scenes.table_path().read_text(encoding="utf-8")) == EXPECTED_TABLE
    assert scenes.load() == EXPECTED_LINES


def test_write_prints_only_the_counts(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["scenes"])
    assert capsys.readouterr().out == COUNTS


def test_extractor_runs_once_with_the_frozen_arguments_in_a_temporary_directory(
    scene_vanilla: Path, fake_game: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    assert len(extractor.calls) == 1
    cmd, kwargs = extractor.calls[0]
    out = cmd[-1]
    assert cmd == [
        "dotnet",
        "run",
        "--project",
        str(repo_root() / "tools/scenes"),
        "-c",
        "Release",
        "--",
        str(fake_game / "FFRS/Content/Paks"),
        str(scene_vanilla / USMAP_NAME),
        "/synthetic/oodle.so",
        out,
    ]
    assert kwargs["check"] is True
    assert kwargs["cwd"] != str(repo_root())
    assert Path(out).parent == Path(kwargs["cwd"])


def test_table_without_a_reference_is_not_a_scene(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    assert "SynUnreferenced" not in {line.scene for line in scenes.load()}


def test_line_is_kept_only_with_nonempty_english(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["scenes"])
    kept = {(line.namespace, line.key) for line in scenes.load()}
    assert ("ST_Syn_001_02", "1") not in kept
    assert ("ST_Syn_999_01", "1") not in kept
    assert kept == {
        ("ST_Syn_002_01", "1"),
        ("ST_Syn_002_01", "2"),
        ("ST_Syn_001_01", "10"),
        ("ST_Syn_001_01", "2"),
        ("ST_Syn_001_01", "Mid_Run"),
    }


def test_order_counts_kept_lines_from_zero_within_each_scene(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    orders = {scene: [line.order for line in scenes.load() if line.scene == scene] for scene in ("SynScene2", "SynScene10")}
    assert orders == {"SynScene2": [0, 1], "SynScene10": [0, 1, 2]}


def test_scenes_sort_by_natural_scene_order_then_order(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    assert [(line.scene, line.order) for line in scenes.load()] == [
        ("SynScene2", 0),
        ("SynScene2", 1),
        ("SynScene10", 0),
        ("SynScene10", 1),
        ("SynScene10", 2),
    ]


def test_speaker_is_copied_as_given_and_null_stays_null(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    speakers = {(line.scene, line.key): line.speaker for line in scenes.load()}
    assert speakers[("SynScene10", "2")] is None
    assert speakers[("SynScene10", "10")] == "SynSpeakerA"


def test_voice_needs_exactly_one_matching_cue(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["scenes"])
    voices = {(line.scene, line.key): line.voice for line in scenes.load()}
    # Key "2" matches two cues in SynSheetA (indexes 1 and 4), so its voice is null.
    assert voices[("SynScene10", "2")] is None
    assert voices[("SynScene10", "10")] == Voice("SynSheetA", 0)


def test_voice_is_a_contiguous_run_of_key_segments_compared_case_insensitively(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    extractor.raw["sheets"] = [{"name": "SynSheetA", "cues": ["Vo_Syn_Mid_Other_Run"]}]
    cli.main(["scenes"])
    assert {(line.scene, line.key): line.voice for line in scenes.load()}[("SynScene10", "Mid_Run")] is None
    extractor.raw["sheets"] = [{"name": "SynSheetA", "cues": ["vo_syn_MID_run_take1"]}]
    cli.main(["scenes"])
    assert {(line.scene, line.key): line.voice for line in scenes.load()}[("SynScene10", "Mid_Run")] == Voice("SynSheetA", 0)


def test_voice_searches_only_sheets_the_table_references(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    # SynSheetC holds a cue for "10" but no table references it, so "10" has no voice.
    extractor.raw["sheets"] = [{"name": "SynSheetA", "cues": ["Vo_Syn_2_Take1"]}, {"name": "SynSheetC", "cues": ["Vo_Syn_10_Take2"]}]
    cli.main(["scenes"])
    assert {(line.scene, line.key): line.voice for line in scenes.load()}[("SynScene10", "10")] is None


def test_check_prints_the_counts_and_exits_1_below_95_percent(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes", "--check"])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == COUNTS
    assert captured.err == "ffrd: attributed 80.0% is below 95%\n"


def test_check_passes_at_exactly_95_percent(scene_vanilla: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_table([row(order, None if order == 0 else "SynSpeakerA") for order in range(20)])
    cli.main(["scenes", "--check"])
    assert capsys.readouterr().out == ("story lines 20\nscenes 1\nattributed 19\nunattributed 1\nwith cue 0\nattributed 95.0%\n")


def test_check_fails_on_an_empty_table(scene_vanilla: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_table([])
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes", "--check"])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "story lines 0\nscenes 0\nattributed 0\nunattributed 0\nwith cue 0\nattributed 0.0%\n"
    assert captured.err == "ffrd: attributed 0.0% is below 95%\n"


def test_check_without_a_table_exits_1(scene_vanilla: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes", "--check"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no scene table; run `ffrd scenes`\n"


def test_check_with_a_stale_table_exits_1(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["scenes"])
    (scene_vanilla / "BUILDID").write_text("999999\n")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes", "--check"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: the scene table is for build 424242, not vanilla build 999999; run `ffrd scenes`\n"


def test_load_returns_the_table_lines(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["scenes"])
    assert scenes.load() == EXPECTED_LINES


def test_load_without_a_table_raises(scene_vanilla: Path) -> None:
    with pytest.raises(Fail, match="no scene table; run `ffrd scenes`"):
        scenes.load()


def test_load_with_a_stale_build_raises(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["scenes"])
    (scene_vanilla / "BUILDID").write_text("999999\n")
    with pytest.raises(Fail, match="the scene table is for build 424242, not vanilla build 999999"):
        scenes.load()


def unreadable_tables() -> dict[str, str]:
    good = row(0, "SynSpeakerA")
    voiced = {**good, "voice": {"sheet": "SynSheetA", "index": 0}}
    tables = {
        "invalid-json": "{",
        "missing-build": json.dumps({"lines": [good]}),
        "missing-lines": json.dumps({"build": "424242"}),
        "table-not-object": "[]",
        "table-null": "null",
        "lines-null": json.dumps({"build": "424242", "lines": None}),
        "row-not-object": json.dumps({"build": "424242", "lines": [1]}),
    }
    for field in ("scene", "order", "namespace", "key", "speaker", "voice"):
        tables[f"row-missing-{field}"] = json.dumps({"build": "424242", "lines": [{k: v for k, v in good.items() if k != field}]})
    for field in ("sheet", "index"):
        voice = {k: v for k, v in voiced["voice"].items() if k != field}
        tables[f"voice-missing-{field}"] = json.dumps({"build": "424242", "lines": [{**voiced, "voice": voice}]})
    return tables


UNREADABLE_TABLES = unreadable_tables()


def test_check_with_an_unreadable_table_exits_1(scene_vanilla: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenes.table_path().write_text("{", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes", "--check"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: the scene table is unreadable; run `ffrd scenes`\n"


@pytest.mark.parametrize("text", list(UNREADABLE_TABLES.values()), ids=list(UNREADABLE_TABLES))
def test_load_with_an_unreadable_table_raises(scene_vanilla: Path, text: str) -> None:
    scenes.table_path().write_text(text, encoding="utf-8")
    with pytest.raises(Fail) as exit_info:
        scenes.load()
    assert str(exit_info.value) == "the scene table is unreadable; run `ffrd scenes`"


def test_missing_usmap_fails_and_names_the_fix(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    (scene_vanilla / USMAP_NAME).unlink()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no Mappings.usmap in vanilla; run `ffrd usmap`\n"
    assert extractor.calls == []


def test_missing_buildid_fails_and_names_the_fix(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    (scene_vanilla / "BUILDID").unlink()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: no vanilla text yet; run `ffrd extract`\n"
    assert extractor.calls == []


def test_game_build_differing_from_buildid_fails(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    (scene_vanilla / "BUILDID").write_text("999999\n")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: the game runs build 424242, not vanilla build 999999; run `ffrd extract` first\n"
    assert extractor.calls == []


def test_unset_oodle_fails_before_the_extractor_runs(
    scene_vanilla: Path, extractor: FakeExtractor, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FFR_OODLE")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: FFR_OODLE is not set; point it at the Oodle library\n"
    assert extractor.calls == []


def test_missing_dotnet_fails(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    extractor.error = FileNotFoundError("dotnet")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: dotnet is not on PATH; install the .NET SDK\n"
    assert not scenes.table_path().exists()


def test_extractor_exit_status_fails_and_writes_no_table(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    extractor.error = subprocess.CalledProcessError(3, ["dotnet"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: the scene extractor exited 3\n"
    assert not scenes.table_path().exists()


def test_extractor_that_writes_no_output_fails(scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]) -> None:
    extractor.writes = False
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes"])
    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "ffrd: the scene extractor wrote no table\n"
    assert not scenes.table_path().exists()


def test_scenes_help_defines_story_lines(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["scenes", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "A story line is a keyed line of a line table that event data references, with non-empty English text." in out
    assert "--check" in out


def test_scenes_is_registered_with_its_help(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert scenes in cli.COMMANDS
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "write the scene table to vanilla/scenes.json, or check it" in capsys.readouterr().out


def test_line_events_keep_the_events_naming_the_lines_talk_group_else_all_in_table_order(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    assert scenes.line_events("ST_Syn_001_01", "Mid_Run") == ["SynEvent10_B"]
    assert scenes.line_events("ST_Syn_001_01", "10") == ["SynEvent10_A"]
    assert scenes.line_events("ST_Syn_002_01", "2") == ["SynEvent2"]
    assert scenes.line_events("ST_Syn_999_01", "1") == []


def test_startups_keep_only_exact_progress_conditions(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    assert scenes.startups("SynEvent10_B") == [(900, 1500)]
    assert scenes.startups("SynEvent2") == [(902, 2500)]
    assert scenes.startups("SynEvent10_A") == []


def test_line_events_with_a_table_missing_events_raises(scene_vanilla: Path) -> None:
    write_table([row(0, "SynSpeakerA")])
    with pytest.raises(Fail, match="unreadable"):
        scenes.line_events("ST_Syn_002_01", "1")


def test_placements_resolve_the_deepest_map_folder_and_skip_unknown_maps(
    scene_vanilla: Path, extractor: FakeExtractor, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["scenes"])
    assert scenes.placements("SynEvent10_B") == [scenes.Placement(900, "npc", 10.0, -20.0, 30.0, "{progress:28}==30")]
    assert scenes.placements("SynEvent2") == [scenes.Placement(901, "trigger", 1.0, 2.0, 3.0, "")]
    assert scenes.placements("SynEvent10_A") == []


@pytest.mark.parametrize(
    ("condition", "settings"),
    [
        ("{progress:28}==30", [(28, 30)]),
        ("{progress:16}==0&{progress:0}==2120", [(16, 0), (0, 2120)]),
        ("{progress:0}>=2000 & {progress:0}<4000", [(0, 2000)]),
        ("!{flag:1406}", []),
        ("", []),
    ],
)
def test_progress_settings_meet_equal_and_at_least_terms_only(condition: str, settings: list[tuple[int, int]]) -> None:
    assert scenes.progress_settings(condition) == settings
