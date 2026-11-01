"""LoRA training job for the GPU host. It trains an adapter on 4-bit Gemma 4 E4B from JSON Lines records of
{"prompt": str, "target": str}, computes the loss on target tokens only, and saves the adapter.

Run from the scratch directory as `python3 -m tools.voice.finetune train <records> <adapter_dir> [--epochs N] [--base-model NAME]`.
Stdout carries one `epoch<TAB>step<TAB>loss` line per optimizer step and nothing else. Errors never carry record text.
"""

import argparse
import json
import math
import pathlib
import random
from collections.abc import Sequence

import torch
import torch.utils.checkpoint
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

BASE_MODEL = "google/gemma-4-E4B-it"
MAX_TOKENS = 512
BATCH_SIZE = 8
LEARNING_RATE = 2e-4
EPOCHS = 1

# Label value that the loss ignores, so prompt positions do not count toward the loss.
IGNORE_INDEX = -100
# The per-layer embedding table's attribute name in Gemma 4 E2B and E4B.
PER_LAYER_TABLE = "embed_tokens_per_layer"
# Answer positions per chunk of the training loss. A chunk of Gemma 4's 262k-vocabulary logits is 128 MiB in fp32.
LOSS_CHUNK = 128
# The learning rate rises linearly over this share of the steps, then falls linearly to zero, and gradients are
# clipped to MAX_GRAD_NORM.
WARMUP_FRACTION = 0.06
MAX_GRAD_NORM = 1.0
SHUFFLE_SEED = 0


def _device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def _autocast(device: torch.device):
    return torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=device.type == "cuda")


def _lr_factor(step: int, warmup: int, total: int) -> float:
    # Linear warmup reaches the full rate at step warmup - 1, then linear decay ends one step past the last.
    if step < warmup:
        return (step + 1) / warmup
    return (total - step) / (total - warmup + 1)


def _lookup_on_cpu(table: torch.nn.Module, args: tuple) -> tuple:
    # The caller's device is kept on the table, so the result can go back to it.
    ids, *rest = args
    table.caller_device = ids.device
    return (ids.to("cpu"), *rest)


def _result_to_caller(table: torch.nn.Module, args: tuple, output: torch.Tensor) -> torch.Tensor:
    return output.to(table.caller_device)


def _prepare(model) -> None:
    # Non-reentrant checkpointing recomputes each layer's activations in backward, so a step holds far fewer of them.
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    # E4B's per-layer table is 5.25 GiB of bf16 and is only ever looked up, so it moves to CPU in its own dtype and only
    # each lookup's result comes back to the caller's device.
    # The hooks leave the module in place under its own name, so the state-dict keys and the saved adapter are the same
    # as without them.
    for name, module in model.named_modules():
        if name.rpartition(".")[2] == PER_LAYER_TABLE:
            module.to("cpu")
            module.register_forward_pre_hook(_lookup_on_cpu)
            module.register_forward_hook(_result_to_caller)


def _load(name: str, device: torch.device):
    # The CUDA branch loads 4-bit nf4 weights with bfloat16 compute. The CPU branch loads full precision with no
    # quantization.
    tokenizer = AutoTokenizer.from_pretrained(name, trust_remote_code=False)
    if device.type == "cuda":
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        model = AutoModelForCausalLM.from_pretrained(name, quantization_config=quantization, trust_remote_code=False)
        # peft's prepare_model_for_kbit_training is not used: it upcasts every unquantized weight to fp32, which
        # doubled E4B's bf16 per-layer table and ran out of memory on the 16 GB GPU.
        _prepare(model)
    else:
        model = AutoModelForCausalLM.from_pretrained(name, trust_remote_code=False)
    return tokenizer, model


def encode(tokenizer, prompt: str, target: str) -> tuple[list[int], list[int]] | None:
    # The prompt, already formatted, is labelled IGNORE_INDEX. The target is labelled with its ids and an EOS. The pair is
    # cut at MAX_TOKENS. A pair with no target token left after the cut is skipped, which is returned as None.
    prompt_ids = tokenizer(prompt)["input_ids"]
    answer = tokenizer(target, add_special_tokens=False)["input_ids"] + [tokenizer.eos_token_id]
    ids = (prompt_ids + answer)[:MAX_TOKENS]
    labels = ([IGNORE_INDEX] * len(prompt_ids) + answer)[:MAX_TOKENS]
    if all(label == IGNORE_INDEX for label in labels):
        return None
    return ids, labels


def _targets(labels: list[int]) -> int:
    return sum(label != IGNORE_INDEX for label in labels)


def _head_loss(
    head: torch.nn.Module,
    softcap: float | None,
    hidden: torch.Tensor,
    targets: torch.Tensor,
) -> torch.Tensor:
    # The output head and final-logit softcap as the Gemma 4 forwards apply them, then the summed token CE on fp32
    # logits as transformers' ForCausalLMLoss computes it.
    logits = head(hidden)
    if softcap is not None:
        logits = logits / softcap
        logits = torch.tanh(logits)
        logits = logits * softcap
    return torch.nn.functional.cross_entropy(logits.float(), targets, reduction="sum")


def _answer_loss(
    model,
    input_ids: torch.Tensor,
    labels: torch.Tensor,
    chunk: int = LOSS_CHUNK,
) -> torch.Tensor:
    # Equal to model(input_ids=input_ids, labels=labels).loss, the mean token CE over target tokens with position i
    # predicting label i + 1, without the logits for every position. Gemma 4's vocabulary is 262k, so 1024 positions of
    # logits are about 3 GiB.
    # The body is the module each Gemma 4 forward runs before its head (base_model, the model.model attribute). For
    # Gemma4ForConditionalGeneration and Gemma4UnifiedForConditionalGeneration that is the multimodal model, which builds
    # the embeddings, per-layer inputs and masks; get_decoder would return the inner language model and skip them.
    base = model.get_base_model() if isinstance(model, PeftModel) else model
    hidden = base.base_model(input_ids=input_ids).last_hidden_state
    shifted = labels[:, 1:]
    answer = shifted != IGNORE_INDEX
    hidden = hidden[:, :-1][answer]
    targets = shifted[answer]
    head = base.get_output_embeddings()
    softcap = base.config.get_text_config().final_logit_softcapping
    # Non-reentrant checkpointing frees each chunk's logits after its loss and recomputes them in backward, so a step
    # holds one chunk of logits at a time.
    total = hidden.new_zeros((), dtype=torch.float32)
    for start in range(0, len(targets), chunk):
        total = total + torch.utils.checkpoint.checkpoint(
            _head_loss,
            head,
            softcap,
            hidden[start : start + chunk],
            targets[start : start + chunk],
            use_reentrant=False,
        )
    return total / len(targets)


def _fit(
    model,
    examples: list[tuple[list[int], list[int]]],
    device: torch.device,
    epochs: int,
    batch_size: int,
    learning_rate: float,
) -> None:
    trainable = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=learning_rate)
    total_steps = epochs * math.ceil(len(examples) / batch_size)
    warmup = max(1, round(total_steps * WARMUP_FRACTION))
    schedule = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda step: _lr_factor(step, warmup, total_steps))
    shuffle = random.Random(SHUFFLE_SEED)
    model.train()
    for epoch in range(epochs):
        order = list(range(len(examples)))
        shuffle.shuffle(order)
        for step, start in enumerate(range(0, len(order), batch_size)):
            batch = [examples[i] for i in order[start : start + batch_size]]
            batch_targets = sum(_targets(labels) for _, labels in batch)
            optimizer.zero_grad()
            total = 0.0
            for ids, labels in batch:
                with _autocast(device):
                    example_loss = _answer_loss(
                        model,
                        torch.tensor([ids], device=device),
                        torch.tensor([labels], device=device),
                    )
                # Each example's mean token loss is weighted by its share of the batch's target tokens, so the summed
                # gradients equal the gradient of the token mean over the batch.
                loss = example_loss.float() * (_targets(labels) / batch_targets)
                loss.backward()
                total += loss.item()
            torch.nn.utils.clip_grad_norm_(trainable, MAX_GRAD_NORM)
            optimizer.step()
            schedule.step()
            print(f"{epoch}\t{step}\t{total:.6f}")


def _private_directory(path: pathlib.Path) -> None:
    # Each missing directory on the way down is created at mode 0700, so the adapter's parents are private as well.
    missing = [directory for directory in (path, *path.parents) if not directory.exists()]
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def train(
    records: pathlib.Path,
    out_dir: pathlib.Path,
    *,
    base_model: str = BASE_MODEL,
    epochs: int = EPOCHS,
    batch_size: int = BATCH_SIZE,
    learning_rate: float = LEARNING_RATE,
) -> None:
    device = _device()
    tokenizer, model = _load(base_model, device)
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
    examples = []
    with records.open(encoding="utf-8") as lines:
        for line in lines:
            row = json.loads(line)
            encoded = encode(tokenizer, row["prompt"], row["target"])
            if encoded is not None:
                examples.append(encoded)
    _fit(model, examples, device, epochs, batch_size, learning_rate)
    _private_directory(out_dir)
    model.save_pretrained(out_dir)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m tools.voice.finetune")
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("train", help="train a LoRA adapter on prompt/target JSON Lines")
    fit.add_argument("records", type=pathlib.Path, help="JSON Lines file of prompt and target")
    fit.add_argument("adapter_dir", type=pathlib.Path, help="directory the adapter is saved to")
    fit.add_argument("--epochs", type=int, default=EPOCHS)
    fit.add_argument("--base-model", default=BASE_MODEL, help="Hugging Face name of the base model to finetune")
    args = parser.parse_args(argv)
    train(args.records, args.adapter_dir, base_model=args.base_model, epochs=args.epochs)


if __name__ == "__main__":
    main()
