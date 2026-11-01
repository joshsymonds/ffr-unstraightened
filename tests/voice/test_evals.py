import argparse
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.accounts import Account
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.store import Store
from ffrd.voice import evals, remote
from ffrd.voice.classifier import Pair
from ffrd.voice.evals import (
    ALTER_PROMPT,
    ALTER_SCHEMA,
    CLASSIFIER_BAR,
    MEANING_MARGIN,
    RENDER_SCORE_BAR,
    REVERT_BAR,
    REVERT_BATCH,
    REVERT_INPUTS,
    SHORT_WORDS,
    Entailer,
    Scored,
    alter,
    auroc,
    classifier_metrics,
    classifier_scored,
    heldout_sample,
    register,
    render_metrics,
    revert_metrics,
    revert_sample,
    word_count,
)
from ffrd.voice.render import PORT


def test_constants_match_the_frozen_interface() -> None:
    assert CLASSIFIER_BAR == 0.90
    assert SHORT_WORDS == 8
    assert RENDER_SCORE_BAR == 0.5
    assert MEANING_MARGIN == 0.03


def test_word_count_splits_on_whitespace() -> None:
    assert word_count("alpha beta  gamma\tdelta\n") == 4
    assert word_count("") == 0


def test_auroc_perfect_separation_is_one() -> None:
    assert auroc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0


def test_auroc_reversed_separation_is_zero() -> None:
    assert auroc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0


def test_auroc_all_tied_is_one_half() -> None:
    assert auroc([0.5, 0.5, 0.5, 0.5], [1, 1, 0, 0]) == 0.5


def test_auroc_mixed_case_with_ties_matches_hand_computed_value() -> None:
    # Positives 0.9, 0.5 against negatives 0.5, 0.1:
    # (0.9,0.5) win, (0.9,0.1) win, (0.5,0.5) tie = 0.5, (0.5,0.1) win.
    # 3.5 / (2 * 2) = 0.875
    assert auroc([0.9, 0.5, 0.5, 0.1], [1, 1, 0, 0]) == 0.875


def test_auroc_is_order_independent() -> None:
    assert auroc([0.1, 0.9, 0.5, 0.5], [0, 1, 0, 1]) == auroc([0.5, 0.5, 0.9, 0.1], [1, 0, 1, 0])


def test_auroc_raises_on_length_mismatch() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9], [1])


def test_auroc_raises_on_label_other_than_zero_or_one() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9, 0.4], [0, 1, 2])


def test_auroc_raises_when_positive_class_is_empty() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9], [0, 0])


def test_auroc_raises_when_negative_class_is_empty() -> None:
    with pytest.raises(Fail):
        auroc([0.1, 0.9], [1, 1])


def test_auroc_raises_on_empty_input() -> None:
    with pytest.raises(Fail):
        auroc([], [])


def test_classifier_metrics_has_exactly_the_contract_keys() -> None:
    scored = [Scored(label=1, words=10, score=0.9), Scored(label=0, words=10, score=0.1)]
    assert set(classifier_metrics(scored)) == {"n", "auroc", "n_short", "auroc_short", "pass"}


def test_classifier_metrics_reports_overall_figures() -> None:
    scored = [
        Scored(label=1, words=10, score=0.9),
        Scored(label=1, words=10, score=0.8),
        Scored(label=0, words=10, score=0.2),
        Scored(label=0, words=10, score=0.1),
    ]
    metrics = classifier_metrics(scored)
    assert metrics["n"] == 4
    assert metrics["auroc"] == 1.0
    assert metrics["pass"] is True


def test_short_subset_is_split_by_the_game_line_word_count() -> None:
    # 7 words is short; 8 words is not.
    scored = [
        Scored(label=1, words=7, score=0.9),
        Scored(label=0, words=7, score=0.1),
        Scored(label=1, words=8, score=0.1),
        Scored(label=0, words=8, score=0.9),
    ]
    metrics = classifier_metrics(scored)
    assert metrics["n_short"] == 2
    assert metrics["auroc_short"] == 1.0
    assert metrics["auroc"] == 0.5


def test_auroc_short_is_none_when_short_subset_is_one_class() -> None:
    scored = [
        Scored(label=1, words=3, score=0.9),
        Scored(label=1, words=3, score=0.4),
        Scored(label=0, words=20, score=0.1),
        Scored(label=1, words=20, score=0.8),
    ]
    metrics = classifier_metrics(scored)
    assert metrics["n_short"] == 2
    assert metrics["auroc_short"] is None


def test_auroc_short_is_none_when_no_item_is_short() -> None:
    scored = [Scored(label=1, words=12, score=0.9), Scored(label=0, words=12, score=0.1)]
    metrics = classifier_metrics(scored)
    assert metrics["n_short"] == 0
    assert metrics["auroc_short"] is None


def test_pass_is_true_at_exactly_the_bar() -> None:
    # One positive against ten negatives: nine wins out of ten pairs = 0.90.
    scored = [Scored(label=1, words=10, score=5.0)]
    scored += [Scored(label=0, words=10, score=1.0)] * 9
    scored += [Scored(label=0, words=10, score=9.0)]
    metrics = classifier_metrics(scored)
    assert metrics["auroc"] == 0.90
    assert metrics["pass"] is True


def test_pass_is_false_just_below_the_bar() -> None:
    # One positive against 1000 negatives: 899 wins out of 1000 pairs = 0.899.
    scored = [Scored(label=1, words=10, score=5.0)]
    scored += [Scored(label=0, words=10, score=1.0)] * 899
    scored += [Scored(label=0, words=10, score=9.0)] * 101
    metrics = classifier_metrics(scored)
    assert metrics["auroc"] == pytest.approx(0.899)
    assert metrics["pass"] is False


def test_short_figure_is_reported_but_does_not_gate_pass() -> None:
    # The short pair is reversed (short AUROC 0.0). Ten long positives beat every
    # negative and ten long negatives sit below every positive, so the overall
    # figure is 120 wins of 121 pairs: the only loss is the reversed short pair.
    scored = [Scored(label=1, words=3, score=0.1), Scored(label=0, words=3, score=0.9)]
    scored += [Scored(label=1, words=20, score=0.95)] * 10
    scored += [Scored(label=0, words=20, score=0.01)] * 10
    metrics = classifier_metrics(scored)
    assert metrics["auroc_short"] == 0.0
    assert metrics["auroc"] == pytest.approx(120 / 121)
    assert metrics["pass"] is True


def make_pair(original: str, paraphrase: str, speaker: str | None = "spk-a") -> Pair:
    return Pair(
        scene="scene-x",
        speaker=speaker,
        paraphrase=paraphrase,
        original=original,
        slot=1,
        model="model-m",
        split="heldout",
    )


class Fakes:
    """Table-driven callables for render_metrics that record every call in one log."""

    def __init__(self, scores: dict[str, float] | None = None, holds: dict[tuple[str, str], bool] | None = None) -> None:
        self.scores = scores or {}
        self.holds = holds or {}
        self.log: list[str] = []
        self.render_inputs: list[tuple[str, str]] = []
        self.hold_inputs: list[list[tuple[str, str]]] = []

    def render(self, speaker: str, paraphrase: str) -> str:
        self.log.append("render")
        self.render_inputs.append((speaker, paraphrase))
        return f"R:{paraphrase}"

    def score_many(self, texts: Sequence[str]) -> list[float]:
        self.log.append("score")
        return [self.scores.get(text, 0.0) for text in texts]

    def holds_many(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
        self.log.append("holds")
        self.hold_inputs.append(list(pairs))
        return [self.holds.get(pair, True) for pair in pairs]


def test_heldout_sample_takes_the_lowest_digest_paraphrase_of_each_line() -> None:
    # sha256 order: para-a2 (41...) < para-a1 (b7...) < para-a3 (eb...);
    # para-b2 (59...) < para-b1 (6f...). The winners are not first in input order.
    pairs = [
        make_pair("line-alpha", "para-a1"),
        make_pair("line-alpha", "para-a2"),
        make_pair("line-alpha", "para-a3"),
        make_pair("line-beta", "para-b1"),
        make_pair("line-beta", "para-b2"),
    ]
    sample = heldout_sample(pairs)
    assert [(pair.original, pair.paraphrase) for pair in sample] == [
        ("line-alpha", "para-a2"),
        ("line-beta", "para-b2"),
    ]


def test_heldout_sample_orders_lines_by_sha256_of_the_original() -> None:
    # sha256("line-alpha") = 4e... sorts before sha256("line-beta") = f5..., though
    # line-beta is given first in the input.
    pairs = [make_pair("line-beta", "para-b1"), make_pair("line-alpha", "para-a1")]
    sample = heldout_sample(pairs)
    assert [pair.original for pair in sample] == ["line-alpha", "line-beta"]


def test_heldout_sample_of_nothing_is_empty() -> None:
    assert heldout_sample([]) == []


def test_heldout_sample_leaves_out_null_speaker_pairs() -> None:
    # sha256("para-a2") sorts first, but its speaker is null, so para-a1 is the sample.
    pairs = [make_pair("line-alpha", "para-a2", None), make_pair("line-alpha", "para-a1")]
    assert [(pair.original, pair.paraphrase) for pair in heldout_sample(pairs)] == [("line-alpha", "para-a1")]


def test_render_metrics_never_passes_a_null_speaker_to_the_renderer() -> None:
    pairs = [make_pair("line-alpha", "para-a2", None), make_pair("line-alpha", "para-a1"), make_pair("line-beta", "para-b1", None)]
    fakes = Fakes()
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert fakes.render_inputs == [("spk-a", "para-a1")]
    assert metrics["n"] == 1


def test_render_metrics_of_only_null_speaker_pairs_raises_before_any_render() -> None:
    fakes = Fakes()
    with pytest.raises(Fail, match="needs at least one held-out pair"):
        render_metrics([make_pair("line-alpha", "para-a1", None)], fakes.render, fakes.score_many, fakes.holds_many)
    assert fakes.render_inputs == []


def test_every_render_happens_before_the_first_score_call() -> None:
    pairs = [make_pair("line-alpha", "para-a1", "spk-1"), make_pair("line-beta", "para-b1", "spk-2")]
    fakes = Fakes()
    render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert fakes.log == ["render", "render", "score", "score", "holds", "holds"]
    assert sorted(fakes.render_inputs) == [("spk-1", "para-a1"), ("spk-2", "para-b1")]


def test_render_metrics_on_a_hand_built_case() -> None:
    # Renders score 0.9, 0.7, 0.6, 0.1 (median 0.65); paraphrases 0.5, 0.4, 0.3, 0.2
    # (median 0.35). Render meaning holds 3 of 4, pairs hold 3 of 4.
    pairs = [make_pair(f"line-{i}", f"para-{i}", f"spk-{i}") for i in range(1, 5)]
    fakes = Fakes(
        scores={
            "R:para-1": 0.9,
            "R:para-2": 0.7,
            "R:para-3": 0.6,
            "R:para-4": 0.1,
            "para-1": 0.5,
            "para-2": 0.4,
            "para-3": 0.3,
            "para-4": 0.2,
        },
        holds={
            ("para-1", "R:para-1"): True,
            ("para-2", "R:para-2"): True,
            ("para-3", "R:para-3"): True,
            ("para-4", "R:para-4"): False,
            ("para-1", "line-1"): True,
            ("para-2", "line-2"): True,
            ("para-3", "line-3"): True,
            ("para-4", "line-4"): False,
        },
    )
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["n"] == 4
    assert metrics["median_render_score"] == pytest.approx(0.65)
    assert metrics["median_paraphrase_score"] == pytest.approx(0.35)
    assert metrics["render_entailment_rate"] == 0.75
    assert metrics["pairs_entailment_rate"] == 0.75
    assert metrics["game_likeness_pass"] is True
    assert metrics["meaning_pass"] is True
    assert metrics["pass"] is True


def test_game_likeness_passes_at_exactly_the_score_bar() -> None:
    fakes = Fakes(scores={"R:para-1": 0.5, "para-1": 0.2})
    metrics = render_metrics([make_pair("line-1", "para-1")], fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["median_render_score"] == 0.5
    assert metrics["game_likeness_pass"] is True


def test_game_likeness_fails_just_below_the_score_bar() -> None:
    fakes = Fakes(scores={"R:para-1": 0.49, "para-1": 0.2})
    metrics = render_metrics([make_pair("line-1", "para-1")], fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["game_likeness_pass"] is False


def test_game_likeness_fails_when_render_median_equals_paraphrase_median() -> None:
    fakes = Fakes(scores={"R:para-1": 0.8, "para-1": 0.8})
    metrics = render_metrics([make_pair("line-1", "para-1")], fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["game_likeness_pass"] is False
    assert metrics["pass"] is False


def meaning_fakes(render_hits: int, pair_hits: int) -> tuple[list[Pair], Fakes]:
    """100 held-out lines; the first render_hits renders and first pair_hits pairs hold."""
    pairs = [make_pair(f"line-{i:03d}", f"para-{i:03d}") for i in range(100)]
    holds: dict[tuple[str, str], bool] = {}
    for i in range(100):
        holds[(f"para-{i:03d}", f"R:para-{i:03d}")] = i < render_hits
        holds[(f"para-{i:03d}", f"line-{i:03d}")] = i < pair_hits
    scores = {f"R:para-{i:03d}": 0.9 for i in range(100)} | {f"para-{i:03d}": 0.1 for i in range(100)}
    return pairs, Fakes(scores=scores, holds=holds)


def test_meaning_passes_at_exactly_the_pairs_rate_minus_the_margin() -> None:
    # Pairs hold 90 of 100; renders hold 87 of 100, which is exactly 0.90 - 0.03.
    pairs, fakes = meaning_fakes(render_hits=87, pair_hits=90)
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["render_entailment_rate"] == 0.87
    assert metrics["pairs_entailment_rate"] == 0.90
    assert metrics["meaning_pass"] is True
    assert metrics["pass"] is True


def test_meaning_fails_just_below_the_pairs_rate_minus_the_margin() -> None:
    pairs, fakes = meaning_fakes(render_hits=86, pair_hits=90)
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert metrics["meaning_pass"] is False
    assert metrics["game_likeness_pass"] is True
    assert metrics["pass"] is False


def test_render_metrics_raises_fail_on_empty_input_before_any_call() -> None:
    fakes = Fakes()
    with pytest.raises(Fail):
        render_metrics([], fakes.render, fakes.score_many, fakes.holds_many)
    assert fakes.log == []


def test_render_metrics_result_has_exactly_the_contract_keys_and_no_text() -> None:
    pairs = [make_pair("line-secret-o", "para-secret-p", "spk-secret-s")]
    fakes = Fakes(scores={"R:para-secret-p": 0.9, "para-secret-p": 0.1})
    metrics = render_metrics(pairs, fakes.render, fakes.score_many, fakes.holds_many)
    assert set(metrics) == {
        "n",
        "median_render_score",
        "median_paraphrase_score",
        "render_entailment_rate",
        "pairs_entailment_rate",
        "game_likeness_pass",
        "meaning_pass",
        "pass",
    }
    assert all(isinstance(value, int | float | bool) for value in metrics.values())
    assert "secret" not in repr(metrics)


def make_train_pair(i: int) -> Pair:
    return Pair(
        scene="scene-x",
        speaker=f"spk-{i}",
        paraphrase=f"para-{i:03d}",
        original=f"line-{i:03d}",
        slot=1,
        model="model-m",
        split="train",
    )


def alter_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def echo_reply(entries: list[dict[str, str]]) -> dict[str, Any]:
    return {"altered": [{"id": entry["id"], "text": f"altered:{entry['text']}"} for entry in entries]}


class FakeOpus:
    """Stands in for claude.ask and accounts.choose; answers every batch with echo_reply unless reply is replaced."""

    def __init__(self, tmp_path: Path, numbers: Sequence[int] = (3, 5)) -> None:
        self.store = Store(tmp_path / "store")
        self.log = tmp_path / "revert-alter.log"
        self.reply: Callable[[list[dict[str, str]]], Any] = echo_reply
        self.calls: list[dict[str, Any]] = []
        self._numbers = list(numbers)
        self._next_account = 0

    def choose(self) -> Account:
        number = self._numbers[self._next_account % len(self._numbers)]
        self._next_account += 1
        return Account(number=number, weekly_pct=0.0, resets=())

    def ask(self, account: int, model: str, parts: Sequence[str], schema: dict[str, Any]) -> tuple[int, Any]:
        self.calls.append({"account": account, "model": model, "parts": list(parts), "schema": schema})
        return account, self.reply(json.loads(parts[1]))


@pytest.fixture
def opus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeOpus:
    fake = FakeOpus(tmp_path)
    monkeypatch.setattr(claude, "ask", fake.ask)
    monkeypatch.setattr(accounts, "choose", fake.choose)
    return fake


def test_revert_constants_match_the_frozen_interface() -> None:
    assert REVERT_BAR == 0.90
    assert REVERT_INPUTS == 200
    assert REVERT_BATCH == 20


def test_revert_sample_keeps_train_pairs_only_and_caps_at_n() -> None:
    train = [make_train_pair(i) for i in range(10)]
    heldout = [
        Pair(
            scene="scene-x",
            speaker="spk-h",
            paraphrase=f"para-h{i}",
            original=f"line-h{i}",
            slot=1,
            model="model-m",
            split="heldout",
        )
        for i in range(10)
    ]
    sample = revert_sample(heldout + train, n=4)
    assert len(sample) == 4
    assert all(pair.split == "train" for pair in sample)
    assert sample == heldout_sample(train)[:4]


def test_revert_sample_leaves_out_null_speaker_train_pairs() -> None:
    train = [make_train_pair(0), make_train_pair(1), make_train_pair(2)]
    train[1] = Pair(
        scene="scene-x",
        speaker=None,
        paraphrase="para-001",
        original="line-001",
        slot=1,
        model="model-m",
        split="train",
    )
    sample = revert_sample(train)
    assert {pair.original for pair in sample} == {"line-000", "line-002"}
    assert all(pair.speaker is not None for pair in sample)


def test_revert_sample_defaults_to_two_hundred_inputs() -> None:
    assert len(revert_sample([make_train_pair(i) for i in range(250)])) == REVERT_INPUTS


def test_revert_sample_of_nothing_is_empty() -> None:
    assert revert_sample([]) == []


def test_alter_sends_forty_five_inputs_as_batches_of_20_20_5(opus: FakeOpus) -> None:
    sample = [make_train_pair(i) for i in range(45)]
    alter(sample, opus.store, opus.log)
    assert len(opus.calls) == 3
    assert [len(json.loads(call["parts"][1])) for call in opus.calls] == [20, 20, 5]
    for call, batch in zip(opus.calls, [sample[0:20], sample[20:40], sample[40:45]], strict=True):
        assert call["model"] == claude.OPUS
        assert call["schema"] == ALTER_SCHEMA
        expected_entries = [{"id": alter_id(pair.paraphrase), "text": pair.paraphrase} for pair in batch]
        assert call["parts"] == [ALTER_PROMPT, json.dumps(expected_entries, ensure_ascii=False)]


def test_alter_returns_texts_in_sample_order_and_calls_per_account(opus: FakeOpus) -> None:
    sample = [make_train_pair(i) for i in range(45)]
    texts, per_account = alter(sample, opus.store, opus.log)
    assert texts == [f"altered:{pair.paraphrase}" for pair in sample]
    assert per_account == {3: 2, 5: 1}


def test_alter_logs_one_line_per_call_with_the_account_used(opus: FakeOpus) -> None:
    sample = [make_train_pair(i) for i in range(45)]
    alter(sample, opus.store, opus.log)
    lines = opus.log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    fields = [line.split("\t") for line in lines]
    for stamp, _, _ in fields:
        assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0)
    assert [(batch, account) for _, batch, account in fields] == [
        ("batch 1", "account 3"),
        ("batch 2", "account 5"),
        ("batch 3", "account 3"),
    ]


def test_alter_rerun_makes_no_calls_and_returns_the_same_texts(opus: FakeOpus) -> None:
    sample = [make_train_pair(i) for i in range(45)]
    first, _ = alter(sample, opus.store, opus.log)
    calls_before = len(opus.calls)
    second, per_account = alter(sample, opus.store, opus.log)
    assert second == first
    assert len(opus.calls) == calls_before
    assert per_account == {}
    assert len(opus.log.read_text(encoding="utf-8").splitlines()) == 3


def test_alter_of_nothing_makes_no_calls(opus: FakeOpus) -> None:
    assert alter([], opus.store, opus.log) == ([], {})
    assert opus.calls == []


def test_alter_unfinished_reply_raises_fail_naming_the_batch(opus: FakeOpus) -> None:
    opus.reply = lambda entries: Unfinished("timeout after 1800 seconds")
    sample = [make_train_pair(i) for i in range(25)]
    with pytest.raises(Fail, match="batch 1") as info:
        alter(sample, opus.store, opus.log)
    assert "Unfinished" in str(info.value)
    assert len(opus.log.read_text(encoding="utf-8").splitlines()) == 1


def test_alter_rejected_reply_raises_fail_naming_the_batch(opus: FakeOpus) -> None:
    opus.reply = lambda entries: Rejected("subtype error_max_structured_output_retries")
    with pytest.raises(Fail, match="batch 1") as info:
        alter([make_train_pair(0)], opus.store, opus.log)
    assert "Rejected" in str(info.value)


def test_failed_batch_is_not_saved_so_rerun_calls_only_that_batch(opus: FakeOpus) -> None:
    sample = [make_train_pair(i) for i in range(25)]
    second_batch_size = len(sample) - REVERT_BATCH

    def fail_second_batch(entries: list[dict[str, str]]) -> Any:
        if len(entries) == second_batch_size:
            return Unfinished("subtype error_during_execution")
        return echo_reply(entries)

    opus.reply = fail_second_batch
    with pytest.raises(Fail, match="batch 2"):
        alter(sample, opus.store, opus.log)
    opus.reply = echo_reply
    texts, per_account = alter(sample, opus.store, opus.log)
    assert texts == [f"altered:{pair.paraphrase}" for pair in sample]
    assert sum(per_account.values()) == 1
    assert len(opus.calls) == 3


MALFORMED_REPLIES: dict[str, Callable[[list[dict[str, str]]], Any]] = {
    "missing id": lambda entries: {"altered": [{"id": e["id"], "text": "made-up-synth"} for e in entries[:-1]]},
    "extra id": lambda entries: {
        "altered": [{"id": e["id"], "text": "made-up-synth"} for e in entries] + [{"id": "0" * 16, "text": "made-up-synth"}]
    },
    "duplicate id": lambda entries: {"altered": [{"id": entries[0]["id"], "text": "made-up-synth"} for _ in entries]},
    "empty text": lambda entries: {"altered": [{"id": e["id"], "text": "  "} for e in entries]},
    "no altered key": lambda entries: {"items": []},
}


@pytest.mark.parametrize("reply", MALFORMED_REPLIES.values(), ids=MALFORMED_REPLIES.keys())
def test_alter_malformed_reply_raises_fail_with_no_text(opus: FakeOpus, reply: Callable[[list[dict[str, str]]], Any]) -> None:
    opus.reply = reply
    sample = [make_train_pair(0), make_train_pair(1)]
    with pytest.raises(Fail) as info:
        alter(sample, opus.store, opus.log)
    assert "made-up-synth" not in str(info.value)


def test_revert_metrics_on_a_hand_built_case() -> None:
    # Nine renders no longer hold the original meaning and one does: nine kept of ten.
    sample = [make_train_pair(i) for i in range(10)]
    altered = [f"altered:{pair.paraphrase}" for pair in sample]
    rendered_holds = [False] * 9 + [True]
    metrics = revert_metrics(sample, altered, lambda speaker, text: f"R:{text}", lambda pairs: rendered_holds)
    assert metrics == {"n": 10, "kept_rate": 0.9, "pass": True}


def test_revert_bar_passes_at_exactly_ninety_percent_kept() -> None:
    sample = [make_train_pair(i) for i in range(100)]
    altered = [f"altered:{pair.paraphrase}" for pair in sample]
    holds = [i >= 90 for i in range(100)]
    metrics = revert_metrics(sample, altered, lambda speaker, text: text, lambda pairs: holds)
    assert metrics["kept_rate"] == 0.9
    assert metrics["pass"] is True


def test_revert_bar_fails_just_below_ninety_percent_kept() -> None:
    sample = [make_train_pair(i) for i in range(100)]
    altered = [f"altered:{pair.paraphrase}" for pair in sample]
    holds = [i >= 89 for i in range(100)]
    metrics = revert_metrics(sample, altered, lambda speaker, text: text, lambda pairs: holds)
    assert metrics["kept_rate"] == 0.89
    assert metrics["pass"] is False


def test_revert_renders_happen_before_holds_many() -> None:
    sample = [make_train_pair(i) for i in range(3)]
    altered = ["altered-0", "altered-1", "altered-2"]
    log: list[str] = []
    hold_inputs: list[list[tuple[str, str]]] = []

    def render(speaker: str, text: str) -> str:
        log.append("render")
        return f"R:{text}"

    def holds_many(pairs: Sequence[tuple[str, str]]) -> list[bool]:
        log.append("holds")
        hold_inputs.append(list(pairs))
        return [False, False, False]

    revert_metrics(sample, altered, render, holds_many)
    assert log == ["render", "render", "render", "holds"]
    assert hold_inputs == [[("R:altered-0", "line-000"), ("R:altered-1", "line-001"), ("R:altered-2", "line-002")]]


def test_revert_metrics_never_passes_a_null_speaker_to_the_renderer() -> None:
    null_pair = Pair(
        scene="scene-x",
        speaker=None,
        paraphrase="para-000",
        original="line-000",
        slot=1,
        model="model-m",
        split="train",
    )
    renders: list[str] = []
    with pytest.raises(Fail):
        revert_metrics(
            [make_train_pair(1), null_pair],
            ["altered-1", "altered-0"],
            lambda speaker, text: renders.append(text) or text,
            lambda pairs: [],
        )
    assert renders == []


def test_revert_metrics_of_nothing_raises_before_any_render() -> None:
    renders: list[str] = []
    with pytest.raises(Fail):
        revert_metrics([], [], lambda speaker, text: renders.append(text) or text, lambda pairs: [])
    assert renders == []


def test_revert_metrics_rejects_an_altered_count_that_differs_from_the_sample() -> None:
    with pytest.raises(Fail):
        revert_metrics([make_train_pair(0)], [], lambda speaker, text: text, lambda pairs: [False])


def test_revert_metrics_result_has_exactly_the_contract_keys_and_no_text() -> None:
    sample = [make_train_pair(0)]
    metrics = revert_metrics(sample, ["altered-secret-a"], lambda speaker, text: f"R:{text}", lambda pairs: [False])
    assert set(metrics) == {"n", "kept_rate", "pass"}
    assert all(isinstance(value, int | float | bool) for value in metrics.values())
    assert "secret" not in repr(metrics)


NLI_MALFORMED = "nli worker sent a malformed reply"


class FakeWorker:
    def __init__(self, argv: Sequence[str], reply: object) -> None:
        self.argv = list(argv)
        self.reply = reply
        self.asks: list[dict[str, Any]] = []
        self.entered = False
        self.exited = False

    def __enter__(self) -> "FakeWorker":
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited = True

    def ask(self, request: dict[str, Any]) -> Any:
        self.asks.append(request)
        return self.reply


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], list[FakeWorker]]:
    def install(reply: object) -> list[FakeWorker]:
        made: list[FakeWorker] = []

        def make(argv: Sequence[str]) -> FakeWorker:
            worker = FakeWorker(argv, reply)
            made.append(worker)
            return worker

        monkeypatch.setattr(remote, "Worker", make)
        return made

    return install


def test_entailer_serves_the_nli_module_through_a_worker(fake_worker: Callable[[object], list[FakeWorker]]) -> None:
    made = fake_worker({"holds": []})
    with Entailer():
        pass
    assert [worker.argv for worker in made] == [["python3", "-m", "tools.voice.nli", "serve"]]
    assert made[0].entered
    assert made[0].exited


def test_holds_many_sends_one_request_of_pairs_and_keeps_order(fake_worker: Callable[[object], list[FakeWorker]]) -> None:
    made = fake_worker({"holds": [True, False]})
    with Entailer() as entailer:
        assert entailer.holds_many([("synthetic-a", "synthetic-b"), ("synthetic-c", "synthetic-d")]) == [True, False]
    assert made[0].asks == [{"pairs": [["synthetic-a", "synthetic-b"], ["synthetic-c", "synthetic-d"]]}]


def test_holds_many_empty_input_sends_nothing(fake_worker: Callable[[object], list[FakeWorker]]) -> None:
    made = fake_worker({"holds": []})
    with Entailer() as entailer:
        assert entailer.holds_many([]) == []
    assert made[0].asks == []


@pytest.mark.parametrize(
    "reply",
    [
        {"holds": [True]},
        {"holds": [True, False, True]},
        {"holds": "yes"},
        {"holds": True},
        {"holds": [1, 0]},
        {"holds": [True, None]},
        {"other": [True, False]},
    ],
    ids=["short", "long", "string", "scalar", "ints", "null-item", "missing-key"],
)
def test_holds_many_malformed_reply_raises_content_free_fail(fake_worker: Callable[[object], list[FakeWorker]], reply: object) -> None:
    fake_worker(reply)
    with Entailer() as entailer, pytest.raises(Fail) as err:
        entailer.holds_many([("synthetic-a", "synthetic-b"), ("synthetic-c", "synthetic-d")])
    assert str(err.value) == NLI_MALFORMED
    assert "synthetic" not in str(err.value)


def test_classifier_scored_labels_distinct_originals_and_differing_paraphrases() -> None:
    pairs = [
        make_pair("Alpha one two", "Alpha one three and more words here"),
        make_pair("Alpha one two", "Alpha one four"),
        make_pair("Beta line", "Beta line"),
    ]
    table = {
        "Alpha one two": 0.9,
        "Beta line": 0.8,
        "Alpha one three and more words here": 0.2,
        "Alpha one four": 0.1,
    }
    calls: list[list[str]] = []

    def score_many(texts: Sequence[str]) -> list[float]:
        calls.append(list(texts))
        return [table[text] for text in texts]

    scored = classifier_scored(pairs, score_many)
    assert calls == [["Alpha one two", "Beta line", "Alpha one three and more words here", "Alpha one four"]]
    # Paraphrase rows take the word count of their original (3), not their own (7).
    # The identical paraphrase of "Beta line" is not scored.
    assert scored == [
        Scored(label=1, words=3, score=0.9),
        Scored(label=1, words=2, score=0.8),
        Scored(label=0, words=3, score=0.2),
        Scored(label=0, words=3, score=0.1),
    ]


def test_classifier_scored_keeps_null_speaker_pairs() -> None:
    pairs = [make_pair("Alpha one two", "Alpha one four", None)]
    table = {"Alpha one two": 0.9, "Alpha one four": 0.1}

    def score_many(texts: Sequence[str]) -> list[float]:
        return [table[text] for text in texts]

    assert classifier_scored(pairs, score_many) == [
        Scored(label=1, words=3, score=0.9),
        Scored(label=0, words=3, score=0.1),
    ]


class Harness:
    """Replaces every live dependency of the eval commands and records the order they are used in."""

    def __init__(self, tmp_path: Path) -> None:
        self.work = tmp_path / "work"
        self.events: list[str] = []
        self.pairs: dict[str, list[Pair]] = {
            "heldout": [make_pair("Alpha one two three", "Alpha paraphrase here", "spk-a")],
            "train": [make_train_pair(i) for i in range(10)],
        }
        self.scores: dict[str, float] = {
            "Alpha one two three": 0.9,
            "Alpha paraphrase here": 0.1,
            "R:Alpha paraphrase here": 0.9,
        }
        self.holds_reply: list[bool] | None = None
        self.account_counts: dict[int, int] = {3: 2, 5: 1}
        self.ports: list[int] = []
        self.render_inputs: list[tuple[str, str]] = []
        self.alter_calls: list[tuple[list[Pair], Store, Path]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(remote, "sync", lambda: self.events.append("sync"))
        monkeypatch.setattr(evals, "work_dir", lambda lens: self.work / lens)
        monkeypatch.setattr(evals, "load_pairs", self._load_pairs)
        monkeypatch.setattr(evals, "Scorer", self._scorer_class())
        monkeypatch.setattr(evals, "Entailer", self._entailer_class())
        monkeypatch.setattr(evals, "rendering", self._rendering)
        monkeypatch.setattr(evals, "alter", self._alter)

    def _load_pairs(self, split: str) -> list[Pair]:
        self.events.append(f"load {split}")
        return list(self.pairs[split])

    @contextmanager
    def _rendering(self, port: int = PORT) -> Iterator[Callable[[str, str], str]]:
        self.events.append(f"rendering {port}")
        self.ports.append(port)

        def render(speaker: str, text: str) -> str:
            self.render_inputs.append((speaker, text))
            return f"R:{text}"

        yield render

    def _alter(self, sample: Sequence[Pair], store: Store, log: Path) -> tuple[list[str], dict[int, int]]:
        self.events.append("alter")
        self.alter_calls.append((list(sample), store, log))
        return [f"altered:{pair.paraphrase}" for pair in sample], dict(self.account_counts)

    def _scorer_class(self) -> type:
        harness = self

        class FakeScorer:
            def __enter__(self) -> "FakeScorer":
                harness.events.append("scorer")
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def score_many(self, texts: Sequence[str]) -> list[float]:
                return [harness.scores[text] for text in texts]

        return FakeScorer

    def _entailer_class(self) -> type:
        harness = self

        class FakeEntailer:
            def __enter__(self) -> "FakeEntailer":
                harness.events.append("entailer")
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def holds_many(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
                if harness.holds_reply is None:
                    return [True] * len(pairs)
                return list(harness.holds_reply)

        return FakeEntailer


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    fake = Harness(tmp_path)
    fake.install(monkeypatch)
    return fake


def parse_eval(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(required=True))
    return parser.parse_args(["eval", *argv])


def test_eval_requires_an_action() -> None:
    with pytest.raises(SystemExit):
        parse_eval([])


def test_eval_rejects_an_unknown_action() -> None:
    with pytest.raises(SystemExit):
        parse_eval(["tally"])


def test_classifier_command_syncs_then_scores_heldout_and_prints_only_metrics(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    args = parse_eval(["classifier"])
    args.fn(args)
    assert harness.events == ["sync", "load heldout", "scorer"]
    # Both lines have 4 words, so both fall in the short subset.
    assert capsys.readouterr().out == "n\t2\nauroc\t1.0\nn_short\t2\nauroc_short\t1.0\npass\ttrue\n"


def test_render_command_syncs_then_renders_heldout_on_the_default_port(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    args = parse_eval(["render"])
    args.fn(args)
    assert harness.events == ["sync", "load heldout", f"rendering {PORT}", "scorer", "entailer"]
    assert harness.ports == [PORT]
    assert harness.render_inputs == [("spk-a", "Alpha paraphrase here")]
    assert capsys.readouterr().out == (
        "n\t1\nmedian_render_score\t0.9\nmedian_paraphrase_score\t0.1\nrender_entailment_rate\t1.0\n"
        "pairs_entailment_rate\t1.0\ngame_likeness_pass\ttrue\nmeaning_pass\ttrue\npass\ttrue\n"
    )


def test_revert_command_syncs_then_alters_train_and_prints_metrics_and_calls(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    harness.holds_reply = [False] * 9 + [True]
    args = parse_eval(["revert"])
    args.fn(args)
    assert harness.events == ["sync", "load train", "alter", f"rendering {PORT}", "entailer"]
    assert capsys.readouterr().out == "n\t10\nkept_rate\t0.9\npass\ttrue\ncalls\t3\naccount 3\t2\naccount 5\t1\n"


def test_revert_alters_the_train_sample_into_work_voice_and_renders_the_altered_texts(harness: Harness) -> None:
    args = parse_eval(["revert"])
    args.fn(args)
    [(sample, store, log)] = harness.alter_calls
    assert sample == revert_sample(harness.pairs["train"])
    assert store.root == harness.work / "voice" / "revert"
    assert log == harness.work / "voice" / "revert-accounts.log"
    assert harness.render_inputs == [(pair.speaker, f"altered:{pair.paraphrase}") for pair in sample]


@pytest.mark.parametrize("command", ["render", "revert"])
def test_render_and_revert_take_a_port_and_default_to_the_renderer_port(harness: Harness, command: str) -> None:
    args = parse_eval([command])
    args.fn(args)
    args = parse_eval([command, "--port", "9001"])
    args.fn(args)
    assert harness.ports == [PORT, 9001]
