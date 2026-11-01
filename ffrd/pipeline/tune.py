"""tune: pick the Jev triage threshold with zero Sonnet-positive misses on a sample of scenes, then save and load it per lens. Writes counts and the threshold, never scene text."""

import argparse
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ffrd.game import Fail, repo_root
from ffrd.pipeline import jev, stages
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.lens import Lens, load_lens
from ffrd.pipeline.scope import scoped
from ffrd.pipeline.stages import Ask, Scene, Triage, screen, triage
from ffrd.pipeline.store import Store, work_dir

SAMPLE = 100
THRESHOLD_FILE = "threshold.json"


@dataclass(frozen=True)
class Tuning:
    threshold: float
    scenes: int
    positives: int
    passing: int


@dataclass(frozen=True)
class _Scored:
    score: float
    positive: bool


def sample(scenes: Sequence[Scene], size: int = SAMPLE) -> list[Scene]:
    if len(scenes) <= size:
        return list(scenes)
    return [scenes[i * len(scenes) // size] for i in range(size)]


def _score(store: Store, lens: Lens, scene: Scene, jev: Triage, ask: Ask) -> _Scored | None:
    answer = triage(store, lens, scene, jev)
    if not isinstance(answer, dict):
        return None
    flagged = screen(store, lens, scene, ask)
    if isinstance(flagged, Unfinished | Rejected):
        return None
    return _Scored(score=max(answer[question.id] for question in lens.triage), positive=bool(flagged))


def tune(store: Store, lens: Lens, scenes: Sequence[Scene], jev: Triage, batch: Callable[[], Ask]) -> Tuning:
    chosen = sample(scenes)
    scored: list[_Scored] = []
    unfinished = 0
    for scene in chosen:
        result = _score(store, lens, scene, jev, batch())
        if result is None:
            unfinished += 1
        else:
            scored.append(result)
    if unfinished:
        raise Fail(f"{unfinished} of {len(chosen)} sampled scenes unfinished; rerun ffrd tune")
    positive_scores = [item.score for item in scored if item.positive]
    if not positive_scores:
        raise Fail("no sampled scene was flagged, so no threshold can be set")
    threshold = min(positive_scores)
    return Tuning(
        threshold=threshold,
        scenes=len(chosen),
        positives=len(positive_scores),
        passing=sum(1 for item in scored if item.score >= threshold),
    )


def save_threshold(directory: Path, tuning: Tuning) -> None:
    path = directory / THRESHOLD_FILE
    text = json.dumps(asdict(tuning)) + "\n"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{THRESHOLD_FILE}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise Fail(f"cannot write {path}: {e.strerror}") from e


def load_threshold(directory: Path) -> float:
    path = directory / THRESHOLD_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise Fail(f"no triage threshold at {path}; run `ffrd tune --lens <name>` or pass --no-triage") from e
    except OSError as e:
        raise Fail(f"cannot read triage threshold {path}: {e.strerror}") from e
    try:
        record = json.loads(raw)
    except ValueError as e:
        raise Fail(f"triage threshold {path} is not JSON") from e
    threshold = record.get("threshold") if isinstance(record, dict) else None
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not 0 <= threshold <= 1:
        raise Fail(f"triage threshold {path} is not a number from 0 to 1")
    return float(threshold)


def cmd_tune(args: argparse.Namespace) -> None:
    root = repo_root()
    lens = load_lens(root / "lenses" / f"{args.lens}.toml")
    directory = work_dir(args.lens)
    scenes = scoped(stages.bindings().scenes.scenes())
    tuning = tune(Store(directory), lens, scenes, jev.triage, lambda: stages.Batch().ask)
    save_threshold(directory, tuning)
    print(
        f"threshold {tuning.threshold:.4f}: {tuning.positives} of {tuning.scenes} sampled scenes flagged by the screen, "
        f"{tuning.passing} at or above it"
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("tune", help="set a lens's Jev triage threshold from Sonnet screen labels on up to 100 scenes")
    p.add_argument("--lens", required=True, help="name of the lens to tune, read from lenses/<name>.toml and saved under work/<name>/")
    p.set_defaults(fn=cmd_tune)
