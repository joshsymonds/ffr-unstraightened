import json
import subprocess
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ffrd.pipeline import claude
from ffrd.pipeline.claude import OPUS, RateLimited, Unfinished, call

# Made-up values. Not game text.
PARTS = ["SYNTHETIC-PROMPT first part", "SYNTHETIC-PROMPT second part"]
SCHEMA: Mapping[str, Any] = {"type": "object"}
MADE_UP = {"answer": "Made-up answer. Not game text."}


def zero_reading() -> claude.Reading:
    return claude.Reading(
        model="opus", input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.0, duration_ms=0
    )


def document(**fields: object) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "structured_output": MADE_UP,
        "usage": {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_read_input_tokens": 33,
            "cache_creation_input_tokens": 44,
        },
        "total_cost_usd": 0.25,
        "duration_ms": 1234,
        **fields,
    }


def fake_cswap(monkeypatch: pytest.MonkeyPatch, stdout: str = "", *, timeout: bool = False) -> None:
    def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if timeout:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(claude.subprocess, "run", run)


def test_call_hands_full_reading_to_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cswap(monkeypatch, json.dumps(document()))
    readings: list[claude.Reading] = []
    with claude.usage_sink(readings.append):
        assert call(3, OPUS, PARTS, SCHEMA) == MADE_UP
    assert readings == [
        claude.Reading(
            model="opus",
            input_tokens=11,
            output_tokens=22,
            cache_read_tokens=33,
            cache_creation_tokens=44,
            cost_usd=0.25,
            duration_ms=1234,
        )
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"usage": None, "total_cost_usd": None, "duration_ms": None},
        {
            "usage": {
                "input_tokens": "SYNTHETIC-MARKER",
                "output_tokens": True,
                "cache_read_input_tokens": -5,
                "cache_creation_input_tokens": None,
            },
            "total_cost_usd": "SYNTHETIC-MARKER",
            "duration_ms": 1.5,
        },
    ],
)
def test_missing_or_non_numeric_usage_fields_read_zero(monkeypatch: pytest.MonkeyPatch, fields: dict[str, object]) -> None:
    result = document(**fields)
    for name, value in fields.items():
        if value is None:
            del result[name]
    fake_cswap(monkeypatch, json.dumps(result))
    readings: list[claude.Reading] = []
    with claude.usage_sink(readings.append):
        assert call(3, OPUS, PARTS, SCHEMA) == MADE_UP
    assert readings == [zero_reading()]


def test_timeout_hands_one_zero_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cswap(monkeypatch, timeout=True)
    readings: list[claude.Reading] = []
    with claude.usage_sink(readings.append):
        assert call(1, OPUS, PARTS, SCHEMA, timeout=0.5) == Unfinished("timeout after 0.5 seconds")
    assert readings == [zero_reading()]


def test_stdout_without_result_object_hands_one_zero_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cswap(monkeypatch, "not json SYNTHETIC-MARKER")
    readings: list[claude.Reading] = []
    with claude.usage_sink(readings.append):
        assert isinstance(call(1, OPUS, PARTS, SCHEMA), Unfinished)
    assert readings == [zero_reading()]


def test_rate_limited_result_hands_its_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cswap(monkeypatch, json.dumps(document(is_error=True, api_error_status=429, structured_output=None)))
    readings: list[claude.Reading] = []
    with claude.usage_sink(readings.append):
        assert call(1, OPUS, PARTS, SCHEMA) == RateLimited("api_error_status 429")
    assert len(readings) == 1
    assert readings[0].input_tokens == 11


def test_call_without_sink_returns_as_before_and_sink_is_gone_after_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cswap(monkeypatch, json.dumps(document()))
    readings: list[claude.Reading] = []
    with claude.usage_sink(readings.append):
        pass
    assert call(3, OPUS, PARTS, SCHEMA) == MADE_UP
    assert readings == []


def test_sink_hears_only_calls_made_in_its_own_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cswap(monkeypatch, json.dumps(document()))
    readings: list[claude.Reading] = []
    with claude.usage_sink(readings.append):
        worker = threading.Thread(target=call, args=(3, OPUS, PARTS, SCHEMA))
        worker.start()
        worker.join()
    assert readings == []
