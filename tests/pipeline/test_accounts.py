import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.pipeline.accounts import WEEKLY_LIMIT, Account, choose, read
from ffrd.pipeline.claude import OPUS, RateLimited, Rejected, Unfinished, ask, call

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

# Stands in for cswap. `cswap run <n> -- <args>` exports the account and execs claude with <args>, as cswap does.
# Any other call logs its arguments, then prints the JSON document the test wrote, and exits with FAKE_CSWAP_EXIT
# when the test sets it.
FAKE_CSWAP = """#!/bin/sh
if [ "$1" = "run" ] && [ "$3" = "--" ]; then
  CSWAP_ACCOUNT="$2"
  export CSWAP_ACCOUNT
  shift 3
  exec claude "$@"
fi
printf '%s\\n' "$*" >> "$FAKE_CSWAP_LOG"
cat "$FAKE_CSWAP_OUTPUT"
exit "${FAKE_CSWAP_EXIT:-0}"
"""

# Stands in for claude. Logs the account, argv and stdin of each call, then sleeps for the answer's seconds, prints
# its stdout and exits with its code. An account the test did not answer exits 99, so a missing answer cannot pass.
FAKE_CLAUDE = """#!PYTHON
import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["FAKE_CLAUDE_DIR"])
account = os.environ["CSWAP_ACCOUNT"]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"account": int(account), "argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
answer = root / ("answer-" + account + ".json")
if not answer.exists():
    sys.exit(99)
spec = json.loads(answer.read_text())
time.sleep(spec["seconds"])
sys.stdout.write(spec["stdout"])
sys.exit(spec["exit"])
"""

# Made-up identity values. Nothing here is account data.
IDENTITY = {"email": "synthetic@example.invalid", "accountId": "synthetic-id"}

# Made-up prompt, schema and result text.
PARTS = ["SYNTHETIC-PROMPT first part", "SYNTHETIC-PROMPT second part"]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
SCHEMA_JSON = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}'
RESULT_TEXT = "SYNTHETIC-MARKER result text"
MADE_UP = {"answer": "made-up"}
RATE_LIMIT = {
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "api_error_status": 429,
    "result": RESULT_TEXT,
}


class FakeCswap:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.output = root / "cswap-output.txt"
        self.log = root / "cswap.log"
        bindir = root / "bin"
        bindir.mkdir()
        exe = bindir / "cswap"
        exe.write_text(FAKE_CSWAP)
        exe.chmod(0o755)
        self.claude_dir = root / "claude"
        self.claude_dir.mkdir()
        claude = bindir / "claude"
        claude.write_text(FAKE_CLAUDE.replace("#!PYTHON", f"#!{sys.executable}"))
        claude.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_CSWAP_OUTPUT", str(self.output))
        monkeypatch.setenv("FAKE_CSWAP_LOG", str(self.log))
        monkeypatch.setenv("FAKE_CLAUDE_DIR", str(self.claude_dir))
        monkeypatch.delenv("FAKE_CSWAP_EXIT", raising=False)

    def publish(self, output: object) -> None:
        self.output.write_text(output if isinstance(output, str) else json.dumps(output))

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def answer(self, account: int, stdout: object, *, code: int = 0, seconds: float = 0.0) -> None:
        text = stdout if isinstance(stdout, str) else json.dumps(stdout)
        spec = {"stdout": text, "exit": code, "seconds": seconds}
        (self.claude_dir / f"answer-{account}.json").write_text(json.dumps(spec))

    def claude_calls(self) -> list[dict[str, Any]]:
        log = self.claude_dir / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


class Sleeps:
    def __init__(self, on_sleep: Callable[[], None] = lambda: None) -> None:
        self.durations: list[float] = []
        self.on_sleep = on_sleep

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
        self.on_sleep()


@pytest.fixture
def cswap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCswap:
    return FakeCswap(tmp_path, monkeypatch)


def stamp(delta: timedelta) -> str:
    return f"{NOW + delta:%Y-%m-%dT%H:%M:%S.%f}+00"


def window(pct: float, resets_in: timedelta) -> dict[str, object]:
    return {"pct": pct, "resetsAt": stamp(resets_in)}


def usable(
    number: int,
    weekly: float,
    weekly_resets_in: timedelta = timedelta(days=3),
    *,
    five_hour: tuple[float, timedelta] | None = None,
    **extra: object,
) -> dict[str, object]:
    usage: dict[str, object] = {"sevenDay": window(weekly, weekly_resets_in)}
    if five_hour is not None:
        usage["fiveHour"] = window(*five_hour)
    return {"number": number, "usageStatus": "ok", "usage": usage, **IDENTITY, **extra}


def unusable(number: int, status: str = "error") -> dict[str, object]:
    return {"number": number, "usageStatus": status, "usage": None, **IDENTITY}


def listing(*rows: dict[str, Any]) -> dict[str, object]:
    return {"schemaVersion": 1, "activeAccountNumber": 1, "accounts": list(rows)}


def fixed_now() -> datetime:
    return NOW


def test_read_keeps_only_ok_rows_with_weekly_window_and_no_disabled_flag(cswap: FakeCswap) -> None:
    cswap.publish(
        listing(
            usable(1, 12.5, timedelta(days=2), five_hour=(3.0, timedelta(hours=4))),
            unusable(2),
            {"number": 3, "usageStatus": "ok", "usage": {"fiveHour": window(5.0, timedelta(hours=2))}, **IDENTITY},
            usable(4, 20.0, disabled=True),
            usable(5, 30.0, disabled=False),
        )
    )
    assert read() == [
        Account(number=1, weekly_pct=12.5, resets=(NOW + timedelta(hours=4), NOW + timedelta(days=2))),
        Account(number=5, weekly_pct=30.0, resets=(NOW + timedelta(days=3),)),
    ]


def test_read_keeps_accounts_whose_windows_carry_no_reset(cswap: FakeCswap) -> None:
    cswap.publish(
        listing(
            {
                "number": 1,
                "usageStatus": "ok",
                "usage": {"fiveHour": {"pct": 0.0}, "sevenDay": window(40.0, timedelta(days=1))},
                **IDENTITY,
            },
            {"number": 2, "usageStatus": "ok", "usage": {"sevenDay": {"pct": 0.0}}, **IDENTITY},
        )
    )
    assert read() == [
        Account(number=1, weekly_pct=40.0, resets=(NOW + timedelta(days=1),)),
        Account(number=2, weekly_pct=0.0, resets=()),
    ]


def test_choose_with_no_known_reset_waits_sixty_seconds_then_drops_exclusions(cswap: FakeCswap) -> None:
    cswap.publish(listing({"number": 1, "usageStatus": "ok", "usage": {"sevenDay": {"pct": 0.0}}, **IDENTITY}))
    sleep = Sleeps()
    assert choose([1], sleep=sleep, now=fixed_now).number == 1
    assert sleep.durations == [60.0]


def test_choose_returns_lowest_weekly_use_under_limit_and_skips_excluded(cswap: FakeCswap) -> None:
    assert WEEKLY_LIMIT == 90.0
    cswap.publish(listing(usable(1, 90.0), usable(2, 45.0), usable(3, 45.0), usable(4, 60.0)))
    assert choose(sleep=Sleeps(), now=fixed_now).number == 2
    assert choose([2], sleep=Sleeps(), now=fixed_now).number == 3
    assert choose([2, 3], sleep=Sleeps(), now=fixed_now).number == 4


def test_choose_rereads_cswap_on_every_call(cswap: FakeCswap) -> None:
    cswap.publish(listing(usable(1, 10.0), usable(2, 50.0)))
    assert choose(sleep=Sleeps(), now=fixed_now).number == 1
    cswap.publish(listing(usable(1, 80.0), usable(2, 20.0)))
    assert choose(sleep=Sleeps(), now=fixed_now).number == 2
    assert cswap.calls() == ["list --json", "list --json"]


def test_choose_waits_for_earliest_reset_across_usable_windows_then_drops_exclusions(cswap: FakeCswap) -> None:
    cswap.publish(
        listing(
            usable(1, 95.0, timedelta(days=2)),
            usable(2, 99.0, timedelta(days=5), five_hour=(40.0, timedelta(minutes=30))),
            usable(3, 91.0, timedelta(hours=6)),
            unusable(4),
            usable(5, 99.0, timedelta(minutes=10), disabled=True),
        )
    )
    sleep = Sleeps(on_sleep=lambda: cswap.publish(listing(usable(1, 95.0), usable(2, 20.0), usable(3, 91.0))))
    assert choose([2], sleep=sleep, now=fixed_now).number == 2
    assert sleep.durations == [1800.0]
    assert cswap.calls() == ["list --json", "list --json"]


@pytest.mark.parametrize("reset_in", [timedelta(seconds=10), timedelta(hours=-1)])
def test_choose_waits_at_least_sixty_seconds(cswap: FakeCswap, reset_in: timedelta) -> None:
    cswap.publish(listing(usable(1, 99.0, reset_in)))
    sleep = Sleeps(on_sleep=lambda: cswap.publish(listing(usable(1, 10.0))))
    assert choose(sleep=sleep, now=fixed_now).number == 1
    assert sleep.durations == [60.0]


def test_choose_with_no_usable_account_raises_fail_without_waiting(cswap: FakeCswap) -> None:
    cswap.publish(listing(unusable(1), unusable(2, status="disabled")))
    sleep = Sleeps()
    with pytest.raises(Fail) as exc:
        choose(sleep=sleep, now=fixed_now)
    assert str(exc.value) == "no usable cswap account"
    assert sleep.durations == []


def test_read_with_cswap_missing_from_path_raises_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(Fail) as exc:
        read()
    assert str(exc.value) == "cswap not found on PATH"


def test_read_with_nonzero_exit_raises_fail_without_cswap_output(cswap: FakeCswap, monkeypatch: pytest.MonkeyPatch) -> None:
    cswap.publish("SYNTHETIC-MARKER")
    monkeypatch.setenv("FAKE_CSWAP_EXIT", "3")
    with pytest.raises(Fail) as exc:
        choose(sleep=Sleeps(), now=fixed_now)
    assert str(exc.value) == "cswap list --json exited 3"
    assert "SYNTHETIC-MARKER" not in str(exc.value)


@pytest.mark.parametrize(
    "output",
    [
        "not json SYNTHETIC-MARKER",
        {"accounts": 5, "marker": "SYNTHETIC-MARKER"},
        {"accounts": [{"number": 1, "usageStatus": "ok", "marker": "SYNTHETIC-MARKER"}]},
    ],
)
def test_read_with_unparseable_output_raises_fail_without_cswap_output(cswap: FakeCswap, output: object) -> None:
    cswap.publish(output)
    with pytest.raises(Fail) as exc:
        choose(sleep=Sleeps(), now=fixed_now)
    assert str(exc.value) == "cswap list --json printed output ffrd cannot read"
    assert "SYNTHETIC-MARKER" not in str(exc.value)


def document(**fields: object) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "result": RESULT_TEXT,
        **fields,
    }


def test_call_runs_cswap_run_with_exact_claude_argv_and_stdin(cswap: FakeCswap) -> None:
    cswap.answer(3, document(structured_output=MADE_UP))
    assert call(3, OPUS, PARTS, SCHEMA) == MADE_UP
    assert cswap.claude_calls() == [
        {
            "account": 3,
            "argv": [
                "-p",
                "--model",
                "opus",
                "--output-format",
                "json",
                "--json-schema",
                SCHEMA_JSON,
                "--no-session-persistence",
            ],
            "stdin": "SYNTHETIC-PROMPT first part\n\nSYNTHETIC-PROMPT second part",
        }
    ]


def test_call_with_api_rate_limit_is_rate_limited(cswap: FakeCswap) -> None:
    cswap.answer(1, RATE_LIMIT)
    assert call(1, OPUS, PARTS, SCHEMA) == RateLimited("api_error_status 429")


def test_call_with_structured_output_retries_exhausted_is_rejected(cswap: FakeCswap) -> None:
    cswap.answer(
        1,
        document(is_error=True, subtype="error_max_structured_output_retries", api_error_status=None),
    )
    assert call(1, OPUS, PARTS, SCHEMA) == Rejected("subtype error_max_structured_output_retries")


def test_call_with_other_error_is_unfinished(cswap: FakeCswap) -> None:
    cswap.answer(1, document(is_error=True, subtype="error_max_turns", api_error_status=None))
    assert call(1, OPUS, PARTS, SCHEMA) == Unfinished("subtype error_max_turns")


@pytest.mark.parametrize(
    "fields",
    [{}, {"structured_output": ["SYNTHETIC-MARKER"]}, {"structured_output": "SYNTHETIC-MARKER"}],
)
def test_call_with_success_lacking_structured_object_is_rejected(cswap: FakeCswap, fields: dict[str, object]) -> None:
    cswap.answer(1, document(**fields))
    assert call(1, OPUS, PARTS, SCHEMA) == Rejected("structured_output is not a JSON object")


def test_call_past_timeout_is_unfinished(cswap: FakeCswap) -> None:
    cswap.answer(1, document(structured_output=MADE_UP), seconds=30)
    assert call(1, OPUS, PARTS, SCHEMA, timeout=0.5) == Unfinished("timeout after 0.5 seconds")


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        ("not json SYNTHETIC-MARKER", 0),
        ({"type": "assistant", "result": RESULT_TEXT}, 3),
        (["SYNTHETIC-MARKER"], 0),
    ],
)
def test_call_with_stdout_that_is_not_a_result_object_is_unfinished(cswap: FakeCswap, stdout: object, code: int) -> None:
    cswap.answer(1, stdout, code=code)
    assert call(1, OPUS, PARTS, SCHEMA) == Unfinished(f"exit {code}, stdout was not a result object")


def test_call_with_cswap_missing_from_path_raises_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(Fail) as exc:
        call(1, OPUS, PARTS, SCHEMA)
    assert str(exc.value) == "cswap not found on PATH"


def test_ask_returns_first_answer_without_reading_usage_when_not_rate_limited(cswap: FakeCswap) -> None:
    cswap.publish(listing(usable(1, 10.0)))
    cswap.answer(1, document(structured_output=MADE_UP))
    sleep = Sleeps()
    assert ask(1, OPUS, PARTS, SCHEMA, sleep=sleep) == (1, MADE_UP)
    assert [call_["account"] for call_ in cswap.claude_calls()] == [1]
    assert cswap.calls() == []
    assert sleep.durations == []


def test_ask_on_rate_limit_rereads_usage_and_retries_past_accounts_already_limited(cswap: FakeCswap) -> None:
    cswap.publish(listing(usable(1, 10.0), usable(2, 20.0), usable(3, 30.0)))
    cswap.answer(1, RATE_LIMIT)
    cswap.answer(2, RATE_LIMIT)
    cswap.answer(3, document(structured_output=MADE_UP))
    sleep = Sleeps()
    assert ask(1, OPUS, PARTS, SCHEMA, sleep=sleep) == (3, MADE_UP)
    assert [call_["account"] for call_ in cswap.claude_calls()] == [1, 2, 3]
    assert cswap.calls() == ["list --json", "list --json"]
    assert sleep.durations == []


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (document(is_error=True, subtype="error_max_turns", api_error_status=None), Unfinished("subtype error_max_turns")),
        (
            document(is_error=True, subtype="error_max_structured_output_retries", api_error_status=None),
            Rejected("subtype error_max_structured_output_retries"),
        ),
    ],
)
def test_ask_returns_unfinished_or_rejected_without_retrying(
    cswap: FakeCswap, answer: dict[str, object], expected: Unfinished | Rejected
) -> None:
    cswap.answer(1, answer)
    assert ask(1, OPUS, PARTS, SCHEMA, sleep=Sleeps()) == (1, expected)
    assert [call_["account"] for call_ in cswap.claude_calls()] == [1]
    assert cswap.calls() == []
