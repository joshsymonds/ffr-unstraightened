"""One stateless headless Claude call under cswap, and the retry that moves a rate-limited call to another account."""

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ffrd.game import Fail
from ffrd.pipeline import accounts

OPUS = "opus"
SONNET = "sonnet"


@dataclass(frozen=True)
class RateLimited:
    detail: str


@dataclass(frozen=True)
class Unfinished:
    detail: str


@dataclass(frozen=True)
class Rejected:
    detail: str


Failure = RateLimited | Unfinished | Rejected


def call(
    account: int,
    model: str,
    parts: Sequence[str],
    schema: Mapping[str, Any],
    *,
    timeout: float = 1800.0,
) -> dict[str, Any] | Failure:
    argv = [
        "cswap",
        "run",
        str(account),
        "--",
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--no-session-persistence",
    ]
    try:
        run = subprocess.run(
            argv,
            input="\n\n".join(parts),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise Fail("cswap not found on PATH") from e
    except subprocess.TimeoutExpired:
        return Unfinished(f"timeout after {timeout:g} seconds")
    try:
        document = json.loads(run.stdout)
    except ValueError:
        document = None
    if not isinstance(document, dict) or document.get("type") != "result":
        return Unfinished(f"exit {run.returncode}, stdout was not a result object")
    return _classify(document)


def _classify(document: dict[str, Any]) -> dict[str, Any] | Failure:
    subtype = document.get("subtype")
    if document.get("is_error") and document.get("api_error_status") == 429:
        return RateLimited("api_error_status 429")
    if subtype == "error_max_structured_output_retries":
        return Rejected("subtype error_max_structured_output_retries")
    if document.get("is_error"):
        return Unfinished(f"subtype {subtype if isinstance(subtype, str) else 'missing'}")
    output = document.get("structured_output")
    if not isinstance(output, dict):
        return Rejected("structured_output is not a JSON object")
    return output


def ask(
    account: int,
    model: str,
    parts: Sequence[str],
    schema: Mapping[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, Any] | Unfinished | Rejected]:
    limited: set[int] = set()
    current = account
    while True:
        outcome = call(current, model, parts, schema)
        if not isinstance(outcome, RateLimited):
            return current, outcome
        limited.add(current)
        current = accounts.choose(limited, sleep=sleep).number
