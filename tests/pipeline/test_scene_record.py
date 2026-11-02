import errno
import json
import os
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.pipeline.scene_record import SCENES, SceneRecord, read_scenes, write_scenes

# Made-up scene records. Not game text.
FIRST = SceneRecord(
    scene="made-up-scene-one",
    chapter=1,
    eligible=True,
    patterns=("made-up-pattern-one", "made-up-pattern-two"),
    plan="made-up-plan-one",
    keep_reason=None,
    planned=4,
    required=2,
    changed=2,
    reverted=1,
    replanned=0,
    gate_failures={"made-up-gate-one": 1, "made-up-gate_two": 2},
)
SECOND = SceneRecord(
    scene="made-up-scene-two",
    chapter=None,
    eligible=False,
    patterns=(),
    plan=None,
    keep_reason="plot",
    planned=0,
    required=0,
    changed=0,
    reverted=0,
    replanned=0,
    gate_failures={},
)
EXPECTED_KEYS = {
    "scene",
    "chapter",
    "eligible",
    "patterns",
    "plan",
    "keep_reason",
    "planned",
    "required",
    "changed",
    "reverted",
    "replanned",
    "gate_failures",
}


def test_round_trip_reads_back_equal_records(tmp_path: Path) -> None:
    path = tmp_path / SCENES
    write_scenes(path, [FIRST, SECOND])
    assert read_scenes(path) == [FIRST, SECOND]


def test_each_line_has_exactly_the_twelve_keys_and_list_and_object_shapes(tmp_path: Path) -> None:
    path = tmp_path / SCENES
    write_scenes(path, [FIRST, SECOND])
    objects = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [set(obj) for obj in objects] == [EXPECTED_KEYS, EXPECTED_KEYS]
    assert objects[0]["patterns"] == ["made-up-pattern-one", "made-up-pattern-two"]
    assert objects[0]["gate_failures"] == {"made-up-gate-one": 1, "made-up-gate_two": 2}
    assert objects[1]["patterns"] == []
    assert objects[1]["gate_failures"] == {}


def test_order_is_kept_and_no_temp_file_remains(tmp_path: Path) -> None:
    path = tmp_path / SCENES
    write_scenes(path, [SECOND, FIRST])
    scenes = [json.loads(line)["scene"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert scenes == [SECOND.scene, FIRST.scene]
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [SCENES]


def test_rewrite_replaces_the_previous_contents(tmp_path: Path) -> None:
    path = tmp_path / SCENES
    write_scenes(path, [FIRST, SECOND])
    write_scenes(path, [SECOND])
    assert read_scenes(path) == [SECOND]
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [SCENES]


def test_non_ascii_text_is_written_verbatim(tmp_path: Path) -> None:
    # Made-up scene key with non-ASCII characters. Not game text.
    record = SceneRecord(
        scene="made-up-scene-合成",
        chapter=None,
        eligible=False,
        patterns=(),
        plan=None,
        keep_reason=None,
        planned=0,
        required=0,
        changed=0,
        reverted=0,
        replanned=0,
        gate_failures={},
    )
    path = tmp_path / SCENES
    write_scenes(path, [record])
    assert "made-up-scene-合成" in path.read_text(encoding="utf-8")


def test_write_failure_names_the_path(tmp_path: Path) -> None:
    blocker = tmp_path / "made-up-file"
    blocker.write_text("Made-up file in the way. Not game text.\n", encoding="utf-8")
    path = blocker / SCENES
    with pytest.raises(Fail) as raised:
        write_scenes(path, [FIRST])
    assert str(raised.value) == f"cannot write {path}: {os.strerror(errno.EEXIST)}"


def test_missing_file_names_the_path(tmp_path: Path) -> None:
    path = tmp_path / SCENES
    with pytest.raises(Fail) as raised:
        read_scenes(path)
    assert str(raised.value) == f"no scenes at {path}; run the lens first"


def test_unreadable_path_names_the_path(tmp_path: Path) -> None:
    path = tmp_path / SCENES
    path.mkdir()
    with pytest.raises(Fail) as raised:
        read_scenes(path)
    assert str(raised.value) == f"cannot read scenes {path}: {os.strerror(errno.EISDIR)}"


VALID = {
    "scene": "made-up-scene-one",
    "chapter": 1,
    "eligible": True,
    "patterns": ["made-up-pattern-one"],
    "plan": "made-up-plan-one",
    "keep_reason": None,
    "planned": 4,
    "required": 2,
    "changed": 2,
    "reverted": 1,
    "replanned": 0,
    "gate_failures": {"made-up-gate-one": 1},
}
VALID_LINE = json.dumps(VALID, ensure_ascii=False).encode("utf-8") + b"\n"


def _line(**fields: object) -> bytes:
    return json.dumps({**VALID, **fields}, ensure_ascii=False).encode("utf-8") + b"\n"


def _without(name: str) -> bytes:
    obj = {key: value for key, value in VALID.items() if key != name}
    return json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


BAD_LINES = [
    pytest.param(b"SENTINEL-NOT-JSON {\n", "is not valid JSON", id="not-json"),
    pytest.param(b'["SENTINEL-ARRAY"]\n', "is not a JSON object", id="not-object"),
    pytest.param(b"\xff\xfe\n", "is not valid UTF-8", id="not-utf-8"),
    pytest.param(_without("scene"), "field scene", id="scene-missing"),
    pytest.param(_line(scene=["SENTINEL-SCENE"]), "field scene", id="scene-type"),
    pytest.param(_line(chapter="SENTINEL-CHAPTER"), "field chapter", id="chapter-type"),
    pytest.param(_line(chapter=0), "field chapter", id="chapter-zero"),
    pytest.param(_line(chapter=True), "field chapter", id="chapter-bool"),
    pytest.param(_line(eligible="SENTINEL-ELIGIBLE"), "field eligible", id="eligible-type"),
    pytest.param(_line(eligible=1), "field eligible", id="eligible-int"),
    pytest.param(_line(patterns="SENTINEL-PATTERNS"), "field patterns", id="patterns-type"),
    pytest.param(_line(patterns=["SENTINEL Bad Pattern"]), "field patterns", id="pattern-not-id"),
    pytest.param(_line(plan=["SENTINEL-PLAN"]), "field plan", id="plan-type"),
    pytest.param(_line(keep_reason="SENTINEL-REASON"), "field keep_reason", id="keep-reason-unknown"),
    pytest.param(_line(planned="SENTINEL-PLANNED"), "field planned", id="planned-type"),
    pytest.param(_line(planned=-1), "field planned", id="planned-negative"),
    pytest.param(_line(planned=True), "field planned", id="planned-bool"),
    pytest.param(_line(required=5), "field required", id="required-above-planned"),
    pytest.param(_line(required=-1), "field required", id="required-negative"),
    pytest.param(_line(changed="SENTINEL-CHANGED"), "field changed", id="changed-type"),
    pytest.param(_line(changed=True), "field changed", id="changed-bool"),
    pytest.param(_line(reverted=-1), "field reverted", id="reverted-negative"),
    pytest.param(_line(replanned=True), "field replanned", id="replanned-bool"),
    pytest.param(_without("gate_failures"), "field gate_failures", id="gate-failures-missing"),
    pytest.param(_line(gate_failures=["SENTINEL-GATES"]), "field gate_failures", id="gate-failures-type"),
    pytest.param(_line(gate_failures={"SENTINEL Bad Gate": 1}), "field gate_failures", id="gate-failures-name"),
    pytest.param(_line(gate_failures={"made-up-gate": "SENTINEL-VALUE"}), "field gate_failures", id="gate-failures-value-type"),
    pytest.param(_line(gate_failures={"made-up-gate": -1}), "field gate_failures", id="gate-failures-value-negative"),
]


@pytest.mark.parametrize(("bad", "fragment"), BAD_LINES)
def test_invalid_line_fails_naming_file_line_and_field_but_not_the_value(tmp_path: Path, bad: bytes, fragment: str) -> None:
    # Line 1 is valid, so the bad line is line 2.
    path = tmp_path / SCENES
    path.write_bytes(VALID_LINE + bad)
    with pytest.raises(Fail) as raised:
        read_scenes(path)
    message = str(raised.value)
    assert f"{path}:2" in message
    assert fragment in message
    assert "SENTINEL" not in message
