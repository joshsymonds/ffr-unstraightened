import argparse

import pytest

from ffrd.voice import lora, remote
from ffrd.voice.classifier import Pair
from ffrd.voice.render import prompt

# Made-up strings for the LoRA records. Not game text.
TRAIN_ARGV = ["python3", "-m", "tools.voice.finetune", "train", lora.DATA, lora.ADAPTER, "--epochs"]
EXPORT_ARGV = ["python3", "-m", "tools.voice.export", lora.ADAPTER, lora.GGUF]


def pair(split: str, n: int, *, null_speaker: bool = False) -> Pair:
    return Pair(
        scene="synth-scene",
        speaker=None if null_speaker else f"SYNTH-SPEAKER-{n}",
        paraphrase=f"SYNTH-PARAPHRASE-{n} wording",
        original=f"SYNTH-ORIGINAL-{n} wording",
        slot=1,
        model="synth-model",
        split=split,
    )


STAGED_COUNT = 7


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    log: list[tuple[object, ...]] = []

    def fake_stage(rows: list[dict[str, str]], name: str) -> int:
        log.append(("stage", list(rows), name))
        return STAGED_COUNT

    monkeypatch.setattr(remote, "sync", lambda: log.append(("sync",)))
    monkeypatch.setattr(remote, "stage", fake_stage)
    monkeypatch.setattr(remote, "run", lambda argv: log.append(("run", list(argv))))
    return log


@pytest.fixture
def train_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load(split: str) -> list[Pair]:
        assert split == "train"
        return [pair("train", 1), pair("heldout", 2), pair("train", 4, null_speaker=True)]

    monkeypatch.setattr(lora, "load_pairs", fake_load)


def test_records_builds_shared_prompt_and_original_target_for_train_pairs_in_order() -> None:
    pairs = [pair("train", 1), pair("heldout", 2), pair("train", 3)]
    rows = lora.records(pairs)
    assert rows == [
        {"prompt": prompt("SYNTH-SPEAKER-1", "SYNTH-PARAPHRASE-1 wording"), "target": "SYNTH-ORIGINAL-1 wording"},
        {"prompt": prompt("SYNTH-SPEAKER-3", "SYNTH-PARAPHRASE-3 wording"), "target": "SYNTH-ORIGINAL-3 wording"},
    ]
    assert "SYNTH-ORIGINAL-1" not in rows[0]["prompt"]


def test_records_skips_heldout_pairs_entirely() -> None:
    assert lora.records([pair("heldout", 1)]) == []


def test_records_leaves_out_null_speaker_train_pairs() -> None:
    rows = lora.records([pair("train", 1), pair("train", 2, null_speaker=True), pair("train", 3)])
    assert rows == [
        {"prompt": prompt("SYNTH-SPEAKER-1", "SYNTH-PARAPHRASE-1 wording"), "target": "SYNTH-ORIGINAL-1 wording"},
        {"prompt": prompt("SYNTH-SPEAKER-3", "SYNTH-PARAPHRASE-3 wording"), "target": "SYNTH-ORIGINAL-3 wording"},
    ]


def test_train_syncs_then_stages_train_records_then_runs(
    calls: list[tuple[object, ...]], train_pairs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    lora.train()

    assert calls == [
        ("sync",),
        ("stage", lora.records([pair("train", 1)]), lora.DATA),
        ("run", [*TRAIN_ARGV, "1"]),
    ]
    assert capsys.readouterr().out == f"staged {STAGED_COUNT} lora records\n"


def test_train_passes_epochs_to_the_finetune_job(calls: list[tuple[object, ...]], train_pairs: None) -> None:
    lora.train(epochs=3)
    assert calls[-1] == ("run", [*TRAIN_ARGV, "3"])


def test_export_syncs_then_exports_adapter_to_gguf(calls: list[tuple[object, ...]]) -> None:
    lora.export()
    assert calls == [("sync",), ("run", EXPORT_ARGV)]


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    lora.register(parser.add_subparsers(required=True))
    return parser.parse_args(argv)


def test_register_lora_train_parses_epochs_and_runs_train(calls: list[tuple[object, ...]], train_pairs: None) -> None:
    args = parse(["lora", "train", "--epochs", "2"])
    args.fn(args)
    assert calls[-1] == ("run", [*TRAIN_ARGV, "2"])


def test_register_lora_train_defaults_epochs_to_one() -> None:
    assert parse(["lora", "train"]).epochs == 1


def test_register_lora_export_runs_export(calls: list[tuple[object, ...]]) -> None:
    args = parse(["lora", "export"])
    args.fn(args)
    assert calls == [("sync",), ("run", EXPORT_ARGV)]


def test_register_lora_requires_an_action() -> None:
    with pytest.raises(SystemExit):
        parse(["lora"])
