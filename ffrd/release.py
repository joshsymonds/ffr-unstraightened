"""release: zip the built mod with its Nexus text, then tag HEAD and push branch and tag to origin.

Run as `python3 -m ffrd.release <tag>`. Every refusal happens before anything is written. Output names
the tag, the files and the zip path and counts, never game text.
"""

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path
from string import Template

from ffrd.game import MOD_NAME, Fail, repo_root, vanilla_dir

EXTS = ("pak", "utoc", "ucas")
TEMPLATE = Path("docs/nexus/description.md")
NEXUS_NAME = "NEXUS.md"


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def release_tag(tag: str) -> None:
    root = repo_root()
    status = git(root, "status", "--porcelain")
    if status.returncode:
        raise Fail(f"git status failed: {status.stderr.strip()}")
    if status.stdout:
        raise Fail("working tree is dirty; commit or stash first")
    if "/" in tag or git(root, "check-ref-format", f"refs/tags/{tag}").returncode:
        raise Fail(f"invalid tag name {tag!r}")
    existing = git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}")
    if existing.returncode == 0:
        raise Fail(f"tag {tag} already exists")
    if existing.returncode != 1:
        raise Fail(f"could not check for tag {tag}: {existing.stderr.strip()}")
    builds = {f"{MOD_NAME}.{ext}": root / "build" / f"{MOD_NAME}.{ext}" for ext in EXTS}
    missing = [name for name, path in builds.items() if not path.is_file()]
    if missing:
        raise Fail(f"missing {missing}; run `ffrd build`")
    buildid = vanilla_dir() / "BUILDID"
    if not buildid.is_file():
        raise Fail(f"no BUILDID at {buildid}")
    nexus = Template((root / TEMPLATE).read_text()).substitute(tag=tag, build_id=buildid.read_text().strip())

    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    zip_path = dist / f"FFRS-Unstraightened-{tag}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in builds.items():
            z.write(str(path), arcname=name)
        z.writestr(NEXUS_NAME, nexus)

    made = git(root, "tag", "-a", tag, "-m", tag)
    if made.returncode:
        raise Fail(f"could not tag {tag}: {made.stderr.strip()}")
    pushed = subprocess.run(["git", "push", "--atomic", "origin", "HEAD", f"refs/tags/{tag}"], cwd=root)
    if pushed.returncode:
        if git(root, "tag", "-d", tag).returncode:
            raise Fail(f"push of {tag} failed and its local tag could not be removed")
        raise Fail(f"push of {tag} failed; local tag removed")

    names = [*builds, NEXUS_NAME]
    print(f"zipped {len(names)} files into {zip_path}: {', '.join(names)}")
    print(f"tagged {tag} and pushed branch and tag to origin")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m ffrd.release", description="Zip the built mod with its Nexus text, tag HEAD and push.")
    p.add_argument("tag")
    args = p.parse_args(argv)
    try:
        release_tag(args.tag)
    except Fail as e:
        print(f"ffrd: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
