"""Load a lens TOML file (lenses/<name>.toml) into a validated, typed Lens."""

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ffrd.game import Fail

Stage = Literal["triage", "screen", "intent", "judge"]
STAGES: tuple[Stage, ...] = ("triage", "screen", "intent", "judge")

ID_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
SECTIONS = frozenset({"triage", "screen", "intent", "judge"})


@dataclass(frozen=True)
class TriageQuestion:
    id: str
    question: str


@dataclass(frozen=True)
class Lens:
    name: str
    triage: tuple[TriageQuestion, ...]
    screen_criteria: tuple[str, ...]
    intent_rule: str
    judge_rubric: str

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


def _triage(data: dict[str, object], report: _Report) -> tuple[TriageQuestion, ...]:
    raw = _lookup(data, "triage", "", report)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        report.add("triage", "must be one or more tables")
        return ()
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
    return tuple(questions)


def _criteria(section: dict[str, object], report: _Report) -> tuple[str, ...]:
    raw = _lookup(section, "criteria", "screen", report)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        report.add("screen.criteria", "must be a non-empty list")
        return ()
    criteria: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, str) and item.strip():
            criteria.append(item)
        else:
            report.add(f"screen.criteria[{index}]", "must be a non-empty string")
    return tuple(criteria)


def load_lens(path: Path) -> Lens:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise Fail(f"{path}: cannot read as TOML: {e}") from e

    report = _Report(str(path))
    _unknown_keys(data, SECTIONS, "", report)
    triage = _triage(data, report)
    screen = _section(data, "screen", frozenset({"criteria"}), report)
    criteria = _criteria(screen, report) if screen is not None else ()
    intent = _section(data, "intent", frozenset({"rule"}), report)
    rule = _text(intent, "rule", "intent", report) if intent is not None else ""
    judge = _section(data, "judge", frozenset({"rubric"}), report)
    rubric = _text(judge, "rubric", "judge", report) if judge is not None else ""

    if report.errors:
        raise Fail("\n".join(report.errors))
    return Lens(name=path.stem, triage=triage, screen_criteria=criteria, intent_rule=rule, judge_rubric=rubric)
