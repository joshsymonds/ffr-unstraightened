import argparse
import io
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline.gates import Classifier
from ffrd.voice import classifier, remote
from ffrd.voice.classifier import MODEL_DIR, Pair, Scorer, examples, load_pairs

# Made-up strings for the pairs loader. Not game text.
SYNTH_TEXTS = (
    "SYNTH-SPEAKER-ALPHA",
    "SYNTH-PARAPHRASE-ONE wording",
    "SYNTH-ORIGINAL-ONE wording",
    "SYNTH-PARAPHRASE-TWO wording",
    "SYNTH-ORIGINAL-TWO wording",
    "SYNTH-RAW-NOT-JSON fragment",
)

# Made-up strings for the scorer. Not game text.
SCORE_TEXTS = ("SYNTH-SCORE-ONE wording", "SYNTH-SCORE-TWO wording")


def record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "scene": "synth-scene-1",
        "speaker": SYNTH_TEXTS[0],
        "paraphrase": SYNTH_TEXTS[1],
        "original": SYNTH_TEXTS[2],
        "slot": "synth-slot-1",
        "model": "synth-model-1",
    }
    base.update(overrides)
    return base


def line(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def pairs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "work"
    monkeypatch.setattr(classifier, "work_dir", lambda lens: root / lens)
    return root / "voice" / "pairs"


def write_split(pairs_dir: Path, split: str, lines: list[str]) -> None:
    pairs_dir.mkdir(parents=True, exist_ok=True)
    (pairs_dir / f"{split}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_pairs_maps_fields_in_file_order(pairs_dir: Path) -> None:
    write_split(
        pairs_dir,
        "train",
        [
            line(record(scene="synth-scene-1", original=SYNTH_TEXTS[2], paraphrase=SYNTH_TEXTS[1])),
            line(record(scene="synth-scene-2", speaker=SYNTH_TEXTS[0], original=SYNTH_TEXTS[4], paraphrase=SYNTH_TEXTS[3])),
        ],
    )
    assert load_pairs("train") == [
        Pair(
            scene="synth-scene-1",
            speaker=SYNTH_TEXTS[0],
            paraphrase=SYNTH_TEXTS[1],
            original=SYNTH_TEXTS[2],
            slot="synth-slot-1",
            model="synth-model-1",
            split="train",
        ),
        Pair(
            scene="synth-scene-2",
            speaker=SYNTH_TEXTS[0],
            paraphrase=SYNTH_TEXTS[3],
            original=SYNTH_TEXTS[4],
            slot="synth-slot-1",
            model="synth-model-1",
            split="train",
        ),
    ]


def test_load_pairs_sets_split_from_argument(pairs_dir: Path) -> None:
    write_split(pairs_dir, "heldout", [line(record()), line(record(scene="synth-scene-2"))])
    loaded = load_pairs("heldout")
    assert len(loaded) == 2
    assert {pair.split for pair in loaded} == {"heldout"}


def test_load_pairs_skips_blank_lines(pairs_dir: Path) -> None:
    write_split(pairs_dir, "train", ["", line(record()), "   ", "\t", line(record(scene="synth-scene-2"))])
    assert [pair.scene for pair in load_pairs("train")] == ["synth-scene-1", "synth-scene-2"]


def test_load_pairs_rejects_unknown_split(pairs_dir: Path) -> None:
    with pytest.raises(Fail, match="unknown pairs split 'synthetic'"):
        load_pairs("synthetic")


def test_load_pairs_missing_file_points_at_pairs_command(pairs_dir: Path) -> None:
    with pytest.raises(Fail, match="no heldout pairs file; run `ffrd voice pairs` first"):
        load_pairs("heldout")


@pytest.mark.parametrize(
    ("bad_line", "reason"),
    [
        (f"[{json.dumps(SYNTH_TEXTS[2])}]", "not a JSON object"),
        (SYNTH_TEXTS[5], "not a JSON object"),
        (line({k: v for k, v in record().items() if k != "model"}), "missing or non-string 'model' field"),
        (line(record(paraphrase=7)), "missing or non-string 'paraphrase' field"),
    ],
    ids=["array", "not-json", "missing-field", "non-string-field"],
)
def test_load_pairs_failures_name_split_and_line_only(pairs_dir: Path, bad_line: str, reason: str) -> None:
    write_split(pairs_dir, "train", [line(record()), bad_line])
    with pytest.raises(Fail) as excinfo:
        load_pairs("train")
    message = str(excinfo.value)
    assert "train line 2" in message
    assert reason in message
    for text in (*SYNTH_TEXTS, "synth-scene-1", "synth-model-1"):
        assert text not in message


def test_pair_repr_hides_speaker_paraphrase_and_original() -> None:
    pair = Pair(
        scene="synth-scene-1",
        speaker=SYNTH_TEXTS[0],
        paraphrase=SYNTH_TEXTS[1],
        original=SYNTH_TEXTS[2],
        slot="synth-slot-1",
        model="synth-model-1",
        split="train",
    )
    text = repr(pair)
    assert "synth-scene-1" in text
    for hidden in (SYNTH_TEXTS[0], SYNTH_TEXTS[1], SYNTH_TEXTS[2]):
        assert hidden not in text


def make_pair(original: str, paraphrase: str) -> Pair:
    return Pair(
        scene="synth-scene-1",
        speaker=SYNTH_TEXTS[0],
        paraphrase=paraphrase,
        original=original,
        slot="synth-slot-1",
        model="synth-model-1",
        split="train",
    )


def test_examples_dedupes_originals_and_keeps_pair_order() -> None:
    pairs = [
        make_pair(SYNTH_TEXTS[2], SYNTH_TEXTS[1]),
        make_pair(SYNTH_TEXTS[4], SYNTH_TEXTS[3]),
        make_pair(SYNTH_TEXTS[2], "SYNTH-PARAPHRASE-THREE wording"),
    ]
    assert examples(pairs) == [
        (SYNTH_TEXTS[2], 1),
        (SYNTH_TEXTS[4], 1),
        (SYNTH_TEXTS[1], 0),
        (SYNTH_TEXTS[3], 0),
        ("SYNTH-PARAPHRASE-THREE wording", 0),
    ]


def test_examples_drops_paraphrase_identical_to_its_original() -> None:
    pairs = [make_pair(SYNTH_TEXTS[2], SYNTH_TEXTS[2]), make_pair(SYNTH_TEXTS[4], SYNTH_TEXTS[3])]
    assert examples(pairs) == [(SYNTH_TEXTS[2], 1), (SYNTH_TEXTS[4], 1), (SYNTH_TEXTS[3], 0)]


class FakeWorker:
    def __init__(self, argv: Sequence[str], reply: object) -> None:
        self.argv = list(argv)
        self.reply = reply
        self.asks: list[dict[str, Any]] = []
        self.entered = False
        self.exited = False

    def __enter__(self) -> "FakeWorker":
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited = True

    def ask(self, request: dict[str, Any]) -> Any:
        self.asks.append(request)
        return self.reply


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], list[FakeWorker]]:
    def install(reply: object) -> list[FakeWorker]:
        made: list[FakeWorker] = []

        def make(argv: Sequence[str]) -> FakeWorker:
            worker = FakeWorker(argv, reply)
            made.append(worker)
            return worker

        monkeypatch.setattr(classifier.remote, "Worker", make)
        return made

    return install


def test_scorer_serves_model_dir_through_classify_module(fake_worker: Callable[[object], list[FakeWorker]]) -> None:
    made = fake_worker({"scores": []})
    Scorer()
    Scorer("models/synthetic")
    assert MODEL_DIR == "models/classifier"
    assert [worker.argv for worker in made] == [
        ["python3", "-m", "tools.voice.classify", "serve", "models/classifier"],
        ["python3", "-m", "tools.voice.classify", "serve", "models/synthetic"],
    ]


def test_score_many_sends_one_request_and_keeps_order(fake_worker: Callable[[object], list[FakeWorker]]) -> None:
    made = fake_worker({"scores": [0.75, 0.25]})
    with Scorer() as scorer:
        assert scorer.score_many(list(SCORE_TEXTS)) == [0.75, 0.25]
    assert made[0].asks == [{"texts": list(SCORE_TEXTS)}]


def test_score_many_empty_input_sends_nothing(fake_worker: Callable[[object], list[FakeWorker]]) -> None:
    made = fake_worker({"scores": []})
    with Scorer() as scorer:
        assert scorer.score_many([]) == []
    assert made[0].asks == []


@pytest.mark.parametrize(
    "reply",
    [
        {"scores": [0.5]},
        {"scores": [0.5, 0.5, 0.5]},
        {"scores": "0.5"},
        {"scores": 0.5},
        {"scores": [0.5, 1.5]},
        {"scores": [-0.1, 0.5]},
        {"scores": [True, 0.5]},
        {"scores": [0.5, False]},
        {"scores": ["0.5", 0.5]},
        {"other": [0.5, 0.5]},
    ],
    ids=["short", "long", "string", "scalar", "above-one", "below-zero", "true", "false", "string-item", "missing-key"],
)
def test_score_many_malformed_reply_raises_content_free_fail(fake_worker: Callable[[object], list[FakeWorker]], reply: object) -> None:
    fake_worker(reply)
    with Scorer() as scorer, pytest.raises(Fail) as err:
        scorer.score_many(list(SCORE_TEXTS))
    assert str(err.value) == "classifier worker sent a malformed reply"
    for text in SCORE_TEXTS:
        assert text not in str(err.value)


def test_score_returns_single_score_and_closes_worker(fake_worker: Callable[[object], list[FakeWorker]]) -> None:
    made = fake_worker({"scores": [0.5]})
    with Scorer() as scorer:
        classify: Classifier = scorer.score
        assert classify(SCORE_TEXTS[0]) == 0.5
    assert made[0].asks == [{"texts": [SCORE_TEXTS[0]]}]
    assert made[0].entered is True
    assert made[0].exited is True


# The same prefix and NIX wrapper every remote command builds in the default scratch dir.
REMOTE_PREFIX = "umask 077 && mkdir -p ~/.local/share/ffr-unstraightened/voice/logs && cd ~/.local/share/ffr-unstraightened/voice"
REMOTE_NIX = "nix develop path:./tools/voice#cuda --command"


class RecordingSsh:
    """Stands in for the ssh layer. Records each remote command in order and keeps the bytes pushed to it."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.pushed: bytes | None = None

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(args[2])
        stdin = kwargs.get("stdin")
        if isinstance(stdin, io.IOBase):
            self.pushed = stdin.read()
        return subprocess.CompletedProcess(args, 0)


@pytest.fixture
def train_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RecordingSsh, Path]:
    ssh = RecordingSsh()
    monkeypatch.setattr(remote.subprocess, "run", ssh)
    monkeypatch.setattr(remote, "repo_root", lambda: tmp_path / "repo")
    voice = tmp_path / "repo" / "tools" / "voice"
    voice.mkdir(parents=True)
    (voice / "flake.nix").write_text("{}\n", encoding="utf-8")
    work = tmp_path / "work"
    monkeypatch.setattr(remote, "work_dir", lambda lens: work / lens)
    monkeypatch.setattr(
        classifier,
        "load_pairs",
        lambda split: (
            [make_pair(SYNTH_TEXTS[2], SYNTH_TEXTS[1]), make_pair(SYNTH_TEXTS[4], SYNTH_TEXTS[3])]
            if split == "train"
            else pytest.fail(f"train must load only the train split, not {split!r}")
        ),
    )
    return ssh, work


def test_train_syncs_then_stages_examples_then_runs_remote_trainer(train_env: tuple[RecordingSsh, Path]) -> None:
    ssh, _ = train_env
    classifier.train(epochs=3)

    assert len(ssh.commands) == 3
    assert ssh.commands[0] == f"{REMOTE_PREFIX} && rm -rf -- tools/voice && tar -x -f -"
    assert ssh.commands[1] == f"{REMOTE_PREFIX} && mkdir -p -- data && cat > data/classifier.jsonl"
    assert ssh.commands[2] == (
        f"{REMOTE_PREFIX} && {REMOTE_NIX} python3 -m tools.voice.classify train data/classifier.jsonl models/classifier --epochs 3 2>> logs/remote.log"
    )


def test_train_stages_text_and_label_rows_for_each_example(train_env: tuple[RecordingSsh, Path]) -> None:
    ssh, work = train_env
    classifier.train()

    expected = [
        (SYNTH_TEXTS[2], 1),
        (SYNTH_TEXTS[4], 1),
        (SYNTH_TEXTS[1], 0),
        (SYNTH_TEXTS[3], 0),
    ]
    staged = "".join(json.dumps({"text": text, "label": label}, ensure_ascii=False) + "\n" for text, label in expected)
    assert (work / "voice" / "staging" / "classifier.jsonl").read_text(encoding="utf-8") == staged
    assert ssh.pushed == staged.encode("utf-8")


def test_train_prints_only_the_staged_count(train_env: tuple[RecordingSsh, Path], capsys: pytest.CaptureFixture[str]) -> None:
    classifier.train()
    assert capsys.readouterr().out == "staged 4 classifier examples\n"


def test_register_parses_classifier_train_epochs_and_dispatches_to_train(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(classifier, "train", lambda epochs=1: calls.append(epochs))
    parser = argparse.ArgumentParser()
    classifier.register(parser.add_subparsers(required=True))

    args = parser.parse_args(["classifier", "train", "--epochs", "3"])
    args.fn(args)
    assert calls == [3]


def test_register_train_epochs_defaults_to_one() -> None:
    parser = argparse.ArgumentParser()
    classifier.register(parser.add_subparsers(required=True))
    assert parser.parse_args(["classifier", "train"]).epochs == 1
