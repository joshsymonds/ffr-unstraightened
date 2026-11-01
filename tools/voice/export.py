"""Merge a LoRA adapter into its base model and export one quantized GGUF for llama-server."""

import argparse
import json
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Sequence

QUANT = "Q8_0"
ADAPTER_CONFIG = "adapter_config.json"
BASE_FIELD = "base_model_name_or_path"


def base_model(adapter_dir: pathlib.Path) -> str:
    try:
        config = json.loads((adapter_dir / ADAPTER_CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = None
    # The error names the file and field only, so a malformed config never echoes its contents.
    base = config.get(BASE_FIELD) if isinstance(config, dict) else None
    if not isinstance(base, str) or not base:
        raise RuntimeError(f"{ADAPTER_CONFIG} has no {BASE_FIELD}")
    return base


def _merge(base: str, adapter_dir: pathlib.Path, merged: pathlib.Path) -> None:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    # Without device_map the bf16 base stays on CPU, and remote code is never enabled.
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16)
    model = PeftModel.from_pretrained(model, adapter_dir).merge_and_unload()
    model.save_pretrained(merged)
    AutoTokenizer.from_pretrained(base).save_pretrained(merged)


def _run(argv: list[str]) -> None:
    # The tool's output is not captured, so it reaches the terminal and stays out of the exception.
    result = subprocess.run(argv)
    if result.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {result.returncode}")


def export(adapter_dir: pathlib.Path, out: pathlib.Path, quant: str = QUANT) -> pathlib.Path:
    base = base_model(adapter_dir)
    # TemporaryDirectory is created 0700 and removed on success and on failure, so the merged model
    # and the unquantized GGUF never outlive the call.
    with tempfile.TemporaryDirectory(dir=out.parent) as work_name:
        work = pathlib.Path(work_name)
        merged = work / "merged"
        _merge(base, adapter_dir, merged)

        unquantized = work / "bf16.gguf"
        _run(
            [
                "convert_hf_to_gguf",
                str(merged),
                "--outfile",
                str(unquantized),
                "--outtype",
                "bf16",
            ]
        )
        # Nothing reads the merged model after conversion, so free its space before quantizing.
        shutil.rmtree(merged)

        try:
            _run(["llama-quantize", str(unquantized), str(out), quant])
        except RuntimeError:
            # llama-quantize writes its output before it can fail, so a partial model must not stay behind.
            out.unlink(missing_ok=True)
            raise
        out.chmod(0o600)
    return out


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Merge a LoRA adapter and export one Q8_0 GGUF.")
    parser.add_argument("adapter_dir", type=pathlib.Path)
    parser.add_argument("out", type=pathlib.Path)
    args = parser.parse_args(argv)
    export(args.adapter_dir, args.out)


if __name__ == "__main__":
    main()
