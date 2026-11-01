"""Voice eval metrics: AUROC of game-likeness scores separating held-out game lines (label 1) from their paraphrases (label 0), overall and for short lines; and the revert check, where Opus alters training lines and the share of renders that keep the alteration is measured."""

import argparse
import hashlib
import json
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from ffrd.game import Fail
from ffrd.pipeline import accounts, claude
from ffrd.pipeline.claude import Rejected, Unfinished
from ffrd.pipeline.store import Store, work_dir
from ffrd.voice import remote
from ffrd.voice.classifier import Pair, Scorer, load_pairs
from ffrd.voice.render import PORT, rendering

CLASSIFIER_BAR = 0.90
SHORT_WORDS = 8
RENDER_SCORE_BAR = 0.5
MEANING_MARGIN = 0.03
REVERT_BAR = 0.90
REVERT_INPUTS = 200
REVERT_BATCH = 20
# Exact decimal so the meaning boundary is compared without float rounding.
_MEANING_MARGIN_EXACT = Fraction(str(MEANING_MARGIN))
_REVERT_BAR_EXACT = Fraction(str(REVERT_BAR))

ALTER_PROMPT = (
    "Each entry below is a line of game dialogue. For each entry, return a version of its text that changes "
    "its meaning in one concrete way: a fact, an object, a place, a number, or a yes to a no. Keep each "
    "entry's length and tone. Return one entry per id, using the same id."
)
ALTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "altered": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["altered"],
    "additionalProperties": False,
}

Renderer = Callable[[str, str], str]
ScoreMany = Callable[[Sequence[str]], list[float]]
HoldsMany = Callable[[Sequence[tuple[str, str]]], list[bool]]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def heldout_sample(pairs: Sequence[Pair]) -> list[Pair]:
    """One pair per held-out line: the paraphrase with the lowest sha256, lines ordered by sha256 of the original."""
    chosen: dict[str, Pair] = {}
    for pair in pairs:
        best = chosen.get(pair.original)
        if best is None or _digest(pair.paraphrase) < _digest(best.paraphrase):
            chosen[pair.original] = pair
    return sorted(chosen.values(), key=lambda pair: _digest(pair.original))


def render_metrics(pairs: Sequence[Pair], render: Renderer, score_many: ScoreMany, holds_many: HoldsMany) -> dict[str, int | float | bool]:
    sample = heldout_sample(pairs)
    if not sample:
        raise Fail("render eval needs at least one held-out pair")
    renders = [render(pair.speaker, pair.paraphrase) for pair in sample]
    render_scores = score_many(renders)
    paraphrase_scores = score_many([pair.paraphrase for pair in sample])
    render_holds = holds_many([(pair.paraphrase, rendered) for pair, rendered in zip(sample, renders, strict=True)])
    pair_holds = holds_many([(pair.paraphrase, pair.original) for pair in sample])
    n = len(sample)
    median_render = statistics.median(render_scores)
    median_paraphrase = statistics.median(paraphrase_scores)
    render_hits = sum(render_holds)
    pair_hits = sum(pair_holds)
    game_likeness_pass = median_render >= RENDER_SCORE_BAR and median_render > median_paraphrase
    meaning_pass = Fraction(render_hits, n) >= Fraction(pair_hits, n) - _MEANING_MARGIN_EXACT
    return {
        "n": n,
        "median_render_score": median_render,
        "median_paraphrase_score": median_paraphrase,
        "render_entailment_rate": render_hits / n,
        "pairs_entailment_rate": pair_hits / n,
        "game_likeness_pass": game_likeness_pass,
        "meaning_pass": meaning_pass,
        "pass": game_likeness_pass and meaning_pass,
    }


def word_count(text: str) -> int:
    return len(text.split())


@dataclass(frozen=True)
class Scored:
    label: int
    words: int
    score: float


def auroc(scores: Sequence[float], labels: Sequence[int]) -> float:
    if len(scores) != len(labels):
        raise Fail("scores and labels differ in length")
    if any(label not in (0, 1) for label in labels):
        raise Fail("labels must be 0 or 1")
    n1 = sum(labels)
    n0 = len(labels) - n1
    if n1 == 0 or n0 == 0:
        raise Fail("AUROC needs both a game line and a paraphrase")
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    rank_sum = 0.0
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and scores[order[end + 1]] == scores[order[start]]:
            end += 1
        average_rank = (start + end) / 2 + 1
        rank_sum += average_rank * sum(labels[order[k]] for k in range(start, end + 1))
        start = end + 1
    u = rank_sum - n1 * (n1 + 1) / 2
    return u / (n1 * n0)


def classifier_metrics(scored: Sequence[Scored]) -> dict[str, int | float | bool | None]:
    overall = auroc([item.score for item in scored], [item.label for item in scored])
    short = [item for item in scored if item.words < SHORT_WORDS]
    short_labels = {item.label for item in short}
    auroc_short = auroc([item.score for item in short], [item.label for item in short]) if short_labels == {0, 1} else None
    return {
        "n": len(scored),
        "auroc": overall,
        "n_short": len(short),
        "auroc_short": auroc_short,
        "pass": overall >= CLASSIFIER_BAR,
    }


def revert_sample(pairs: Sequence[Pair], n: int = REVERT_INPUTS) -> list[Pair]:
    return heldout_sample([pair for pair in pairs if pair.split == "train"])[:n]


def _alter_id(pair: Pair) -> str:
    return _digest(pair.paraphrase)[:16]


def _log_call(log: Path, number: int, account: int) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\tbatch {number}\taccount {account}\n")
    except OSError as e:
        raise Fail(f"cannot append to {log}: {e.strerror}") from e


def _altered_texts(number: int, entries: list[dict[str, str]], outcome: dict[str, Any]) -> dict[str, str]:
    replies = outcome.get("altered")
    if not isinstance(replies, list) or not all(
        isinstance(reply, dict) and isinstance(reply.get("id"), str) and isinstance(reply.get("text"), str) for reply in replies
    ):
        raise Fail(f"revert batch {number} reply is malformed")
    if sorted(reply["id"] for reply in replies) != sorted(entry["id"] for entry in entries):
        raise Fail(f"revert batch {number} reply ids do not match the batch")
    if any(not reply["text"].strip() for reply in replies):
        raise Fail(f"revert batch {number} reply has an empty text")
    return {reply["id"]: reply["text"] for reply in replies}


def _ask_batch(number: int, entries: list[dict[str, str]], log: Path, calls: dict[int, int]) -> dict[str, str]:
    account = accounts.choose()
    used, outcome = claude.ask(
        account.number,
        claude.OPUS,
        [ALTER_PROMPT, json.dumps(entries, ensure_ascii=False)],
        ALTER_SCHEMA,
    )
    calls[used] = calls.get(used, 0) + 1
    _log_call(log, number, used)
    if isinstance(outcome, Unfinished | Rejected):
        raise Fail(f"revert batch {number} failed: {type(outcome).__name__}: {outcome.detail}")
    return _altered_texts(number, entries, outcome)


def alter(sample: Sequence[Pair], store: Store, log: Path) -> tuple[list[str], dict[int, int]]:
    texts: dict[str, str] = {}
    calls: dict[int, int] = {}
    for number, start in enumerate(range(0, len(sample), REVERT_BATCH), start=1):
        entries = [{"id": _alter_id(pair), "text": pair.paraphrase} for pair in sample[start : start + REVERT_BATCH]]
        result = store.run("revert-alter", entries, None, partial(_ask_batch, number, entries, log, calls))
        texts.update(result.value)
    return [texts[_alter_id(pair)] for pair in sample], calls


def revert_metrics(
    sample: Sequence[Pair],
    altered: Sequence[str],
    render: Renderer,
    holds_many: HoldsMany,
) -> dict[str, int | float | bool]:
    if not sample:
        raise Fail("revert eval needs at least one training input")
    if len(altered) != len(sample):
        raise Fail("revert eval needs one altered text per training input")
    renders = [render(pair.speaker, text) for pair, text in zip(sample, altered, strict=True)]
    holds = holds_many([(rendered, pair.original) for pair, rendered in zip(sample, renders, strict=True)])
    n = len(sample)
    kept = n - sum(holds)
    return {
        "n": n,
        "kept_rate": kept / n,
        "pass": Fraction(kept, n) >= _REVERT_BAR_EXACT,
    }


NLI_MALFORMED_REPLY = "nli worker sent a malformed reply"


class Entailer:
    def __init__(self) -> None:
        self._worker = remote.Worker(["python3", "-m", "tools.voice.nli", "serve"])

    def __enter__(self) -> Self:
        self._worker.__enter__()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self._worker.__exit__(exc_type, exc, tb)

    def holds_many(self, pairs: Sequence[tuple[str, str]]) -> list[bool]:
        if not pairs:
            return []
        reply = self._worker.ask({"pairs": [list(pair) for pair in pairs]})
        holds = reply.get("holds")
        if not isinstance(holds, list) or len(holds) != len(pairs) or not all(isinstance(hold, bool) for hold in holds):
            raise Fail(NLI_MALFORMED_REPLY)
        return holds


def classifier_scored(pairs: Sequence[Pair], score_many: ScoreMany) -> list[Scored]:
    labelled = [(original, 1, word_count(original)) for original in dict.fromkeys(pair.original for pair in pairs)]
    labelled += [(pair.paraphrase, 0, word_count(pair.original)) for pair in pairs if pair.paraphrase != pair.original]
    scores = score_many([text for text, _, _ in labelled])
    return [Scored(label=label, words=words, score=score) for (_, label, words), score in zip(labelled, scores, strict=True)]


def _print_metrics(metrics: Mapping[str, object]) -> None:
    for key, value in metrics.items():
        print(f"{key}\t{json.dumps(value)}")


def cmd_classifier(args: argparse.Namespace) -> None:
    remote.sync()
    pairs = load_pairs("heldout")
    with Scorer() as scorer:
        scored = classifier_scored(pairs, scorer.score_many)
    _print_metrics(classifier_metrics(scored))


def cmd_render(args: argparse.Namespace) -> None:
    remote.sync()
    pairs = load_pairs("heldout")
    with rendering(args.port) as render_line, Scorer() as scorer, Entailer() as entailer:
        metrics = render_metrics(pairs, render_line, scorer.score_many, entailer.holds_many)
    _print_metrics(metrics)


def cmd_revert(args: argparse.Namespace) -> None:
    remote.sync()
    sample = revert_sample(load_pairs("train"))
    voice = work_dir("voice")
    altered, calls = alter(sample, Store(voice / "revert"), voice / "revert-accounts.log")
    with rendering(args.port) as render_line, Entailer() as entailer:
        metrics = revert_metrics(sample, altered, render_line, entailer.holds_many)
    _print_metrics(metrics)
    print(f"calls\t{sum(calls.values())}")
    for account in sorted(calls):
        print(f"account {account}\t{calls[account]}")


def _add_port(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", type=int, default=PORT, help="llama-server port on the GPU host's 127.0.0.1")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ev = subparsers.add_parser("eval", help="print voice-model metrics: classifier AUROC, render and revert checks")
    actions = ev.add_subparsers(dest="action", required=True)
    classifier_p = actions.add_parser("classifier", help="AUROC of the classifier on held-out game lines vs paraphrases")
    classifier_p.set_defaults(fn=cmd_classifier)
    render_p = actions.add_parser("render", help="render held-out paraphrases on the GPU host and check game-likeness and meaning")
    _add_port(render_p)
    render_p.set_defaults(fn=cmd_render)
    revert_p = actions.add_parser("revert", help="alter train lines with Opus and check that renders keep the alteration")
    _add_port(revert_p)
    revert_p.set_defaults(fn=cmd_revert)
