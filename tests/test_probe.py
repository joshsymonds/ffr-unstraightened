"""Runs the UE4SS probe mod under plain Lua 5.4 with the game globals stubbed.

Made-up namespaces, keys and text. Not game text.
"""

import contextlib
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

from ffrd import deck, gnomon
from ffrd.game import APP_ID, Fail, vanilla_hash

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
LAUNCH_CMD = f"timeout 30 steam steam://rungameid/{APP_ID}"
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
        if command == LAUNCH_CMD:
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


@pytest.mark.parametrize(
    "launches",
    [
        pytest.param([HEALTHY], id="healthy"),
        pytest.param([STALL] * 4, id="all-stalled"),
        pytest.param([Launch(exit_after=20)], id="early-exit"),
        pytest.param([Launch(lines=((10, SYNTH_LINE),))], id="no-done"),
        pytest.param([Launch(exe=False)], id="never-starts"),
    ],
)
def test_game_killed_and_mod_removed_in_every_outcome(make_remote: Callable[..., FakeRemote], mod: Path, launches: list[Launch]) -> None:
    fake = make_remote(launches)
    with contextlib.suppress(Fail):
        gnomon.run_mod(mod, FILES, LOG)
    assert EXE_PKILL in fake.commands
    assert not fake.game_running()
    assert not [key for key in fake.fs if key.startswith(DEST)]
