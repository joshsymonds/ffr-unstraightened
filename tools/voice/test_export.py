import json
import os
import pathlib
import stat
import sys
from types import SimpleNamespace

import pytest

from tools.voice import export

BASE = "synthetic/base-model"
FIELD = "base_model_name_or_path"
MARKER = "SYNTHETIC-RECORD-MARKER"

# One fake for both tools. Each call is logged as JSON before the tool writes its output, and the
# quantizer also logs what sits beside its input so the test can see whether the merged model still exists.
FAKE_TOOL = r"""#!__PYTHON__
import json
import os
import sys

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
entry = {"tool": name, "argv": args}
if name == "llama-quantize":
    entry["siblings"] = sorted(os.listdir(os.path.dirname(args[0])))
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
target = args[args.index("--outfile") + 1] if name == "convert_hf_to_gguf" else args[1]
with open(target, "wb") as handle:
    handle.write(b"synthetic")
if os.environ.get("FAKE_FAIL") == name:
    sys.stderr.write("SECRET\n")
    sys.exit(3)
"""


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("convert_hf_to_gguf", "llama-quantize"):
        script = bin_dir / name
        script.write_text(FAKE_TOOL.replace("__PYTHON__", sys.executable))
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_FAIL", "")

    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text(json.dumps({FIELD: BASE, "r": 8}))
    exports = tmp_path / "exports"
    exports.mkdir()

    merges = []

    def fake_merge(base, adapter_dir, merged):
        merges.append({"base": base, "adapter": adapter_dir, "merged": merged, "parent_mode": mode(merged.parent)})
        merged.mkdir()
        (merged / "model.safetensors").write_bytes(b"synthetic")

    monkeypatch.setattr(export, "_merge", fake_merge)

    # Umask 0 makes the test prove the 0600 chmod itself rather than inheriting a safe default.
    old_umask = os.umask(0)
    try:
        yield SimpleNamespace(adapter=adapter, exports=exports, merges=merges, log=tmp_path / "calls.jsonl")
    finally:
        os.umask(old_umask)


def calls(world):
    return [json.loads(line) for line in world.log.read_text().splitlines()]


def test_base_model_reads_adapter_config_field(tmp_path):
    (tmp_path / "adapter_config.json").write_text(json.dumps({FIELD: BASE, "r": 8}))

    assert export.base_model(tmp_path) == BASE


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{not json " + MARKER,
        json.dumps({"r": 8, "note": MARKER}),
        json.dumps([MARKER]),
        json.dumps({FIELD: ""}),
    ],
)
def test_base_model_fails_naming_field_without_file_contents(tmp_path, content):
    if content is not None:
        (tmp_path / "adapter_config.json").write_text(content)

    with pytest.raises(RuntimeError) as info:
        export.base_model(tmp_path)

    assert FIELD in str(info.value)
    assert MARKER not in str(info.value)


def test_export_merges_the_base_named_by_the_adapter(world):
    export.export(world.adapter, world.exports / "model.gguf")

    assert [(m["base"], m["adapter"]) for m in world.merges] == [(BASE, world.adapter)]


def test_converter_then_quantizer_argv(world):
    out = world.exports / "model.gguf"

    assert export.export(world.adapter, out) == out

    merged = world.merges[0]["merged"]
    converter, quantizer = calls(world)
    bf16 = pathlib.Path(converter["argv"][converter["argv"].index("--outfile") + 1])
    assert converter == {
        "tool": "convert_hf_to_gguf",
        "argv": [str(merged), "--outfile", str(bf16), "--outtype", "bf16"],
    }
    assert quantizer["tool"] == "llama-quantize"
    assert quantizer["argv"] == [str(bf16), str(out), "Q8_0"]
    assert bf16.name == "bf16.gguf"
    assert bf16.parent == merged.parent
    assert bf16.parent.parent == out.parent


def test_merged_model_removed_before_quantizing(world):
    export.export(world.adapter, world.exports / "model.gguf")

    merged_name = world.merges[0]["merged"].name
    quantizer = calls(world)[1]
    assert merged_name not in quantizer["siblings"]
    assert "bf16.gguf" in quantizer["siblings"]


def test_temp_dir_removed_on_success(world):
    export.export(world.adapter, world.exports / "model.gguf")

    assert os.listdir(world.exports) == ["model.gguf"]


@pytest.mark.parametrize("tool", ["convert_hf_to_gguf", "llama-quantize"])
def test_temp_dir_removed_on_tool_failure(world, monkeypatch, tool):
    monkeypatch.setenv("FAKE_FAIL", tool)

    with pytest.raises(RuntimeError):
        export.export(world.adapter, world.exports / "model.gguf")

    assert os.listdir(world.exports) == []


@pytest.mark.parametrize("tool", ["convert_hf_to_gguf", "llama-quantize"])
def test_failure_names_tool_and_exit_code_only(world, monkeypatch, tool):
    monkeypatch.setenv("FAKE_FAIL", tool)

    with pytest.raises(RuntimeError) as info:
        export.export(world.adapter, world.exports / "model.gguf")

    assert str(info.value) == f"{tool} exited 3"


def test_out_is_mode_0600(world):
    out = world.exports / "model.gguf"
    export.export(world.adapter, out)

    assert mode(out) == 0o600


def test_merge_runs_in_0700_directory_beside_out(world):
    export.export(world.adapter, world.exports / "model.gguf")

    (merge,) = world.merges
    assert merge["parent_mode"] == 0o700
    assert merge["merged"].parent.parent == world.exports


def test_partial_out_removed_when_quantizer_fails(world, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "llama-quantize")
    out = world.exports / "model.gguf"

    with pytest.raises(RuntimeError):
        export.export(world.adapter, out)

    assert not out.exists()


def test_main_merges_and_writes_out(world):
    out = world.exports / "model.gguf"

    assert export.main([str(world.adapter), str(out)]) is None

    assert os.listdir(world.exports) == ["model.gguf"]
    assert mode(out) == 0o600
