import http.client
import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any, Self

import pytest

from ffrd.game import Fail
from ffrd.pipeline import jev
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.lens import TriageQuestion

ROOT = Path(__file__).resolve().parents[2]

# Made-up values for the scene, questions, key and echoed response text. Not game text.
SCENE = "Made-up scene about a harbor at dusk. Not game text."
KEY = "synthetic-key-0001"
ECHO = "Made-up message echoed by the service. Not game text."
QUESTIONS = (
    TriageQuestion("made-up-one", "Made-up question one. Not game text."),
    TriageQuestion("made-up-two", "Made-up question two. Not game text."),
)


class FakeResponse:
    """A 200 response whose body is fixed bytes."""

    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class BrokenResponse(FakeResponse):
    """A 200 response whose body is cut short while it is read."""

    def __init__(self) -> None:
        super().__init__(b"")

    def read(self) -> bytes:
        raise http.client.IncompleteRead(b"synthetic partial body")


def _ok(answers: dict[str, object]) -> FakeResponse:
    document = {
        "model": "jev-latest",
        "answers": {question_id: {"type": "noul", "noul": value} for question_id, value in answers.items()},
        "usage": {"input_tokens": 1},
    }
    return FakeResponse(json.dumps(document).encode("utf-8"))


def _http_error(status: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://fake.invalid/v1/systemone", status, "synthetic", Message(), io.BytesIO(body))


def _sent_body(request: urllib.request.Request) -> Any:
    data = request.data
    assert isinstance(data, bytes)
    return json.loads(data)


def _fake_http(monkeypatch: pytest.MonkeyPatch, outcome: BaseException | FakeResponse) -> list[tuple[urllib.request.Request, float]]:
    """Replace urlopen with a fake that records each request and its timeout, then returns or raises the outcome."""
    sent: list[tuple[urllib.request.Request, float]] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0.0) -> FakeResponse:
        sent.append((request, timeout))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_MODEL", raising=False)
    return monkeypatch


def test_request_carries_default_url_bearer_key_model_scene_and_one_noul_question_per_id(env: pytest.MonkeyPatch) -> None:
    sent = _fake_http(env, _ok({"made-up-one": 0.25, "made-up-two": 0.75}))

    jev.triage(SCENE, QUESTIONS)

    assert len(sent) == 1
    request, timeout = sent[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert request.full_url == "https://api.typesafe.ai/v1/systemone"
    assert request.get_method() == "POST"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert headers["content-type"] == "application/json"
    assert timeout == 120.0
    assert _sent_body(request) == {
        "model": "jev-latest",
        "state": SCENE,
        "questions": {
            "made-up-one": {"type": "noul", "instructions": "Made-up question one. Not game text."},
            "made-up-two": {"type": "noul", "instructions": "Made-up question two. Not game text."},
        },
    }


def test_overrides_replace_default_url_and_model(env: pytest.MonkeyPatch) -> None:
    env.setenv("TYPESAFE_BASE_URL", "https://fake.invalid/base")
    env.setenv("TYPESAFE_MODEL", "made-up-model")
    sent = _fake_http(env, _ok({"made-up-one": 0.5, "made-up-two": 0.5}))

    jev.triage(SCENE, QUESTIONS)

    request, _ = sent[0]
    assert request.full_url == "https://fake.invalid/base/v1/systemone"
    assert _sent_body(request)["model"] == "made-up-model"


def test_200_returns_exactly_the_asked_ids_with_their_probabilities(env: pytest.MonkeyPatch) -> None:
    _fake_http(env, _ok({"made-up-one": 0.25, "made-up-two": 0.75, "made-up-extra": 0.5}))

    assert jev.triage(SCENE, QUESTIONS) == {"made-up-one": 0.25, "made-up-two": 0.75}


def test_probabilities_at_the_bounds_zero_and_one_are_accepted(env: pytest.MonkeyPatch) -> None:
    _fake_http(env, _ok({"made-up-one": 0, "made-up-two": 1}))

    assert jev.triage(SCENE, QUESTIONS) == {"made-up-one": 0.0, "made-up-two": 1.0}


@pytest.mark.parametrize(
    ("make_outcome", "expected"),
    [
        pytest.param(lambda: _http_error(429), RateLimited, id="429"),
        pytest.param(lambda: _http_error(500), Unfinished, id="500"),
        pytest.param(lambda: _http_error(503), Unfinished, id="503"),
        pytest.param(lambda: urllib.error.URLError("synthetic refused"), Unfinished, id="connection-error"),
        pytest.param(lambda: TimeoutError("synthetic timeout"), Unfinished, id="timeout"),
        pytest.param(lambda: FakeResponse(b"Made-up body that is not JSON. Not game text."), Unfinished, id="200-not-json"),
        pytest.param(lambda: BrokenResponse(), Unfinished, id="200-truncated"),
        pytest.param(lambda: _http_error(400), Rejected, id="400"),
        pytest.param(lambda: _http_error(422), Rejected, id="422"),
        pytest.param(lambda: _ok({"made-up-one": 0.5}), Rejected, id="200-missing-id"),
        pytest.param(lambda: _ok({"made-up-one": "0.5", "made-up-two": 0.5}), Rejected, id="200-string-probability"),
        pytest.param(lambda: _ok({"made-up-one": 1.5, "made-up-two": 0.5}), Rejected, id="200-above-one"),
        pytest.param(lambda: _ok({"made-up-one": -0.1, "made-up-two": 0.5}), Rejected, id="200-below-zero"),
        pytest.param(lambda: _ok({"made-up-one": True, "made-up-two": 0.5}), Rejected, id="200-boolean"),
        pytest.param(lambda: _ok({"made-up-one": None, "made-up-two": 0.5}), Rejected, id="200-null"),
        pytest.param(lambda: _ok({"made-up-one": float("nan"), "made-up-two": 0.5}), Rejected, id="200-nan"),
    ],
)
def test_failure_outcomes_map_to_their_failure_type(
    env: pytest.MonkeyPatch, make_outcome: Callable[[], BaseException | FakeResponse], expected: type[Any]
) -> None:
    _fake_http(env, make_outcome())

    assert type(jev.triage(SCENE, QUESTIONS)) is expected


@pytest.mark.parametrize("status", [401, 403])
def test_bad_key_status_raises_fail(env: pytest.MonkeyPatch, status: int) -> None:
    _fake_http(env, _http_error(status, b'{"detail": {"error_type": "authentication_error"}}'))

    with pytest.raises(Fail):
        jev.triage(SCENE, QUESTIONS)


def test_missing_key_raises_fail_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    sent = _fake_http(monkeypatch, _ok({"made-up-one": 0.5, "made-up-two": 0.5}))

    with pytest.raises(Fail):
        jev.triage(SCENE, QUESTIONS)
    assert sent == []


@pytest.mark.parametrize(
    "make_outcome",
    [
        pytest.param(
            lambda: _http_error(400, json.dumps({"detail": ECHO}).encode("utf-8")),
            id="400",
        ),
        pytest.param(
            lambda: _http_error(422, json.dumps({"detail": [{"msg": ECHO, "input": SCENE, "key": KEY}]}).encode("utf-8")),
            id="422-echoes-input",
        ),
        pytest.param(lambda: _http_error(429, json.dumps({"detail": ECHO}).encode("utf-8")), id="429"),
        pytest.param(lambda: _http_error(500, json.dumps({"detail": ECHO}).encode("utf-8")), id="500"),
        pytest.param(
            lambda: _http_error(403, json.dumps({"detail": {"error_type": "authentication_error", "message": ECHO}}).encode("utf-8")),
            id="403",
        ),
        pytest.param(lambda: FakeResponse(f"{SCENE} {ECHO} {KEY}".encode()), id="200-not-json-echoes-all"),
        pytest.param(
            lambda: FakeResponse(json.dumps({"answers": {"made-up-one": {"noul": 0.5, "message": ECHO, "state": SCENE}}}).encode("utf-8")),
            id="200-missing-id-echoes-all",
        ),
    ],
)
def test_failure_text_never_carries_the_key_scene_or_response_message(
    env: pytest.MonkeyPatch, make_outcome: Callable[[], BaseException | FakeResponse]
) -> None:
    _fake_http(env, make_outcome())

    try:
        result = jev.triage(SCENE, QUESTIONS)
    except Fail as error:
        text = str(error)
    else:
        text = repr(result)
    assert KEY not in text
    assert SCENE not in text
    assert ECHO not in text


def test_envrc_loads_dotenv_local_after_the_devenv_block() -> None:
    lines = (ROOT / ".envrc").read_text(encoding="utf-8").splitlines()

    assert "dotenv_if_exists .env.local" in lines
    assert lines.index("dotenv_if_exists .env.local") > lines.index("fi")
