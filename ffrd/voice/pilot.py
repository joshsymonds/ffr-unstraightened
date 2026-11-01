"""pilot: the measured pilot. It runs the first N scenes through pairs.run with Claude Code telemetry on, then the classifier and
LoRA trainings, and prints counts and timings only: call time and tokens from the telemetry, weekly usage per account before
and after the pairs run, and training steps with seconds per step.

The telemetry receiver binds 127.0.0.1 and keeps only the api_request numbers and model names. The output holds no speaker,
key, line, paraphrase or account email. A training's own output is read for its step lines and staged count, and is not
printed.
"""

import argparse
import contextlib
import json
import os
import re
import statistics
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

from ffrd import scenes
from ffrd.game import Fail
from ffrd.pipeline import accounts
from ffrd.pipeline.store import work_dir
from ffrd.voice import classifier, lora, pairs

DEFAULT_SCENES = 50
TELEMETRY = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_LOGS_EXPORT_INTERVAL": "500",
}
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
STEP = re.compile(r"\d+\t\d+\t\S+")
CLASSIFIER_STAGED = re.compile(r"staged (\d+) classifier examples")
LORA_STAGED = re.compile(r"staged (\d+) lora records")

Clock = Callable[[], float]


@dataclass(frozen=True)
class Call:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    duration_ms: float


API_REQUEST = {"stringValue": "api_request"}


def _value(value: Mapping[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    raise ValueError("attribute has no supported value")


def _call(raw: Mapping[str, Any]) -> Call:
    model = _value(raw["model"])
    if not isinstance(model, str):
        raise ValueError("model is not a string")
    return Call(
        model=model,
        input_tokens=int(_value(raw["input_tokens"])),
        output_tokens=int(_value(raw["output_tokens"])),
        cache_read_tokens=int(_value(raw["cache_read_tokens"])),
        cache_creation_tokens=int(_value(raw["cache_creation_tokens"])),
        duration_ms=float(_value(raw["duration_ms"])),
    )


def _read_logs(body: bytes) -> tuple[list[Call], int]:
    """The api_request calls in one OTLP/HTTP JSON logs body, and the count of api_request records that could not be read.
    Records of other events are skipped before their attribute values are read."""
    try:
        document = json.loads(body)
        records = [record for resource in document["resourceLogs"] for scope in resource["scopeLogs"] for record in scope["logRecords"]]
    except (ValueError, KeyError, TypeError):
        return [], 1
    calls: list[Call] = []
    malformed = 0
    for record in records:
        try:
            raw = {item["key"]: item["value"] for item in record["attributes"]}
            if raw.get("event.name") == API_REQUEST:
                calls.append(_call(raw))
        except (KeyError, TypeError, ValueError):
            malformed += 1
    return calls, malformed


class Receiver:
    """A loopback OTLP/HTTP receiver. It keeps the api_request calls, counts the records it cannot read, and drops every other
    record and attribute. It answers the logs and metrics paths and refuses any other."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.malformed = 0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def add_logs(self, body: bytes) -> None:
        calls, malformed = _read_logs(body)
        with self._lock:
            self.calls.extend(calls)
            self.malformed += malformed


def _handler(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path not in ("/v1/logs", "/v1/metrics"):
                self.send_error(404)
                return
            if self.path == "/v1/logs":
                receiver.add_logs(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: Any) -> None:
            """Request lines stay off stderr."""

    return Handler


@contextlib.contextmanager
def _telemetry(endpoint: str) -> Iterator[None]:
    values = {**TELEMETRY, ENDPOINT: endpoint}
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class _Watch:
    """Stands in for sys.stdout during a training. It keeps the clock time each step line is written and the staged count,
    and passes nothing on."""

    def __init__(self, clock: Clock, staged: re.Pattern[str]) -> None:
        self._clock = clock
        self._staged = staged
        self._partial = ""
        self.steps: list[float] = []
        self.count: int | None = None

    def write(self, text: str) -> int:
        *complete, self._partial = (self._partial + text).split("\n")
        for line in complete:
            self._line(line)
        return len(text)

    def flush(self) -> None:
        """Nothing is buffered outside this object."""

    def finish(self) -> None:
        if self._partial:
            self._line(self._partial)
            self._partial = ""

    def _line(self, line: str) -> None:
        if STEP.fullmatch(line):
            self.steps.append(self._clock())
        match = self._staged.fullmatch(line)
        if match is not None:
            self.count = int(match.group(1))


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _training(name: str, noun: str, staged: re.Pattern[str], train: Callable[[], None], clock: Clock) -> list[str]:
    watch = _Watch(clock, staged)
    started = clock()
    with contextlib.redirect_stdout(watch):
        train()
        watch.finish()
    ended = clock()
    if watch.count is None:
        raise Fail(f"{name} training printed no staged count")
    steps = watch.steps
    startup = steps[0] - started if steps else None
    per_step = (steps[-1] - steps[0]) / (len(steps) - 1) if len(steps) > 1 else None
    return [
        f"{name} {noun} {watch.count}",
        f"{name} steps {len(steps)}",
        f"{name} startup_s {_num(startup)}",
        f"{name} s_per_step {_num(per_step)}",
        f"{name} wall_s {_num(ended - started)}",
    ]


def _weekly(rows: Iterable[accounts.Account]) -> dict[int, float]:
    return {row.number: row.weekly_pct for row in rows}


def _reported(report: Sequence[str], name: str) -> int:
    for row in report:
        key, _, value = row.partition(" ")
        if key == name:
            return int(value)
    raise Fail(f"pairs reported no {name} count")


def _mean_seconds(calls: Sequence[Call]) -> float | None:
    return statistics.fmean(call.duration_ms for call in calls) / 1000 if calls else None


def _model_lines(calls: Sequence[Call]) -> list[str]:
    by_model: dict[str, list[Call]] = {}
    for call in calls:
        by_model.setdefault(call.model, []).append(call)
    return [
        f"pairs model {model} requests {len(rows)} input {sum(c.input_tokens for c in rows)} "
        f"output {sum(c.output_tokens for c in rows)} cache_read {sum(c.cache_read_tokens for c in rows)} "
        f"cache_creation {sum(c.cache_creation_tokens for c in rows)}"
        for model, rows in sorted(by_model.items())
    ]


def _account_lines(before: Mapping[int, float], after: Mapping[int, float]) -> list[str]:
    return [
        f"pairs account {number} weekly_pct {_num(before.get(number))} {_num(after.get(number))}"
        for number in sorted(before.keys() | after.keys())
    ]


def measure(count: int, clock: Clock = time.monotonic) -> None:
    lines = scenes.load()
    texts = pairs.english()
    root = work_dir("voice")
    first = list(dict.fromkeys(line.scene for line in lines))[:count]
    chosen = set(first)
    in_scenes = [line for line in lines if line.scene in chosen]
    before = _weekly(accounts.read())
    with Receiver() as receiver, _telemetry(receiver.endpoint):
        started = clock()
        report = pairs.run(lines, texts, root, limit=count)
        wall = clock() - started
    after = _weekly(accounts.read())
    for row in [
        f"pairs scenes {len(first)}",
        f"pairs lines {len(in_scenes)}",
        f"pairs calls {_reported(report, 'calls')}",
        f"pairs resumed {_reported(report, 'resumed')}",
        f"pairs failed {_reported(report, 'failed')}",
        f"pairs wall_s {_num(wall)}",
        f"pairs api_requests {len(receiver.calls)}",
        f"pairs api_unreadable {receiver.malformed}",
        f"pairs api_s_mean {_num(_mean_seconds(receiver.calls))}",
        *_model_lines(receiver.calls),
        *_account_lines(before, after),
    ]:
        print(row)
    for row in _training("classifier", "examples", CLASSIFIER_STAGED, classifier.train, clock):
        print(row)
    for row in _training("lora", "records", LORA_STAGED, lora.train, clock):
        print(row)


def cmd_pilot(args: argparse.Namespace) -> None:
    measure(args.scenes)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "pilot", help="run the first N scenes through pairs and both trainings, and print the measured calls, tokens, steps and timings"
    )
    p.add_argument("--scenes", type=pairs._positive, default=DEFAULT_SCENES, metavar="N", help="scenes to run through pairs (default 50)")
    p.set_defaults(fn=cmd_pilot)
