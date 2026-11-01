"""probe and usmap: run the game on the gnomon host under UE4SS.

`ffrd probe` checks whether FFR honours the built mod. It refuses while FFR runs on the Steam Deck or on the gnomon host,
when the gnomon build is not the vanilla build, and when a file of build/FFRS-Unstraightened_P is missing. It puts the
three build files on the host in Paks/~mods unchanged, beside a marker pak FFRS-UnstraightenedProbe_P: the full shape with
one edited line of PROBE_KEY, its .utoc and .ucas copied from the host's global container. It runs the probe mod and
passes only when the marker is honoured. Every file it put is removed in every outcome.
`--shape` probes one shape instead: target, full, partial or ue4ss. The ue4ss shape installs no pak: it runs the probe mod
with the repo's UE4SS text mod beside it, whose edits.txt holds the one probe edit. A launch whose log holds no
observation line for the probe key is inconclusive: the probe stops there and fails, without trying another shape.
It prints only keys and hashes, never the probe text.

`ffrd usmap` dumps the game's mappings with the usmap mod and saves them as Mappings.usmap in the vanilla folder.
"""

import argparse
import re
import secrets
import sys
import tempfile
from pathlib import Path

from pylocres import LocresFile

from ffrd import deck, gnomon
from ffrd.build import PAK_SHAPES, UE4SS_NAME, edits_text, pack, pak_files
from ffrd.game import MOD_NAME, Fail, repo_root, vanilla, vanilla_dir, vanilla_hash
from ffrd.install import EXTS

PROBE_KEY = ("ST_UI_TitleMenu", "UI_Title_Copyright")
PROBE_MOD = "FFRS-UnstraightenedProbe_P"
MOD_DIR = "ue4ss/UnstraightenedProbe"
LOG = "probe.log"
UE4SS_SHAPE = "ue4ss"
SHAPE_CHOICES = (*PAK_SHAPES, UE4SS_SHAPE)
TEXT_MOD_DIR = f"ue4ss/{UE4SS_NAME}"
USMAP_MOD = "ue4ss/UnstraightenedUsmap"
USMAP_LOG = "usmap.log"
USMAP_NAME = "Mappings.usmap"


def _probe_text() -> str:
    return f"Unstraightened probe {secrets.token_hex(4)}"


def _check_deck() -> None:
    host = deck.host()
    status = deck.ssh_status(host, f"pgrep -f '{gnomon.EXE_PATTERN}'")
    if status == 0:
        raise Fail(f"FINAL FANTASY RESONANCE is running on {host}; refusing to probe")
    if status != 1:
        print(f"note: could not check {host} for FFR (ssh status {status}); proceeding without that check", file=sys.stderr)


def _check_build() -> None:
    stamp = vanilla_dir() / "BUILDID"
    if not stamp.exists():
        raise Fail("no vanilla text yet; run `ffrd extract`")
    local = stamp.read_text().strip()
    remote = gnomon.build_id()
    if local != remote:
        raise Fail(f"gnomon on {gnomon.host()} runs build {remote}, not vanilla build {local}; run `ffrd extract` first")


def _build_files() -> list[Path]:
    files = [repo_root() / "build" / f"{MOD_NAME}.{ext}" for ext in EXTS]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise Fail(f"missing {missing}; run `ffrd build`")
    return files


def _fetch_globals() -> dict[str, bytes]:
    return {ext: gnomon.fetch(f"{gnomon.paks()}/global.{ext}") for ext in ("utoc", "ucas")}


def _pack(shape: str, en: LocresFile, text: str, out: Path, fetched: dict[str, bytes]) -> list[Path]:
    pak = pack(pak_files(shape, en, {PROBE_KEY: text}), out, PROBE_MOD)
    files = [pak]
    for ext in ("utoc", "ucas"):
        path = out / f"{PROBE_MOD}.{ext}"
        path.write_bytes(fetched[ext])
        files.append(path)
    return files


def _run_probe(companion: tuple[Path, dict[str, bytes]] | None) -> list[str]:
    return gnomon.run_mod(repo_root() / MOD_DIR, {"keys.txt": f"{PROBE_KEY[0]},{PROBE_KEY[1]}\n".encode()}, LOG, companion)


def _launch(files: list[Path]) -> list[str]:
    host = gnomon.host()
    dest = f"{gnomon.paks()}/~mods"
    try:
        deck.put(host, dest, files)
        return _run_probe(None)
    finally:
        deck.delete(host, dest, [f.name for f in files])


def _launch_ue4ss(text: str) -> list[str]:
    edits = edits_text({PROBE_KEY: text}).encode("utf-8")
    return _run_probe((repo_root() / TEXT_MOD_DIR, {"edits.txt": edits}))


def _verdict(shape: str, lines: list[str], marker: str, vanilla_digest: str) -> bool:
    ns, key = PROBE_KEY
    pattern = re.compile(rf"{re.escape(ns)},{re.escape(key)} ([0-9a-f]{{16}}|missing)")
    observed = [match.group(1) for line in lines if (match := pattern.fullmatch(line))]
    if not observed:
        raise Fail(f"{shape}: the game produced no observation for {ns},{key}; the probe is inconclusive")
    seen: list[str] = []
    for digest in observed:
        if digest != "missing" and digest not in seen:
            seen.append(digest)
    honoured = marker in seen and vanilla_digest not in seen
    verdict = "honoured" if honoured else "not honoured"
    print(f"{shape}: {ns},{key} marker {marker} vanilla {vanilla_digest} seen {' '.join(seen) or 'none'}: {verdict}")
    return honoured


def _vanilla_digest(en: LocresFile) -> str:
    ns, key = PROBE_KEY
    entry = en[ns][key] if en[ns] else None
    if entry is None:
        raise Fail(f"the probe key {ns},{key} is not in the vanilla text")
    return vanilla_hash(entry.translation)


def _run_shape(shape: str, fetched: dict[str, bytes]) -> bool:
    text = _probe_text()
    en = vanilla()
    vanilla_digest = _vanilla_digest(en)
    if shape == UE4SS_SHAPE:
        lines = _launch_ue4ss(text)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            files = _pack(shape, en, text, Path(tmp), fetched)
            lines = _launch(files)
    return _verdict(shape, lines, vanilla_hash(text), vanilla_digest)


def _run_build(build: list[Path], fetched: dict[str, bytes]) -> bool:
    text = _probe_text()
    en = vanilla()
    vanilla_digest = _vanilla_digest(en)
    with tempfile.TemporaryDirectory() as tmp:
        marker = _pack("full", en, text, Path(tmp), fetched)
        lines = _launch([*build, *marker])
    return _verdict("build", lines, vanilla_hash(text), vanilla_digest)


def cmd_probe(args: argparse.Namespace) -> None:
    build = _build_files() if args.shape is None else []
    _check_deck()
    if gnomon.running():
        raise Fail(f"FINAL FANTASY RESONANCE is running on {gnomon.host()}; refusing to probe")
    _check_build()
    if args.shape is None:
        if not _run_build(build, _fetch_globals()):
            raise Fail("build is not honoured by the game")
        return
    fetched = {} if args.shape == UE4SS_SHAPE else _fetch_globals()
    if not _run_shape(args.shape, fetched):
        raise Fail(f"shape {args.shape} is not honoured by the game")
    print(f"shape honoured: {args.shape}")


def _newest_usmap(host: str) -> str | None:
    # The glob runs under sh: a login shell such as zsh aborts on a glob that matches nothing.
    out = deck.ssh(host, f"sh -c 'ls -t \"$1\"/*.usmap 2>/dev/null | head -n 1' sh {deck.remote(gnomon.ue4ss())}")
    path = out.decode("utf-8", errors="replace").strip()
    return path or None


def cmd_usmap(args: argparse.Namespace) -> None:
    _check_deck()
    host = gnomon.host()
    deck.ssh(host, f"sh -c 'rm -f \"$1\"/*.usmap' sh {deck.remote(gnomon.ue4ss())}")
    lines = gnomon.run_mod(repo_root() / USMAP_MOD, {}, USMAP_LOG)
    if "failed" in lines:
        raise Fail(f"the usmap mod reported failed on {host}")
    remote = _newest_usmap(host)
    if remote is None:
        raise Fail(f"no .usmap in {gnomon.ue4ss()} on {host}")
    data = gnomon.fetch(remote)
    root = vanilla_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / USMAP_NAME
    dest.write_bytes(data)
    deck.ssh(host, f"rm -f {deck.remote(remote)}")
    print(f"wrote {dest} ({len(data)} bytes)")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    probe = subparsers.add_parser("probe", help="run the UE4SS probe on the gnomon host to see which text shape FFR honours")
    probe.add_argument(
        "--shape",
        choices=SHAPE_CHOICES,
        help="probe only this shape instead of the built mod: target, full, partial or ue4ss "
        "(default: the built mod, with a full-shape marker pak beside it)",
    )
    probe.set_defaults(fn=cmd_probe)
    usmap = subparsers.add_parser("usmap", help="dump the game's usmap on the gnomon host and save it as Mappings.usmap in vanilla")
    usmap.set_defaults(fn=cmd_usmap)
