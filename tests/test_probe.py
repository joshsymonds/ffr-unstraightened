"""Runs the UE4SS probe mod under plain Lua 5.4 with the game globals stubbed.

Made-up namespaces, keys and text. Not game text.
"""

import contextlib
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest
from pylocres import LocresFile

from ffrd import cli, deck, gnomon, probe
from ffrd.build import USER_GAME_TEXT, edits_text
from ffrd.game import APP_ID, LOCRES, MOD_NAME, Fail, vanilla_hash

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


# Gnomon helpers. The remote machine is a fake: a file map plus one scripted FFR launch at a time.
# Times are fake seconds on a clock the tests advance; nothing sleeps and no host is reached.

GAME = "~/synthetic/Game"
DEST = f"{GAME}/FFRS/Binaries/Win64/ue4ss/Mods/UnstraightenedProbe"
LOG = "probe.log"
FILES = {"keys.txt": b"ST_SYNTH_001,1\n"}
SYNTH_LINE = "ST_SYNTH_001,1 0123456789abcdef"
LAUNCH_CMD = f"timeout 30 steam -applaunch {APP_ID} -nosound -RenderOffscreen"
VISIBLE_LAUNCH_CMD = f"timeout 30 steam -applaunch {APP_ID} -nosound"
GAME_PGREP = "pgrep -f 'FINAL FANTASY RESONANC[E]'"
EXE_PGREP = "pgrep -f 'FFRS-Win64-Shippin[g]'"
EXE_PKILL = "pkill -KILL -f 'FFRS-Win64-Shippin[g]'"
EXE_DELAY = 6
REAP_DELAY = 2
LAUNCH_CHAIN = 40


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class Launch:
    exe: bool = True  # whether the exe appears EXE_DELAY seconds after the launch
    lines: tuple[tuple[float, str], ...] = ()  # (seconds after the exe appears, log line as written)
    exit_after: float | None = None  # the exe exits on its own this long after it appears


HEALTHY = Launch(lines=((15, SYNTH_LINE), (20, "done\r")))
STALL = Launch()


class FakeRemote:
    def __init__(self, clock: Clock, launches: list[Launch], external: bool) -> None:
        self.clock = clock
        self.pending = launches
        self.external = external  # a game the probe did not start
        self.fs: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.snapshots: list[dict[str, bytes]] = []
        self.start = 0.0
        self.current: Launch | None = None
        self.exe_at: float | None = None
        self.exit_at: float | None = None
        self.killed_at: float | None = None
        self.log_lines: list[tuple[float, str]] = []

    @property
    def launch_count(self) -> int:
        return len(self.snapshots)

    def game_running(self) -> bool:
        if self.external:
            return True
        if self.current is None:
            return False
        ends: list[float] = []
        if self.killed_at is not None:
            ends.append(self.killed_at + REAP_DELAY)
        if self.exit_at is not None:
            ends.append(self.exit_at + REAP_DELAY)
        if self.exe_at is None:
            ends.append(self.start + LAUNCH_CHAIN)
        return self.clock.now < min(ends, default=float("inf"))

    def _exe_alive(self) -> bool:
        now = self.clock.now
        if self.exe_at is None or now < self.exe_at or self.killed_at is not None:
            return False
        return self.exit_at is None or now < self.exit_at

    def _launch(self) -> None:
        assert not self.game_running(), "launched while the game still ran"
        assert self.pending, "more launches than the test scripted"
        self.current = self.pending.pop(0)
        self.start = self.clock.now
        self.killed_at = None
        if self.current.exe:
            self.exe_at = self.start + EXE_DELAY
            if self.current.exit_after is not None:
                self.exit_at = self.exe_at + self.current.exit_after
            self.log_lines = [(self.exe_at + at, text) for at, text in self.current.lines]
        else:
            self.exe_at = None
            self.exit_at = None
            self.log_lines = []
        self.snapshots.append(dict(self.fs))

    def _read(self, path: str) -> bytes:
        body = self.fs.get(path, b"")
        if path.endswith("/" + LOG):
            now = self.clock.now
            body += "".join(f"{text}\n" for at, text in self.log_lines if at <= now).encode("utf-8")
        return body

    def ssh(self, host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
        self.commands.append(command)
        if command in (LAUNCH_CMD, VISIBLE_LAUNCH_CMD):
            self._launch()
            return b""
        if command.startswith("mkdir -p "):
            assert not isinstance(stdin, int)
            target = shlex.split(command.split(" && cat > ", 1)[1])[0]
            self.fs[target] = stdin.read()
            return b""
        if command.startswith("rm -rf "):
            target = shlex.split(command)[2]
            for key in [k for k in self.fs if k == target or k.startswith(target + "/")]:
                del self.fs[key]
            return b""
        if command.startswith("rm -f "):
            self.fs.pop(shlex.split(command)[2], None)
            return b""
        if command.startswith("cat ") and command.endswith(" 2>/dev/null || true"):
            path = shlex.split(command[len("cat ") : -len(" 2>/dev/null || true")])[0]
            return self._read(path)
        raise AssertionError(f"unexpected ssh command: {command}")

    def ssh_status(self, host: str, command: str) -> int:
        self.commands.append(command)
        if command == GAME_PGREP:
            return 0 if self.game_running() else 1
        if command == EXE_PGREP:
            return 0 if self._exe_alive() else 1
        if command == EXE_PKILL:
            if self._exe_alive():
                self.killed_at = self.clock.now
                return 0
            return 1
        raise AssertionError(f"unexpected status command: {command}")


@pytest.fixture(autouse=True)
def probe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FFR_PROBE_HOST", raising=False)
    monkeypatch.setenv("FFR_PROBE_GAME", GAME)


@pytest.fixture
def make_remote(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeRemote]:
    def make(launches: Sequence[Launch] = (), external: bool = False) -> FakeRemote:
        clock = Clock()
        fake = FakeRemote(clock, list(launches), external)
        monkeypatch.setattr(gnomon, "time", clock)
        monkeypatch.setattr(deck, "ssh", fake.ssh)
        monkeypatch.setattr(deck, "ssh_status", fake.ssh_status)
        return fake

    return make


@pytest.fixture
def mod(tmp_path: Path) -> Path:
    folder = tmp_path / "UnstraightenedProbe"
    (folder / "Scripts").mkdir(parents=True)
    (folder / "Scripts/main.lua").write_text("-- synthetic probe\n", encoding="utf-8")
    return folder


def test_host_and_game_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    default_game = "~/.local/share/Steam/steamapps/common/FINAL FANTASY RESONANCE"
    monkeypatch.delenv("FFR_PROBE_HOST", raising=False)
    monkeypatch.delenv("FFR_PROBE_GAME", raising=False)
    assert gnomon.host() == "gnomon"
    assert gnomon.game() == default_game
    assert gnomon.paks() == f"{default_game}/FFRS/Content/Paks"
    assert gnomon.ue4ss() == f"{default_game}/FFRS/Binaries/Win64/ue4ss"

    monkeypatch.setenv("FFR_PROBE_HOST", "synthetic-host")
    monkeypatch.setenv("FFR_PROBE_GAME", GAME)
    assert gnomon.host() == "synthetic-host"
    assert gnomon.paks() == f"{GAME}/FFRS/Content/Paks"
    assert gnomon.ue4ss() == f"{GAME}/FFRS/Binaries/Win64/ue4ss"


@pytest.mark.parametrize(("status", "expected"), [(0, True), (1, False)])
def test_running_maps_pgrep_status(monkeypatch: pytest.MonkeyPatch, status: int, expected: bool) -> None:
    seen: list[tuple[str, str]] = []

    def fake_status(host: str, command: str) -> int:
        seen.append((host, command))
        return status

    monkeypatch.setattr(deck, "ssh_status", fake_status)
    assert gnomon.running() is expected
    assert seen == [("gnomon", GAME_PGREP)]


@pytest.mark.parametrize("status", [2, 255])
def test_running_raises_on_other_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setattr(deck, "ssh_status", lambda host, command: status)
    with pytest.raises(Fail, match=f"gnomon.*{status}"):
        gnomon.running()


def test_build_id_reads_appmanifest(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    def fake_ssh(host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
        commands.append(command)
        return b'"AppState"\n{\n\t"appid"\t\t"3259780"\n\t"buildid"\t\t"24250053"\n}\n'

    monkeypatch.setattr(deck, "ssh", fake_ssh)
    assert gnomon.build_id() == "24250053"
    assert commands == [f"cat {GAME}/../../appmanifest_{APP_ID}.acf"]


def test_build_id_fails_when_manifest_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
        raise Fail(f"ssh to {host} exited 1")

    monkeypatch.setattr(deck, "ssh", missing)
    with pytest.raises(Fail):
        gnomon.build_id()


def test_fetch_returns_remote_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    def fake_ssh(host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
        commands.append(command)
        return b"synthetic bytes"

    monkeypatch.setattr(deck, "ssh", fake_ssh)
    assert gnomon.fetch(f"{GAME}/synthetic.bin") == b"synthetic bytes"
    assert commands == [f"cat {GAME}/synthetic.bin"]


def test_run_mod_refuses_when_game_runs(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote(external=True)
    with pytest.raises(Fail, match="running"):
        gnomon.run_mod(mod, FILES, LOG)
    assert fake.commands == [GAME_PGREP]


def test_run_mod_refuses_a_nameless_mod_folder(make_remote: Callable[..., FakeRemote]) -> None:
    fake = make_remote()
    with pytest.raises(Fail, match="no name"):
        gnomon.run_mod(Path("/"), FILES, LOG)
    assert fake.commands == []


def test_run_mod_returns_log_lines_on_healthy_launch(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([HEALTHY])
    assert gnomon.run_mod(mod, FILES, LOG) == [SYNTH_LINE, "done"]
    assert fake.launch_count == 1


def test_launch_sends_offscreen_render_command(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([HEALTHY])
    gnomon.run_mod(mod, FILES, LOG)
    assert f"timeout 30 steam -applaunch {APP_ID} -nosound -RenderOffscreen" in fake.commands


def test_run_mod_installs_tree_enabled_txt_and_files(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([HEALTHY])
    gnomon.run_mod(mod, FILES, LOG)
    installed = fake.snapshots[0]
    assert set(installed) == {f"{DEST}/Scripts/main.lua", f"{DEST}/enabled.txt", f"{DEST}/keys.txt"}
    assert installed[f"{DEST}/Scripts/main.lua"] == b"-- synthetic probe\n"
    assert installed[f"{DEST}/enabled.txt"] == b""
    assert installed[f"{DEST}/keys.txt"] == FILES["keys.txt"]


def test_run_mod_relaunches_after_stall(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([STALL, HEALTHY])
    assert gnomon.run_mod(mod, FILES, LOG) == [SYNTH_LINE, "done"]
    assert fake.launch_count == 2
    launches = [i for i, c in enumerate(fake.commands) if c == LAUNCH_CMD]
    kills = [i for i, c in enumerate(fake.commands) if c == EXE_PKILL]
    assert any(launches[0] < k < launches[1] for k in kills)


def test_stale_log_removed_before_each_launch(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([STALL, HEALTHY])
    gnomon.run_mod(mod, FILES, LOG)
    launches = [i for i, c in enumerate(fake.commands) if c == LAUNCH_CMD]
    assert len(launches) == 2
    for i in launches:
        assert fake.commands[i - 1] == f"rm -f {DEST}/{LOG}"


def test_run_mod_raises_after_all_launches_stall(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([STALL] * 4)
    with pytest.raises(Fail, match="stalled"):
        gnomon.run_mod(mod, FILES, LOG)
    assert fake.launch_count == 4


def test_run_mod_raises_without_retry_when_exe_exits_early(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([Launch(exit_after=20)])
    with pytest.raises(Fail, match="exited"):
        gnomon.run_mod(mod, FILES, LOG)
    assert fake.launch_count == 1


def test_run_mod_raises_when_done_never_comes(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([Launch(lines=((10, SYNTH_LINE),))])
    with pytest.raises(Fail, match="done"):
        gnomon.run_mod(mod, FILES, LOG)
    assert fake.launch_count == 1


def test_run_mod_raises_when_exe_never_appears(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([Launch(exe=False)])
    with pytest.raises(Fail, match="did not start"):
        gnomon.run_mod(mod, FILES, LOG)
    assert fake.launch_count == 1


OUTCOMES = [
    pytest.param([HEALTHY], id="healthy"),
    pytest.param([STALL] * 4, id="all-stalled"),
    pytest.param([Launch(exit_after=20)], id="early-exit"),
    pytest.param([Launch(lines=((10, SYNTH_LINE),))], id="no-done"),
    pytest.param([Launch(exe=False)], id="never-starts"),
]


@pytest.mark.parametrize("launches", OUTCOMES)
def test_game_killed_and_mod_removed_in_every_outcome(make_remote: Callable[..., FakeRemote], mod: Path, launches: list[Launch]) -> None:
    fake = make_remote(launches)
    with contextlib.suppress(Fail):
        gnomon.run_mod(mod, FILES, LOG)
    assert EXE_PKILL in fake.commands
    assert not fake.game_running()
    assert not [key for key in fake.fs if key.startswith(DEST)]


COMP_DEST = f"{GAME}/FFRS/Binaries/Win64/ue4ss/Mods/UnstraightenedText"
COMP_FILES = {"edits.txt": b"ST_SYNTH_001\t1\tSynthetic text.\n"}


@pytest.fixture
def companion(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    folder = tmp_path / "UnstraightenedText"
    (folder / "Scripts").mkdir(parents=True)
    (folder / "Scripts/main.lua").write_text("-- synthetic text mod\n", encoding="utf-8")
    return folder, COMP_FILES


def test_run_mod_installs_companion_tree_enabled_txt_and_files(
    make_remote: Callable[..., FakeRemote], mod: Path, companion: tuple[Path, dict[str, bytes]]
) -> None:
    fake = make_remote([HEALTHY])
    assert gnomon.run_mod(mod, FILES, LOG, companion=companion) == [SYNTH_LINE, "done"]
    installed = fake.snapshots[0]
    assert {key for key in installed if key.startswith(COMP_DEST)} == {
        f"{COMP_DEST}/Scripts/main.lua",
        f"{COMP_DEST}/enabled.txt",
        f"{COMP_DEST}/edits.txt",
    }
    assert installed[f"{COMP_DEST}/Scripts/main.lua"] == b"-- synthetic text mod\n"
    assert installed[f"{COMP_DEST}/enabled.txt"] == b""
    assert installed[f"{COMP_DEST}/edits.txt"] == COMP_FILES["edits.txt"]
    assert installed[f"{DEST}/keys.txt"] == FILES["keys.txt"]


def test_run_mod_refuses_a_nameless_companion_before_any_remote_command(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote()
    with pytest.raises(Fail, match="no name"):
        gnomon.run_mod(mod, FILES, LOG, companion=(Path("/"), COMP_FILES))
    assert fake.commands == []


@pytest.mark.parametrize("launches", OUTCOMES)
def test_companion_removed_in_every_outcome(
    make_remote: Callable[..., FakeRemote], mod: Path, companion: tuple[Path, dict[str, bytes]], launches: list[Launch]
) -> None:
    fake = make_remote(launches)
    with contextlib.suppress(Fail):
        gnomon.run_mod(mod, FILES, LOG, companion=companion)
    assert EXE_PKILL in fake.commands
    assert not fake.game_running()
    assert not [key for key in fake.fs if key.startswith((DEST, COMP_DEST))]


def test_companion_removed_even_when_the_main_mod_removal_fails(
    make_remote: Callable[..., FakeRemote], mod: Path, companion: tuple[Path, dict[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_remote([HEALTHY])

    def failing_removal(host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
        if command == f"rm -rf {DEST}" and fake.launch_count:
            raise Fail("synthetic ssh failure")
        return fake.ssh(host, command, stdin)

    monkeypatch.setattr(deck, "ssh", failing_removal)
    with pytest.raises(Fail, match="synthetic ssh failure"):
        gnomon.run_mod(mod, FILES, LOG, companion=companion)
    assert not [key for key in fake.fs if key.startswith(COMP_DEST)]


class WatchStop(Exception):
    pass


def test_offscreen_false_launches_without_render_flag(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([HEALTHY])
    assert gnomon.run_mod(mod, FILES, LOG, offscreen=False) == [SYNTH_LINE, "done"]
    assert VISIBLE_LAUNCH_CMD in fake.commands
    assert LAUNCH_CMD not in fake.commands


def test_offscreen_false_applies_to_every_relaunch(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([STALL, HEALTHY])
    assert gnomon.run_mod(mod, FILES, LOG, offscreen=False) == [SYNTH_LINE, "done"]
    assert fake.launch_count == 2
    assert fake.commands.count(VISIBLE_LAUNCH_CMD) == 2
    assert LAUNCH_CMD not in fake.commands


def test_watch_sees_growing_log_on_every_poll_before_done(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    make_remote([HEALTHY])
    seen: list[list[str]] = []
    assert gnomon.run_mod(mod, FILES, LOG, watch=seen.append) == [SYNTH_LINE, "done"]
    assert seen == [[], [], [], [SYNTH_LINE], [SYNTH_LINE, "done"]]


def test_watch_exception_ends_run_and_removes_mod(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([HEALTHY])

    def stop_on_line(lines: list[str]) -> None:
        if SYNTH_LINE in lines:
            raise WatchStop

    with pytest.raises(WatchStop):
        gnomon.run_mod(mod, FILES, LOG, watch=stop_on_line)
    assert EXE_PKILL in fake.commands
    assert not fake.game_running()
    assert not [key for key in fake.fs if key.startswith(DEST)]


def test_done_timeout_ends_run_at_the_given_wait(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    fake = make_remote([Launch(lines=((10, SYNTH_LINE),))])
    with pytest.raises(Fail, match="within 20s"):
        gnomon.run_mod(mod, FILES, LOG, done_timeout=20)
    assert fake.launch_count == 1


def test_done_timeout_default_gives_up_before_a_slow_done_line(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    make_remote([Launch(lines=((15, SYNTH_LINE), (400, "done\r")))])
    with pytest.raises(Fail, match="within 300s"):
        gnomon.run_mod(mod, FILES, LOG)


def test_done_timeout_lets_a_slow_done_line_through(make_remote: Callable[..., FakeRemote], mod: Path) -> None:
    make_remote([Launch(lines=((15, SYNTH_LINE), (400, "done\r")))])
    assert gnomon.run_mod(mod, FILES, LOG, done_timeout=500) == [SYNTH_LINE, "done"]


# ffrd probe: deck and gnomon refusals, verdicts, paks removed, and the packed shapes. Made-up keys and text only.

DECK = "synthetic-deck"
MODS = f"{GAME}/FFRS/Content/Paks/~mods"
PROBE_PAK = "FFRS-UnstraightenedProbe_P.pak"
PROBE_FILES = [PROBE_PAK, "FFRS-UnstraightenedProbe_P.utoc", "FFRS-UnstraightenedProbe_P.ucas"]
TARGET_LOCRES = "FFRS/Content/Localization/Unstraightened/en/Unstraightened.locres"
USER_GAME_INI = "FFRS/Config/UserGame.ini"
BUILD_BYTES = {ext: f"Synthetic build {ext}.\n".encode() for ext in ("pak", "utoc", "ucas")}
BUILD_FILES = [f"{MOD_NAME}.{ext}" for ext in ("pak", "utoc", "ucas")]
ALL_FILES = [*BUILD_FILES, *PROBE_FILES]
PROBE_KEY_SYNTH = ("ST_C01_001_01", "10")
OTHER_KEY = ("ST_C01_001_01", "2")
SYNTH_VANILLA = "Synthetic line ten."
SYNTH_TABLE = {
    ("ST_C01_001_01", "10"): "Synthetic line ten.",
    ("ST_C01_001_01", "2"): "Synthetic line two.",
    ("ST_C01_001_02", "1"): "",
    ("ST_C03_002_01", "1"): "Same in both.",
}

Responder = Callable[[str], list[str]] | BaseException


def edited_line(text: str) -> list[str]:
    return [line(PROBE_KEY_SYNTH, vanilla_hash(text)), "done"]


def vanilla_line(_text: str) -> list[str]:
    return [line(PROBE_KEY_SYNTH, vanilla_hash(SYNTH_VANILLA)), "done"]


def no_observation(_text: str) -> list[str]:
    return ["done"]


def run_ffrd_probe(*args: str) -> int:
    try:
        cli.main(["probe", *args])
    except SystemExit as exit_info:
        assert isinstance(exit_info.code, int)
        return exit_info.code
    return 0


def translations(table: LocresFile) -> dict[tuple[str, str], str]:
    return {(ns.name, key): ns[key].translation for ns in table for key in ns.entrys}


def packed_file(pak: bytes, tmp_path: Path, rel: str) -> Path:
    """Write one file out of a probe pak with repak get, under its own name in tmp_path."""
    pak_path = tmp_path / PROBE_PAK
    pak_path.write_bytes(pak)
    data = subprocess.run(["repak", "get", str(pak_path), rel], check=True, capture_output=True).stdout
    path = tmp_path / Path(rel).name
    path.write_bytes(data)
    return path


def packed_locres(pak: bytes, tmp_path: Path, name: str) -> LocresFile:
    """Read one English locres back out of a probe pak."""
    table = LocresFile()
    table.read(str(packed_file(pak, tmp_path, f"{LOCRES}/en/{name}")))
    return table


def packed_paths(pak: bytes, tmp_path: Path) -> list[str]:
    """Every path inside a probe pak, as repak list prints them."""
    pak_path = tmp_path / PROBE_PAK
    pak_path.write_bytes(pak)
    out = subprocess.run(["repak", "list", str(pak_path)], check=True, capture_output=True, text=True, encoding="utf-8").stdout
    return out.splitlines()


class ProbeRemote:
    """Stands in for ssh, gnomon and the Deck copy. Records every call and answers each launch in turn."""

    def __init__(self, responders: list[Responder], deck_status: int, running: bool, build: str, put_error: Fail | None) -> None:
        self.responders = responders
        self.deck_status = deck_status
        self.running = running
        self.build = build
        self.put_error = put_error
        self.texts: list[str] = []
        self.status_calls: list[tuple[str, str]] = []
        self.fetched: list[str] = []
        self.puts: list[tuple[str, dict[str, bytes]]] = []
        self.deletes: list[tuple[str, list[str]]] = []
        self.runs: list[tuple[Path, dict[str, bytes], str, tuple[Path, dict[str, bytes]] | None]] = []

    def next_text(self) -> str:
        text = f"Unstraightened probe {len(self.texts) + 1:08x}"
        self.texts.append(text)
        return text

    def ssh_status(self, host: str, command: str) -> int:
        self.status_calls.append((host, command))
        return self.deck_status

    def fetch(self, path: str) -> bytes:
        self.fetched.append(path)
        return path.encode()

    def put(self, host: str, dest: str, files: list[Path]) -> None:
        self.puts.append((dest, {f.name: f.read_bytes() for f in files}))
        if self.put_error is not None:
            raise self.put_error

    def delete(self, host: str, dest: str, names: list[str]) -> None:
        self.deletes.append((dest, names))

    def run(self, mod: Path, files: dict[str, bytes], log: str, companion: tuple[Path, dict[str, bytes]] | None = None) -> list[str]:
        self.runs.append((mod, files, log, companion))
        if not self.responders:
            raise AssertionError("more launches than the test expects")
        answer = self.responders.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer(self.texts[-1])


@pytest.fixture
def make_probe(fake_vanilla: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., ProbeRemote]:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    (root / "build").mkdir()
    for ext, data in BUILD_BYTES.items():
        (root / "build" / f"{MOD_NAME}.{ext}").write_bytes(data)
    monkeypatch.chdir(root)
    monkeypatch.setenv("FFR_DECK_HOST", DECK)
    monkeypatch.setattr(probe, "PROBE_KEY", PROBE_KEY_SYNTH)

    def make(
        responders: Sequence[Responder] = (),
        deck_status: int = 1,
        running: bool = False,
        build: str = "424242",
        put_error: Fail | None = None,
    ) -> ProbeRemote:
        fake = ProbeRemote(list(responders), deck_status, running, build, put_error)
        monkeypatch.setattr(probe, "_probe_text", fake.next_text)
        monkeypatch.setattr(deck, "ssh_status", fake.ssh_status)
        monkeypatch.setattr(deck, "put", fake.put)
        monkeypatch.setattr(deck, "delete", fake.delete)
        monkeypatch.setattr(gnomon, "running", lambda: fake.running)
        monkeypatch.setattr(gnomon, "build_id", lambda: fake.build)
        monkeypatch.setattr(gnomon, "fetch", fake.fetch)
        monkeypatch.setattr(gnomon, "run_mod", fake.run)
        return fake

    return make


def test_build_honoured_exits_0_after_one_launch(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    assert len(fake.runs) == 1
    marker = vanilla_hash(fake.texts[0])
    vanilla_digest = vanilla_hash(SYNTH_VANILLA)
    assert capsys.readouterr().out == f"build: ST_C01_001_01,10 marker {marker} vanilla {vanilla_digest} seen {marker}: honoured\n"


def test_build_not_honoured_exits_1_after_one_launch(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe([vanilla_line])
    assert run_ffrd_probe() == 1
    assert len(fake.runs) == 1
    marker = vanilla_hash(fake.texts[0])
    vanilla_digest = vanilla_hash(SYNTH_VANILLA)
    captured = capsys.readouterr()
    assert captured.out == f"build: ST_C01_001_01,10 marker {marker} vanilla {vanilla_digest} seen {vanilla_digest}: not honoured\n"
    assert captured.err == "ffrd: build is not honoured by the game\n"


def test_shape_probe_does_not_need_the_build_files(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    (tmp_path / "repo" / "build" / f"{MOD_NAME}.ucas").unlink()
    assert run_ffrd_probe("--shape", "full") == 0
    assert len(fake.runs) == 1


def test_partial_shape_launches_once_and_carries_only_the_probe_line(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe("--shape", "partial") == 0
    assert len(fake.runs) == 1
    table = packed_locres(fake.puts[0][1][PROBE_PAK], tmp_path, "Unstraightened.locres")
    assert translations(table) == {PROBE_KEY_SYNTH: fake.texts[0]}


def test_full_shape_not_honoured_exits_1(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe([vanilla_line])
    assert run_ffrd_probe("--shape", "full") == 1
    assert len(fake.runs) == 1
    assert capsys.readouterr().err == "ffrd: shape full is not honoured by the game\n"


def test_unknown_shape_exits_2_before_any_launch(make_probe: Callable[..., ProbeRemote]) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe("--shape", "bogus") == 2
    assert fake.status_calls == []
    assert fake.runs == []


@pytest.mark.parametrize(
    ("lines", "exit_code"),
    [
        pytest.param(lambda t: [line(PROBE_KEY_SYNTH, vanilla_hash(t)), "done"], 0, id="marker-only"),
        pytest.param(
            lambda t: [line(PROBE_KEY_SYNTH, vanilla_hash(t)), line(PROBE_KEY_SYNTH, vanilla_hash(SYNTH_VANILLA)), "done"],
            1,
            id="marker-and-vanilla",
        ),
        pytest.param(lambda _t: [line(PROBE_KEY_SYNTH, vanilla_hash(SYNTH_VANILLA)), "done"], 1, id="vanilla-only"),
        pytest.param(lambda _t: [line(PROBE_KEY_SYNTH, "missing"), "done"], 1, id="missing"),
        pytest.param(lambda t: [line(PROBE_KEY_SYNTH, vanilla_hash(t).upper()), "done"], 1, id="uppercase-digest"),
        pytest.param(lambda t: [line(PROBE_KEY_SYNTH, vanilla_hash(t)) + " extra", "done"], 1, id="trailing-text"),
        pytest.param(lambda t: [line(OTHER_KEY, vanilla_hash(t)), "done"], 1, id="other-key"),
        pytest.param(lambda _t: ["done"], 1, id="no-key-line"),
    ],
)
def test_verdict_needs_the_marker_and_no_vanilla_line(
    make_probe: Callable[..., ProbeRemote], lines: Callable[[str], list[str]], exit_code: int
) -> None:
    fake = make_probe([lines])
    assert run_ffrd_probe() == exit_code
    assert len(fake.runs) == 1


def test_log_without_an_observation_is_inconclusive(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe([no_observation, edited_line])
    assert run_ffrd_probe() == 1
    assert len(fake.runs) == 1
    captured = capsys.readouterr()
    assert "produced no observation" in captured.err
    assert "not honoured" not in captured.out


def test_shape_without_an_observation_is_inconclusive(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe([no_observation])
    assert run_ffrd_probe("--shape", "full") == 1
    assert len(fake.runs) == 1
    captured = capsys.readouterr()
    assert "produced no observation" in captured.err
    assert "not honoured" not in captured.out


def test_refuses_when_ffr_runs_on_the_deck(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe(deck_status=0)
    assert run_ffrd_probe() == 1
    assert capsys.readouterr().err == f"ffrd: FINAL FANTASY RESONANCE is running on {DECK}; refusing to probe\n"
    assert fake.status_calls == [(DECK, EXE_PGREP)]
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.runs == []


def test_proceeds_silently_when_the_deck_answers_without_ffr(
    make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]
) -> None:
    make_probe([edited_line], deck_status=1)
    assert run_ffrd_probe() == 0
    assert capsys.readouterr().err == ""


def test_proceeds_with_a_note_when_the_deck_is_unreachable(
    make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]
) -> None:
    make_probe([edited_line], deck_status=255)
    assert run_ffrd_probe() == 0
    err = capsys.readouterr().err
    assert err.startswith("note: ")
    assert DECK in err
    assert "255" in err


def test_refuses_when_ffr_runs_on_gnomon(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe(running=True)
    assert run_ffrd_probe() == 1
    assert "is running on" in capsys.readouterr().err
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.runs == []


def test_refuses_when_gnomon_build_differs_from_vanilla(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe(build="999999")
    assert run_ffrd_probe() == 1
    assert "999999" in capsys.readouterr().err
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.runs == []


def test_probe_asks_the_mod_for_the_probe_key(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    mod, files, log, companion = fake.runs[0]
    assert mod == tmp_path / "repo" / "ue4ss/UnstraightenedProbe"
    assert files == {"keys.txt": b"ST_C01_001_01,10\n"}
    assert log == "probe.log"
    assert companion is None


def test_ue4ss_shape_puts_fetches_and_deletes_no_pak(make_probe: Callable[..., ProbeRemote]) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe("--shape", "ue4ss") == 0
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.deletes == []


def test_ue4ss_shape_runs_the_probe_mod_with_the_text_mod_beside_it(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe("--shape", "ue4ss") == 0
    assert len(fake.runs) == 1
    mod, files, log, companion = fake.runs[0]
    assert mod == tmp_path / "repo" / "ue4ss/UnstraightenedProbe"
    assert files == {"keys.txt": b"ST_C01_001_01,10\n"}
    assert log == "probe.log"
    assert companion == (
        tmp_path / "repo" / "ue4ss/UnstraightenedText",
        {"edits.txt": edits_text({PROBE_KEY_SYNTH: fake.texts[0]}).encode("utf-8")},
    )


def test_ue4ss_shape_honoured_prints_the_verdict_and_exits_0(
    make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe("--shape", "ue4ss") == 0
    marker = vanilla_hash(fake.texts[0])
    vanilla_digest = vanilla_hash(SYNTH_VANILLA)
    assert capsys.readouterr().out == (
        f"ue4ss: ST_C01_001_01,10 marker {marker} vanilla {vanilla_digest} seen {marker}: honoured\nshape honoured: ue4ss\n"
    )


def test_ue4ss_shape_not_honoured_exits_1(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe([vanilla_line])
    assert run_ffrd_probe("--shape", "ue4ss") == 1
    assert len(fake.runs) == 1
    assert capsys.readouterr().err == "ffrd: shape ue4ss is not honoured by the game\n"


def test_build_probe_never_runs_the_text_mod(make_probe: Callable[..., ProbeRemote]) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    assert [run[3] for run in fake.runs] == [None]


def test_probe_help_names_the_ue4ss_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_ffrd_probe("--help") == 0
    assert "target, full, partial or ue4ss" in " ".join(capsys.readouterr().out.split())


def test_probe_help_describes_the_built_mod_with_a_marker_pak(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_ffrd_probe("--help") == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "the built mod" in text
    assert "marker pak" in text


def test_build_and_marker_are_installed_beside_the_global_container_copies(make_probe: Callable[..., ProbeRemote]) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    assert fake.fetched == [f"{GAME}/FFRS/Content/Paks/global.utoc", f"{GAME}/FFRS/Content/Paks/global.ucas"]
    assert len(fake.puts) == 1
    dest, files = fake.puts[0]
    assert dest == MODS
    assert sorted(files) == sorted(ALL_FILES)
    assert files["FFRS-UnstraightenedProbe_P.utoc"] == f"{GAME}/FFRS/Content/Paks/global.utoc".encode()
    assert files["FFRS-UnstraightenedProbe_P.ucas"] == f"{GAME}/FFRS/Content/Paks/global.ucas".encode()


def test_build_files_are_put_byte_identical(make_probe: Callable[..., ProbeRemote]) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    files = fake.puts[0][1]
    assert {name: files[name] for name in BUILD_FILES} == {f"{MOD_NAME}.{ext}": data for ext, data in BUILD_BYTES.items()}


def test_target_pak_lists_exactly_the_two_target_paths(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe("--shape", "target") == 0
    assert sorted(packed_paths(fake.puts[0][1][PROBE_PAK], tmp_path)) == sorted([TARGET_LOCRES, USER_GAME_INI])


def test_target_pak_carries_only_the_probe_line_and_user_game_text(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe("--shape", "target") == 0
    pak = fake.puts[0][1][PROBE_PAK]
    table = LocresFile()
    table.read(str(packed_file(pak, tmp_path, TARGET_LOCRES)))
    assert translations(table) == {PROBE_KEY_SYNTH: fake.texts[0]}
    assert packed_file(pak, tmp_path, USER_GAME_INI).read_bytes() == USER_GAME_TEXT.encode("utf-8")


def test_marker_pak_packs_only_the_full_game_locres(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    assert packed_paths(fake.puts[0][1][PROBE_PAK], tmp_path) == [f"{LOCRES}/en/Game.locres"]


def test_marker_pak_replaces_the_probe_line_and_keeps_the_rest(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    table = packed_locres(fake.puts[0][1][PROBE_PAK], tmp_path, "Game.locres")
    assert translations(table) == {**SYNTH_TABLE, PROBE_KEY_SYNTH: fake.texts[0]}


def test_output_has_no_probe_text(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    captured = capsys.readouterr()
    assert fake.texts[0] not in captured.out + captured.err
    assert "Unstraightened probe" not in captured.out + captured.err


def junk_then_edited(text: str) -> list[str]:
    return ["Synthetic junk line the probe must never echo.", f"{line(PROBE_KEY_SYNTH, vanilla_hash(text))} trailing", *edited_line(text)]


def test_output_never_echoes_unmatched_log_lines(make_probe: Callable[..., ProbeRemote], capsys: pytest.CaptureFixture[str]) -> None:
    make_probe([junk_then_edited])
    assert run_ffrd_probe() == 0
    captured = capsys.readouterr()
    assert "Synthetic junk line" not in captured.out + captured.err
    assert "trailing" not in captured.out + captured.err


@pytest.mark.parametrize("missing", [("ST_C01_001_01", "99"), ("ST_SYNTH_NONE", "1")], ids=["key", "namespace"])
def test_probe_key_absent_from_vanilla_exits_1(
    make_probe: Callable[..., ProbeRemote], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: tuple[str, str]
) -> None:
    fake = make_probe([edited_line])
    monkeypatch.setattr(probe, "PROBE_KEY", missing)
    assert run_ffrd_probe() == 1
    assert capsys.readouterr().err == f"ffrd: the probe key {missing[0]},{missing[1]} is not in the vanilla text\n"
    assert fake.puts == []


@pytest.mark.parametrize(
    ("responders", "code"),
    [
        pytest.param([edited_line], 0, id="honoured"),
        pytest.param([vanilla_line], 1, id="not-honoured"),
        pytest.param([no_observation], 1, id="no-observation"),
        pytest.param([Fail("synthetic launch failure")], 1, id="launch-fails"),
    ],
)
def test_probe_files_removed_after_every_run(make_probe: Callable[..., ProbeRemote], responders: list[Responder], code: int) -> None:
    fake = make_probe(responders)
    assert run_ffrd_probe() == code
    assert len(fake.puts) == 1
    assert all(dest == MODS and sorted(files) == sorted(ALL_FILES) for dest, files in fake.puts)
    assert fake.deletes == [(MODS, ALL_FILES)] * len(fake.puts)


def test_probe_files_removed_when_copy_fails(make_probe: Callable[..., ProbeRemote]) -> None:
    fake = make_probe([edited_line], put_error=Fail("synthetic copy failure"))
    assert run_ffrd_probe() == 1
    assert fake.deletes == [(MODS, ALL_FILES)]
    assert fake.runs == []


def test_refuses_when_a_build_file_is_missing_before_any_remote_call(
    make_probe: Callable[..., ProbeRemote], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = make_probe([edited_line], running=True)
    (tmp_path / "repo" / "build" / f"{MOD_NAME}.ucas").unlink()
    assert run_ffrd_probe() == 1
    assert capsys.readouterr().err == f"ffrd: missing ['{MOD_NAME}.ucas']; run `ffrd build`\n"
    assert fake.status_calls == []
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.runs == []


def test_probe_text_is_a_fresh_hex_line_per_call() -> None:
    first, second = probe._probe_text(), probe._probe_text()
    assert re.fullmatch(r"Unstraightened probe [0-9a-f]{8}", first)
    assert first != second


def test_refuses_when_vanilla_has_no_build_stamp(
    make_probe: Callable[..., ProbeRemote], fake_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = make_probe()
    (fake_vanilla / "BUILDID").unlink()
    assert run_ffrd_probe() == 1
    assert capsys.readouterr().err == "ffrd: no vanilla text yet; run `ffrd extract`\n"
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.runs == []


# ffrd probe with dialog edits: a build that overrides PROBE_KEY, a stale build, and dialog edits that do not load. Made-up text only.

BUILD_TEXT = "Synthetic build line ten, edited."
BUILD_DIGEST = vanilla_hash(BUILD_TEXT)
DIALOG_REL = "dialog/synth/build.toml"
OTHER_VANILLA = "Synthetic line two."


def dialog_toml(text: str = BUILD_TEXT, vanilla: str = vanilla_hash(SYNTH_VANILLA)) -> str:
    ns, key = PROBE_KEY_SYNTH
    return f'[{ns}]\n{key} = {{ vanilla = "{vanilla}", text = "{text}" }}\n'


def other_key_toml() -> str:
    ns, key = OTHER_KEY
    return f'[{ns}]\n{key} = {{ vanilla = "{vanilla_hash(OTHER_VANILLA)}", text = "Synthetic line two, edited." }}\n'


def write_dialog(tmp_path: Path, body: str, mtime: float = 1_000_000_000.0) -> None:
    """Write one dialog edit under the fake repo, dated before the build pak unless mtime says otherwise."""
    path = tmp_path / "repo" / DIALOG_REL
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def build_seen(_text: str) -> list[str]:
    return [line(PROBE_KEY_SYNTH, BUILD_DIGEST), "done"]


def test_build_overriding_probe_key_is_honoured_when_only_its_hash_is_seen(
    make_probe: Callable[..., ProbeRemote], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dialog(tmp_path, dialog_toml())
    fake = make_probe([build_seen])
    assert run_ffrd_probe() == 0
    assert len(fake.runs) == 1
    marker = vanilla_hash(fake.texts[0])
    vanilla_digest = vanilla_hash(SYNTH_VANILLA)
    assert capsys.readouterr().out == (
        f"build: ST_C01_001_01,10 marker {marker} vanilla {vanilla_digest} build {BUILD_DIGEST} seen {BUILD_DIGEST}: honoured\n"
    )


@pytest.mark.parametrize(
    ("lines", "exit_code"),
    [
        pytest.param(build_seen, 0, id="build-only"),
        pytest.param(
            lambda t: [line(PROBE_KEY_SYNTH, BUILD_DIGEST), line(PROBE_KEY_SYNTH, vanilla_hash(t)), "done"],
            1,
            id="build-and-marker",
        ),
        pytest.param(
            lambda _t: [line(PROBE_KEY_SYNTH, BUILD_DIGEST), line(PROBE_KEY_SYNTH, vanilla_hash(SYNTH_VANILLA)), "done"],
            1,
            id="build-and-vanilla",
        ),
        pytest.param(lambda t: [line(PROBE_KEY_SYNTH, vanilla_hash(t)), "done"], 1, id="marker-only"),
        pytest.param(lambda _t: [line(PROBE_KEY_SYNTH, vanilla_hash(SYNTH_VANILLA)), "done"], 1, id="vanilla-only"),
        pytest.param(lambda _t: [line(PROBE_KEY_SYNTH, "missing"), "done"], 1, id="missing"),
    ],
)
def test_build_overriding_probe_key_needs_the_build_hash_and_no_other(
    make_probe: Callable[..., ProbeRemote], tmp_path: Path, lines: Callable[[str], list[str]], exit_code: int
) -> None:
    write_dialog(tmp_path, dialog_toml())
    fake = make_probe([lines])
    assert run_ffrd_probe() == exit_code
    assert len(fake.runs) == 1


def test_build_overriding_probe_key_puts_the_build_and_the_marker_once(make_probe: Callable[..., ProbeRemote], tmp_path: Path) -> None:
    write_dialog(tmp_path, dialog_toml())
    fake = make_probe([build_seen])
    assert run_ffrd_probe() == 0
    assert len(fake.puts) == 1
    dest, files = fake.puts[0]
    assert dest == MODS
    assert sorted(files) == sorted(ALL_FILES)
    assert {name: files[name] for name in BUILD_FILES} == {f"{MOD_NAME}.{ext}": data for ext, data in BUILD_BYTES.items()}
    assert translations(packed_locres(files[PROBE_PAK], tmp_path, "Game.locres")) == {**SYNTH_TABLE, PROBE_KEY_SYNTH: fake.texts[0]}
    assert fake.deletes == [(MODS, ALL_FILES)]


def test_build_editing_another_key_keeps_the_marker_verdict(
    make_probe: Callable[..., ProbeRemote], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dialog(tmp_path, other_key_toml())
    fake = make_probe([edited_line])
    assert run_ffrd_probe() == 0
    marker = vanilla_hash(fake.texts[0])
    vanilla_digest = vanilla_hash(SYNTH_VANILLA)
    assert capsys.readouterr().out == f"build: ST_C01_001_01,10 marker {marker} vanilla {vanilla_digest} seen {marker}: honoured\n"


def test_stale_dialog_edit_fails_before_any_put(
    make_probe: Callable[..., ProbeRemote], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dialog(tmp_path, dialog_toml(), mtime=time.time() + 3600)
    fake = make_probe([build_seen])
    assert run_ffrd_probe() == 1
    assert capsys.readouterr().err == f"ffrd: {DIALOG_REL} is newer than build/{MOD_NAME}.pak; run `ffrd build`\n"
    assert fake.status_calls == []
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.runs == []


def test_dialog_edit_that_does_not_load_fails_before_any_put(
    make_probe: Callable[..., ProbeRemote], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dialog(tmp_path, dialog_toml(vanilla="0000000000000000"))
    fake = make_probe([build_seen])
    assert run_ffrd_probe() == 1
    assert capsys.readouterr().err == f"ffrd: {DIALOG_REL}: ST_C01_001_01,10 changed in the game\n"
    assert fake.status_calls == []
    assert fake.fetched == []
    assert fake.puts == []
    assert fake.runs == []


def test_unreadable_dialog_edit_fails_before_any_put(
    make_probe: Callable[..., ProbeRemote], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dialog(tmp_path, "[ST_C01_001_01\n")
    fake = make_probe([build_seen])
    assert run_ffrd_probe() == 1
    assert capsys.readouterr().err.startswith(f"ffrd: {DIALOG_REL}: cannot read as TOML: ")
    assert fake.status_calls == []
    assert fake.puts == []
    assert fake.runs == []


# ffrd usmap: the usmap mod's log, the newest dump fetched into vanilla, the remote dump removed. Made-up bytes only.

USMAP_SCRIPT = WORKSPACE / "ue4ss/UnstraightenedUsmap/Scripts/main.lua"
USMAP_PRELUDE = """
function ExecuteWithDelay(_, f) f() end
function ExecuteInGameThread(f) f() end
"""


def run_usmap_mod(root: Path, dump: str) -> list[str]:
    scripts = root / "UnstraightenedUsmap" / "Scripts"
    scripts.mkdir(parents=True)
    copied = scripts / "main.lua"
    shutil.copyfile(USMAP_SCRIPT, copied)
    stub = root / "stub.lua"
    stub.write_text(USMAP_PRELUDE + dump + f"\ndofile({lua_string(str(copied))})\n", encoding="utf-8")
    subprocess.run(["lua", str(stub)], capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    return (scripts.parent / "usmap.log").read_text(encoding="utf-8").splitlines()


def test_usmap_mod_logs_only_done_after_a_clean_dump(tmp_path: Path) -> None:
    assert run_usmap_mod(tmp_path, "function DumpUSMAP(all) assert(all == true, 'not called with true') end") == ["done"]


def test_usmap_mod_logs_failed_then_done_when_the_dump_raises(tmp_path: Path) -> None:
    assert run_usmap_mod(tmp_path, 'function DumpUSMAP(_) error("synthetic dump failure") end') == ["failed", "done"]


UE4SS = f"{GAME}/FFRS/Binaries/Win64/ue4ss"
DUMP = f"{UE4SS}/FFRS-5.6.1-synthetic.usmap"
OLD_DUMP = f"{UE4SS}/FFRS-5.6.0-synthetic.usmap"
STALE_DUMP = f"{UE4SS}/FFRS-5.5.0-stale.usmap"
DUMP_BYTES = b"synthetic usmap bytes\x00\x01"
RM_DUMPS = f"sh -c 'rm -f \"$1\"/*.usmap' sh {UE4SS}"
LS_DUMPS = f"sh -c 'ls -t \"$1\"/*.usmap 2>/dev/null | head -n 1' sh {UE4SS}"
OLD_BYTES = b"old synthetic usmap bytes"
USMAP_MOD_PATH = "ue4ss/UnstraightenedUsmap"


class UsmapRemote:
    """Stands in for ssh, the Deck check and the usmap mod. Running the mod adds its dumps to the remote files."""

    def __init__(
        self,
        deck_status: int,
        lines: list[str],
        stale: dict[str, tuple[float, bytes]],
        dumps: dict[str, tuple[float, bytes]],
    ) -> None:
        self.deck_status = deck_status
        self.lines = lines
        self.files = dict(stale)
        self.dumps = dumps
        self.status_calls: list[tuple[str, str]] = []
        self.commands: list[str] = []
        self.runs: list[tuple[Path, dict[str, bytes], str]] = []
        self.seen_at_run: set[str] = set()
        self.fetched: list[str] = []

    def ssh_status(self, host: str, command: str) -> int:
        self.status_calls.append((host, command))
        return self.deck_status

    def ssh(self, host: str, command: str, stdin: IO[bytes] | int = subprocess.DEVNULL) -> bytes:
        self.commands.append(command)
        if command == RM_DUMPS:
            for path in [p for p in self.files if p.startswith(f"{UE4SS}/") and p.endswith(".usmap")]:
                del self.files[path]
            return b""
        if command == LS_DUMPS:
            newest = sorted(
                ((mtime, path) for path, (mtime, _) in self.files.items() if path.startswith(f"{UE4SS}/") and path.endswith(".usmap")),
                reverse=True,
            )
            return f"{newest[0][1]}\n".encode() if newest else b""
        if command.startswith("rm -f "):
            self.files.pop(command[len("rm -f ") :], None)
            return b""
        raise AssertionError(f"unexpected ssh command: {command}")

    def fetch(self, path: str) -> bytes:
        self.fetched.append(path)
        return self.files[path][1]

    def run(self, mod: Path, files: dict[str, bytes], log: str) -> list[str]:
        self.runs.append((mod, files, log))
        self.seen_at_run = set(self.files)
        self.files.update(self.dumps)
        return list(self.lines)


def run_ffrd_usmap() -> int:
    try:
        cli.main(["usmap"])
    except SystemExit as exit_info:
        assert isinstance(exit_info.code, int)
        return exit_info.code
    return 0


@pytest.fixture
def make_usmap(fake_vanilla: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., UsmapRemote]:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("FFR_DECK_HOST", DECK)

    def make(
        deck_status: int = 1,
        lines: Sequence[str] = ("done",),
        stale: dict[str, tuple[float, bytes]] | None = None,
        dumps: dict[str, tuple[float, bytes]] | None = None,
    ) -> UsmapRemote:
        fake = UsmapRemote(deck_status, list(lines), stale or {}, dumps or {})
        monkeypatch.setattr(deck, "ssh_status", fake.ssh_status)
        monkeypatch.setattr(deck, "ssh", fake.ssh)
        monkeypatch.setattr(gnomon, "fetch", fake.fetch)
        monkeypatch.setattr(gnomon, "run_mod", fake.run)
        return fake

    return make


def test_usmap_refuses_when_ffr_runs_on_the_deck(
    make_usmap: Callable[..., UsmapRemote], fake_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = make_usmap(deck_status=0, dumps={DUMP: (1.0, DUMP_BYTES)})
    assert run_ffrd_usmap() == 1
    assert capsys.readouterr().err == f"ffrd: FINAL FANTASY RESONANCE is running on {DECK}; refusing to probe\n"
    assert fake.status_calls == [(DECK, EXE_PGREP)]
    assert fake.commands == []
    assert fake.runs == []
    assert fake.fetched == []
    assert not (fake_vanilla / "Mappings.usmap").exists()


def test_usmap_writes_the_fetched_bytes_to_vanilla(
    make_usmap: Callable[..., UsmapRemote], fake_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = make_usmap(dumps={DUMP: (1.0, DUMP_BYTES)})
    assert run_ffrd_usmap() == 0
    dest = fake_vanilla / "Mappings.usmap"
    assert dest.read_bytes() == DUMP_BYTES
    assert fake.fetched == [DUMP]
    assert capsys.readouterr().out == f"wrote {dest} ({len(DUMP_BYTES)} bytes)\n"


def test_usmap_removes_the_remote_dump_after_the_write(make_usmap: Callable[..., UsmapRemote]) -> None:
    fake = make_usmap(dumps={DUMP: (1.0, DUMP_BYTES)})
    assert run_ffrd_usmap() == 0
    assert DUMP not in fake.files
    assert fake.commands == [RM_DUMPS, LS_DUMPS, f"rm -f {DUMP}"]


def test_usmap_clears_stale_dumps_before_the_mod_runs(make_usmap: Callable[..., UsmapRemote]) -> None:
    fake = make_usmap(stale={STALE_DUMP: (0.5, OLD_BYTES)}, dumps={DUMP: (1.0, DUMP_BYTES)})
    assert run_ffrd_usmap() == 0
    assert len(fake.runs) == 1
    assert STALE_DUMP not in fake.seen_at_run
    assert fake.fetched == [DUMP]


def test_usmap_fetches_the_newest_of_several_dumps(make_usmap: Callable[..., UsmapRemote], fake_vanilla: Path) -> None:
    fake = make_usmap(dumps={OLD_DUMP: (1.0, OLD_BYTES), DUMP: (2.0, DUMP_BYTES)})
    assert run_ffrd_usmap() == 0
    assert fake.fetched == [DUMP]
    assert (fake_vanilla / "Mappings.usmap").read_bytes() == DUMP_BYTES


def test_usmap_runs_the_usmap_mod_with_no_extra_files(make_usmap: Callable[..., UsmapRemote], tmp_path: Path) -> None:
    fake = make_usmap(dumps={DUMP: (1.0, DUMP_BYTES)})
    assert run_ffrd_usmap() == 0
    assert fake.runs == [(tmp_path / "repo" / USMAP_MOD_PATH, {}, "usmap.log")]


def test_usmap_exits_1_when_the_mod_reports_failed(
    make_usmap: Callable[..., UsmapRemote], fake_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = make_usmap(lines=["failed", "done"])
    assert run_ffrd_usmap() == 1
    assert "gnomon" in capsys.readouterr().err
    assert fake.fetched == []
    assert not (fake_vanilla / "Mappings.usmap").exists()


def test_usmap_exits_1_when_no_dump_exists(
    make_usmap: Callable[..., UsmapRemote], fake_vanilla: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = make_usmap(lines=["done"])
    assert run_ffrd_usmap() == 1
    err = capsys.readouterr().err
    assert "no .usmap" in err
    assert "gnomon" in err
    assert fake.fetched == []
    assert not (fake_vanilla / "Mappings.usmap").exists()


def test_usmap_creates_the_vanilla_directory_when_absent(
    make_usmap: Callable[..., UsmapRemote], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = tmp_path / "absent" / "vanilla"
    monkeypatch.setenv("FFR_VANILLA", str(fresh))
    make_usmap(dumps={DUMP: (1.0, DUMP_BYTES)})
    assert run_ffrd_usmap() == 0
    assert (fresh / "Mappings.usmap").read_bytes() == DUMP_BYTES
