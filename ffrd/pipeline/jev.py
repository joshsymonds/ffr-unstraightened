"""Jev client: ask one scene's triage questions and return Jev's yes-probability for each question id."""

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence

from ffrd.game import Fail
from ffrd.pipeline.claude import RateLimited, Rejected, Unfinished
from ffrd.pipeline.lens import TriageQuestion

BASE_URL = "https://api.typesafe.ai"
MODEL = "jev-latest"


def triage(
    scene: str,
    questions: Sequence[TriageQuestion],
    *,
    timeout: float = 120.0,
) -> dict[str, float] | RateLimited | Unfinished | Rejected:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise Fail("TYPESAFE_API_KEY is not set")
    base_url = os.environ.get("TYPESAFE_BASE_URL") or BASE_URL
    model = os.environ.get("TYPESAFE_MODEL") or MODEL
    body = {
        "model": model,
        "state": scene,
        "questions": {question.id: {"type": "noul", "instructions": question.question} for question in questions},
    }
    request = urllib.request.Request(
        f"{base_url}/v1/systemone",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        return _status_failure(error.code)
    except (OSError, http.client.HTTPException) as error:
        return Unfinished(f"request failed: {type(error).__name__}")
    if status != 200:
        return _status_failure(status)
    return _answers(raw, questions)


def _status_failure(status: int) -> RateLimited | Unfinished | Rejected:
    match status:
        case 401 | 403:
            raise Fail(f"TypeSafe rejected the API key (HTTP {status})")
        case 429:
            return RateLimited(f"HTTP {status}")
        case 400 | 422:
            return Rejected(f"HTTP {status}")
        case _:
            return Unfinished(f"HTTP {status}")


def _answers(raw: bytes, questions: Sequence[TriageQuestion]) -> dict[str, float] | Unfinished | Rejected:
    try:
        document = json.loads(raw)
    except ValueError:
        return Unfinished("HTTP 200 body is not JSON")
    answers = document.get("answers") if isinstance(document, dict) else None
    if not isinstance(answers, dict):
        return Unfinished("HTTP 200 body has no answers object")
    probabilities: dict[str, float] = {}
    for question in questions:
        answer = answers.get(question.id)
        probability = _probability(answer.get("noul")) if isinstance(answer, dict) else None
        if probability is None:
            return Rejected(f"no probability between 0 and 1 for question id {question.id}")
        probabilities[question.id] = probability
    return probabilities


def _probability(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        return None
    return float(value)
