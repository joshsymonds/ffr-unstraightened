"""Render prompt shared by LoRA training and serving, and the llama-server client that voices a line."""

import http.client
import json
import urllib.request
from urllib.error import HTTPError

from ffrd.game import Fail

PROMPT = "Rewrite the line below as {speaker} says it in FINAL FANTASY RESONANCE, keeping its meaning.\n\n{text}\n\n"
N_PREDICT = 256
TEMPERATURE = 0.2
TIMEOUT = 300.0


def prompt(speaker: str, text: str) -> str:
    return PROMPT.format(speaker=speaker, text=text)


class Renderer:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def render(self, speaker: str, text: str) -> str:
        body = json.dumps({"prompt": prompt(speaker, text), "temperature": TEMPERATURE, "n_predict": N_PREDICT}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/completion",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT) as response:
                raw = response.read()
        except HTTPError as e:
            raise Fail(f"llama-server returned HTTP {e.code}") from None
        except (OSError, http.client.HTTPException) as e:
            raise Fail(f"llama-server request failed: {type(e).__name__}") from None
        try:
            document = json.loads(raw)
        except ValueError:
            raise Fail("llama-server returned a malformed completion") from None
        if not isinstance(document, dict) or not isinstance(document.get("content"), str):
            raise Fail("llama-server returned a malformed completion")
        return document["content"].strip()
