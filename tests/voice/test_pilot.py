"""pilot: the loopback telemetry receiver, the telemetry environment around pairs.run, the step timing of a training, and the
exact lines `ffrd voice pilot` prints, over made-up scenes, lines, accounts and OTLP records. The pairs run, both trainings,
the account reads and the scene load are replaced, so nothing here calls claude, cswap or ssh.

Made-up scene, namespace, key, speaker, text and email values. Not game text and not an account.
"""

import argparse
import http.client
import json
import os
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from ffrd import scenes
from ffrd.game import Fail
from ffrd.pipeline import accounts
from ffrd.scenes import SceneLine
from ffrd.voice import classifier, lora, pairs, pilot
from ffrd.voice.pilot import Call, Receiver

SPEAKER = "SENTINEL-SPEAKER"
TEXT = "SENTINEL-TEXT"
EMAIL = "SENTINEL-EMAIL"
TELEMETRY_NAMES = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_LOGS_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_LOGS_EXPORT_INTERVAL",
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _line(scene: str, key: str, order: int = 0) -> SceneLine:
    return SceneLine(scene=scene, order=order, namespace="ST_Syn_006_01", key=key, speaker=SPEAKER, voice=None)


def _lines() -> list[SceneLine]:
    """Four made-up scenes of two lines each."""
    return [_line(f"made-up-scene-{i:02d}", str(n), order=n) for i in range(4) for n in range(2)]


def _texts(lines: Sequence[SceneLine]) -> dict[tuple[str, str], str]:
    return {(line.namespace, line.key): f"Made-up {TEXT} of {line.scene}. Not game text." for line in lines}


def _record(event: str, **attributes: Any) -> dict[str, Any]:
    named = [{"key": "event.name", "value": {"stringValue": event}}]
    named += [{"key": key, "value": value} for key, value in attributes.items()]
    return {"attributes": named}


def _api_request(
    model: str = "made-up-model-a",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 300,
    cache_creation: int = 40,
    duration_ms: float = 1500.0,
) -> dict[str, Any]:
    return _record(
        "api_request",
        model={"stringValue": model},
        input_tokens={"intValue": str(input_tokens)},
        output_tokens={"intValue": output_tokens},
        cache_read_tokens={"intValue": str(cache_read)},
        cache_creation_tokens={"intValue": cache_creation},
        duration_ms={"doubleValue": duration_ms},
    )


def _logs(*records: dict[str, Any]) -> bytes:
    body = {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}
    return json.dumps(body).encode("utf-8")


def _post(port: int, path: str, body: bytes, host: str = "127.0.0.1") -> int:
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _post_to_telemetry_endpoint(body: bytes) -> None:
    endpoint = urllib.parse.urlsplit(os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
    assert endpoint.hostname is not None and endpoint.port is not None
    assert _post(endpoint.port, "/v1/logs", body, host=endpoint.hostname) == 200


def _account(number: int, weekly_pct: float) -> accounts.Account:
    return accounts.Account(number=number, weekly_pct=weekly_pct, resets=())


class Fakes:
    """Replaces the pairs run, both trainings, the account reads, the scene load and the work directory. The fake pairs run
    posts `bodies` to the telemetry receiver the environment names. Each fake training advances the clock by its step gaps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        self.clock = Clock()
        self.bodies: list[bytes] = [
            _logs(
                _api_request(),
                _record("tool_result", **{"user.email": {"stringValue": EMAIL}, "prompt": {"stringValue": TEXT}}),
            ),
            _logs(
                _api_request(model="made-up-model-b", input_tokens=7, output_tokens=3, cache_read=0, cache_creation=0, duration_ms=2500.0)
            ),
        ]
        self.report = ["calls 3", "resumed 1", "failed 1", "train pairs 4", "heldout pairs 0", "account 3 calls 3"]
        self.reads = iter([[_account(3, 10.0)], [_account(3, 12.5), _account(7, 40.0)]])
        self.classifier_staged: str | None = "staged 6 classifier examples"
        self.lora_staged: str | None = "staged 4 lora records"
        self.trained: list[str] = []
        self.pairs_limit: int | None = None
        self.env_during: dict[str, str | None] = {}
        self.observe: pytest.CaptureFixture[str] | None = None
        self.seen_at_training_start: list[list[str]] = []
        lines = _lines()
        monkeypatch.setattr(scenes, "load", lambda: lines)
        monkeypatch.setattr(pairs, "english", lambda: _texts(lines))
        monkeypatch.setattr(pilot, "work_dir", lambda lens: root / lens)
        monkeypatch.setattr(accounts, "read", lambda: next(self.reads))
        monkeypatch.setattr(pairs, "run", self._pairs_run)
        monkeypatch.setattr(classifier, "train", self._training("classifier", [5.0, 2.0, 2.0], 1.0, lambda: self.classifier_staged))
        monkeypatch.setattr(lora, "train", self._training("lora", [3.0], 1.0, lambda: self.lora_staged))

    def _pairs_run(
        self,
        lines: Sequence[SceneLine],
        texts: dict[tuple[str, str], str],
        root: Path,
        *,
        limit: int | None = None,
        workers: int = pairs.WORKERS,
    ) -> list[str]:
        self.pairs_limit = limit
        self.env_during = {name: os.environ.get(name) for name in TELEMETRY_NAMES}
        for body in self.bodies:
            _post_to_telemetry_endpoint(body)
        self.clock.now += 40.0
        return list(self.report)

    def _training(self, name: str, gaps: list[float], tail: float, staged: Callable[[], str | None]) -> Callable[[], None]:
        def train() -> None:
            if self.observe is not None:
                self.seen_at_training_start.append(self.observe.readouterr().out.splitlines())
            self.trained.append(name)
            for step, gap in enumerate(gaps, start=1):
                self.clock.now += gap
                print(f"0\t{step}\t0.693147")
            line = staged()
            if line is not None:
                print(line)
            self.clock.now += tail

        return train


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    return Fakes(monkeypatch, tmp_path / "work")


# Receiver: the loopback OTLP/HTTP receiver keeps api_request numbers and model names only.


def test_receiver_listens_on_loopback_only() -> None:
    with Receiver() as receiver:
        assert receiver._server.server_address == ("127.0.0.1", receiver.port)
        assert receiver.port != 0


def test_receiver_keeps_api_request_numbers_and_model_names_and_drops_everything_else() -> None:
    other = _record("tool_result", **{"user.email": {"stringValue": EMAIL}, "prompt": {"stringValue": TEXT}})
    with Receiver() as receiver:
        assert _post(receiver.port, "/v1/logs", _logs(_api_request(), other)) == 200
        assert _post(receiver.port, "/v1/metrics", b'{"resourceMetrics": []}') == 200
        assert _post(receiver.port, "/v1/logs", _logs(_api_request(model="made-up-model-b", input_tokens=7))) == 200
    assert receiver.malformed == 0
    assert receiver.calls == [
        Call(
            model="made-up-model-a", input_tokens=100, output_tokens=20, cache_read_tokens=300, cache_creation_tokens=40, duration_ms=1500.0
        ),
        Call(
            model="made-up-model-b", input_tokens=7, output_tokens=20, cache_read_tokens=300, cache_creation_tokens=40, duration_ms=1500.0
        ),
    ]
    assert EMAIL not in repr(receiver.calls)
    assert TEXT not in repr(receiver.calls)


def test_other_events_are_discarded_whatever_the_type_of_their_attributes() -> None:
    other = _record("tool_result", success={"boolValue": True}, tools={"arrayValue": {"values": []}})
    with Receiver() as receiver:
        assert _post(receiver.port, "/v1/logs", _logs(other, _api_request())) == 200
    assert receiver.malformed == 0
    assert [call.model for call in receiver.calls] == ["made-up-model-a"]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        _logs(_record("api_request", input_tokens={"intValue": "5"})),
        _logs(_record("api_request", model={"stringValue": "made-up-model-a"}, input_tokens={"intValue": "many"})),
    ],
)
def test_receiver_counts_an_unreadable_api_request_as_malformed_and_keeps_no_record(body: bytes) -> None:
    with Receiver() as receiver:
        assert _post(receiver.port, "/v1/logs", body) == 200
    assert receiver.calls == []
    assert receiver.malformed == 1


def test_receiver_refuses_an_unknown_path() -> None:
    with Receiver() as receiver:
        assert _post(receiver.port, "/v1/traces", b"{}") == 404


# Environment: telemetry is on while pairs.run runs and restored after.


def test_telemetry_is_on_while_pairs_runs_and_the_environment_is_restored_after(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "console")
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    pilot.measure(2, clock=fakes.clock)
    assert fakes.env_during["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert fakes.env_during["OTEL_LOGS_EXPORTER"] == "otlp"
    assert fakes.env_during["OTEL_METRICS_EXPORTER"] == "otlp"
    assert fakes.env_during["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
    assert fakes.env_during["OTEL_LOGS_EXPORT_INTERVAL"] == "500"
    endpoint = fakes.env_during["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert endpoint is not None and endpoint.startswith("http://127.0.0.1:")
    assert os.environ["OTEL_LOGS_EXPORTER"] == "console"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in os.environ
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ


def test_telemetry_environment_is_restored_when_pairs_run_fails(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: Any, **kwargs: Any) -> list[str]:
        raise Fail("made-up pairs failure")

    monkeypatch.setattr(pairs, "run", failing)
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_TELEMETRY", raising=False)
    with pytest.raises(Fail, match="made-up pairs failure"):
        pilot.measure(2, clock=fakes.clock)
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in os.environ


# Measurement: the first N scenes, the step timing and the exact output lines.


def test_measure_runs_the_first_n_scenes_through_pairs(fakes: Fakes) -> None:
    pilot.measure(2, clock=fakes.clock)
    assert fakes.pairs_limit == 2


def test_measure_prints_the_exact_lines_in_order(fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    pilot.measure(2, clock=fakes.clock)
    assert capsys.readouterr().out.splitlines() == [
        "pairs scenes 2",
        "pairs lines 4",
        "pairs calls 3",
        "pairs resumed 1",
        "pairs failed 1",
        "pairs wall_s 40.00",
        "pairs api_requests 2",
        "pairs api_unreadable 0",
        "pairs api_s_mean 2.00",
        "pairs model made-up-model-a requests 1 input 100 output 20 cache_read 300 cache_creation 40",
        "pairs model made-up-model-b requests 1 input 7 output 3 cache_read 0 cache_creation 0",
        "pairs account 3 weekly_pct 10.00 12.50",
        "pairs account 7 weekly_pct n/a 40.00",
        "classifier examples 6",
        "classifier steps 3",
        "classifier startup_s 5.00",
        "classifier s_per_step 2.00",
        "classifier wall_s 10.00",
        "lora records 4",
        "lora steps 1",
        "lora startup_s 3.00",
        "lora s_per_step n/a",
        "lora wall_s 4.00",
    ]


def test_training_output_is_not_passed_to_stdout(fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    pilot.measure(2, clock=fakes.clock)
    out = capsys.readouterr().out
    assert "0\t1\t" not in out
    assert "staged" not in out


def test_each_phase_prints_its_lines_when_it_ends(fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.observe = capsys
    pilot.measure(2, clock=fakes.clock)
    classifier_start, lora_start = fakes.seen_at_training_start
    assert classifier_start[-1] == "pairs account 7 weekly_pct n/a 40.00"
    assert lora_start[-1] == "classifier wall_s 10.00"


def test_a_training_with_one_step_has_no_seconds_per_step(fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    pilot.measure(2, clock=fakes.clock)
    assert "lora s_per_step n/a" in capsys.readouterr().out.splitlines()


def test_unreadable_telemetry_still_prints_the_pairs_lines_and_both_trainings_run(fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.bodies = [b"not json", _logs(_api_request())]
    pilot.measure(2, clock=fakes.clock)
    out = capsys.readouterr().out.splitlines()
    assert out[:8] == [
        "pairs scenes 2",
        "pairs lines 4",
        "pairs calls 3",
        "pairs resumed 1",
        "pairs failed 1",
        "pairs wall_s 40.00",
        "pairs api_requests 1",
        "pairs api_unreadable 1",
    ]
    assert "classifier examples 6" in out
    assert "lora records 4" in out
    assert fakes.trained == ["classifier", "lora"]


def test_a_training_that_prints_no_staged_count_fails(fakes: Fakes) -> None:
    fakes.classifier_staged = None
    with pytest.raises(Fail, match="classifier training printed no staged count"):
        pilot.measure(2, clock=fakes.clock)


# --scenes validation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ffrd")
    pilot.register(parser.add_subparsers(dest="command"))
    return parser


@pytest.mark.parametrize("value", ["0", "-3", "x", "1.5"])
def test_scenes_must_be_a_positive_integer(value: str) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["pilot", "--scenes", value])


def test_scenes_defaults_to_fifty_and_accepts_a_positive_integer() -> None:
    assert _parser().parse_args(["pilot"]).scenes == 50
    assert _parser().parse_args(["pilot", "--scenes", "7"]).scenes == 7
