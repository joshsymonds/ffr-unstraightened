"""Arc context: story-bible arcs merged across chapters, and the character notes a scene's stages receive."""

from collections.abc import Collection, Mapping, Sequence
from typing import Any


def consolidate(bibles: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One record per arc id, in order of first appearance across the bibles."""
    records: dict[str, dict[str, Any]] = {}
    for bible in bibles:
        chapter = bible["chapter"]
        for arc in bible["arcs"]:
            record = records.setdefault(arc["id"], {"id": arc["id"], "characters": [], "states": [], "beats": []})
            for speaker in arc["characters"]:
                if speaker not in record["characters"]:
                    record["characters"].append(speaker)
            record["states"].append({"chapter": chapter, "state": arc["state"]})
            for beat in arc["beats"]:
                record["beats"].append(
                    {
                        "line": dict(beat["line"]),
                        "kind": beat["kind"],
                        "earned": beat["earned"],
                        "chapter": chapter,
                    }
                )
    return list(records.values())


def context(
    arcs: Sequence[Mapping[str, Any]],
    bibles: Sequence[Mapping[str, Any]],
    speakers: Collection[str | None],
    speaker: str | None,
) -> dict[str, Any]:
    """Arcs involving any scene speaker, and the target speaker's character notes from every bible."""
    scene = {name for name in speakers if name is not None}
    return {
        "arcs": [arc for arc in arcs if any(name in scene for name in arc["characters"])],
        "characters": _notes(bibles, speaker) if speaker is not None else [],
    }


def cast(bibles: Sequence[Mapping[str, Any]], speakers: Collection[str | None]) -> list[dict[str, Any]]:
    """Every character entry, in bible order, whose speaker is in the scene."""
    return [
        {**entry, "chapter": bible["chapter"]}
        for bible in bibles
        for entry in bible["characters"]
        if entry["speaker"] is not None and entry["speaker"] in speakers
    ]


def _notes(bibles: Sequence[Mapping[str, Any]], speaker: str) -> list[dict[str, Any]]:
    return [
        {**{key: value for key, value in entry.items() if key != "speaker"}, "chapter": bible["chapter"]}
        for bible in bibles
        for entry in bible["characters"]
        if entry["speaker"] == speaker
    ]
