"""Runs the UE4SS probe mod under plain Lua 5.4 with the game globals stubbed.

Made-up namespaces, keys and text. Not game text.
"""

import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from ffrd.game import vanilla_hash

WORKSPACE = Path(__file__).resolve().parents[1]
MOD = WORKSPACE / "ue4ss/UnstraightenedProbe/Scripts/main.lua"

KEY_A = ("ST_PROBE_001", "1")
KEY_B = ("ST_PROBE_001", "2")
KEY_C = ("ST_PROBE_002", "9")

ASCII_TEXT = "Synthetic probe line one."
CHANGED_TEXT = "Synthetic probe line one, edited."
MULTIBYTE_TEXT = "合成テキスト一"
LINE_RE = re.compile(r"[A-Za-z0-9_]+,[A-Za-z0-9_]+ (?:[0-9a-f]{16}|missing)|done")

STUB_HEAD = """
local TABLE, SCHEDULE, tick = {}, {}, 0
local function set(ns, key, text)
  TABLE[ns] = TABLE[ns] or {}
  TABLE[ns][key] = text
end
local function at(n, ns, key, text)
  SCHEDULE[n] = SCHEDULE[n] or {}
  table.insert(SCHEDULE[n], {ns, key, text})
end
local KTL = {
  IsValid = function() return true end,
  FindTextInLocalizationTable = function(_, ns, key, out)
    local row = TABLE[ns]
    local text = row and row[key]
    if text == nil then return false end
    out.OutText = { ToString = function() return text end }
    return true
  end,
}
function StaticFindObject(path)
  assert(path == "/Script/Engine.Default__KismetTextLibrary")
  return KTL
end
function ExecuteInGameThread(f) f() end
function LoopAsync(_, f)
  repeat
    tick = tick + 1
    assert(tick <= 1000, "probe loop did not stop")
    for _, op in ipairs(SCHEDULE[tick] or {}) do set(op[1], op[2], op[3]) end
  until f()
end
"""


def lua_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def line(key: tuple[str, str], digest: str) -> str:
    return f"{key[0]},{key[1]} {digest}"


def multibyte_text(n: int) -> str:
    return "合" * (n // 3) + "x" * (n % 3)


def run_probe(
    root: Path,
    keys: Sequence[tuple[str, str]],
    initial: dict[tuple[str, str], str] | None = None,
    changes: Sequence[tuple[int, tuple[str, str], str]] = (),
    stale: str | None = None,
) -> tuple[str, str]:
    mod_dir = root / "UnstraightenedProbe"
    scripts = mod_dir / "Scripts"
    scripts.mkdir(parents=True)
    copied = scripts / "main.lua"
    shutil.copyfile(MOD, copied)
    (mod_dir / "keys.txt").write_text("".join(f"{ns},{key}\n" for ns, key in keys), encoding="utf-8")
    if stale is not None:
        (mod_dir / "probe.log").write_text(stale, encoding="utf-8")

    parts = [STUB_HEAD]
    for (ns, key), text in (initial or {}).items():
        parts.append(f"set({lua_string(ns)}, {lua_string(key)}, {lua_string(text)})\n")
    for n, (ns, key), text in changes:
        parts.append(f"at({n}, {lua_string(ns)}, {lua_string(key)}, {lua_string(text)})\n")
    parts.append(f"dofile({lua_string(str(copied))})\n")
    parts.append('print("ticks " .. tick)\n')
    stub = root / "stub.lua"
    stub.write_text("".join(parts), encoding="utf-8")

    result = subprocess.run(
        ["lua", str(stub)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    log = (mod_dir / "probe.log").read_text(encoding="utf-8")
    return log, result.stdout


def test_first_observation_then_only_changes(tmp_path: Path) -> None:
    log, _ = run_probe(
        tmp_path,
        keys=[KEY_A, KEY_B, KEY_C],
        initial={KEY_A: ASCII_TEXT},
        changes=[
            (4, KEY_A, CHANGED_TEXT),
            (9, KEY_A, CHANGED_TEXT),
            (12, KEY_B, MULTIBYTE_TEXT),
        ],
    )
    assert log.splitlines() == [
        line(KEY_A, vanilla_hash(ASCII_TEXT)),
        line(KEY_B, "missing"),
        line(KEY_C, "missing"),
        line(KEY_A, vanilla_hash(CHANGED_TEXT)),
        line(KEY_B, vanilla_hash(MULTIBYTE_TEXT)),
        "done",
    ]


def test_done_is_last_line_after_thirty_ticks(tmp_path: Path) -> None:
    log, stdout = run_probe(tmp_path, keys=[KEY_A], initial={KEY_A: ASCII_TEXT})
    lines = log.splitlines()
    assert lines[-1] == "done"
    assert lines.count("done") == 1
    assert stdout == "ticks 30\n"


@pytest.mark.parametrize("n", [55, 56, 63, 64, 119])
@pytest.mark.parametrize("multibyte", [False, True], ids=["ascii", "utf8"])
def test_hash_matches_vanilla_hash(tmp_path: Path, n: int, multibyte: bool) -> None:
    text = multibyte_text(n) if multibyte else "a" * n
    assert len(text.encode("utf-8")) == n
    log, _ = run_probe(tmp_path, keys=[KEY_A], initial={KEY_A: text})
    assert log.splitlines() == [line(KEY_A, vanilla_hash(text)), "done"]


def test_no_synthetic_text_in_log(tmp_path: Path) -> None:
    log, _ = run_probe(
        tmp_path,
        keys=[KEY_A, KEY_B, KEY_C],
        initial={KEY_A: ASCII_TEXT, KEY_B: MULTIBYTE_TEXT},
        changes=[(3, KEY_A, CHANGED_TEXT)],
    )
    for text in (ASCII_TEXT, CHANGED_TEXT, MULTIBYTE_TEXT, "Synthetic"):
        assert text not in log
    for entry in log.splitlines():
        assert LINE_RE.fullmatch(entry), entry


def test_log_is_truncated_at_start(tmp_path: Path) -> None:
    log, _ = run_probe(
        tmp_path,
        keys=[KEY_A],
        initial={KEY_A: ASCII_TEXT},
        stale="stale,line 0000000000000000\n",
    )
    assert "stale" not in log
    assert log.splitlines() == [line(KEY_A, vanilla_hash(ASCII_TEXT)), "done"]
