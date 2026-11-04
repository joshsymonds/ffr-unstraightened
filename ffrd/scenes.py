"""scenes: write the scene table from the extractor's output, check it, and read it back with load().

`ffrd scenes` runs the scene extractor on the installed game and writes vanilla/scenes.json. A story line is a keyed
line of a line table that event data references, with non-empty English text. `ffrd scenes --check` reads the table
without the game and fails when it is missing, stale or below 95 percent attributed. The table holds keys, speaker ids
and voice cue positions, never the text. It also records, for the shot command, the event ids that play each scene
with their arguments, each line's talk group, the maps that start an event on load at a story progress, each map's
level, and the placed NPCs and triggers that start events, with their positions and conditions.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ffrd.game import Fail, build_id, entries, natural, paks_dir, repo_root, vanilla, vanilla_dir
from ffrd.probe import USMAP_NAME

TABLE_NAME = "scenes.json"
THRESHOLD = 95
UNREADABLE = "the scene table is unreadable; run `ffrd scenes`"
PROGRESS_CONDITION = re.compile(r"\{progress:0\}==(\d+)")
PROGRESS_TERM = re.compile(r"\{progress:(\d+)\}\s*(==|>=|=>)\s*(\d+)")
DESCRIPTION = (
    "A story line is a keyed line of a line table that event data references, with non-empty English text. "
    "attributed counts story lines with a speaker; with cue counts story lines matched to exactly one voice cue."
)


@dataclass(frozen=True)
class Voice:
    sheet: str
    index: int


@dataclass(frozen=True)
class SceneLine:
    scene: str
    order: int
    namespace: str
    key: str
    speaker: str | None
    voice: Voice | None


@dataclass(frozen=True)
class Placement:
    map: int
    kind: str  # "npc": talk to it; "trigger": stand in it
    x: float
    y: float
    z: float
    condition: str


Cue = tuple[str, int, list[str]]  # (sheet, index, lowercased `_`-separated segments)


def table_path() -> Path:
    return vanilla_dir() / TABLE_NAME


def _vanilla_build() -> str:
    stamp = vanilla_dir() / "BUILDID"
    if not stamp.exists():
        raise Fail("no vanilla text yet; run `ffrd extract`")
    return stamp.read_text().strip()


def _table() -> dict[str, Any]:
    path = table_path()
    if not path.exists():
        raise Fail("no scene table; run `ffrd scenes`")
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
        build = table["build"]
    except (ValueError, KeyError, TypeError) as e:
        raise Fail(UNREADABLE) from e
    local = _vanilla_build()
    if build != local:
        raise Fail(f"the scene table is for build {build}, not vanilla build {local}; run `ffrd scenes`")
    return table


def load() -> list[SceneLine]:
    table = _table()
    try:
        return [_line(row) for row in table["lines"]]
    except (KeyError, TypeError) as e:
        raise Fail(UNREADABLE) from e


def line_events(namespace: str, key: str) -> list[str]:
    """The event ids that play a scene holding the line, in table order.

    A free-talk event names the talk group it plays with an id argument; when an event names the line's group, only
    those events are returned.
    """
    table = _table()
    found: list[str] = []
    try:
        scenes = [row["scene"] for row in table["lines"] if row["namespace"] == namespace and row["key"] == key]
        for scene in scenes:
            group = table["groups"][scene].get(f"{namespace} {key}", -1)
            events = table["events"][scene]
            named = [e["event"] for e in events if group >= 0 and str(group) in (v for k, v in e["args"].items() if k.startswith("id"))]
            found.extend(named or [e["event"] for e in events])
    except (KeyError, TypeError, AttributeError) as e:
        raise Fail(UNREADABLE) from e
    return list(dict.fromkeys(found))


def startups(event: str) -> list[tuple[int, int]]:
    """The (map id, story progress) pairs whose map plays the event on load; other conditions are left out."""
    table = _table()
    try:
        found = [(row["map"], PROGRESS_CONDITION.fullmatch(row["condition"])) for row in table["startup"] if row["event"] == event]
    except (KeyError, TypeError) as e:
        raise Fail(UNREADABLE) from e
    return [(int(map_id), int(match.group(1))) for map_id, match in found if match]


def _map_of(level: str, maps: list[dict[str, Any]]) -> int | None:
    """The map whose level folder holds the placed actor's level; the deepest folder wins."""
    best: tuple[int, int] | None = None
    for row in maps:
        folder = row["level"].replace("/Game/", "FFRS/Content/", 1).rsplit("/", 1)[0] + "/"
        if level.startswith(folder) and (best is None or len(folder) > best[0]):
            best = (len(folder), int(row["map"]))
    return None if best is None else best[1]


def placements(event: str) -> list[Placement]:
    """The placed NPCs and triggers that start the event, on maps the table knows."""
    table = _table()
    found: list[Placement] = []
    try:
        for row in table["placements"]:
            kind = "npc" if "NPC" in row["class"] else "trigger" if "Trigger" in row["class"] else None
            map_id = _map_of(row["level"], table["maps"])
            if kind is None or map_id is None:
                continue
            found.extend(
                Placement(map_id, kind, float(row["x"]), float(row["y"]), float(row["z"]), e["condition"])
                for e in row["events"]
                if e["event"] == event
            )
    except (KeyError, TypeError, ValueError) as e:
        raise Fail(UNREADABLE) from e
    return found


def progress_settings(condition: str) -> list[tuple[int, int]]:
    """(counter, value) pairs that meet the condition's `==` and `>=` story-progress terms; other terms are left out."""
    return [(int(n), int(v)) for n, _op, v in PROGRESS_TERM.findall(condition)]


def _line(row: dict[str, Any]) -> SceneLine:
    voice = row["voice"]
    return SceneLine(
        scene=row["scene"],
        order=row["order"],
        namespace=row["namespace"],
        key=row["key"],
        speaker=row["speaker"],
        voice=None if voice is None else Voice(sheet=voice["sheet"], index=voice["index"]),
    )


def _extract(usmap: Path, oodle: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.json"
        cmd = [
            "dotnet",
            "run",
            "--project",
            str(repo_root() / "tools/scenes"),
            "-c",
            "Release",
            "--",
            str(paks_dir()),
            str(usmap),
            oodle,
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, cwd=tmp, capture_output=True)
        except FileNotFoundError as e:
            raise Fail("dotnet is not on PATH; install the .NET SDK") from e
        except subprocess.CalledProcessError as e:
            raise Fail(f"the scene extractor exited {e.returncode}") from e
        if not out.exists():
            raise Fail("the scene extractor wrote no table")
        return json.loads(out.read_text(encoding="utf-8"))


def _cues(sheets: list[dict[str, Any]], names: set[str]) -> list[Cue]:
    return [
        (sheet["name"], index, cue.lower().split("_"))
        for sheet in sheets
        if sheet["name"] in names
        for index, cue in enumerate(sheet["cues"])
    ]


def _has_run(cue: list[str], segments: list[str]) -> bool:
    n = len(segments)
    return any(cue[i : i + n] == segments for i in range(len(cue) - n + 1))


def _voice(key: str, cues: list[Cue]) -> Voice | None:
    segments = key.lower().split("_")
    hits = [Voice(sheet=sheet, index=index) for sheet, index, cue in cues if _has_run(cue, segments)]
    return hits[0] if len(hits) == 1 else None


def _scene_lines(raw: dict[str, Any], english: set[tuple[str, str]]) -> list[SceneLine]:
    references: list[dict[str, Any]] = raw["references"]
    lines: list[SceneLine] = []
    for table in raw["tables"]:
        scene = table["id"]
        refs = [ref for ref in references if ref["table"] == scene]
        if not refs:
            continue
        cues = _cues(raw["sheets"], {ref["sheet"] for ref in refs if ref["sheet"] is not None})
        kept = [line for line in table["lines"] if (line["namespace"], line["key"]) in english]
        lines.extend(
            SceneLine(
                scene=scene,
                order=order,
                namespace=line["namespace"],
                key=line["key"],
                speaker=line["speaker"],
                voice=_voice(line["key"], cues),
            )
            for order, line in enumerate(kept)
        )
    lines.sort(key=lambda line: (natural(line.scene), line.order))
    return lines


def _write() -> list[SceneLine]:
    usmap = vanilla_dir() / USMAP_NAME
    if not usmap.exists():
        raise Fail("no Mappings.usmap in vanilla; run `ffrd usmap`")
    build = _vanilla_build()
    remote = build_id()
    if remote != build:
        raise Fail(f"the game runs build {remote}, not vanilla build {build}; run `ffrd extract` first")
    oodle = os.environ.get("FFR_OODLE")
    if not oodle:
        raise Fail("FFR_OODLE is not set; point it at the Oodle library")
    english = {(ns, key) for ns, key, text in entries(vanilla("en")) if text}
    raw = _extract(usmap, oodle)
    lines = _scene_lines(raw, english)
    scenes = list(dict.fromkeys(line.scene for line in lines))
    events: dict[str, list[dict[str, Any]]] = {}
    for scene in scenes:
        seen: dict[str, dict[str, Any]] = {}
        for ref in raw["references"]:
            if ref["table"] == scene:
                seen.setdefault(ref["event"], {"event": ref["event"], "args": ref["args"]})
        events[scene] = list(seen.values())
    groups = {
        t["id"]: {f"{line['namespace']} {line['key']}": line["group"] for line in t["lines"]} for t in raw["tables"] if t["id"] in events
    }
    table = {
        "build": build,
        "lines": [asdict(line) for line in lines],
        "events": events,
        "groups": groups,
        "startup": raw["startup"],
        "maps": raw["maps"],
        "placements": raw["placements"],
    }
    table_path().write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return lines


def _attributed(lines: list[SceneLine]) -> int:
    return sum(1 for line in lines if line.speaker)


def _percent(lines: list[SceneLine]) -> float:
    return 100 * _attributed(lines) / len(lines) if lines else 0.0


def _report(lines: list[SceneLine]) -> list[str]:
    total = len(lines)
    attributed = _attributed(lines)
    return [
        f"story lines {total}",
        f"scenes {len({line.scene for line in lines})}",
        f"attributed {attributed}",
        f"unattributed {total - attributed}",
        f"with cue {sum(1 for line in lines if line.voice is not None)}",
        f"attributed {_percent(lines):.1f}%",
    ]


def cmd_scenes(args: argparse.Namespace) -> None:
    if not args.check:
        lines = _write()
        print("\n".join(_report(lines)))
        return
    lines = load()
    print("\n".join(_report(lines)))
    if not lines or _attributed(lines) * 100 < THRESHOLD * len(lines):
        raise Fail(f"attributed {_percent(lines):.1f}% is below {THRESHOLD}%")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    scenes = subparsers.add_parser("scenes", help="write the scene table to vanilla/scenes.json, or check it", description=DESCRIPTION)
    scenes.add_argument(
        "--check",
        action="store_true",
        help="read the table and print its counts; exit 1 if it is missing, stale or below 95 percent attributed",
    )
    scenes.set_defaults(fn=cmd_scenes)
