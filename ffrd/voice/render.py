"""Render prompt shared by LoRA training and serving, and the llama-server client that voices a line."""

import argparse
import http.client
import json
import sys
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.error import HTTPError

from ffrd.game import Fail
from ffrd.voice import remote

PROMPT = "Rewrite the line below as {speaker} says it in FINAL FANTASY RESONANCE, keeping its meaning.\n\n{text}\n\n"
N_PREDICT = 256
TEMPERATURE = 0.2
TIMEOUT = 300.0
PORT = 8737


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


@contextmanager
def rendering(port: int = PORT) -> Iterator[Callable[[str, str], str]]:
    with remote.tunnel(port) as local:
        yield Renderer(f"http://127.0.0.1:{local}").render


def cmd_render(args: argparse.Namespace) -> None:
    text = sys.stdin.read()
    if text.endswith("\n"):
        text = text[:-1]
    with rendering(args.port) as render_line:
        line = render_line(args.speaker, text)
    print(line)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    render_p = subparsers.add_parser("render", help="voice a line read from stdin through the GPU host's llama-server")
    render_p.add_argument("--speaker", required=True, help="speaker name for the prompt")
    render_p.add_argument("--port", type=int, default=PORT, help="llama-server port on the GPU host's 127.0.0.1")
    render_p.set_defaults(fn=cmd_render)
