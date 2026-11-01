"""Train and serve the single-label game-vs-LLM classifier.

Training reads JSON Lines rows of {"text": str, "label": 0 or 1}, where label 1 is a game line, and writes a private model
directory. Serving loads that directory once and answers JSON Lines requests of {"texts": [...]} with sigmoid scores.
Standard output carries only the training step lines and serving replies, and never record text.
"""

import argparse
import json
import math
import pathlib
import random
import sys
from collections.abc import Sequence
from typing import IO

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

BASE_MODEL = "microsoft/deberta-v3-large"
MAX_TOKENS = 128
BATCH_SIZE = 16
# Each batch runs in microbatches of at most this many examples, with gradients accumulated, so deberta-v3-large fits
# on a 16 GB card. The update is the same as one pass over the batch.
MICRO_BATCH_SIZE = 8
LEARNING_RATE = 2e-5
EPOCHS = 1
# The learning rate rises linearly over this share of the steps, then falls linearly to zero, and gradients are clipped.
WARMUP_FRACTION = 0.06
MAX_GRAD_NORM = 1.0
SEED = 0


def _device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _autocast(device: torch.device):
    return torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=device.type == "cuda")


def _encode(tokenizer, texts: Sequence[str], device: torch.device):
    return tokenizer(list(texts), padding=True, truncation=True, max_length=MAX_TOKENS, return_tensors="pt").to(device)


def _lr_factor(step: int, warmup: int, total: int) -> float:
    # Linear warmup reaches the full rate at step warmup - 1, then linear decay ends one step past the last.
    if step < warmup:
        return (step + 1) / warmup
    return (total - step) / (total - warmup + 1)


def _read_examples(path: pathlib.Path) -> tuple[list[str], list[int]]:
    texts: list[str] = []
    labels: list[int] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            texts.append(row["text"])
            labels.append(int(row["label"]))
    return texts, labels


def _fit(
    model,
    tokenizer,
    texts: Sequence[str],
    labels: Sequence[int],
    device: torch.device,
    epochs: int,
    batch_size: int,
    learning_rate: float,
) -> None:
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    total_steps = epochs * math.ceil(len(texts) / batch_size)
    warmup = max(1, round(total_steps * WARMUP_FRACTION))
    schedule = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda step: _lr_factor(step, warmup, total_steps))
    loss_fn = torch.nn.BCEWithLogitsLoss()
    shuffle = random.Random(SEED)
    model.gradient_checkpointing_enable()
    model.train()
    for epoch in range(epochs):
        order = list(range(len(texts)))
        shuffle.shuffle(order)
        for step, start in enumerate(range(0, len(order), batch_size)):
            indices = order[start : start + batch_size]
            optimizer.zero_grad()
            total = 0.0
            for micro in range(0, len(indices), MICRO_BATCH_SIZE):
                part = indices[micro : micro + MICRO_BATCH_SIZE]
                batch = _encode(tokenizer, [texts[i] for i in part], device)
                target = torch.tensor([labels[i] for i in part], dtype=torch.float32, device=device)
                with _autocast(device):
                    logit = model(**batch).logits[:, 0]
                # Each microbatch mean is weighted by its share of the batch, so the summed gradients equal the
                # gradient of the batch mean.
                loss = loss_fn(logit.float(), target) * (len(part) / len(indices))
                loss.backward()
                total += loss.item()
            torch.nn.utils.clip_grad_norm_(model.parameters(), MAX_GRAD_NORM)
            optimizer.step()
            schedule.step()
            print(f"{epoch}\t{step}\t{total:.6f}")


def train(
    examples: pathlib.Path,
    out_dir: pathlib.Path,
    *,
    base_model: str = BASE_MODEL,
    epochs: int = EPOCHS,
    batch_size: int = BATCH_SIZE,
    learning_rate: float = LEARNING_RATE,
) -> None:
    texts, labels = _read_examples(examples)
    device = _device()
    # The weights and the training text they learned from stay readable only by the owner.
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=False)
    model = AutoModelForSequenceClassification.from_pretrained(base_model, num_labels=1, dtype=torch.float32, trust_remote_code=False).to(
        device
    )
    _fit(model, tokenizer, texts, labels, device, epochs, batch_size, learning_rate)
    model.save_pretrained(out_dir)
    tokenizer.save_pretrained(out_dir)


def scores(model, tokenizer, texts: Sequence[str]) -> list[float]:
    device = next(model.parameters()).device
    was_training = model.training
    model.eval()
    try:
        result: list[float] = []
        with torch.no_grad(), _autocast(device):
            for start in range(0, len(texts), BATCH_SIZE):
                batch = _encode(tokenizer, texts[start : start + BATCH_SIZE], device)
                result.extend(torch.sigmoid(model(**batch).logits[:, 0].float()).tolist())
    finally:
        model.train(was_training)
    return result


def serve(model_dir: pathlib.Path, stdin: IO[str], stdout: IO[str]) -> None:
    tokenizer = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=False)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir, dtype=torch.float32, trust_remote_code=False).to(_device())
    for line in stdin:
        try:
            texts = json.loads(line)["texts"]
            if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
                raise ValueError("texts must be a list of strings")
        except (ValueError, KeyError, TypeError):
            reply = {"error": "invalid request"}
        else:
            reply = {"scores": scores(model, tokenizer, texts)}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m tools.voice.classify")
    commands = parser.add_subparsers(dest="command", required=True)
    train_command = commands.add_parser("train")
    train_command.add_argument("examples", type=pathlib.Path)
    train_command.add_argument("out_dir", type=pathlib.Path)
    train_command.add_argument("--epochs", type=int, default=EPOCHS)
    serve_command = commands.add_parser("serve")
    serve_command.add_argument("model_dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.command == "train":
        train(args.examples, args.out_dir, epochs=args.epochs)
    else:
        serve(args.model_dir, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
