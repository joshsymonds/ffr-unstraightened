"""cswap account choice: read usage from `cswap list --json` and pick the least-used account under the weekly limit."""

import json
import subprocess
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ffrd.game import Fail

WEEKLY_LIMIT = 90.0
MIN_WAIT = 60.0
TRANSIENT_BACKOFF = 30.0
TRANSIENT_BOUND = 300.0


@dataclass(frozen=True)
class Account:
    number: int
    weekly_pct: float
    resets: tuple[datetime, ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _usable(row: dict[str, Any]) -> bool:
    return row["usageStatus"] == "ok" and not row.get("disabled", False) and "sevenDay" in row["usage"]


def _account(row: dict[str, Any]) -> Account:
    usage = row["usage"]
    windows = [usage[key] for key in ("fiveHour", "sevenDay") if key in usage]
    return Account(
        number=row["number"],
        weekly_pct=float(usage["sevenDay"]["pct"]),
        resets=tuple(datetime.fromisoformat(window["resetsAt"]) for window in windows if "resetsAt" in window),
    )


def read() -> list[Account]:
    try:
        result = subprocess.run(["cswap", "list", "--json"], capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise Fail("cswap not found on PATH") from e
    if result.returncode != 0:
        raise Fail(f"cswap list --json exited {result.returncode}")
    try:
        rows = json.loads(result.stdout)["accounts"]
        return [_account(row) for row in rows if _usable(row)]
    except (KeyError, TypeError, ValueError) as e:
        raise Fail("cswap list --json printed output ffrd cannot read") from e


def _least_used(accounts: list[Account], exclude: Collection[int]) -> Account | None:
    under = [a for a in accounts if a.number not in exclude and a.weekly_pct < WEEKLY_LIMIT]
    return min(under, key=lambda a: (a.weekly_pct, a.number), default=None)


def choose(
    exclude: Collection[int] = (),
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> Account:
    excluded = exclude
    absent_since: datetime | None = None
    while True:
        accounts = read()
        if not accounts:
            if absent_since is None:
                absent_since = now()
            elif (now() - absent_since).total_seconds() >= TRANSIENT_BOUND:
                raise Fail("no usable cswap account")
            sleep(TRANSIENT_BACKOFF)
            continue
        absent_since = None
        picked = _least_used(accounts, excluded)
        if picked is not None:
            return picked
        earliest = min((reset for account in accounts for reset in account.resets), default=None)
        sleep(MIN_WAIT if earliest is None else max((earliest - now()).total_seconds(), MIN_WAIT))
        excluded = ()
