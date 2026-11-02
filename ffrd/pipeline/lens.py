"""Load a lens TOML file (lenses/<name>.toml) into a validated, typed Lens."""

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ffrd.game import Fail

Stage = Literal["triage", "screen", "intent", "judge", "scene"]

ID_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
SECTIONS = frozenset({"triage", "screen", "intent", "judge", "scene"})
SCENE_KEYS = frozenset({"patterns", "criteria", "plan", "rubric"})


@dataclass(frozen=True)
class TriageQuestion:
    id: str
    question: str


@dataclass(frozen=True)
class ScenePattern:
    id: str
    question: str


@dataclass(frozen=True)
class SceneSection:
    patterns: tuple[ScenePattern, ...]
    criteria: tuple[str, ...]
    plan: str
    rubric: str


@dataclass(frozen=True)
class Lens:
    name: str
    triage: tuple[TriageQuestion, ...]
    screen_criteria: tuple[str, ...]
    intent_rule: str
    judge_rubric: str
    scene: SceneSection | None = None

    def stage_text(self, stage: Stage) -> str:
        section: object
        match stage:
            case "triage":
                section = [{"id": q.id, "question": q.question} for q in self.triage]
            case "screen":
                section = {"criteria": list(self.screen_criteria)}
            case "intent":
                section = {"rule": self.intent_rule}
            case "judge":
                section = {"rubric": self.judge_rubric}
            case "scene":
                if self.scene is None:
                    raise Fail(f"lens {self.name} has no [scene] section")
                section = {
                    "criteria": list(self.scene.criteria),
                    "patterns": [{"id": p.id, "question": p.question} for p in self.scene.patterns],
                    "plan": self.scene.plan,
                    "rubric": self.scene.rubric,
                }
        return json.dumps(section, ensure_ascii=False, sort_keys=True)


class _Report:
    def __init__(self, file: str) -> None:
        self.file = file
        self.errors: list[str] = []

    def add(self, field: str, problem: str) -> None:
        self.errors.append(f"{self.file}: {field} {problem}")


def _path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _unknown_keys(table: dict[str, object], allowed: frozenset[str], prefix: str, report: _Report) -> None:
    for key in table:
        if key not in allowed:
            report.add(_path(prefix, key), "is an unknown key")


def _lookup(table: dict[str, object], key: str, prefix: str, report: _Report) -> object | None:
    if key not in table:
        report.add(_path(prefix, key), "is missing")
        return None
    return table[key]


def _text(table: dict[str, object], key: str, prefix: str, report: _Report) -> str:
    value = _lookup(table, key, prefix, report)
    if isinstance(value, str) and value.strip():
        return value
    if value is not None:
        report.add(_path(prefix, key), "must be a non-empty string")
    return ""


def _section(data: dict[str, object], name: str, allowed: frozenset[str], report: _Report) -> dict[str, object] | None:
    raw = _lookup(data, name, "", report)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        report.add(name, "must be a table")
        return None
    _unknown_keys(raw, allowed, name, report)
    return raw


def _triage(data: dict[str, object], report: _Report) -> tuple[tuple[TriageQuestion, ...], dict[str, int]]:
    raw = _lookup(data, "triage", "", report)
    if raw is None:
        return (), {}
    if not isinstance(raw, list) or not raw:
        report.add("triage", "must be one or more tables")
        return (), {}
    questions: list[TriageQuestion] = []
    first_index: dict[str, int] = {}
    for index, item in enumerate(raw):
        where = f"triage[{index}]"
        if not isinstance(item, dict):
            report.add(where, "must be a table")
            continue
        _unknown_keys(item, frozenset({"id", "question"}), where, report)
        qid = _lookup(item, "id", where, report)
        if isinstance(qid, str) and ID_PATTERN.fullmatch(qid):
            if qid in first_index:
                report.add(f"{where}.id", f"duplicates triage[{first_index[qid]}].id")
            else:
                first_index[qid] = index
        elif qid is not None:
            report.add(f"{where}.id", "must be lowercase letters, digits and single hyphens, like made-up-id")
        question = _text(item, "question", where, report)
        questions.append(TriageQuestion(id=qid if isinstance(qid, str) else "", question=question))
    return tuple(questions), first_index


def _patterns(section: dict[str, object], triage_index: dict[str, int], report: _Report) -> tuple[ScenePattern, ...]:
    raw = _lookup(section, "patterns", "scene", report)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        report.add("scene.patterns", "must be one or more tables")
        return ()
    patterns: list[ScenePattern] = []
    first_index: dict[str, int] = {}
    for index, item in enumerate(raw):
        where = f"scene.patterns[{index}]"
        if not isinstance(item, dict):
            report.add(where, "must be a table")
            continue
        _unknown_keys(item, frozenset({"id", "question"}), where, report)
        pid = _lookup(item, "id", where, report)
        if isinstance(pid, str) and ID_PATTERN.fullmatch(pid):
            if pid in first_index:
                report.add(f"{where}.id", f"duplicates scene.patterns[{first_index[pid]}].id")
            elif pid in triage_index:
                report.add(f"{where}.id", f"duplicates triage[{triage_index[pid]}].id")
            else:
                first_index[pid] = index
        elif pid is not None:
            report.add(f"{where}.id", "must be lowercase letters, digits and single hyphens, like made-up-id")
        question = _text(item, "question", where, report)
        patterns.append(ScenePattern(id=pid if isinstance(pid, str) else "", question=question))
    return tuple(patterns)


def _criteria(section: dict[str, object], prefix: str, report: _Report) -> tuple[str, ...]:
    raw = _lookup(section, "criteria", prefix, report)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        report.add(f"{prefix}.criteria", "must be a non-empty list")
        return ()
    criteria: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, str) and item.strip():
            criteria.append(item)
        else:
            report.add(f"{prefix}.criteria[{index}]", "must be a non-empty string")
    return tuple(criteria)


def _scene(data: dict[str, object], triage_index: dict[str, int], report: _Report) -> SceneSection | None:
    if "scene" not in data:
        return None
    section = _section(data, "scene", SCENE_KEYS, report)
    if section is None:
        return None
    patterns = _patterns(section, triage_index, report)
    criteria = _criteria(section, "scene", report)
    plan = _text(section, "plan", "scene", report)
    rubric = _text(section, "rubric", "scene", report)
    return SceneSection(patterns=patterns, criteria=criteria, plan=plan, rubric=rubric)


def load_lens(path: Path) -> Lens:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise Fail(f"{path}: cannot read as TOML: {e}") from e

    report = _Report(str(path))
    _unknown_keys(data, SECTIONS, "", report)
    triage, triage_index = _triage(data, report)
    screen = _section(data, "screen", frozenset({"criteria"}), report)
    criteria = _criteria(screen, "screen", report) if screen is not None else ()
    intent = _section(data, "intent", frozenset({"rule"}), report)
    rule = _text(intent, "rule", "intent", report) if intent is not None else ""
    judge = _section(data, "judge", frozenset({"rubric"}), report)
    rubric = _text(judge, "rubric", "judge", report) if judge is not None else ""
    scene = _scene(data, triage_index, report)

    if report.errors:
        raise Fail("\n".join(report.errors))
    return Lens(
        name=path.stem,
        triage=triage,
        screen_criteria=criteria,
        intent_rule=rule,
        judge_rubric=rubric,
        scene=scene,
    )
