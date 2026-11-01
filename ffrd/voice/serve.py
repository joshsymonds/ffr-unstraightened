"""Start and stop llama-server on the GPU host's 127.0.0.1 with the exported voice model, detached, and wait until it answers /health through an ssh tunnel."""

import argparse
import http.client
import time
import urllib.request

from ffrd.game import Fail
from ffrd.voice import remote
from ffrd.voice.lora import GGUF
from ffrd.voice.render import PORT

PIDFILE = "serve.pid"
CONTEXT = 4096
SLOTS = 4
READY_TIMEOUT = 300.0
POLL_SECONDS = 2.0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def server_argv(port: int, gguf: str) -> list[str]:
    return ["llama-server", "-m", gguf, "--host", "127.0.0.1", "--port", str(port), "-c", str(CONTEXT), "-np", str(SLOTS), "-ngl", "999"]


def running() -> bool:
    try:
        remote.shell(f"test -f {PIDFILE} && kill -0 -- -$(cat {PIDFILE})")
    except Fail:
        return False
    return True


def _healthy(local: int) -> bool:
    try:
        with _OPENER.open(f"http://127.0.0.1:{local}/health", timeout=2.0) as response:
            return response.status == 200
    except (OSError, http.client.HTTPException):
        return False


def launch(port: int, gguf: str) -> None:
    # remote.shell joins its scratch-dir prefix with &&, which binds tighter than &, so the background job and the
    # pidfile write are grouped to run in the scratch dir. Output is discarded because llama-server can echo prompts.
    remote.shell(f"{{ setsid nohup {remote.env(server_argv(port, gguf))} > /dev/null 2>&1 < /dev/null & echo $! > {PIDFILE}; }}")


def start(port: int = PORT, gguf: str = GGUF) -> None:
    remote.sync()
    host = remote.host()
    if running():
        print(f"llama-server already running on {host}")
        return
    launch(port, gguf)
    with remote.tunnel(port) as local:
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            if _healthy(local):
                print(f"llama-server ready on {host} 127.0.0.1:{port}")
                return
            if not running():
                raise Fail(f"llama-server exited on {host} before it was ready")
            if time.monotonic() >= deadline:
                stop()
                raise Fail(f"llama-server was not ready on {host} within {READY_TIMEOUT:g} s")
            time.sleep(POLL_SECONDS)


def stop() -> None:
    remote.shell(f"if [ -f {PIDFILE} ]; then kill -TERM -- -$(cat {PIDFILE}) 2>/dev/null; rm -f {PIDFILE}; fi")
    print(f"llama-server stopped on {remote.host()}")


def cmd_serve(args: argparse.Namespace) -> None:
    if args.stop:
        stop()
    else:
        start(args.port)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    serve_p = subparsers.add_parser("serve", help="start or stop llama-server on the GPU host's 127.0.0.1")
    serve_p.add_argument("--stop", action="store_true", help="stop the running llama-server instead of starting one")
    serve_p.add_argument("--port", type=int, default=PORT, help="llama-server port on the GPU host's 127.0.0.1")
    serve_p.set_defaults(fn=cmd_serve)
