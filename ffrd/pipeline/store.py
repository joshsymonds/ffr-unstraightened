"""Durable JSON result store: each stage result is saved as it lands and reused until its inputs or lens text change."""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ffrd.game import Fail, repo_root

KEY = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Result:
    key: str
    stage: str
    value: Any
    digest: str


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def work_dir(lens: str) -> Path:
    if not lens or lens in {".", ".."} or "/" in lens or "\\" in lens:
        raise Fail(f"lens name {lens!r} must be one plain path segment")
    return repo_root() / "work" / lens


def key(stage: str, inputs: Any, lens: str | None) -> str:
    return _sha256(_canonical({"stage": stage, "inputs": inputs, "lens": lens}))


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, stage: str, inputs: Any, lens: str | None, call: Callable[[], Any]) -> Result:
        k = key(stage, inputs, lens)
        saved = self.read(k)
        if saved is not None:
            return saved
        try:
            value = call()
            self._save(k, stage, value)
        except BaseException:
            self._mark_unfinished(k)
            raise
        result = self.read(k)
        if result is None:
            raise Fail(f"result {k} is missing right after it was saved")
        return result

    def read(self, key: str) -> Result | None:
        if not KEY.fullmatch(key):
            raise Fail(f"{key!r} is not a result key")
        path = self._record(key)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise Fail(f"cannot read result {path}: {e.strerror}") from e
        try:
            record = json.loads(text)
            stage = record["stage"]
            value = record["value"]
        except (ValueError, KeyError, TypeError) as e:
            raise Fail(f"result {path} is not a valid record") from e
        return Result(key=key, stage=stage, value=value, digest=_sha256(_canonical(value)))

    def finished(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "results").glob("*.json"))

    def unfinished(self) -> list[str]:
        marked = [p.name for p in (self.root / "unfinished").glob("*") if p.is_file()]
        return sorted(k for k in marked if not self._record(k).exists())

    def _record(self, k: str) -> Path:
        return self.root / "results" / f"{k}.json"

    def _marker(self, k: str) -> Path:
        return self.root / "unfinished" / k

    def _save(self, k: str, stage: str, value: Any) -> None:
        path = self._record(k)
        text = _canonical({"stage": stage, "value": value})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{k}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            self._marker(k).unlink(missing_ok=True)
        except OSError as e:
            raise Fail(f"cannot save result {path}: {e.strerror}") from e

    def _mark_unfinished(self, k: str) -> None:
        marker = self._marker(k)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as e:
            raise Fail(f"cannot mark {k} unfinished in {marker.parent}: {e.strerror}") from e
