"""Two-way NLI entailment worker for the GPU host.

Reads JSON Lines requests on stdin, `{"pairs": [[a, b], ...]}`, and writes one JSON line
per request on stdout, `{"holds": [bool, ...]}`. A line of any other shape gets
`{"error": "invalid request"}`. The worker prints decisions only and never echoes input text.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import IO

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

NLI_MODEL = "MoritzLaurer/DeBERTa-v3-large-mnli-fever-anli-ling-wanli"
ENTAILMENT = "entailment"
# deberta-v3-large runs out of memory at 32 rows on a 16 GB card.
BATCH_SIZE = 16
INVALID_REQUEST = {"error": "invalid request"}


class Entailment:
    def __init__(self, model: str = NLI_MODEL, device: str | None = None):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        # Scores in fp32, whatever dtype the checkpoint was saved in.
        self.model = (
            AutoModelForSequenceClassification.from_pretrained(model, dtype=torch.float32, trust_remote_code=False).to(device).eval()
        )
        self.tokenizer = AutoTokenizer.from_pretrained(model, trust_remote_code=False)
        labels = {name.lower(): int(index) for index, name in self.model.config.id2label.items()}
        self._entailment = labels[ENTAILMENT]

    def holds_many(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
        forward = [(a, b) for a, b in pairs]
        backward = [(b, a) for a, b in pairs]
        results = self._entails_many(forward + backward)
        count = len(pairs)
        return [one and two for one, two in zip(results[:count], results[count:], strict=True)]

    def _entails_many(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
        decisions: list[bool] = []
        for start in range(0, len(pairs), BATCH_SIZE):
            decisions.extend(self._entails_batch(pairs[start : start + BATCH_SIZE]))
        return decisions

    @torch.no_grad()
    def _entails_batch(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
        premises, hypotheses = zip(*pairs, strict=True)
        # Pairs longer than the tokenizer's model_max_length are truncated, not rejected.
        batch = self.tokenizer(
            list(premises),
            list(hypotheses),
            padding=True,
            truncation=True,
            return_tensors="pt",
        ).to(self.device)
        predicted = self.model(**batch).logits.argmax(dim=-1)
        return (predicted == self._entailment).tolist()


def _parse_pairs(line: str) -> list[tuple[str, str]] | None:
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(request, dict):
        return None
    pairs = request.get("pairs")
    if not isinstance(pairs, list):
        return None
    for pair in pairs:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(text, str) for text in pair)):
            return None
    return [(a, b) for a, b in pairs]


def serve(entailment: Entailment, stdin: IO[str], stdout: IO[str]) -> None:
    for line in stdin:
        pairs = _parse_pairs(line)
        if pairs is None:
            response: dict[str, object] = INVALID_REQUEST
        else:
            response = {"holds": entailment.holds_many(pairs)}
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m tools.voice.nli")
    commands = parser.add_subparsers(dest="command", required=True)
    serving = commands.add_parser("serve", help="answer two-way entailment requests from stdin")
    serving.add_argument("--model", default=NLI_MODEL)
    args = parser.parse_args(argv)
    serve(Entailment(args.model), sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
