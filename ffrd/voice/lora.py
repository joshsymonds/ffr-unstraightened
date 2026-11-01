"""LoRA finetune of the voice model: stage the training records under work/voice/, then run the finetune and GGUF export on the GPU host."""

import argparse
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ffrd.pipeline.store import work_dir
from ffrd.voice import remote
from ffrd.voice.classifier import Pair, load_pairs
from ffrd.voice.render import prompt

DATA = "data/lora.jsonl"
ADAPTER = "models/adapter"
GGUF = "models/voice.gguf"


def records(pairs: Sequence[Pair]) -> list[dict[str, str]]:
    return [{"prompt": prompt(p.speaker, p.paraphrase), "target": p.original} for p in pairs if p.split == "train"]


def _stage(rows: list[dict[str, str]]) -> Path:
    staging = work_dir("voice") / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / "lora.jsonl"
    fd, tmp = tempfile.mkstemp(dir=staging, prefix=".lora.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def train(epochs: int = 1) -> None:
    rows = records(load_pairs("train"))
    local = _stage(rows)
    remote.sync()
    remote.push(local, DATA)
    remote.run(["python3", "-m", "tools.voice.finetune", "train", DATA, ADAPTER, "--epochs", str(epochs)])
    print(f"staged {len(rows)} lora records")


def export() -> None:
    remote.sync()
    remote.run(["python3", "-m", "tools.voice.export", ADAPTER, GGUF])


def cmd_train(args: argparse.Namespace) -> None:
    train(args.epochs)


def cmd_export(args: argparse.Namespace) -> None:
    export()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    lora = subparsers.add_parser("lora", help="stage LoRA training records and run the remote finetune and GGUF export")
    actions = lora.add_subparsers(dest="action", required=True)
    train_p = actions.add_parser("train", help="stage the train pairs and finetune the adapter on the GPU host")
    train_p.add_argument("--epochs", type=int, default=1, help="passes over the training records")
    train_p.set_defaults(fn=cmd_train)
    export_p = actions.add_parser("export", help="export the finetuned adapter to GGUF on the GPU host")
    export_p.set_defaults(fn=cmd_export)
