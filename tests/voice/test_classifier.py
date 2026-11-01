import json
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.voice import classifier
from ffrd.voice.classifier import Pair, examples, load_pairs

# Made-up strings for the pairs loader. Not game text.
SYNTH_TEXTS = (
    "SYNTH-SPEAKER-ALPHA",
    "SYNTH-PARAPHRASE-ONE wording",
    "SYNTH-ORIGINAL-ONE wording",
    "SYNTH-PARAPHRASE-TWO wording",
    "SYNTH-ORIGINAL-TWO wording",
    "SYNTH-RAW-NOT-JSON fragment",
)


def record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "scene": "synth-scene-1",
        "speaker": SYNTH_TEXTS[0],
        "paraphrase": SYNTH_TEXTS[1],
        "original": SYNTH_TEXTS[2],
        "slot": "synth-slot-1",
        "model": "synth-model-1",
    }
    base.update(overrides)
    return base


def line(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def pairs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "work"
    monkeypatch.setattr(classifier, "work_dir", lambda lens: root / lens)
    return root / "voice" / "pairs"


def write_split(pairs_dir: Path, split: str, lines: list[str]) -> None:
    pairs_dir.mkdir(parents=True, exist_ok=True)
    (pairs_dir / f"{split}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_pairs_maps_fields_in_file_order(pairs_dir: Path) -> None:
    write_split(
        pairs_dir,
        "train",
        [
            line(record(scene="synth-scene-1", original=SYNTH_TEXTS[2], paraphrase=SYNTH_TEXTS[1])),
            line(record(scene="synth-scene-2", speaker=SYNTH_TEXTS[0], original=SYNTH_TEXTS[4], paraphrase=SYNTH_TEXTS[3])),
        ],
    )
    assert load_pairs("train") == [
        Pair(
            scene="synth-scene-1",
            speaker=SYNTH_TEXTS[0],
            paraphrase=SYNTH_TEXTS[1],
            original=SYNTH_TEXTS[2],
            slot="synth-slot-1",
            model="synth-model-1",
            split="train",
        ),
        Pair(
            scene="synth-scene-2",
            speaker=SYNTH_TEXTS[0],
            paraphrase=SYNTH_TEXTS[3],
            original=SYNTH_TEXTS[4],
            slot="synth-slot-1",
            model="synth-model-1",
            split="train",
        ),
    ]


def test_load_pairs_sets_split_from_argument(pairs_dir: Path) -> None:
    write_split(pairs_dir, "heldout", [line(record()), line(record(scene="synth-scene-2"))])
    loaded = load_pairs("heldout")
    assert len(loaded) == 2
    assert {pair.split for pair in loaded} == {"heldout"}


def test_load_pairs_skips_blank_lines(pairs_dir: Path) -> None:
    write_split(pairs_dir, "train", ["", line(record()), "   ", "\t", line(record(scene="synth-scene-2"))])
    assert [pair.scene for pair in load_pairs("train")] == ["synth-scene-1", "synth-scene-2"]


def test_load_pairs_rejects_unknown_split(pairs_dir: Path) -> None:
    with pytest.raises(Fail, match="unknown pairs split 'synthetic'"):
        load_pairs("synthetic")


def test_load_pairs_missing_file_points_at_pairs_command(pairs_dir: Path) -> None:
    with pytest.raises(Fail, match="no heldout pairs file; run `ffrd voice pairs` first"):
        load_pairs("heldout")


@pytest.mark.parametrize(
    ("bad_line", "reason"),
    [
        (f"[{json.dumps(SYNTH_TEXTS[2])}]", "not a JSON object"),
        (SYNTH_TEXTS[5], "not a JSON object"),
        (line({k: v for k, v in record().items() if k != "model"}), "missing or non-string 'model' field"),
        (line(record(paraphrase=7)), "missing or non-string 'paraphrase' field"),
    ],
    ids=["array", "not-json", "missing-field", "non-string-field"],
)
def test_load_pairs_failures_name_split_and_line_only(pairs_dir: Path, bad_line: str, reason: str) -> None:
    write_split(pairs_dir, "train", [line(record()), bad_line])
    with pytest.raises(Fail) as excinfo:
        load_pairs("train")
    message = str(excinfo.value)
    assert "train line 2" in message
    assert reason in message
    for text in (*SYNTH_TEXTS, "synth-scene-1", "synth-model-1"):
        assert text not in message


def test_pair_repr_hides_speaker_paraphrase_and_original() -> None:
    pair = Pair(
        scene="synth-scene-1",
        speaker=SYNTH_TEXTS[0],
        paraphrase=SYNTH_TEXTS[1],
        original=SYNTH_TEXTS[2],
        slot="synth-slot-1",
        model="synth-model-1",
        split="train",
    )
    text = repr(pair)
    assert "synth-scene-1" in text
    for hidden in (SYNTH_TEXTS[0], SYNTH_TEXTS[1], SYNTH_TEXTS[2]):
        assert hidden not in text


def make_pair(original: str, paraphrase: str) -> Pair:
    return Pair(
        scene="synth-scene-1",
        speaker=SYNTH_TEXTS[0],
        paraphrase=paraphrase,
        original=original,
        slot="synth-slot-1",
        model="synth-model-1",
        split="train",
    )


def test_examples_dedupes_originals_and_keeps_pair_order() -> None:
    pairs = [
        make_pair(SYNTH_TEXTS[2], SYNTH_TEXTS[1]),
        make_pair(SYNTH_TEXTS[4], SYNTH_TEXTS[3]),
        make_pair(SYNTH_TEXTS[2], "SYNTH-PARAPHRASE-THREE wording"),
    ]
    assert examples(pairs) == [
        (SYNTH_TEXTS[2], 1),
        (SYNTH_TEXTS[4], 1),
        (SYNTH_TEXTS[1], 0),
        (SYNTH_TEXTS[3], 0),
        ("SYNTH-PARAPHRASE-THREE wording", 0),
    ]


def test_examples_drops_paraphrase_identical_to_its_original() -> None:
    pairs = [make_pair(SYNTH_TEXTS[2], SYNTH_TEXTS[2]), make_pair(SYNTH_TEXTS[4], SYNTH_TEXTS[3])]
    assert examples(pairs) == [(SYNTH_TEXTS[2], 1), (SYNTH_TEXTS[4], 1), (SYNTH_TEXTS[3], 0)]
