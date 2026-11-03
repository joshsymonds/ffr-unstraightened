"""Queering selection: the core cast and antagonists that stay vanilla, the flirt, the run-wide ranking that picks queer and trans speakers, and the redirect half of the flirt. Inputs are plain bible records, speaker ids, line refs and counts."""

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Any, Literal

from ffrd.game import Fail

CORE_LINES = 475
CORE_ARCS = 20
QUEER_PERCENT = 15
TRANS_PERCENT = 5
REDIRECT_PERCENT = 50

Bucket = Literal["queer", "trans", "untouched"]
Selection = Literal["queer", "trans", "untouched", "redirect", "not-redirected"]
SELECTIONS: tuple[Selection, ...] = ("queer", "trans", "untouched", "redirect", "not-redirected")
REWRITTEN: frozenset[Selection] = frozenset({"queer", "trans", "redirect"})

ANTAGONIST_PREFIXES = ("villain", "antagonis")
ANTAGONIST_WORDS = frozenset({"nemesis", "archenemy", "archvillain"})
FLIRT_PREFIX = "flirt"


def core_cast(bibles: Sequence[Mapping[str, Any]], line_counts: Mapping[str, int]) -> frozenset[str]:
    arcs_by_speaker: defaultdict[str, set[str]] = defaultdict(set)
    for bible in bibles:
        for arc in bible["arcs"]:
            for speaker in arc["characters"]:
                arcs_by_speaker[speaker].add(arc["id"])
    return frozenset(
        speaker for speaker, arc_ids in arcs_by_speaker.items() if len(arc_ids) >= CORE_ARCS and line_counts.get(speaker, 0) >= CORE_LINES
    )


def antagonists(bibles: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    return frozenset(
        character["speaker"]
        for character in _characters(bibles)
        if any(word.startswith(ANTAGONIST_PREFIXES) or word in ANTAGONIST_WORDS for word in _words(character["notes"]))
    )


def flirt(bibles: Sequence[Mapping[str, Any]]) -> str | None:
    counts = Counter(
        character["speaker"]
        for character in _characters(bibles)
        if any(word.startswith(FLIRT_PREFIX) for word in _words(character["notes"]))
    )
    if not counts:
        return None
    top = max(counts.values())
    leaders = [speaker for speaker, count in counts.items() if count == top]
    if len(leaders) > 1:
        raise Fail(f"{len(leaders)} speakers tie for the most flirt entries ({top} each)")
    return leaders[0]


def rank(
    marked: Sequence[tuple[str, str, str | None]], core: Collection[str], antagonists: Collection[str], flirt: str | None
) -> dict[tuple[str, str], Bucket]:
    """Rank the units of the marked lines across the run. A speaker is one unit and each speakerless line is its own unit, identified by its namespace and key. Units sort by the SHA-256 of their id: the first ceiling of QUEER_PERCENT of them are queer, the next ceiling of TRANS_PERCENT of the rest are trans. Core, antagonist and flirt lines are not ranked."""
    units: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for namespace, key, speaker in marked:
        if speaker and (speaker in core or speaker in antagonists or speaker == flirt):
            continue
        units[speaker if speaker else f"{namespace}/{key}"].append((namespace, key))
    order = sorted(units, key=lambda unit: (hashlib.sha256(unit.encode("utf-8")).digest(), unit))
    queer_end = _ceil_share(QUEER_PERCENT, len(order))
    trans_end = queer_end + min(_ceil_share(TRANS_PERCENT, len(order)), len(order) - queer_end)
    buckets: dict[tuple[str, str], Bucket] = {}
    for position, unit in enumerate(order):
        bucket: Bucket = "queer" if position < queer_end else "trans" if position < trans_end else "untouched"
        for ref in units[unit]:
            buckets[ref] = bucket
    return buckets


def redirected(namespace: str, key: str) -> bool:
    return _share(f"{namespace}/{key}") < REDIRECT_PERCENT


def markable(speaker: str | None, core: Collection[str], antagonists: Collection[str], flirt: str | None) -> bool:
    if flirt is not None and speaker == flirt:
        return True
    if not speaker:
        return True
    return speaker not in core and speaker not in antagonists


def select(namespace: str, key: str, speaker: str | None, flirt: str | None, ranked: Mapping[tuple[str, str], Bucket]) -> Selection:
    if flirt is not None and speaker == flirt:
        return "redirect" if redirected(namespace, key) else "not-redirected"
    return ranked.get((namespace, key), "untouched")


def _characters(bibles: Sequence[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    for bible in bibles:
        yield from bible["characters"]


def _words(notes: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", notes.lower())


def _share(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big") % 100


def _ceil_share(percent: int, units: int) -> int:
    return -(-percent * units // 100)
