"""Requirement R3: the classifier trains on JSONL pairs into a private directory and serves sigmoid scores over JSON Lines.

Tests use a tiny randomly initialized DeBERTa-v2 model and synthetic strings, and never download a model.
"""

import functools
import io
import json
import pathlib
import re
import stat

import pytest
import sentencepiece as spm
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer, DebertaV2Config, DebertaV2Tokenizer

from tools.voice import classify

# Synthetic stand-in for private text. It must never appear in an output or error message.
SENTINEL = "SENTINEL-PRIVATE-TEXT-7f21"
SPECIAL_TOKENS = ["[PAD]", "[CLS]", "[SEP]", "[MASK]", "[UNK]"]
CORPUS = [
    "qxz zxq qzx xqz zqx qxzq",
    "mnw wnm nwm wmn mwn nmwm",
    "abc def ghi jkl",
    SENTINEL,
]
GAME_TEXTS = ["qxz zxq qzx", "xqz qxzq zxq", "zqx qzx qxz"]
LLM_TEXTS = ["mnw wnm nwm", "wmn mwn nmwm", "nwm wnm mwn"]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Every test loads from local directories only, so no download is ever attempted.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture(scope="module")
def tiny_base(tmp_path_factory):
    # A randomly initialized DeBERTa-v2 sequence classifier with one logit, saved with a character-level tokenizer.
    root = tmp_path_factory.mktemp("tiny")
    corpus = root / "corpus.txt"
    corpus.write_text("\n".join(CORPUS) + "\n", encoding="utf-8")
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
    tokenizer = DebertaV2Tokenizer(vocab=pieces)
    torch.manual_seed(0)
    config = DebertaV2Config(
        vocab_size=len(tokenizer),
        hidden_size=32,
        num_hidden_layers=2,
        num_attention_heads=2,
        intermediate_size=64,
        initializer_range=0.5,
        max_position_embeddings=256,
        num_labels=1,
    )
    base = root / "base"
    AutoModelForSequenceClassification.from_config(config).save_pretrained(base)
    tokenizer.save_pretrained(base)
    return base


def write_examples(path: pathlib.Path, rows: list[tuple[str, int]]) -> pathlib.Path:
    path.write_text("".join(json.dumps({"text": text, "label": label}) + "\n" for text, label in rows), encoding="utf-8")
    return path


def separable_rows() -> list[tuple[str, int]]:
    return [(text, 1) for text in GAME_TEXTS] * 4 + [(text, 0) for text in LLM_TEXTS] * 4


@pytest.fixture(scope="module")
def trained(tiny_base, tmp_path_factory):
    # One training run shared by the tests that only read its output.
    root = tmp_path_factory.mktemp("trained")
    examples = write_examples(root / "examples.jsonl", separable_rows())
    out_dir = root / "model"
    classify.train(examples, out_dir, base_model=str(tiny_base), epochs=40, learning_rate=1e-3)
    return out_dir


def load(out_dir: pathlib.Path):
    tokenizer = AutoTokenizer.from_pretrained(out_dir)
    model = AutoModelForSequenceClassification.from_pretrained(out_dir, dtype=torch.float32)
    model.eval()
    return model, tokenizer


def test_train_saves_loadable_model_in_private_directory(trained):
    assert stat.S_IMODE(trained.stat().st_mode) == 0o700
    model, tokenizer = load(trained)
    assert model.config.num_labels == 1
    assert tokenizer("qxz", add_special_tokens=False)["input_ids"]


def test_train_stdout_carries_only_numeric_step_lines(tiny_base, tmp_path, capsys):
    rows = [(SENTINEL, 1), ("mnw wnm", 0), ("qxz zxq", 1), ("wmn mwn", 0), ("zqx qzx", 1)]
    examples = write_examples(tmp_path / "examples.jsonl", rows)
    classify.train(examples, tmp_path / "model", base_model=str(tiny_base), batch_size=2)
    lines = capsys.readouterr().out.splitlines()
    # Five examples in batches of two make three steps per epoch, and one epoch runs by default.
    assert len(lines) == 3
    for line in lines:
        assert re.fullmatch(r"\d+\t\d+\t\d+\.\d{6}", line), line
    assert SENTINEL not in "\n".join(lines)


def test_separable_set_trains_to_correct_side_of_half(trained):
    model, tokenizer = load(trained)
    game = classify.scores(model, tokenizer, GAME_TEXTS)
    llm = classify.scores(model, tokenizer, LLM_TEXTS)
    assert all(score > 0.5 for score in game)
    assert all(score < 0.5 for score in llm)


def test_train_loss_falls_on_separable_set(tiny_base, tmp_path, capsys):
    examples = write_examples(tmp_path / "examples.jsonl", separable_rows())
    classify.train(examples, tmp_path / "model", base_model=str(tiny_base), epochs=40, learning_rate=1e-3)
    lines = capsys.readouterr().out.splitlines()
    steps_per_epoch = -(-len(separable_rows()) // classify.BATCH_SIZE)
    assert len(lines) == 40 * steps_per_epoch
    losses = [float(line.split("\t")[2]) for line in lines]
    assert losses[-1] < losses[0]


def test_scores_are_sigmoid_in_unit_interval_and_keep_order(trained):
    model, tokenizer = load(trained)
    texts = GAME_TEXTS + LLM_TEXTS
    batched = classify.scores(model, tokenizer, texts)
    assert len(batched) == len(texts)
    assert all(0.0 <= score <= 1.0 for score in batched)
    one_by_one = [classify.scores(model, tokenizer, [text])[0] for text in texts]
    assert batched == pytest.approx(one_by_one, rel=1e-4, abs=1e-5)


def test_scores_restore_training_flag(trained):
    model, tokenizer = load(trained)
    model.train()
    classify.scores(model, tokenizer, GAME_TEXTS)
    assert model.training


def test_scores_truncate_to_max_tokens(trained):
    model, tokenizer = load(trained)
    seen: list[int] = []
    model.register_forward_pre_hook(lambda _, __, kwargs: seen.append(kwargs["input_ids"].shape[1]), with_kwargs=True)
    classify.scores(model, tokenizer, ["qxz " * 400])
    assert seen and max(seen) <= classify.MAX_TOKENS


def test_serve_answers_requests_and_rejects_invalid_line(trained, monkeypatch):
    stdin = io.StringIO(json.dumps({"texts": GAME_TEXTS}) + "\n" + "not json\n" + json.dumps({"texts": LLM_TEXTS}) + "\n")
    stdout = io.StringIO()
    classify.serve(trained, stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(replies) == 3
    assert len(replies[0]["scores"]) == len(GAME_TEXTS)
    assert replies[1] == {"error": "invalid request"}
    assert len(replies[2]["scores"]) == len(LLM_TEXTS)
    assert all(0.0 <= score <= 1.0 for score in replies[0]["scores"] + replies[2]["scores"])


@pytest.mark.parametrize("line", ['{"texts": "qxz"}', '{"texts": [1, 2]}', '["qxz"]', '{"other": []}', "null"])
def test_serve_rejects_wrong_request_shapes(trained, line):
    stdout = io.StringIO()
    classify.serve(trained, io.StringIO(line + "\n"), stdout)
    assert [json.loads(reply) for reply in stdout.getvalue().splitlines()] == [{"error": "invalid request"}]


def test_serve_output_never_carries_request_text(trained):
    stdout = io.StringIO()
    classify.serve(trained, io.StringIO(json.dumps({"texts": [SENTINEL]}) + "\n"), stdout)
    assert SENTINEL not in stdout.getvalue()


def test_main_trains_then_serves(tiny_base, tmp_path, monkeypatch, capsys):
    # The command line has no base-model option, so the tiny local model stands in for the default.
    monkeypatch.setattr(classify, "train", functools.partial(classify.train, base_model=str(tiny_base)))
    examples = write_examples(tmp_path / "examples.jsonl", separable_rows())
    out_dir = tmp_path / "model"
    classify.main(["train", str(examples), str(out_dir), "--epochs", "1"])
    assert "\t" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"texts": GAME_TEXTS}) + "\n"))
    classify.main(["serve", str(out_dir)])
    reply = json.loads(capsys.readouterr().out)
    assert len(reply["scores"]) == len(GAME_TEXTS)
