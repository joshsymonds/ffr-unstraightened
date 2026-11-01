import argparse
import io
import json
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from ffrd.game import Fail
from ffrd.voice import remote, render
from ffrd.voice.render import PROMPT, Renderer, prompt

# Made-up strings. Not game text.
SPEAKER = "Synthetic Speaker"
TEXT = "Synthetic line {x} with braces."
REPLY = "Synthetic reply."
SECRETS = (SPEAKER, TEXT, REPLY)


@dataclass
class Hit:
    method: str
    path: str
    content_type: str | None
    body: bytes


@dataclass
class Stub:
    base_url: str = ""
    status: int = 200
    body: bytes = b""
    hits: list[Hit] = field(default_factory=list)


@contextmanager
def _serve() -> Iterator[Stub]:
    stub = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            stub.hits.append(Hit("POST", self.path, self.headers.get("Content-Type"), self.rfile.read(length)))
            self.send_response(stub.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(stub.body)))
            self.end_headers()
            self.wfile.write(stub.body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def stub() -> Iterator[Stub]:
    with _serve() as s:
        yield s


def _reply(content: str) -> bytes:
    return json.dumps({"content": content}).encode()


def _closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_prompt_template_is_frozen() -> None:
    assert PROMPT == "Rewrite the line below as {speaker} says it in FINAL FANTASY RESONANCE, keeping its meaning.\n\n{text}\n\n"
    assert prompt(SPEAKER, TEXT) == (
        f"Rewrite the line below as {SPEAKER} says it in FINAL FANTASY RESONANCE, keeping its meaning.\n\n{TEXT}\n\n"
    )


def test_prompt_keeps_braces_in_speaker_and_text_literal() -> None:
    assert prompt("Speaker {0}", "{speaker}") == (
        "Rewrite the line below as Speaker {0} says it in FINAL FANTASY RESONANCE, keeping its meaning.\n\n{speaker}\n\n"
    )


def test_request_path_body_and_headers(stub: Stub) -> None:
    stub.body = _reply(REPLY)
    Renderer(stub.base_url).render(SPEAKER, TEXT)
    assert len(stub.hits) == 1
    hit = stub.hits[0]
    assert hit.method == "POST"
    assert hit.path == "/completion"
    assert hit.content_type == "application/json"
    assert json.loads(hit.body) == {"prompt": prompt(SPEAKER, TEXT), "temperature": 0.2, "n_predict": 256}


def test_render_returns_stripped_content(stub: Stub) -> None:
    stub.body = _reply(f"  \n{REPLY}\n\t ")
    assert Renderer(stub.base_url).render(SPEAKER, TEXT) == REPLY


def test_render_is_the_renderer_protocol(stub: Stub) -> None:
    stub.body = _reply(REPLY)
    protocol: Callable[[str, str], str] = Renderer(stub.base_url).render
    assert protocol(SPEAKER, TEXT) == REPLY


def test_http_error_raises_fail_without_content(stub: Stub) -> None:
    stub.status = 500
    stub.body = _reply(REPLY)
    with pytest.raises(Fail) as exc:
        Renderer(stub.base_url).render(SPEAKER, TEXT)
    assert str(exc.value) == "llama-server returned HTTP 500"
    for secret in SECRETS:
        assert secret not in str(exc.value)


def test_connection_refused_raises_fail_with_exception_name() -> None:
    with pytest.raises(Fail) as exc:
        Renderer(_closed_port_url()).render(SPEAKER, TEXT)
    assert str(exc.value) == "llama-server request failed: URLError"
    for secret in SECRETS:
        assert secret not in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        f"not json {REPLY}".encode(),
        b"[]",
        b"{}",
        json.dumps({"content": 3}).encode(),
        json.dumps({"content": None}).encode(),
    ],
)
def test_malformed_reply_raises_fail_without_content(stub: Stub, body: bytes) -> None:
    stub.body = body
    with pytest.raises(Fail) as exc:
        Renderer(stub.base_url).render(SPEAKER, TEXT)
    assert str(exc.value) == "llama-server returned a malformed completion"
    for secret in SECRETS:
        assert secret not in str(exc.value)


def test_proxy_environment_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    with _serve() as direct, _serve() as proxy:
        direct.body = _reply(REPLY)
        proxy.body = _reply("Synthetic proxied reply.")
        monkeypatch.setenv("http_proxy", proxy.base_url)
        monkeypatch.setenv("HTTP_PROXY", proxy.base_url)
        assert Renderer(direct.base_url).render(SPEAKER, TEXT) == REPLY
    assert len(direct.hits) == 1
    assert proxy.hits == []


def test_module_constants() -> None:
    assert render.N_PREDICT == 256
    assert render.TEMPERATURE == 0.2
    assert render.TIMEOUT == 300.0


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    render.register(parser.add_subparsers(required=True))
    return parser.parse_args(argv)


@pytest.fixture
def tunnel(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    # Replaces remote.tunnel with a stub that yields the stub llama-server's port and logs open and close.
    log: list[tuple[object, ...]] = []
    local = int(stub.base_url.rsplit(":", 1)[1])

    @contextmanager
    def fake_tunnel(remote_port: int) -> Iterator[int]:
        log.append(("open", remote_port))
        try:
            yield local
        finally:
            log.append(("close", remote_port))

    monkeypatch.setattr(remote, "tunnel", fake_tunnel)
    return log


def test_default_port_is_llama_server_port() -> None:
    assert render.PORT == 8737


def test_rendering_yields_renderer_reaching_completion_through_tunnel(stub: Stub, tunnel: list[tuple[object, ...]]) -> None:
    stub.body = _reply(REPLY)
    with render.rendering() as render_line:
        assert render_line(SPEAKER, TEXT) == REPLY
    assert tunnel == [("open", 8737), ("close", 8737)]
    assert [(hit.method, hit.path) for hit in stub.hits] == [("POST", "/completion")]
    assert json.loads(stub.hits[0].body)["prompt"] == prompt(SPEAKER, TEXT)


def test_rendering_passes_port_to_tunnel(stub: Stub, tunnel: list[tuple[object, ...]]) -> None:
    stub.body = _reply(REPLY)
    with render.rendering(9001):
        pass
    assert tunnel == [("open", 9001), ("close", 9001)]


def test_rendering_closes_tunnel_when_body_raises(tunnel: list[tuple[object, ...]]) -> None:
    with pytest.raises(RuntimeError, match="synthetic body failure"), render.rendering():
        raise RuntimeError("synthetic body failure")
    assert tunnel == [("open", 8737), ("close", 8737)]


def test_render_command_prints_stripped_reply_for_stdin_text(
    stub: Stub, tunnel: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.body = _reply(f"  {REPLY}\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{TEXT}\n"))
    args = parse(["render", "--speaker", SPEAKER])
    args.fn(args)
    assert capsys.readouterr().out == f"{REPLY}\n"
    assert len(stub.hits) == 1
    assert json.loads(stub.hits[0].body)["prompt"] == prompt(SPEAKER, TEXT)
    assert tunnel == [("open", 8737), ("close", 8737)]


@pytest.mark.parametrize(
    ("stdin", "expected"),
    [
        pytest.param(TEXT, TEXT, id="no-newline"),
        pytest.param(f"{TEXT}\n", TEXT, id="one-newline"),
        pytest.param(f"{TEXT}\n\n", f"{TEXT}\n", id="two-newlines"),
    ],
)
def test_render_command_strips_exactly_one_trailing_newline(
    stub: Stub, tunnel: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch, stdin: str, expected: str
) -> None:
    stub.body = _reply(REPLY)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    args = parse(["render", "--speaker", SPEAKER])
    args.fn(args)
    assert json.loads(stub.hits[0].body)["prompt"] == prompt(SPEAKER, expected)


def test_render_command_port_flag_reaches_tunnel(
    stub: Stub, tunnel: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.body = _reply(REPLY)
    monkeypatch.setattr(sys, "stdin", io.StringIO(TEXT))
    args = parse(["render", "--speaker", SPEAKER, "--port", "9001"])
    args.fn(args)
    assert tunnel == [("open", 9001), ("close", 9001)]


def test_render_command_requires_speaker() -> None:
    with pytest.raises(SystemExit):
        parse(["render"])
