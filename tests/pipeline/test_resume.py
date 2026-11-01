import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail, repo_root
from ffrd.pipeline.store import Store, key, work_dir


class FakeStage:
    """Counts calls and returns a fixed value in place of a real stage."""

    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self.value


def _fail() -> Any:
    raise RuntimeError("synthetic failure")


def test_reopened_store_reruns_nothing_and_returns_equal_values(tmp_path: Path) -> None:
    stage = FakeStage({"lines": ["Made-up resume line one. Not game text."], "pair": (1, 2)})
    first = Store(tmp_path).run("extract", {"chapter": 1}, "lens v1 text", stage)
    again = Store(tmp_path).run("extract", {"chapter": 1}, "lens v1 text", stage)

    assert stage.calls == 1
    assert again == first
    assert again.digest == first.digest
    assert first.value == {"lines": ["Made-up resume line one. Not game text."], "pair": [1, 2]}
    canonical = json.dumps(first.value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert first.digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_key_is_sha256_of_canonical_json(tmp_path: Path) -> None:
    canonical = '{"inputs":{"a":"é","b":1},"lens":null,"stage":"s"}'
    assert key("s", {"b": 1, "a": "é"}, None) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_lens_edit_reruns_lens_reader_and_only_downstream_of_changed_values(tmp_path: Path) -> None:
    store = Store(tmp_path)
    translate = FakeStage({"line": "Made-up translated line. Not game text."})
    polish = FakeStage({"polished": "Made-up polished line. Not game text."})
    glossary = FakeStage({"terms": ["Made-up term. Not game text."]})
    inputs = {"line_id": "ST_C01_001_01/1"}

    first = store.run("translate", inputs, "lens v1 text", translate)
    down = store.run("polish", {"upstream": first.digest}, None, polish)
    store.run("glossary", {"set": "a"}, None, glossary)
    assert translate.calls == 1
    assert polish.calls == 1

    # Lens text changed, output unchanged: translate reruns, polish keeps its record.
    second = store.run("translate", inputs, "lens v2 text", translate)
    assert translate.calls == 2
    assert second.digest == first.digest
    assert store.run("polish", {"upstream": second.digest}, None, polish) == down
    assert polish.calls == 1

    # Lens-free stage is keyed without the lens, so later lens edits never rerun it.
    store.run("glossary", {"set": "a"}, None, glossary)
    assert glossary.calls == 1

    # Lens text changed and output changed: translate reruns and polish reruns for the new digest.
    translate.value = {"line": "Made-up retranslated line. Not game text."}
    third = store.run("translate", inputs, "lens v3 text", translate)
    assert translate.calls == 3
    assert third.digest != first.digest
    store.run("polish", {"upstream": third.digest}, None, polish)
    assert polish.calls == 2


def test_raising_call_stores_no_record_and_success_clears_the_marker(tmp_path: Path) -> None:
    store = Store(tmp_path)
    inputs = {"line_id": "ST_C01_001_01/1"}
    k = key("translate", inputs, "lens v1 text")

    def rate_limited() -> Any:
        raise RuntimeError("synthetic rate limit")

    with pytest.raises(RuntimeError, match="synthetic rate limit"):
        store.run("translate", inputs, "lens v1 text", rate_limited)

    assert store.read(k) is None
    assert store.finished() == []
    assert store.unfinished() == [k]

    recovered = FakeStage({"line": "Made-up recovered line. Not game text."})
    result = store.run("translate", inputs, "lens v1 text", recovered)

    assert result.key == k
    assert recovered.calls == 1
    assert store.unfinished() == []
    assert store.finished() == [k]


def test_records_are_json_and_no_temporary_files_remain(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.run("extract", {"chapter": 1}, "lens v1 text", FakeStage(["Made-up line. Not game text."]))
    store.run("extract", {"chapter": 2}, "lens v1 text", FakeStage({"n": 2}))

    records = sorted((tmp_path / "results").iterdir())
    assert len(records) == 2
    for record in records:
        parsed = json.loads(record.read_text(encoding="utf-8"))
        assert set(parsed) == {"stage", "value"}
        assert parsed["stage"] == "extract"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_nothing_prints(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(tmp_path)
    store.run("extract", {"chapter": 1}, "lens v1 text", FakeStage({"n": 1}))
    store.run("extract", {"chapter": 1}, "lens v1 text", FakeStage({"n": 1}))
    with pytest.raises(RuntimeError, match="synthetic failure"):
        store.run("extract", {"chapter": 2}, "lens v1 text", _fail)
    store.finished()
    store.unfinished()

    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "path",
    [
        "work/smoke/results/synthetic.json",
        "work/smoke/unfinished/synthetic",
        ".env.local",
    ],
)
def test_git_ignores_work_and_env_local(path: str) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=repo_root(), check=False)
    assert result.returncode == 0


def test_work_dir_is_one_plain_segment_under_work() -> None:
    assert work_dir("smoke") == repo_root() / "work" / "smoke"


@pytest.mark.parametrize("lens", ["", ".", "..", "a/b", "a\\b", "/abs"])
def test_work_dir_rejects_bad_lens_names(lens: str) -> None:
    with pytest.raises(Fail):
        work_dir(lens)


@pytest.mark.parametrize("bad", ["", "ABC", "g" * 64, "A" * 64, "a" * 63, "a" * 65, "../" + "a" * 61])
def test_read_rejects_malformed_keys(tmp_path: Path, bad: str) -> None:
    with pytest.raises(Fail):
        Store(tmp_path).read(bad)
