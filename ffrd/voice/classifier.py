"""Paraphrase pair loader shared by the voice-model modules, the labelled examples the game-vs-LLM classifier trains on, and the game-likeness Scorer the classifier gate calls."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

from ffrd.game import Fail
from ffrd.pipeline.store import work_dir
from ffrd.voice import pairs, remote

SPLIT_FILES = {"train": pairs.TRAIN, "heldout": pairs.HELDOUT}
DATA = "data/classifier.jsonl"
MODEL_DIR = "models/classifier"
MALFORMED_REPLY = "classifier worker sent a malformed reply"


@dataclass(frozen=True)
class Pair:
    scene: str
    speaker: str | None = field(repr=False)
    paraphrase: str = field(repr=False)
    original: str = field(repr=False)
    slot: int
    model: str
    split: str


def load_pairs(split: str) -> list[Pair]:
    if split not in SPLIT_FILES:
        raise Fail(f"unknown pairs split {split!r}; expected one of {', '.join(SPLIT_FILES)}")
    return [
        Pair(
            scene=row.scene,
            speaker=row.speaker,
            paraphrase=row.paraphrase,
            original=row.original,
            slot=row.slot,
            model=row.model,
            split=split,
        )
        for row in pairs.read(SPLIT_FILES[split], work_dir("voice"))
    ]


def examples(pairs: Sequence[Pair]) -> list[tuple[str, int]]:
    originals = dict.fromkeys(pair.original for pair in pairs)
    labelled = [(original, 1) for original in originals]
    labelled += [(pair.paraphrase, 0) for pair in pairs if pair.paraphrase != pair.original]
    return labelled


class Scorer:
    def __init__(self, model_dir: str = MODEL_DIR) -> None:
        self._worker = remote.Worker(["python3", "-m", "tools.voice.classify", "serve", model_dir])

    def __enter__(self) -> Self:
        self._worker.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._worker.__exit__(exc_type, exc, tb)

    def score_many(self, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        reply = self._worker.ask({"texts": list(texts)})
        scores = reply.get("scores")
        if not isinstance(scores, list) or len(scores) != len(texts):
            raise Fail(MALFORMED_REPLY)
        if not all(_is_score(score) for score in scores):
            raise Fail(MALFORMED_REPLY)
        return [float(score) for score in scores]

    def score(self, text: str) -> float:
        return self.score_many([text])[0]


def _is_score(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1


def train(epochs: int = 1) -> None:
    rows = [{"text": text, "label": label} for text, label in examples(load_pairs("train"))]
    remote.sync()
    count = remote.stage(rows, DATA)
    remote.run(["python3", "-m", "tools.voice.classify", "train", DATA, MODEL_DIR, "--epochs", str(epochs)])
    print(f"staged {count} classifier examples")


def cmd_train(args: argparse.Namespace) -> None:
    train(args.epochs)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    clf = subparsers.add_parser("classifier", help="stage classifier examples and run the remote classifier training")
    actions = clf.add_subparsers(dest="action", required=True)
    train_p = actions.add_parser("train", help="stage the train examples and train the classifier on the GPU host")
    train_p.add_argument("--epochs", type=int, default=1, help="passes over the training examples")
    train_p.set_defaults(fn=cmd_train)
