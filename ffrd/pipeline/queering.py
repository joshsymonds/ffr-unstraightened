"""Queering selection: the core cast and antagonists that stay vanilla, the flirt, the seeded order and target that pick the queer side, and the redirect half of the flirt. Inputs are plain bible records, speaker ids, line refs and counts."""

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from typing import Any, Literal

from ffrd.game import Fail

CORE_LINES = 475
CORE_ARCS = 20
REDIRECT_PERCENT = 50

Selection = Literal["queer", "world", "nonbinary", "trans", "untouched", "redirect", "not-redirected"]
SELECTIONS: tuple[Selection, ...] = ("queer", "world", "nonbinary", "trans", "untouched", "redirect", "not-redirected")
REWRITTEN: frozenset[Selection] = frozenset({"queer", "redirect"})

ANTAGONIST_PREFIXES = ("villain", "antagonis")
ANTAGONIST_WORDS = frozenset({"nemesis", "archenemy", "archvillain"})
FLIRT_PREFIX = "flirt"

QUEER_SALT = "queer"
WORLD_SALT = "world"
NONBINARY_SALT = "nonbinary"
TRANS_SALT = "trans"
ROMANCE = "romance"
GENDER_ROLES = "gender-roles"


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


def unit(namespace: str, key: str, speaker: str | None) -> str:
    return speaker if speaker else f"{namespace}/{key}"


def order(units: Iterable[str], salt: str) -> list[str]:
    return sorted(set(units), key=lambda unit: (hashlib.sha256(f"{salt}/{unit}".encode()).digest(), unit))


def target(share: int, size: int) -> int:
    return (share * size + 50) // 100


def redirected(namespace: str, key: str) -> bool:
    return _share(f"{namespace}/{key}") < REDIRECT_PERCENT


def markable(speaker: str | None, core: Collection[str], antagonists: Collection[str], flirt: str | None) -> bool:
    if flirt is not None and speaker == flirt:
        return True
    if not speaker:
        return True
    return speaker not in core and speaker not in antagonists


def selectable(speaker: str | None, core: Collection[str], antagonists: Collection[str], flirt: str | None) -> bool:
    """A unit the queer side may pick: a speakerless line, or a speaker outside the core cast, the antagonists and the flirt."""
    if not speaker:
        return True
    return speaker not in core and speaker not in antagonists and speaker != flirt


def _characters(bibles: Sequence[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    for bible in bibles:
        yield from bible["characters"]


def _words(notes: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", notes.lower())


def _share(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big") % 100
