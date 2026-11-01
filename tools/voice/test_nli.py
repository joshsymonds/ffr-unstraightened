"""Requirement R5: holds_many decides two-way entailment, a entails b and b entails a, and serve answers JSON Lines requests. The NLI classifier is a tiny randomly initialized DeBERTa-v2 saved to a temporary directory, and the one-way decisions are stubbed from synthetic pairs, so nothing touches the network, a model hub, or a GPU."""

import io
import json
import sys
from types import SimpleNamespace

import pytest
import sentencepiece as spm
import torch
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DebertaV2Config,
    DebertaV2Tokenizer,
)

from tools.voice import nli

# Synthetic sentences. Every character they use is in the tokenizer's corpus.
PREMISE = "The harbor was quiet before the boats came in."
HYPOTHESIS = "Boats came into the harbor."
OTHER = "Bread was baked at dawn."
LONG = " ".join([PREMISE] * 20)
# The short synthetic pairs fit under MAX_LENGTH, so the stubs can match them unpadded.
MAX_LENGTH = 128
SPECIAL_TOKENS = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"]

# The entailment label is written in different cases and at different indices, so a
# case-sensitive or index-fixed lookup fails one of these orders.
LABEL_ORDERS = [
    {0: "contradiction", 1: "ENTAILMENT", 2: "neutral"},
    {0: "Neutral", 1: "contradiction", 2: "Entailment"},
]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Every model in this file is a local directory, so the hub is never consulted.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def build_tokenizer(root):
    # A character-level sentencepiece model trained on the synthetic sentences, passed to
    # DebertaV2Tokenizer as its vocab.
    corpus = root / "corpus.txt"
    corpus.write_text("\n".join([PREMISE, HYPOTHESIS, OTHER, LONG]) + "\n", encoding="utf-8")
    prefix = root / "spm"
    spm.SentencePieceTrainer.train(
        input=str(corpus),
        model_prefix=str(prefix),
        model_type="char",
        character_coverage=1.0,
        hard_vocab_limit=False,
    )
    processor = spm.SentencePieceProcessor(model_file=f"{prefix}.model")
    pieces = [(token, 0.0) for token in SPECIAL_TOKENS]
    pieces += [
        (processor.id_to_piece(i), processor.get_score(i))
        for i in range(processor.get_piece_size())
        if processor.id_to_piece(i) not in SPECIAL_TOKENS
    ]
    return DebertaV2Tokenizer(vocab=pieces)


@pytest.fixture(
    scope="module",
    params=LABEL_ORDERS,
    ids=["upper-entailment-at-1", "mixed-entailment-at-2"],
)
def tiny_dir(request, tmp_path_factory):
    # A randomly initialized sequence classifier and its tokenizer, saved together so the
    # worker loads them from a local directory only. The tokenizer's model_max_length is small
    # so an over-length input must be truncated, and the position table is too short without it.
    root = tmp_path_factory.mktemp("tiny")
    tokenizer = build_tokenizer(root)
    tokenizer.model_max_length = MAX_LENGTH
    torch.manual_seed(0)
    labels = request.param
    config = DebertaV2Config(
        vocab_size=len(tokenizer),
        hidden_size=32,
        num_hidden_layers=2,
        num_attention_heads=2,
        intermediate_size=64,
        max_position_embeddings=MAX_LENGTH + 32,
        num_labels=len(labels),
        id2label=labels,
        label2id={name: index for index, name in labels.items()},
    )
    model_dir = root / "classifier"
    # Saved in float16, so a load that keeps the saved dtype is visible to the test.
    AutoModelForSequenceClassification.from_config(config).half().save_pretrained(model_dir)
    tokenizer.save_pretrained(model_dir)
    return model_dir


@pytest.fixture
def entailment(tiny_dir):
    return nli.Entailment(str(tiny_dir), device="cpu")


def label_index(entailment, name):
    # The index whose id2label name matches `name` ignoring case.
    for index, label in entailment.model.config.id2label.items():
        if label.lower() == name:
            return index
    raise AssertionError(f"no label named {name!r}")


def stub_logits(monkeypatch, entailment, index):
    # Every row gets logits whose argmax is the given label index.
    def forward(**batch):
        logits = torch.zeros(batch["input_ids"].shape[0], len(entailment.model.config.id2label))
        logits[:, index] = 1.0
        return SimpleNamespace(logits=logits)

    monkeypatch.setattr(entailment.model, "forward", forward)


def stub_nli(monkeypatch, entailment, entailed):
    # Each (premise, hypothesis) pair in `entailed` gets entailment; every other pair gets
    # contradiction. Rows are matched on their unpadded token ids.
    entailed_ids = {tuple(entailment.tokenizer(p, h)["input_ids"]) for p, h in entailed}
    contradiction = label_index(entailment, "contradiction")
    entailment_label = label_index(entailment, "entailment")

    def forward(**batch):
        logits = torch.zeros(batch["input_ids"].shape[0], len(entailment.model.config.id2label))
        rows = zip(batch["input_ids"].tolist(), batch["attention_mask"].tolist(), strict=True)
        for row, (ids, mask) in enumerate(rows):
            key = tuple(ids[: sum(mask)])
            label = entailment_label if key in entailed_ids else contradiction
            logits[row, label] = 1.0
        return SimpleNamespace(logits=logits)

    monkeypatch.setattr(entailment.model, "forward", forward)


def stub_one_way(monkeypatch, entailment, entailed):
    # The one-way decision for each (a, b) pair is entailment exactly when it is in `entailed`.
    def decide(pairs):
        return [pair in entailed for pair in pairs]

    monkeypatch.setattr(entailment, "_entails_many", decide)


def test_entailment_label_is_found_regardless_of_case(entailment, monkeypatch):
    stub_logits(monkeypatch, entailment, label_index(entailment, "entailment"))
    assert entailment.holds_many([(PREMISE, HYPOTHESIS)]) == [True]

    for name in ("contradiction", "neutral"):
        stub_logits(monkeypatch, entailment, label_index(entailment, name))
        assert entailment.holds_many([(PREMISE, HYPOTHESIS)]) == [False]


@pytest.mark.parametrize(
    "entailed",
    [
        pytest.param(set(), id="neither"),
        pytest.param({(PREMISE, HYPOTHESIS)}, id="forward-only"),
        pytest.param({(HYPOTHESIS, PREMISE)}, id="backward-only"),
        pytest.param({(PREMISE, HYPOTHESIS), (HYPOTHESIS, PREMISE)}, id="both"),
    ],
)
def test_holds_many_is_entailment_both_ways(entailment, monkeypatch, entailed):
    stub_one_way(monkeypatch, entailment, entailed)
    expected = (PREMISE, HYPOTHESIS) in entailed and (HYPOTHESIS, PREMISE) in entailed

    assert entailment.holds_many([(PREMISE, HYPOTHESIS)]) == [expected]


def test_holds_many_runs_passes_of_at_most_16_rows_and_keeps_order(entailment, monkeypatch):
    # 20 pairs make 40 rows, which must reach the classifier in passes of at most 16.
    stub_nli(monkeypatch, entailment, {(PREMISE, HYPOTHESIS), (HYPOTHESIS, PREMISE)})
    stubbed = entailment.model.forward
    row_counts = []

    def counting(**batch):
        row_counts.append(batch["input_ids"].shape[0])
        return stubbed(**batch)

    monkeypatch.setattr(entailment.model, "forward", counting)
    # The holding pairs follow no period, so results moved between passes change the list.
    expected = [i in {0, 3, 4, 6, 10, 11, 12, 15, 17, 18} for i in range(20)]
    pairs = [(PREMISE, HYPOTHESIS) if holds else (PREMISE, OTHER) for holds in expected]

    result = entailment.holds_many(pairs)

    assert nli.BATCH_SIZE == 16
    assert max(row_counts) <= nli.BATCH_SIZE
    assert sum(row_counts) == 2 * len(pairs)
    assert result == expected
    assert all(type(value) is bool for value in result)
    assert entailment.holds_many([]) == []


def test_over_length_pair_is_truncated_without_error(entailment):
    # The tokenizer truncates the pair to model_max_length, which keeps this input inside the
    # classifier's position table. Without truncation the forward pass fails.
    assert len(entailment.tokenizer(LONG, HYPOTHESIS)["input_ids"]) > MAX_LENGTH

    result = entailment.holds_many([(LONG, HYPOTHESIS)])

    assert len(result) == 1
    assert type(result[0]) is bool


def test_loads_fp32_with_remote_code_disabled(tiny_dir, monkeypatch):
    calls = []

    def record(load):
        def wrapped(name, **kwargs):
            calls.append(kwargs.get("trust_remote_code", "unset"))
            return load(name, **kwargs)

        return wrapped

    monkeypatch.setattr(
        AutoModelForSequenceClassification,
        "from_pretrained",
        record(AutoModelForSequenceClassification.from_pretrained),
    )
    monkeypatch.setattr(
        AutoTokenizer,
        "from_pretrained",
        record(AutoTokenizer.from_pretrained),
    )

    loaded = nli.Entailment(str(tiny_dir), device="cpu")

    assert calls == [False, False]
    assert next(loaded.model.parameters()).dtype is torch.float32


def test_device_defaults_to_cpu_without_cuda(tiny_dir, monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    assert nli.Entailment(str(tiny_dir)).device == "cpu"


class RecordingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.events = []

    def write(self, text):
        self.events.append("write")
        return super().write(text)

    def flush(self):
        self.events.append("flush")
        super().flush()


def test_serve_answers_each_request_and_rejects_invalid_lines(entailment, monkeypatch):
    stub_one_way(monkeypatch, entailment, {(PREMISE, HYPOTHESIS), (HYPOTHESIS, PREMISE)})
    lines = [
        json.dumps({"pairs": [[PREMISE, HYPOTHESIS], [PREMISE, OTHER]]}),
        json.dumps({"pairs": [[HYPOTHESIS, PREMISE]]}),
        OTHER,
        json.dumps({"pairs": [[OTHER]]}),
    ]
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()

    nli.serve(entailment, stdin, stdout)

    output = stdout.getvalue()
    assert [json.loads(line) for line in output.splitlines()] == [
        {"holds": [True, False]},
        {"holds": [True]},
        {"error": "invalid request"},
        {"error": "invalid request"},
    ]
    for text in (PREMISE, HYPOTHESIS, OTHER):
        assert text not in output


def test_serve_flushes_after_each_response(entailment, monkeypatch):
    stub_one_way(monkeypatch, entailment, {(PREMISE, HYPOTHESIS), (HYPOTHESIS, PREMISE)})
    stdin = io.StringIO(json.dumps({"pairs": [[PREMISE, HYPOTHESIS]]}) + "\n" + json.dumps({"pairs": []}) + "\n")
    stdout = RecordingStream()

    nli.serve(entailment, stdin, stdout)

    assert stdout.events == ["write", "flush", "write", "flush"]


def test_main_serves_stdin_with_the_requested_model(tiny_dir, monkeypatch):
    stdin = io.StringIO(json.dumps({"pairs": [[PREMISE, HYPOTHESIS]]}) + "\n")
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    nli.main(["serve", "--model", str(tiny_dir)])

    response = json.loads(stdout.getvalue())
    assert list(response) == ["holds"]
    assert len(response["holds"]) == 1
    assert type(response["holds"][0]) is bool
