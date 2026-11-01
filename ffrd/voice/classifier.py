"""Paraphrase pair loader shared by the voice-model modules, and the labelled examples the game-vs-LLM classifier trains on."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ffrd.game import Fail
from ffrd.pipeline.store import work_dir

SPLITS = ("train", "heldout")


@dataclass(frozen=True)
class Pair:
    scene: str
    speaker: str = field(repr=False)
    paraphrase: str = field(repr=False)
    original: str = field(repr=False)
    slot: str
    model: str
    split: str


def load_pairs(split: str) -> list[Pair]:
    if split not in SPLITS:
        raise Fail(f"unknown pairs split {split!r}; expected one of {', '.join(SPLITS)}")
    path = work_dir("voice") / "pairs" / f"{split}.jsonl"
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise Fail(f"no {split} pairs file; run `ffrd voice pairs` first") from None
    pairs: list[Pair] = []
    with handle:
        for number, raw in enumerate(handle, start=1):
            if raw.strip():
                pairs.append(_parse(split, number, raw))
    return pairs


def _parse(split: str, number: int, raw: bytes) -> Pair:
    try:
        record: Any = json.loads(raw)
    except ValueError:
        raise Fail(f"{split} line {number} is not a JSON object") from None
    if not isinstance(record, dict):
        raise Fail(f"{split} line {number} is not a JSON object")
    return Pair(
        scene=_text(record, "scene", split, number),
        speaker=_text(record, "speaker", split, number),
        paraphrase=_text(record, "paraphrase", split, number),
        original=_text(record, "original", split, number),
        slot=_text(record, "slot", split, number),
        model=_text(record, "model", split, number),
        split=split,
    )


def _text(record: dict[str, Any], name: str, split: str, number: int) -> str:
    value = record.get(name)
    if not isinstance(value, str):
        raise Fail(f"{split} line {number} has a missing or non-string {name!r} field")
    return value


def examples(pairs: Sequence[Pair]) -> list[tuple[str, int]]:
    originals = dict.fromkeys(pair.original for pair in pairs)
    labelled = [(original, 1) for original in originals]
    labelled += [(pair.paraphrase, 0) for pair in pairs if pair.paraphrase != pair.original]
    return labelled
