"""One stateless headless Claude call under cswap, and the retry that moves a rate-limited call to another account."""

import json
import math
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ffrd.game import Fail
from ffrd.pipeline import accounts

OPUS = "opus"
SONNET = "sonnet"
HAIKU = "haiku"
SYSTEM_PROMPT = "You follow the instructions in the user message and answer only through the structured output."
DEFAULT_CONCURRENCY = 8

_gate = threading.Condition()
_limit = DEFAULT_CONCURRENCY
_in_flight = 0


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


@dataclass(frozen=True)
class Reading:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    duration_ms: int


_sinks = threading.local()


@contextmanager
def usage_sink(sink: Callable[[Reading], None]) -> Iterator[None]:
    """Hand each usage reading made in this thread, while the block runs, to the sink."""
    previous = getattr(_sinks, "sink", None)
    _sinks.sink = sink
    try:
        yield
    finally:
        _sinks.sink = previous


def record(reading: Reading) -> None:
    sink = getattr(_sinks, "sink", None)
    if sink is not None:
        sink(reading)


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
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--tools",
        "",
        "--system-prompt",
        SYSTEM_PROMPT,
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
        record(_reading(model, {}))
        return Unfinished(f"timeout after {timeout:g} seconds")
    document = _result_object(run.stdout)
    record(_reading(model, document or {}))
    if document is None:
        return Unfinished(f"exit {run.returncode}, stdout was not a result object")
    return _classify(document)


def _reading(model: str, document: Mapping[str, Any]) -> Reading:
    usage = document.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return Reading(
        model=model,
        input_tokens=_count(usage.get("input_tokens")),
        output_tokens=_count(usage.get("output_tokens")),
        cache_read_tokens=_count(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_count(usage.get("cache_creation_input_tokens")),
        cost_usd=_amount(document.get("total_cost_usd")),
        duration_ms=_count(document.get("duration_ms")),
    )


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _amount(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0.0
    return float(value)


def _result_object(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("type") == "result":
            return document
    return None


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


def set_concurrency(limit: int) -> None:
    global _limit
    if limit < 1:
        raise Fail("concurrency must be at least 1")
    with _gate:
        _limit = limit
        _gate.notify_all()


def concurrency() -> int:
    with _gate:
        return _limit


@contextmanager
def _slot() -> Iterator[None]:
    global _in_flight
    with _gate:
        _gate.wait_for(lambda: _in_flight < _limit)
        _in_flight += 1
    try:
        yield
    finally:
        with _gate:
            _in_flight -= 1
            _gate.notify()


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
        with _slot():
            outcome = call(current, model, parts, schema)
        if not isinstance(outcome, RateLimited):
            return current, outcome
        limited.add(current)
        current = accounts.choose(limited, sleep=sleep).number


def fan_out[T, R](work: Callable[[T], R], items: Sequence[T]) -> list[R]:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(concurrency(), len(items))) as pool:
        futures = [pool.submit(work, item) for item in items]
        try:
            wait(futures, return_when=FIRST_EXCEPTION)
        finally:
            # On a failed item or an interrupt (Ctrl-C), unstarted items never start; running items finish.
            for future in futures:
                future.cancel()
    for future in futures:
        if future.cancelled():
            continue
        error = future.exception()
        if error is not None:
            raise error
    return [future.result() for future in futures]
