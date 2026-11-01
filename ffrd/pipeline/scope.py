"""scope: decide which namespaces the rewrite pipeline may change (DL22).

In scope: main story dialogue (`ST_C<n>_`), side-event dialogue (`ST_SubEv_`), and the `ST_DialogChoice`, `ST_MainSummary` and `ST_UnitDescription` namespaces, bare or followed by `_`. Everything else is out of scope, including names, menus, items, skills and tutorials. Matching is case-sensitive and anchored at the start of the namespace.
"""

import re

_IN_SCOPE = re.compile(r"ST_C[0-9]+_|ST_SubEv_|ST_(?:DialogChoice|MainSummary|UnitDescription)(?:_|\Z)")


def in_scope(namespace: str) -> bool:
    return _IN_SCOPE.match(namespace) is not None
