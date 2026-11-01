"""pairs: the held-out split, the DL5 exclusion and the counts-only check report, over made-up scenes and lines.

Made-up scene, namespace, key, speaker and text values. Not game text.
"""

import hashlib
import re
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.scenes import SceneLine
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
