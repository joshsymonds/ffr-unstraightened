"""bindings(): the scene source over the scene table and the DL38 namespaces, the voice classifier and the voice renderer.

Every line, speaker and key here is made up. Not game text. The remote calls are fakes, so no test reaches gnomon.
"""

import atexit
import json
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pylocres import Entry, LocresFile, Namespace

from ffrd import scenes
from ffrd.game import Fail
from ffrd.pipeline.stages import Scene, SceneLine, bindings
from ffrd.voice import classifier, remote
from ffrd.voice import render as voice_render
from ffrd.voice.evals import RENDER_SCORE_BAR

LOCRES = Path("FFRS/Content/Localization/Game")

# Made-up (namespace, key, English, Japanese) lines. Not game text.
LINES: tuple[tuple[str, str, str, str], ...] = (
    ("ST_C01_001_01", "1", "Synthetic line one. Not game text.", "合成一"),
    ("ST_C01_001_01", "2", "Synthetic line two. Not game text.", "合成二"),
    ("ST_C01_001_02", "1", "", "空"),
    ("ST_UnitDescription_01", "1", "Synthetic unit one. Not game text.", "合成ユニット一"),
    ("ST_UnitDescription_01", "2", "Synthetic unit two. Not game text.", "合成ユニット二"),
    ("ST_DialogChoice_01", "10", "Synthetic choice ten. Not game text.", "合成選択十"),
    ("ST_DialogChoice_01", "2", "Synthetic choice two. Not game text.", "合成選択二"),
    ("ST_MainSummary_01", "1", "Synthetic summary. Not game text.", "合成要約"),
    ("ST_DialogChoiceX_01", "1", "Synthetic line outside the family. Not game text.", "合成外"),
    ("ST_Other_01", "1", "Synthetic line in no family. Not game text.", "合成他"),
)

# Made-up scene table rows in the order the table writer sorts them. Not game text.
TABLE: tuple[dict[str, Any], ...] = (
    {"scene": "SynScene2", "order": 0, "namespace": "ST_C01_001_01", "key": "1", "speaker": "Synthetic speaker A", "voice": None},
    {
        "scene": "SynScene2",
        "order": 1,
        "namespace": "ST_UnitDescription_01",
        "key": "1",
        "speaker": None,
        "voice": {"sheet": "SynSheet", "index": 3},
    },
    {"scene": "SynScene10", "order": 0, "namespace": "ST_C01_001_01", "key": "2", "speaker": "Synthetic speaker B", "voice": None},
)

# Made-up expected scenes, with the synthetic English and Japanese above. Not game text.
EXPECTED: tuple[Scene, ...] = (
    Scene(
        key="SynScene2",
        lines=(
            SceneLine("ST_C01_001_01", "1", "Synthetic speaker A", "Synthetic line one. Not game text.", "合成一", 1),
            SceneLine("ST_UnitDescription_01", "1", None, "Synthetic unit one. Not game text.", "合成ユニット一", None),
        ),
    ),
    Scene(
        key="SynScene10",
        lines=(SceneLine("ST_C01_001_01", "2", "Synthetic speaker B", "Synthetic line two. Not game text.", "合成二", 1),),
    ),
    Scene(
        key="ST_DialogChoice_01",
        lines=(
            SceneLine("ST_DialogChoice_01", "2", None, "Synthetic choice two. Not game text.", "合成選択二", None),
            SceneLine("ST_DialogChoice_01", "10", None, "Synthetic choice ten. Not game text.", "合成選択十", None),
        ),
    ),
    Scene(
        key="ST_MainSummary_01",
        lines=(SceneLine("ST_MainSummary_01", "1", None, "Synthetic summary. Not game text.", "合成要約", None),),
    ),
    Scene(
        key="ST_UnitDescription_01",
        lines=(SceneLine("ST_UnitDescription_01", "2", None, "Synthetic unit two. Not game text.", "合成ユニット二", None),),
    ),
)


@pytest.fixture
def synthetic(fake_vanilla: Path) -> Path:
    for lang in ("en", "ja"):
        namespaces: dict[str, Namespace] = {}
        for ns, key, english, japanese in LINES:
            text = english if lang == "en" else japanese
            namespaces.setdefault(ns, Namespace(ns)).add(Entry(key, text, text, is_hash=False))
        locres = LocresFile()
        for ns in namespaces.values():
            locres.add(ns)
        locres.write(str(fake_vanilla / "pak" / LOCRES / lang / "Game.locres"))
    scenes.table_path().write_text(json.dumps({"build": "424242", "lines": list(TABLE)}), encoding="utf-8")
    return fake_vanilla


class FakeScorer:
    """Stands in for the classifier's Scorer and logs the calls it gets. Not game text."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __enter__(self) -> "FakeScorer":
        self.log.append("scorer open")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.log.append("scorer close")

    def score(self, text: str) -> float:
        self.log.append(f"score {text}")
        return 0.9


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replaces the remote sync, the Scorer and the renderer's tunnel with fakes that log each call. Not game text."""
    calls: list[str] = []

    @contextmanager
    def fake_rendering(port: int = 0) -> Iterator[Callable[[str, str], str]]:
        calls.append("render open")

        def render(speaker: str, text: str) -> str:
            calls.append(f"render {speaker}")
            return f"voiced {text}"

        try:
            yield render
        finally:
            calls.append("render close")

    monkeypatch.setattr(remote, "sync", lambda: calls.append("sync"))
    monkeypatch.setattr(classifier, "Scorer", lambda: FakeScorer(calls))
    monkeypatch.setattr(voice_render, "rendering", fake_rendering)
    return calls


def test_scene_table_scenes_group_by_scene_in_table_order_with_keys_english_japanese_chapter_and_speaker(
    synthetic: Path,
) -> None:
    assert list(bindings().scenes.scenes()[:3]) == list(EXPECTED[:3])


def test_dl38_namespaces_are_yielded_once_each_without_repeating_table_lines(synthetic: Path) -> None:
    found = bindings().scenes.scenes()
    keys = [(line.namespace, line.key) for scene in found for line in scene.lines]

    assert len(keys) == len(set(keys))
    assert ("ST_UnitDescription_01", "1") in keys
    assert [scene.key for scene in found] == [scene.key for scene in EXPECTED]
    assert list(found[3:]) == list(EXPECTED[3:])
    assert ("ST_C01_001_02", "1") not in keys
    assert all(namespace != "ST_DialogChoiceX_01" and namespace != "ST_Other_01" for namespace, _ in keys)


def test_table_line_without_english_fails_naming_namespace_key_and_ffrd_scenes(synthetic: Path) -> None:
    rows = [*TABLE, {"scene": "SynScene2", "order": 2, "namespace": "ST_C01_001_02", "key": "1", "speaker": None, "voice": None}]
    scenes.table_path().write_text(json.dumps({"build": "424242", "lines": rows}), encoding="utf-8")

    with pytest.raises(Fail) as excinfo:
        bindings().scenes.scenes()

    assert str(excinfo.value) == "scene table line ST_C01_001_02/1 has no English text; run `ffrd scenes`"


def test_scene_source_is_a_scene_table() -> None:
    assert bindings().scenes.scene_table is True


def test_classifier_threshold_is_the_render_score_bar() -> None:
    assert bindings().classifier.threshold == RENDER_SCORE_BAR == 0.5


def test_nothing_opens_before_the_first_score_or_render(synthetic: Path, log: list[str]) -> None:
    bound = bindings()
    bound.scenes.scenes()
    assert bound.classifier.threshold == RENDER_SCORE_BAR

    assert log == []


def test_classifier_syncs_before_the_scorer_opens_and_opens_once(log: list[str]) -> None:
    bound = bindings()

    bound.classifier.score("Synthetic first. Not game text.")
    bound.classifier.score("Synthetic second. Not game text.")

    assert log == [
        "sync",
        "scorer open",
        "score Synthetic first. Not game text.",
        "score Synthetic second. Not game text.",
    ]


def test_renderer_returns_a_none_speaker_unchanged_without_opening_the_tunnel(log: list[str]) -> None:
    bound = bindings()

    assert bound.renderer.render(None, "Synthetic narration. Not game text.") == "Synthetic narration. Not game text."
    assert log == []


def test_renderer_opens_the_tunnel_on_first_speaker_render_and_keeps_it(log: list[str]) -> None:
    bound = bindings()

    assert bound.renderer.render("Synthetic speaker", "Synthetic line. Not game text.") == "voiced Synthetic line. Not game text."
    bound.renderer.render("Synthetic speaker", "Synthetic again. Not game text.")

    assert log == [
        "render open",
        "render Synthetic speaker",
        "render Synthetic speaker",
    ]


def test_exit_stack_closes_what_opened(log: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[Callable[[], None]] = []
    monkeypatch.setattr(atexit, "register", registered.append)
    bound = bindings()
    bound.classifier.score("Synthetic scored. Not game text.")
    bound.renderer.render("Synthetic speaker", "Synthetic voiced. Not game text.")

    assert len(registered) == 1
    registered[0]()

    assert log[-2:] == ["render close", "scorer close"]


RACERS = 8
RACE_PAUSE = 0.05


def _race(call: Callable[[], Any]) -> list[Any]:
    """Starts RACERS threads on one call at once and returns their answers in submit order. A thread's error is re-raised."""
    start = threading.Barrier(RACERS, timeout=10)

    def run() -> Any:
        start.wait()
        return call()

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        futures = [pool.submit(run) for _ in range(RACERS)]
        return [future.result() for future in futures]


class Overlap:
    """Records the calls a race makes and the most scores in flight at once. Not game text."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self._guard = threading.Lock()

    def enter(self) -> None:
        with self._guard:
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)

    def leave(self) -> None:
        with self._guard:
            self.in_flight -= 1


class SlowScorer:
    """A Scorer that pauses on open and on each score, so racing threads overlap. Not game text."""

    def __init__(self, overlap: Overlap) -> None:
        self.overlap = overlap

    def __enter__(self) -> "SlowScorer":
        time.sleep(RACE_PAUSE)
        self.overlap.calls.append("scorer open")
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def score(self, text: str) -> float:
        self.overlap.enter()
        try:
            time.sleep(RACE_PAUSE)
            self.overlap.calls.append("score")
            return 0.9
        finally:
            self.overlap.leave()


def test_racing_first_scores_sync_and_open_the_scorer_once_and_never_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    overlap = Overlap()

    def slow_sync() -> None:
        time.sleep(RACE_PAUSE)
        overlap.calls.append("sync")

    monkeypatch.setattr(remote, "sync", slow_sync)
    monkeypatch.setattr(classifier, "Scorer", lambda: SlowScorer(overlap))
    bound = bindings()

    scores = _race(lambda: bound.classifier.score("Synthetic racing. Not game text."))

    assert scores == [0.9] * RACERS
    assert overlap.calls.count("sync") == 1
    assert overlap.calls.count("scorer open") == 1
    assert overlap.calls.count("score") == RACERS
    assert overlap.most_in_flight == 1


def test_racing_first_renders_open_the_tunnel_once_and_every_render_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    opens: list[str] = []

    @contextmanager
    def slow_rendering(port: int = 0) -> Iterator[Callable[[str, str], str]]:
        time.sleep(RACE_PAUSE)
        opens.append("render open")
        yield lambda speaker, text: f"voiced {text}"

    monkeypatch.setattr(voice_render, "rendering", slow_rendering)
    bound = bindings()

    renders = _race(lambda: bound.renderer.render("Synthetic speaker", "Synthetic racing. Not game text."))

    assert renders == ["voiced Synthetic racing. Not game text."] * RACERS
    assert opens == ["render open"]
