"""scope: decide which namespaces the rewrite pipeline may change (DL22, DL38).

Hard exclusions are out on every path: namespaces starting `ST_Tutorial` or `ST_UI`, namespaces ending `Name` or `SkillDescription`, and namespaces starting `ST_ConsumablesDescription`, `ST_EquipmentDescription` or `ST_ArtifactDescription`. Matching is case-sensitive.

With the game's scene table (`scene_table=True`), every namespace not excluded is in scope. Without it (`scene_table=False`), the fallback allowlist applies, minus the same exclusions: `ST_C<n>_`, `ST_SubEv`, `ST_ChrEv`, `ST_FEV`, `ST_LegendEV`, `ST_GimEv`, `ST_BtlEv` and `ST_EV` prefixes, and the `ST_DialogChoice`, `ST_MainSummary` and `ST_UnitDescription` namespaces, bare or followed by `_`.

`scoped` applies that rule to scenes: each scene keeps only its in-scope lines, in order and under its key, and a scene left with no line is dropped.
"""

import re
from collections.abc import Sequence

from ffrd.pipeline.stages import Scene

_HARD_EXCLUDED = re.compile(
    r"^(?:ST_Tutorial|ST_UI|ST_ConsumablesDescription|ST_EquipmentDescription|ST_ArtifactDescription)"
    r"|(?:Name|SkillDescription)\Z"
)
_FALLBACK = re.compile(
    r"ST_C[0-9]+_|ST_(?:SubEv|ChrEv|FEV|LegendEV|GimEv|BtlEv|EV)|ST_(?:DialogChoice|MainSummary|UnitDescription)(?:_|\Z)"
)


def in_scope(namespace: str, *, scene_table: bool) -> bool:
    if _HARD_EXCLUDED.search(namespace) is not None:
        return False
    return scene_table or _FALLBACK.match(namespace) is not None


def scoped(scenes: Sequence[Scene], *, scene_table: bool) -> list[Scene]:
    kept: list[Scene] = []
    for scene in scenes:
        lines = tuple(line for line in scene.lines if in_scope(line.namespace, scene_table=scene_table))
        if lines:
            kept.append(Scene(key=scene.key, lines=lines))
    return kept
