"""probe: run the UE4SS probe mod on the gnomon host to see which text shape FFR honours.

It refuses while FFR runs on the Steam Deck or on the gnomon host, and when the gnomon build is not the vanilla build.
For each shape it packs one edited line of PROBE_KEY, installs the pak beside the game's own global container, and
launches the probe mod. The partial shape is tried first and the full shape only when the partial one is not honoured.
Only keys and hashes are printed, never the probe text.
"""

import argparse
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from pylocres import LocresFile

from ffrd import deck, gnomon
from ffrd.build import REPAK, partial_table
from ffrd.game import LOCRES, Fail, repo_root, vanilla, vanilla_dir, vanilla_hash

PROBE_KEY = ("ST_UI_TitleMenu", "UI_Title_Copyright")
PROBE_MOD = "FFRS-UnstraightenedProbe_P"
MOD_DIR = "ue4ss/UnstraightenedProbe"
LOG = "probe.log"
SHAPES = ("partial", "full")


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


def _pack(table: LocresFile, name: str, out: Path, fetched: dict[str, bytes]) -> list[Path]:
    stage = out / "stage"
    locres_path = stage / LOCRES / "en" / name
    locres_path.parent.mkdir(parents=True)
    locres_path.write_bytes(table.to_binary())
    pak = out / f"{PROBE_MOD}.pak"
    subprocess.run(
        [REPAK, "pack", "-q", "--version", "V11", "--compression", "Zlib", "-m", "../../../", str(stage), str(pak)],
        check=True,
    )
    files = [pak]
    for ext in ("utoc", "ucas"):
        path = out / f"{PROBE_MOD}.{ext}"
        path.write_bytes(fetched[ext])
        files.append(path)
    return files


def _launch(files: list[Path]) -> list[str]:
    host = gnomon.host()
    dest = f"{gnomon.paks()}/~mods"
    try:
        deck.put(host, dest, files)
        return gnomon.run_mod(repo_root() / MOD_DIR, {"keys.txt": f"{PROBE_KEY[0]},{PROBE_KEY[1]}\n".encode()}, LOG)
    finally:
        deck.delete(host, dest, [f.name for f in files])


def _verdict(shape: str, lines: list[str], marker: str, vanilla_digest: str) -> bool:
    ns, key = PROBE_KEY
    pattern = re.compile(rf"{re.escape(ns)},{re.escape(key)} ([0-9a-f]{{16}}|missing)")
    seen: list[str] = []
    for line in lines:
        match = pattern.fullmatch(line)
        if match and match.group(1) != "missing" and match.group(1) not in seen:
            seen.append(match.group(1))
    honoured = marker in seen and vanilla_digest not in seen
    verdict = "honoured" if honoured else "not honoured"
    print(f"{shape}: {ns},{key} marker {marker} vanilla {vanilla_digest} seen {' '.join(seen) or 'none'}: {verdict}")
    return honoured


def _run_shape(shape: str, fetched: dict[str, bytes]) -> bool:
    ns, key = PROBE_KEY
    text = _probe_text()
    en = vanilla()
    entry = en[ns][key] if en[ns] else None
    if entry is None:
        raise Fail(f"the probe key {ns},{key} is not in the vanilla text")
    vanilla_digest = vanilla_hash(entry.translation)
    if shape == "partial":
        name, table = "Unstraightened.locres", partial_table(en, {PROBE_KEY: text})
    else:
        entry.translation = text
        name, table = "Game.locres", en
    with tempfile.TemporaryDirectory() as tmp:
        files = _pack(table, name, Path(tmp), fetched)
        lines = _launch(files)
    return _verdict(shape, lines, vanilla_hash(text), vanilla_digest)


def cmd_probe(args: argparse.Namespace) -> None:
    _check_deck()
    if gnomon.running():
        raise Fail(f"FINAL FANTASY RESONANCE is running on {gnomon.host()}; refusing to probe")
    _check_build()
    fetched = {ext: gnomon.fetch(f"{gnomon.paks()}/global.{ext}") for ext in ("utoc", "ucas")}
    for shape in SHAPES:
        if _run_shape(shape, fetched):
            print(f"shape honoured: {shape}")
            return
    raise Fail("neither shape is honoured by the game")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    probe = subparsers.add_parser("probe", help="run the UE4SS probe on the gnomon host to see which text shape FFR honours")
    probe.set_defaults(fn=cmd_probe)
