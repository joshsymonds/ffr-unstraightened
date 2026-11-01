"""pairs: the held-out split, the DL5 exclusion, the counts-only check report, and the paraphrase run with its pair files,
over made-up scenes and lines, with fake cswap and claude executables on PATH.

Made-up scene, namespace, key, speaker and text values. Not game text.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from ffrd import scenes
from ffrd.game import Fail
from ffrd.pipeline.store import Store
from ffrd.scenes import SceneLine
from ffrd.voice import pairs
from ffrd.voice.pairs import CALLS, check, english, excluded, normalize, split

SPEAKER = "SENTINEL-SPEAKER"
TEXT = "SENTINEL-TEXT"
SHARED_TRAIN = ("ST_Syn_001_01", "1")
SHARED_HELD = ("ST_Syn_002_01", "1")
DISTINCT_HELD = ("ST_Syn_002_02", "1")
DISTINCT_HELD_DUPLICATE = ("ST_Syn_002_03", "1")


def _scenes(count: int) -> list[str]:
    return [f"made-up-scene-{i:02d}" for i in range(count)]


def _line(scene: str, namespace: str, key: str, order: int = 0) -> SceneLine:
    return SceneLine(scene=scene, order=order, namespace=namespace, key=key, speaker=SPEAKER, voice=None)


def _sha_order(scenes: list[str]) -> list[str]:
    return sorted(scenes, key=lambda scene: hashlib.sha256(scene.encode("utf-8")).hexdigest())


@pytest.mark.parametrize(("count", "heldout"), [(3, 0), (4, 0), (10, 1), (12, 1), (15, 2), (20, 2)])
def test_split_holds_out_round_tenth_of_scenes(count: int, heldout: int) -> None:
    lines = [_line(scene, "ST_Syn_001_01", str(i)) for i, scene in enumerate(_scenes(count))]
    assert len(split(lines).heldout) == heldout


def test_split_partitions_every_scene_exactly_once() -> None:
    scenes = _scenes(12)
    parts = split([_line(scene, "ST_Syn_001_01", str(i)) for i, scene in enumerate(scenes)])
    assert parts.train | parts.heldout == frozenset(scenes)
    assert not parts.train & parts.heldout


def test_split_takes_heldout_from_the_front_of_the_sha256_order_regardless_of_input_order() -> None:
    scenes = _scenes(12)
    forward = [_line(scene, "ST_Syn_001_01", str(i)) for i, scene in enumerate(scenes)]
    forward += [_line(scene, "ST_Syn_001_01", "extra", order=1) for scene in scenes]
    parts = split(forward)
    assert parts == split(list(reversed(forward)))
    assert parts.heldout == frozenset(_sha_order(scenes)[:1])


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Made-up Line, here!", "made up line here"),
        ("  made-up\tLINE   here\n", "made up line here"),
        ("\uff2d\uff41\uff44\uff45-up line", "made up line"),
    ],
)
def test_normalize_folds_case_punctuation_width_and_spacing(left: str, right: str) -> None:
    assert normalize(left) == right


def test_normalize_keeps_distinct_text_distinct() -> None:
    assert normalize("Made-up line one.") != normalize("Made-up line two.")


def test_english_maps_namespace_and_key_to_english_text(fake_vanilla: Path) -> None:
    table = english()
    assert len(table) == 4
    assert table[("ST_C01_001_01", "10")] == "Synthetic line ten."
    assert table[("ST_C03_002_01", "1")] == "Same in both."


def _fixture() -> tuple[list[SceneLine], dict[tuple[str, str], str]]:
    """Ten scenes, one filler line each. The first train scene holds the shared line, and the held-out scene holds
    a case, punctuation and spacing variant of it, a distinct line and a duplicate of that distinct line."""
    scenes = _scenes(10)
    base = [_line(scene, f"ST_Filler_{i:02d}", "1") for i, scene in enumerate(scenes)]
    texts = {(line.namespace, line.key): f"Made-up {TEXT} filler {i}. Not game text." for i, line in enumerate(base)}
    parts = split(base)
    train_scene = min(parts.train)
    held_scene = min(parts.heldout)
    texts[SHARED_TRAIN] = "Made-up shared line. Not game text."
    texts[SHARED_HELD] = "made-up SHARED line, not GAME text!"
    texts[DISTINCT_HELD] = "Made-up distinct line. Not game text."
    texts[DISTINCT_HELD_DUPLICATE] = "Made-up distinct line. Not game text."
    lines = [
        *base,
        _line(train_scene, *SHARED_TRAIN, order=1),
        _line(held_scene, *SHARED_HELD, order=1),
        _line(held_scene, *DISTINCT_HELD, order=2),
        _line(held_scene, *DISTINCT_HELD_DUPLICATE, order=3),
    ]
    return lines, texts


def test_excluded_drops_heldout_lines_whose_text_recurs_in_a_training_scene() -> None:
    lines, texts = _fixture()
    assert excluded(lines, texts, split(lines)) == frozenset({SHARED_HELD})


def test_excluded_names_a_line_missing_from_texts() -> None:
    lines = [_line(scene, "ST_Syn_003_01", "1") for scene in _scenes(10)]
    with pytest.raises(Fail, match=re.escape("ST_Syn_003_01/1")):
        excluded(lines, {}, split(lines))


def test_check_reports_counts_in_order_and_no_account_lines_without_calls_file(tmp_path: Path) -> None:
    lines, texts = _fixture()
    assert check(lines, texts, tmp_path) == [
        "scenes 10",
        "train scenes 9",
        "heldout scenes 1",
        "train lines 10",
        "heldout lines 3",
        "excluded lines 1",
    ]


def test_check_counts_calls_per_account_ascending_and_prints_no_text_or_speaker(tmp_path: Path) -> None:
    lines, texts = _fixture()
    (tmp_path / CALLS).write_text(
        '{"scene": "made-up-scene-00", "slot": 1, "model": "made-up-model", "account": 10, "outcome": "ok"}\n'
        '{"scene": "made-up-scene-01", "slot": 2, "model": "made-up-model", "account": 2, "outcome": "ok"}\n'
        '{"scene": "made-up-scene-02", "slot": 3, "model": "made-up-model", "account": 2, "outcome": "failed"}\n',
        encoding="utf-8",
    )
    report = check(lines, texts, tmp_path)
    assert report[-2:] == ["account 2 calls 2", "account 10 calls 1"]
    assert len(report) == 8
    assert "SENTINEL" not in "\n".join(report)


@pytest.mark.parametrize(
    "bad",
    [
        "not json SENTINEL",
        '["SENTINEL"]',
        '{"scene": "SENTINEL", "slot": 1}',
        '{"scene": "SENTINEL", "account": "SENTINEL"}',
        '{"scene": "SENTINEL", "account": true}',
    ],
)
def test_check_rejects_a_malformed_calls_line_naming_file_and_line_only(tmp_path: Path, bad: str) -> None:
    lines, texts = _fixture()
    (tmp_path / CALLS).write_text('{"scene": "made-up-scene-00", "account": 1}\n' + bad + "\n", encoding="utf-8")
    with pytest.raises(Fail) as info:
        check(lines, texts, tmp_path)
    message = str(info.value)
    assert f"{CALLS}:2" in message
    assert "SENTINEL" not in message


def test_check_reports_an_unreadable_calls_file_as_fail(tmp_path: Path) -> None:
    lines, texts = _fixture()
    (tmp_path / CALLS).mkdir()
    with pytest.raises(Fail, match=re.escape(f"cannot read calls {tmp_path / CALLS}")):
        check(lines, texts, tmp_path)


def test_check_reports_a_calls_file_that_is_not_utf8_as_fail(tmp_path: Path) -> None:
    lines, texts = _fixture()
    (tmp_path / CALLS).write_bytes(b'{"account": 1, "scene": "\xff"}\n')
    with pytest.raises(Fail, match=re.escape(f"{tmp_path / CALLS}: is not valid UTF-8")):
        check(lines, texts, tmp_path)


# Stands in for cswap. `cswap run <n> -- <args>` exports the account and execs claude with <args>, as cswap does.
# Any other call is `cswap list --json` and prints the listing the test wrote. Every call logs its arguments first.
FAKE_CSWAP = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_CSWAP_LOG"
if [ "$1" = "run" ] && [ "$3" = "--" ]; then
  CSWAP_ACCOUNT="$2"
  export CSWAP_ACCOUNT
  shift 3
  exec claude "$@"
fi
cat "$FAKE_CSWAP_LISTING"
"""

# Stands in for claude. Logs the account, argv and stdin of each call, sleeps for the seconds the test set, then answers
# the ids in the scene it was sent. The test's mode picks the answer: "answer" paraphrases every id, "blank" blanks the
# first paraphrase, "blank-scene-00" blanks the first paraphrase only for calls about made-up-scene-00, "gap" leaves out
# the last id, and "unfinished" answers with an error result.
FAKE_CLAUDE = """#!PYTHON
import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["FAKE_CLAUDE_DIR"])
account = int(os.environ["CSWAP_ACCOUNT"])
stdin = sys.stdin.read()
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"account": account, "argv": sys.argv[1:], "stdin": stdin}) + "\\n")
spec = json.loads((root / "behavior.json").read_text())
time.sleep(spec["seconds"])
if spec["mode"] == "unfinished":
    document = {"type": "result", "subtype": "error_max_turns", "is_error": True, "api_error_status": None}
else:
    ids = [line["id"] for line in json.loads(stdin.rsplit("\\n\\n", 1)[-1])["lines"]]
    rows = [{"id": n, "paraphrase": f"Made-up paraphrase {n}. Not game text."} for n in ids]
    if spec["mode"] == "blank":
        rows[0]["paraphrase"] = " "
    if spec["mode"] == "blank-scene-00" and "made-up-scene-00" in stdin:
        rows[0]["paraphrase"] = " "
    if spec["mode"] == "gap":
        rows = rows[:-1]
    document = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "structured_output": {"lines": rows},
    }
sys.stdout.write(json.dumps(document))
"""

EXPECTED_MODELS = ("opus", "opus", "sonnet", "haiku")
PAIR_FIELDS = {"scene", "namespace", "key", "speaker", "slot", "model", "original", "paraphrase"}
PAIR_ROW = pairs.Pair(
    scene="made-up-scene-00",
    namespace="ST_Syn_005_01",
    key="1",
    speaker="made-up-speaker",
    slot=0,
    model="opus",
    original="Made-up original. Not game text.",
    paraphrase="Made-up paraphrase. Not game text.",
)


class Fakes:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.listing_file = root / "listing.json"
        self.cswap_log = root / "cswap.log"
        self.claude_dir = root / "claude"
        self.claude_dir.mkdir()
        bindir = root / "bin"
        bindir.mkdir()
        for name, text in (("cswap", FAKE_CSWAP), ("claude", FAKE_CLAUDE.replace("#!PYTHON", f"#!{sys.executable}"))):
            exe = bindir / name
            exe.write_text(text)
            exe.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_CSWAP_LISTING", str(self.listing_file))
        monkeypatch.setenv("FAKE_CSWAP_LOG", str(self.cswap_log))
        monkeypatch.setenv("FAKE_CLAUDE_DIR", str(self.claude_dir))
        self.behave()

    def accounts(self, *rows: tuple[int, float]) -> None:
        listing = {"accounts": [{"number": n, "usageStatus": "ok", "usage": {"sevenDay": {"pct": pct}}} for n, pct in rows]}
        self.listing_file.write_text(json.dumps(listing))

    def behave(self, mode: str = "answer", seconds: float = 0.0) -> None:
        (self.claude_dir / "behavior.json").write_text(json.dumps({"mode": mode, "seconds": seconds}))

    def claude_calls(self) -> list[dict[str, Any]]:
        log = self.claude_dir / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def cswap_runs(self) -> list[str]:
        if not self.cswap_log.exists():
            return []
        return [line for line in self.cswap_log.read_text().splitlines() if line.startswith("run ")]


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    return Fakes(tmp_path, monkeypatch)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _model(argv: list[str]) -> str:
    return argv[argv.index("--model") + 1]


def _small() -> tuple[list[SceneLine], dict[tuple[str, str], str]]:
    """Three scenes of two made-up lines each. Each text names its scene, so a call's stdin shows the scene it answers."""
    lines = [_line(scene, "ST_Syn_004_01", f"{scene}-{i}", order=i) for scene in _scenes(3) for i in range(2)]
    texts = {(line.namespace, line.key): f"Made-up line {line.order} of {line.scene}. Not game text." for line in lines}
    return lines, texts


def test_run_makes_one_call_per_scene_and_slot_under_the_account_choose_picked(fakes: Fakes, tmp_path: Path) -> None:
    lines, texts = _small()
    fakes.accounts((3, 10.0), (7, 50.0))
    root = tmp_path / "work"
    assert pairs.run(lines, texts, root) == [
        "calls 12",
        "resumed 0",
        "failed 0",
        "train pairs 24",
        "heldout pairs 0",
        "account 3 calls 12",
    ]
    assert len(fakes.cswap_runs()) == 12
    assert all(line.startswith("run 3 -- -p --model ") for line in fakes.cswap_runs())
    for scene in _scenes(3):
        models = [_model(call["argv"]) for call in fakes.claude_calls() if scene in call["stdin"]]
        assert sorted(models) == sorted(EXPECTED_MODELS)
    rows = _rows(root / CALLS)
    assert {(row["scene"], row["slot"]) for row in rows} == {(scene, slot) for scene in _scenes(3) for slot in range(4)}
    assert all(row["model"] == EXPECTED_MODELS[row["slot"]] for row in rows)
    assert all(row["account"] == 3 and row["outcome"] == "ok" for row in rows)


def test_rerun_makes_no_calls_and_counts_saved_results_resumed(fakes: Fakes, tmp_path: Path) -> None:
    lines, texts = _small()
    fakes.accounts((3, 10.0))
    root = tmp_path / "work"
    pairs.run(lines, texts, root)
    made = len(fakes.claude_calls())
    assert pairs.run(lines, texts, root) == ["calls 0", "resumed 12", "failed 0", "train pairs 24", "heldout pairs 0"]
    assert len(fakes.claude_calls()) == made


@pytest.mark.parametrize(("mode", "outcome"), [("blank", "rejected"), ("gap", "rejected"), ("unfinished", "unfinished")])
def test_a_call_with_an_unusable_answer_is_failed_saved_nowhere_and_retried_on_rerun(
    fakes: Fakes, tmp_path: Path, mode: str, outcome: str
) -> None:
    lines, texts = _small()
    fakes.accounts((3, 10.0))
    fakes.behave(mode)
    root = tmp_path / "work"
    assert pairs.run(lines, texts, root) == [
        "calls 12",
        "resumed 0",
        "failed 12",
        "train pairs 0",
        "heldout pairs 0",
        "account 3 calls 12",
    ]
    assert Store(root).finished() == []
    assert len(Store(root).unfinished()) == 12
    assert {row["outcome"] for row in _rows(root / CALLS)} == {outcome}
    fakes.behave("answer")
    assert pairs.run(lines, texts, root) == [
        "calls 12",
        "resumed 0",
        "failed 0",
        "train pairs 24",
        "heldout pairs 0",
        "account 3 calls 12",
    ]
    assert Store(root).unfinished() == []


def test_eight_slow_calls_overlap(fakes: Fakes, tmp_path: Path) -> None:
    lines, texts = _small()
    fakes.accounts((3, 10.0))
    fakes.behave(seconds=1.0)
    started = time.monotonic()
    report = pairs.run(lines, texts, tmp_path / "work", limit=2)
    elapsed = time.monotonic() - started
    assert report[0] == "calls 8"
    assert len(fakes.claude_calls()) == 8
    assert elapsed < 4.0


def test_workers_of_one_runs_calls_in_series(fakes: Fakes, tmp_path: Path) -> None:
    lines, texts = _small()
    fakes.accounts((3, 10.0))
    fakes.behave(seconds=0.25)
    started = time.monotonic()
    assert pairs.run(lines, texts, tmp_path / "work", limit=1, workers=1)[0] == "calls 4"
    assert time.monotonic() - started >= 1.0


def test_pair_files_hold_the_pair_fields_omit_excluded_heldout_lines_and_read_back(fakes: Fakes, tmp_path: Path) -> None:
    lines, texts = _fixture()
    fakes.accounts((3, 10.0))
    root = tmp_path / "work"
    assert pairs.run(lines, texts, root) == [
        "calls 40",
        "resumed 0",
        "failed 0",
        "train pairs 40",
        "heldout pairs 12",
        "account 3 calls 40",
    ]
    for name in (pairs.TRAIN, pairs.HELDOUT):
        assert all(set(row) == PAIR_FIELDS for row in _rows(root / name))
    parts = split(lines)
    train = pairs.read(pairs.TRAIN, root)
    assert {p.scene for p in train} == parts.train
    heldout = pairs.read(pairs.HELDOUT, root)
    assert {p.scene for p in heldout} == parts.heldout
    assert SHARED_HELD not in {(p.namespace, p.key) for p in heldout}
    held_scene = min(parts.heldout)
    kept = [(line.namespace, line.key) for line in lines if line.scene == held_scene and (line.namespace, line.key) != SHARED_HELD]
    assert [(p.namespace, p.key) for p in heldout if p.slot == 0] == kept
    first = heldout[0]
    assert first == pairs.Pair(
        scene=held_scene,
        namespace=kept[0][0],
        key=kept[0][1],
        speaker=SPEAKER,
        slot=0,
        model="opus",
        original=texts[kept[0]],
        paraphrase="Made-up paraphrase 0. Not game text.",
    )


def test_limit_calls_only_the_first_scene_and_a_full_rerun_still_reads_its_results(fakes: Fakes, tmp_path: Path) -> None:
    lines, texts = _fixture()
    first = lines[0].scene
    fakes.accounts((3, 10.0))
    root = tmp_path / "work"
    assert pairs.run(lines, texts, root, limit=1)[0] == "calls 4"
    assert {row["scene"] for row in _rows(root / CALLS)} == {first}
    assert pairs.run(lines, texts, root) == [
        "calls 36",
        "resumed 4",
        "failed 0",
        "train pairs 40",
        "heldout pairs 12",
        "account 3 calls 36",
    ]


@pytest.mark.parametrize("argv", [["pairs"], ["pairs", "--check"], ["pairs", "--limit", "1"]])
def test_register_parses_check_and_limit_and_defaults_to_a_full_run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    pairs.register(parser.add_subparsers(required=True))
    args = parser.parse_args(argv)
    assert args.check == ("--check" in argv)
    assert args.limit == (1 if "--limit" in argv else None)


@pytest.mark.parametrize(
    "argv", [["pairs", "--check", "--limit", "1"], ["pairs", "--limit", "0"], ["pairs", "--limit", "-2"], ["pairs", "--limit", "one"]]
)
def test_register_rejects_check_with_limit_and_a_limit_that_is_not_positive(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    pairs.register(parser.add_subparsers(required=True))
    with pytest.raises(SystemExit):
        parser.parse_args(argv)


@pytest.mark.parametrize("argv", [["pairs", "--check"], ["pairs", "--limit", "1"]])
def test_pairs_command_prints_counts_only(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    lines, texts = _fixture()
    fakes.accounts((3, 10.0))
    root = tmp_path / "work"
    monkeypatch.setattr(scenes, "load", lambda: lines)
    monkeypatch.setattr(pairs, "english", lambda: texts)
    monkeypatch.setattr(pairs, "work_dir", lambda lens: root)
    parser = argparse.ArgumentParser()
    pairs.register(parser.add_subparsers(required=True))
    args = parser.parse_args(argv)
    args.fn(args)
    out = capsys.readouterr().out
    assert out
    assert all(re.fullmatch(r"[a-z ]+\d+( calls \d+)?", line) for line in out.splitlines())
    assert "SENTINEL" not in out


def _command(fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> argparse.Namespace:
    lines, texts = _small()
    monkeypatch.setattr(scenes, "load", lambda: lines)
    monkeypatch.setattr(pairs, "english", lambda: texts)
    monkeypatch.setattr(pairs, "work_dir", lambda lens: tmp_path / "work")
    parser = argparse.ArgumentParser()
    pairs.register(parser.add_subparsers(required=True))
    return parser.parse_args(argv)


@pytest.mark.parametrize(("argv", "calls"), [(["pairs"], 12), (["pairs", "--limit", "1"], 4)])
def test_pairs_command_prints_its_report_then_fails_when_every_call_failed(
    fakes: Fakes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    calls: int,
) -> None:
    fakes.accounts((3, 10.0))
    fakes.behave("blank")
    args = _command(fakes, tmp_path, monkeypatch, argv)
    with pytest.raises(Fail, match=re.escape(f"all {calls} calls failed")):
        args.fn(args)
    out = capsys.readouterr().out.splitlines()
    assert f"calls {calls}" in out
    assert f"failed {calls}" in out
    assert "SENTINEL" not in "\n".join(out)


def test_pairs_command_does_not_fail_when_some_calls_succeeded(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.accounts((3, 10.0))
    fakes.behave("blank-scene-00")
    args = _command(fakes, tmp_path, monkeypatch, ["pairs"])
    args.fn(args)
    out = capsys.readouterr().out.splitlines()
    assert "calls 12" in out
    assert "failed 4" in out


def test_pairs_command_does_not_fail_when_every_call_was_resumed(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.accounts((3, 10.0))
    args = _command(fakes, tmp_path, monkeypatch, ["pairs"])
    args.fn(args)
    capsys.readouterr()
    args.fn(args)
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == ["calls 0", "resumed 12", "failed 0"]


def test_read_missing_file_raises_fail_naming_the_run_command(tmp_path: Path) -> None:
    with pytest.raises(Fail) as info:
        pairs.read(pairs.TRAIN, tmp_path)
    assert str(info.value) == f"no pairs at {tmp_path / pairs.TRAIN}; run `ffrd voice pairs`"


@pytest.mark.parametrize(
    "bad",
    [
        "not json SENTINEL",
        '["SENTINEL"]',
        '{"scene": "SENTINEL"}',
        json.dumps({**asdict(PAIR_ROW), "slot": "SENTINEL"}),
        json.dumps({**asdict(PAIR_ROW), "extra": "SENTINEL"}),
    ],
)
def test_read_malformed_row_raises_fail_naming_file_and_line_only(tmp_path: Path, bad: str) -> None:
    (tmp_path / pairs.TRAIN).write_text(json.dumps(asdict(PAIR_ROW)) + "\n" + bad + "\n", encoding="utf-8")
    with pytest.raises(Fail) as info:
        pairs.read(pairs.TRAIN, tmp_path)
    message = str(info.value)
    assert f"{tmp_path / pairs.TRAIN}:2" in message
    assert "SENTINEL" not in message
