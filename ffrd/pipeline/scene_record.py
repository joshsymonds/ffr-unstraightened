"""scene_record: the per-scene record a lens run writes to work/<lens>/scenes.jsonl. Counts and keys only, never line text."""

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ffrd.game import Fail
from ffrd.pipeline.lens import ID_PATTERN

SCENES = "scenes.jsonl"
SCENE_KEEP_REASONS = ("protection", "plot", "trait", "earned-arc", "not-recurring")
GATE = re.compile(r"[a-z0-9]+([-_][a-z0-9]+)*")
COUNTS = ("planned", "required", "changed", "reverted", "replanned")


@dataclass(frozen=True)
class SceneRecord:
    scene: str
    chapter: int | None
    eligible: bool
    patterns: tuple[str, ...]
    plan: str | None
    keep_reason: str | None
    planned: int
    required: int
    changed: int
    reverted: int
    replanned: int
    gate_failures: Mapping[str, int]


def _record_line(record: SceneRecord) -> str:
    obj = {
        "scene": record.scene,
        "chapter": record.chapter,
        "eligible": record.eligible,
        "patterns": list(record.patterns),
        "plan": record.plan,
        "keep_reason": record.keep_reason,
        "planned": record.planned,
        "required": record.required,
        "changed": record.changed,
        "reverted": record.reverted,
        "replanned": record.replanned,
        "gate_failures": dict(record.gate_failures),
    }
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise Fail(f"cannot write {path}: {e.strerror}") from e


def write_scenes(path: Path, records: Sequence[SceneRecord]) -> None:
    _write_atomic(path, "".join(_record_line(record) for record in records))


def _field(obj: dict[str, object], name: str, where: str) -> object:
    if name not in obj:
        raise Fail(f"{where}: field {name} is missing")
    return obj[name]


def _string(obj: dict[str, object], name: str, where: str) -> str:
    value = _field(obj, name, where)
    if not isinstance(value, str):
        raise Fail(f"{where}: field {name} must be a string")
    return value


def _nullable_string(obj: dict[str, object], name: str, where: str) -> str | None:
    value = _field(obj, name, where)
    if value is None or isinstance(value, str):
        return value
    raise Fail(f"{where}: field {name} must be a string or null")


def _chapter(obj: dict[str, object], where: str) -> int | None:
    value = _field(obj, "chapter", where)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Fail(f"{where}: field chapter must be a positive integer or null")
    return value


def _eligible(obj: dict[str, object], where: str) -> bool:
    value = _field(obj, "eligible", where)
    if not isinstance(value, bool):
        raise Fail(f"{where}: field eligible must be a boolean")
    return value


def _patterns(obj: dict[str, object], where: str) -> tuple[str, ...]:
    value = _field(obj, "patterns", where)
    if not isinstance(value, list) or not all(isinstance(p, str) and ID_PATTERN.fullmatch(p) for p in value):
        raise Fail(f"{where}: field patterns must be a list of pattern ids")
    return tuple(value)


def _keep_reason(obj: dict[str, object], where: str) -> str | None:
    value = _field(obj, "keep_reason", where)
    if value is None or (isinstance(value, str) and value in SCENE_KEEP_REASONS):
        return value
    raise Fail(f"{where}: field keep_reason must be a listed keep reason or null")


def _count(obj: dict[str, object], name: str, where: str) -> int:
    value = _field(obj, name, where)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Fail(f"{where}: field {name} must be a non-negative integer")
    return value


def _gate_failures(obj: dict[str, object], where: str) -> dict[str, int]:
    value = _field(obj, "gate_failures", where)
    if not isinstance(value, dict):
        raise Fail(f"{where}: field gate_failures must map gate names to non-negative integers")
    for gate, count in value.items():
        if not GATE.fullmatch(gate) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise Fail(f"{where}: field gate_failures must map gate names to non-negative integers")
    return value


def _parse(text: str, where: str) -> SceneRecord:
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise Fail(f"{where}: is not valid JSON") from e
    if not isinstance(obj, dict):
        raise Fail(f"{where}: is not a JSON object")
    counts = {name: _count(obj, name, where) for name in COUNTS}
    if counts["required"] > counts["planned"]:
        raise Fail(f"{where}: field required must not exceed planned")
    return SceneRecord(
        scene=_string(obj, "scene", where),
        chapter=_chapter(obj, where),
        eligible=_eligible(obj, where),
        patterns=_patterns(obj, where),
        plan=_nullable_string(obj, "plan", where),
        keep_reason=_keep_reason(obj, where),
        planned=counts["planned"],
        required=counts["required"],
        changed=counts["changed"],
        reverted=counts["reverted"],
        replanned=counts["replanned"],
        gate_failures=_gate_failures(obj, where),
    )


def read_scenes(path: Path) -> list[SceneRecord]:
    try:
        handle = path.open("rb")
    except FileNotFoundError as e:
        raise Fail(f"no scenes at {path}; run the lens first") from e
    except OSError as e:
        raise Fail(f"cannot read scenes {path}: {e.strerror}") from e
    records: list[SceneRecord] = []
    with handle:
        try:
            for lineno, raw in enumerate(handle, 1):
                where = f"{path}:{lineno}"
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as e:
                    raise Fail(f"{where}: is not valid UTF-8") from e
                records.append(_parse(text, where))
        except OSError as e:
            raise Fail(f"cannot read scenes {path}: {e.strerror}") from e
    return records
