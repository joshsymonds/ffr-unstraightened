"""Tests for the GPU-host LoRA training job. Every model here is a tiny randomly initialized Gemma 4 saved to a local
directory, with a character tokenizer and synthetic strings. Nothing is downloaded: HF_HUB_OFFLINE is set and every
model path is local.
"""

import inspect
import json
import re
import stat
import string

import pytest
import torch
from peft import LoraConfig, get_peft_model
from safetensors.torch import load_file
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Gemma4Config,
    Gemma4ForCausalLM,
    Gemma4ForConditionalGeneration,
    Gemma4TextConfig,
    Gemma4UnifiedConfig,
    Gemma4UnifiedForConditionalGeneration,
    Gemma4UnifiedTextConfig,
    PreTrainedTokenizerFast,
)

from tools.voice import finetune

SPECIAL_TOKENS = ["[PAD]", "[UNK]", "[BOS]", "[EOS]"]

# Synthetic stand-ins. The sentinels must never reach stdout or an error message.
PROMPT_SENTINEL = "SENTINEL-PROMPT-TEXT"
TARGET_SENTINEL = "SENTINEL-TARGET-TEXT"

NUMERIC_STEP_LINE = re.compile(r"^\d+\t\d+\t\d+\.\d{6}$")


@pytest.fixture(scope="module")
def tiny_causal_dir(tmp_path_factory):
    # A randomly initialized Gemma 4 text model and its character tokenizer, saved together so the job loads both from a
    # local directory.
    root = tmp_path_factory.mktemp("tiny-causal")
    vocab = {token: index for index, token in enumerate([*SPECIAL_TOKENS, *string.printable])}
    backend = Tokenizer(models.WordLevel(vocab=vocab, unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.Split("", "isolated")
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend,
        bos_token="[BOS]",
        eos_token="[EOS]",
        pad_token="[PAD]",
        unk_token="[UNK]",
    )
    size = len(vocab)
    config = Gemma4TextConfig(
        vocab_size=size,
        vocab_size_per_layer_input=size,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=2,
        num_key_value_heads=1,
        head_dim=16,
        global_head_dim=16,
        hidden_size_per_layer_input=8,
        sliding_window=64,
        pad_token_id=vocab["[PAD]"],
        bos_token_id=vocab["[BOS]"],
        eos_token_id=vocab["[EOS]"],
    )
    torch.manual_seed(0)
    Gemma4ForCausalLM(config).save_pretrained(root)
    tokenizer.save_pretrained(root)
    return root


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def tokenizer(tiny_causal_dir):
    return AutoTokenizer.from_pretrained(tiny_causal_dir, trust_remote_code=False)


def write_records(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


SYNTHETIC_ROWS = [
    {"prompt": "Synthetic prompt one.", "target": "Synthetic target one."},
    {"prompt": "Synthetic prompt two.", "target": "Synthetic target two."},
    {"prompt": "Synthetic prompt three.", "target": "Synthetic target three."},
]


def run_train(root, model_dir, out_dir, rows=SYNTHETIC_ROWS, **kwargs):
    records = write_records(root / "records.jsonl", rows)
    finetune.train(records, out_dir, base_model=str(model_dir), **kwargs)
    return out_dir


# Frozen interface.


def test_frozen_constants_and_train_defaults():
    assert finetune.BASE_MODEL == "google/gemma-4-E4B-it"
    assert finetune.MAX_TOKENS == 512
    assert finetune.BATCH_SIZE == 8
    assert finetune.LEARNING_RATE == 2e-4
    assert finetune.EPOCHS == 1
    parameters = inspect.signature(finetune.train).parameters
    assert parameters["base_model"].default == finetune.BASE_MODEL
    assert parameters["epochs"].default == finetune.EPOCHS
    assert parameters["batch_size"].default == finetune.BATCH_SIZE
    assert parameters["learning_rate"].default == finetune.LEARNING_RATE
    assert parameters["records"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["out_dir"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["base_model"].kind is inspect.Parameter.KEYWORD_ONLY


# encode.


def test_encode_masks_the_prompt_and_labels_the_target_with_an_eos(tokenizer):
    prompt = "Synthetic prompt."
    target = "Synthetic target."
    encoded = finetune.encode(tokenizer, prompt, target)
    assert encoded is not None
    ids, labels = encoded
    prompt_ids = tokenizer(prompt)["input_ids"]
    answer_ids = tokenizer(target, add_special_tokens=False)["input_ids"]
    assert ids == prompt_ids + answer_ids + [tokenizer.eos_token_id]
    assert labels == [finetune.IGNORE_INDEX] * len(prompt_ids) + answer_ids + [tokenizer.eos_token_id]


def test_encode_cuts_at_max_tokens_and_keeps_the_target_prefix(tokenizer):
    prompt = "Synthetic prompt."
    target = "lorem ipsum " * 200
    ids, labels = finetune.encode(tokenizer, prompt, target)
    prompt_ids = tokenizer(prompt)["input_ids"]
    answer_ids = tokenizer(target, add_special_tokens=False)["input_ids"]
    assert len(answer_ids) > finetune.MAX_TOKENS
    assert len(ids) == finetune.MAX_TOKENS
    assert len(labels) == finetune.MAX_TOKENS
    assert labels[: len(prompt_ids)] == [finetune.IGNORE_INDEX] * len(prompt_ids)
    # The cut target has no EOS, since the EOS sits past MAX_TOKENS.
    kept = finetune.MAX_TOKENS - len(prompt_ids)
    assert labels[len(prompt_ids) :] == answer_ids[:kept]
    assert tokenizer.eos_token_id not in labels


def test_encode_keeps_one_target_token_when_exactly_one_fits(tokenizer):
    prompt = "a" * (finetune.MAX_TOKENS - 1)
    assert len(tokenizer(prompt)["input_ids"]) == finetune.MAX_TOKENS - 1
    ids, labels = finetune.encode(tokenizer, prompt, "Target.")
    assert len(ids) == finetune.MAX_TOKENS
    assert labels[-1] == tokenizer("Target.", add_special_tokens=False)["input_ids"][0]
    assert sum(label != finetune.IGNORE_INDEX for label in labels) == 1


def test_encode_returns_none_when_the_prompt_leaves_no_target_token(tokenizer):
    prompt = "a" * finetune.MAX_TOKENS
    assert len(tokenizer(prompt)["input_ids"]) == finetune.MAX_TOKENS
    assert finetune.encode(tokenizer, prompt, TARGET_SENTINEL) is None


# Chunked answer loss equals the model's own loss.

LOSS_VOCAB = 40
# Small enough that tanh(logit / cap) is far from linear on the tiny logits, so a missed cap changes the loss.
LOSS_SOFTCAP = 0.1
ARCHITECTURES = ["text", "multimodal", "unified"]


def tiny_loss_model(architecture, softcap):
    shared = dict(
        vocab_size=LOSS_VOCAB,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=2,
        num_key_value_heads=1,
        head_dim=16,
        sliding_window=64,
        final_logit_softcapping=softcap,
    )
    text = dict(
        shared,
        global_head_dim=16,
        vocab_size_per_layer_input=LOSS_VOCAB,
        hidden_size_per_layer_input=8,
    )
    torch.manual_seed(0)
    if architecture == "text":
        model = Gemma4ForCausalLM(Gemma4TextConfig(**text))
    elif architecture == "multimodal":
        model = Gemma4ForConditionalGeneration(
            Gemma4Config(
                text_config=Gemma4TextConfig(**text),
                vision_config=None,
                audio_config=None,
            )
        )
    else:
        model = Gemma4UnifiedForConditionalGeneration(
            Gemma4UnifiedConfig(
                text_config=Gemma4UnifiedTextConfig(**shared),
                vision_config=None,
                audio_config=None,
            )
        )
    model = get_peft_model(
        model,
        LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            target_modules="all-linear",
            task_type="CAUSAL_LM",
        ),
    )
    # lora_B starts at zero, which would leave every lora_A gradient zero, so every LoRA weight is randomized.
    with torch.no_grad():
        for name, parameter in model.named_parameters():
            if ".lora_" in name:
                parameter.normal_(std=0.1)
    model.train()
    return model


def loss_example():
    # Fourteen positions, the first five a masked prompt, so nine targets after the shift.
    ids = torch.tensor([[2, 10, 11, 12, 13, 20, 21, 22, 23, 24, 25, 26, 27, 3]])
    labels = ids.clone()
    labels[0, :5] = finetune.IGNORE_INDEX
    return ids, labels


def loss_and_grads(model, compute):
    model.zero_grad(set_to_none=True)
    torch.manual_seed(1)
    loss = compute()
    loss.backward()
    grads = {name: parameter.grad.clone() for name, parameter in model.named_parameters() if parameter.requires_grad}
    return loss.detach(), grads


@pytest.mark.parametrize("chunk", [1, 4, 128])
@pytest.mark.parametrize("softcap", [None, LOSS_SOFTCAP])
@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_answer_loss_and_lora_gradients_equal_the_models_own_loss(architecture, softcap, chunk):
    model = tiny_loss_model(architecture, softcap)
    ids, labels = loss_example()
    own, own_grads = loss_and_grads(model, lambda: model(input_ids=ids, labels=labels).loss)
    chunked, chunked_grads = loss_and_grads(model, lambda: finetune._answer_loss(model, ids, labels, chunk))
    torch.testing.assert_close(chunked, own)
    assert own_grads
    assert set(chunked_grads) == set(own_grads)
    assert all(".lora_" in name for name in own_grads)
    for name, grad in own_grads.items():
        assert torch.count_nonzero(grad) > 0
        torch.testing.assert_close(chunked_grads[name], grad)


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_answer_loss_softcap_case_changes_the_models_own_loss(architecture):
    # Proves the softcap case above exercises the cap: without it the same weights give a loss the equality check rejects.
    model = tiny_loss_model(architecture, LOSS_SOFTCAP)
    model.eval()
    ids, labels = loss_example()
    with torch.no_grad():
        capped = model(input_ids=ids, labels=labels).loss
        model.get_base_model().config.get_text_config().final_logit_softcapping = None
        uncapped = model(input_ids=ids, labels=labels).loss
    with pytest.raises(AssertionError):
        torch.testing.assert_close(uncapped, capped)


def test_answer_loss_default_chunk_is_128():
    assert finetune.LOSS_CHUNK == 128
    parameters = inspect.signature(finetune._answer_loss).parameters
    assert parameters["chunk"].default == finetune.LOSS_CHUNK


@pytest.mark.parametrize("architecture", ARCHITECTURES)
def test_answer_loss_runs_the_head_on_answer_positions_one_chunk_at_a_time(
    architecture,
):
    model = tiny_loss_model(architecture, LOSS_SOFTCAP)
    head = model.get_base_model().get_output_embeddings()
    rows = []
    head.register_forward_hook(lambda module, args, output: rows.append(args[0].shape[:-1]))
    ids, labels = loss_example()
    loss = finetune._answer_loss(model, ids, labels, 4)
    # Forward: the nine answer positions in chunks of 4, 4 and 1, never a logit for a prompt position.
    assert rows == [torch.Size([4]), torch.Size([4]), torch.Size([1])]
    loss.backward()
    # Backward recomputes each chunk's logits, last chunk first.
    assert rows[3:] == [torch.Size([1]), torch.Size([4]), torch.Size([4])]


# Training run on CPU.


def record_adapter_model(monkeypatch):
    # Captures the peft model train wraps, with a copy of every parameter as it was before training.
    wrapped = []
    original = finetune.get_peft_model

    def capture(*args, **kwargs):
        model = original(*args, **kwargs)
        before = {name: parameter.detach().clone() for name, parameter in model.named_parameters()}
        wrapped.append((model, before))
        return model

    monkeypatch.setattr(finetune, "get_peft_model", capture)
    return wrapped


def test_train_updates_only_lora_weights_and_saves_the_frozen_adapter(tmp_path, tiny_causal_dir, monkeypatch):
    wrapped = record_adapter_model(monkeypatch)
    out = run_train(
        tmp_path,
        tiny_causal_dir,
        tmp_path / "made" / "nested" / "adapter",
        epochs=2,
        batch_size=1,
        learning_rate=1e-2,
    )
    [(model, before)] = wrapped
    changed = {name for name, parameter in model.named_parameters() if not torch.equal(parameter.detach(), before[name])}
    assert changed
    assert all(".lora_" in name for name in changed)

    config = json.loads((out / "adapter_config.json").read_text(encoding="utf-8"))
    assert config["r"] == 16
    assert config["lora_alpha"] == 32
    assert config["lora_dropout"] == 0.05
    assert config["task_type"] == "CAUSAL_LM"

    tensors = load_file(str(out / "adapter_model.safetensors"))
    assert tensors
    assert all(".lora_" in name for name in tensors)
    # all-linear: the adapter covers every linear projection of the base model except the output head.
    base = AutoModelForCausalLM.from_pretrained(tiny_causal_dir, trust_remote_code=False)
    linear = {name for name, module in base.named_modules() if isinstance(module, torch.nn.Linear) and name != "lm_head"}
    covered = {name.removeprefix("base_model.model.").split(".lora_")[0] for name in tensors}
    assert covered == linear
    assert any(torch.count_nonzero(tensor) > 0 for name, tensor in tensors.items() if ".lora_B." in name)

    for directory in (
        tmp_path / "made",
        tmp_path / "made" / "nested",
        out,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_train_prints_only_numeric_step_lines_and_no_record_text(tmp_path, tiny_causal_dir, capsys):
    rows = [
        {"prompt": PROMPT_SENTINEL, "target": TARGET_SENTINEL},
        {"prompt": "Synthetic prompt four.", "target": "Synthetic target four."},
        {"prompt": "Synthetic prompt five.", "target": "Synthetic target five."},
    ]
    run_train(tmp_path, tiny_causal_dir, tmp_path / "adapter", rows=rows, batch_size=2)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(NUMERIC_STEP_LINE.match(line) for line in lines)
    assert [line.split("\t")[:2] for line in lines] == [["0", "0"], ["0", "1"]]
    assert PROMPT_SENTINEL not in "\n".join(lines)
    assert TARGET_SENTINEL not in "\n".join(lines)


def test_train_error_on_a_malformed_record_never_carries_its_text(tmp_path, tiny_causal_dir):
    rows = [{"prompt": PROMPT_SENTINEL}]
    records = write_records(tmp_path / "records.jsonl", rows)
    with pytest.raises(KeyError) as excinfo:
        finetune.train(records, tmp_path / "adapter", base_model=str(tiny_causal_dir))
    assert PROMPT_SENTINEL not in str(excinfo.value)
    assert not (tmp_path / "adapter").exists()


def test_train_uses_the_cpu_path_without_waiting_for_vram(tmp_path, tiny_causal_dir, monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert finetune._device() == torch.device("cpu")
    run_train(tmp_path, tiny_causal_dir, tmp_path / "adapter")
    assert (tmp_path / "adapter" / "adapter_model.safetensors").is_file()


# Preparation of the CUDA load, checked on CPU with a tiny model.


def test_prepare_moves_the_per_layer_table_to_cpu_hooks_its_lookups_and_keeps_the_forward(
    tiny_causal_dir,
):
    model = AutoModelForCausalLM.from_pretrained(tiny_causal_dir, trust_remote_code=False)
    model.eval()
    ids = torch.tensor([[2, 10, 11, 12, 3]])
    with torch.no_grad():
        before = model(input_ids=ids).logits
    keys = set(model.state_dict())
    tables = [module for name, module in model.named_modules() if name.rpartition(".")[2] == finetune.PER_LAYER_TABLE]
    assert len(tables) == 1
    table = tables[0]
    # This host has only a CPU, so the move is recorded to show _prepare asks for CPU and no dtype.
    moves = []
    move = table.to

    def record(*args, **kwargs):
        moves.append((args, kwargs))
        return move(*args, **kwargs)

    table.to = record
    finetune._prepare(model)
    assert moves == [(("cpu",), {})]
    assert finetune._lookup_on_cpu in table._forward_pre_hooks.values()
    assert finetune._result_to_caller in table._forward_hooks.values()
    assert set(model.state_dict()) == keys
    with torch.no_grad():
        after = model(input_ids=ids).logits
    torch.testing.assert_close(after, before)


# This host has only a CPU, so the two tests below call the hooks directly with a device other than CPU.
class FakeIds:
    def __init__(self, device):
        self.device = device
        self.moves = []

    def to(self, *args, **kwargs):
        self.moves.append((args, kwargs))
        return "moved"


def test_lookup_hook_sends_ids_to_cpu_and_records_the_caller_device():
    table = torch.nn.Module()
    ids = FakeIds(torch.device("cuda", 0))
    rest = object()
    args = finetune._lookup_on_cpu(table, (ids, rest))
    assert ids.moves == [(("cpu",), {})]
    assert args == ("moved", rest)
    assert table.caller_device == torch.device("cuda", 0)


def test_result_hook_moves_the_output_to_the_caller_device():
    table = torch.nn.Module()
    table.caller_device = torch.device("meta")
    result = finetune._result_to_caller(table, (), torch.ones(2))
    assert result.device == torch.device("meta")


# Command line.


def test_main_passes_the_records_adapter_dir_and_epochs_to_train(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        finetune,
        "train",
        lambda records, out_dir, **kwargs: calls.append((records, out_dir, kwargs)),
    )
    finetune.main(["train", str(tmp_path / "r.jsonl"), str(tmp_path / "a"), "--epochs", "2"])
    assert calls == [
        (
            tmp_path / "r.jsonl",
            tmp_path / "a",
            {"epochs": 2},
        )
    ]


def test_main_defaults_to_the_frozen_epochs(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        finetune,
        "train",
        lambda records, out_dir, **kwargs: calls.append(kwargs),
    )
    finetune.main(["train", str(tmp_path / "r.jsonl"), str(tmp_path / "a")])
    assert calls == [{"epochs": finetune.EPOCHS}]
