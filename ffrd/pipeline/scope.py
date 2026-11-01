"""scope: decide which namespaces the rewrite pipeline may change (DL22).

In scope: main story dialogue (`ST_C<n>_`), side-event dialogue (`ST_SubEv_`), and the `ST_DialogChoice`, `ST_MainSummary` and `ST_UnitDescription` namespaces, bare or followed by `_`. Everything else is out of scope, including names, menus, items, skills and tutorials. Matching is case-sensitive and anchored at the start of the namespace.

`scoped` applies that rule to scenes: each scene keeps only its in-scope lines, in order and under its key, and a scene left with no line is dropped.
"""

import re
from collections.abc import Sequence

from ffrd.pipeline.stages import Scene

_IN_SCOPE = re.compile(r"ST_C[0-9]+_|ST_SubEv_|ST_(?:DialogChoice|MainSummary|UnitDescription)(?:_|\Z)")


def in_scope(namespace: str) -> bool:
    return _IN_SCOPE.match(namespace) is not None


def scoped(scenes: Sequence[Scene]) -> list[Scene]:
    kept: list[Scene] = []
    for scene in scenes:
        lines = tuple(line for line in scene.lines if in_scope(line.namespace))
        if lines:
            kept.append(Scene(key=scene.key, lines=lines))
    return kept
