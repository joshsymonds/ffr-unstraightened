"""Release tests on a scratch origin and scratch clone, with git isolated from the user's config. Every build byte here is made up."""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from ffrd import release
from ffrd.game import MOD_NAME

WORKSPACE = Path(__file__).resolve().parents[1]
TAG = "v1.0.0"
BUILD_FILES = tuple(f"{MOD_NAME}.{ext}" for ext in ("pak", "utoc", "ucas"))
MARKER = "FFRS-UnstraightenedProbe_P.pak"
EXPECTED_MEMBERS = sorted([*BUILD_FILES, "NEXUS.md"])
ZIP_NAME = f"FFRS-Unstraightened-{TAG}.zip"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def resolve(repo: Path, ref: str) -> str | None:
    done = subprocess.run(["git", "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd=repo, capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Synthetic")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "synthetic@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Synthetic")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "synthetic@example.invalid")


@pytest.fixture
def release_repo(git_env: None, fake_vanilla: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    clone.mkdir()
    git(clone, "init", "-q", "-b", "main")
    (clone / ".gitignore").write_text("/build/\n")
    (clone / "devenv.nix").write_text("{ }\n")
    (clone / "ffrd").mkdir()
    (clone / "ffrd" / "__init__.py").write_text("")
    template = clone / "docs" / "nexus" / "description.md"
    template.parent.mkdir(parents=True)
    template.write_text((WORKSPACE / "docs" / "nexus" / "description.md").read_text())
    build = clone / "build"
    build.mkdir()
    for name in (*BUILD_FILES, MARKER):
        (build / name).write_bytes(b"synthetic " + name.encode())
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "Synthetic release base")
    git(clone, "remote", "add", "origin", str(origin))
    git(clone, "push", "-q", "origin", "main")
    (clone / "release.txt").write_text("synthetic release commit\n")
    git(clone, "add", "release.txt")
    git(clone, "commit", "-q", "-m", "Synthetic release commit")
    monkeypatch.chdir(clone)
    return clone, origin


def zip_members(clone: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(clone / "dist" / ZIP_NAME) as z:
        return {name: z.read(name) for name in z.namelist()}


def test_release_pushes_branch_and_annotated_tag_at_head(release_repo: tuple[Path, Path]) -> None:
    clone, origin = release_repo
    release.main([TAG])
    head = git(clone, "rev-parse", "HEAD")
    assert resolve(origin, f"refs/tags/{TAG}") == head
    assert git(origin, "cat-file", "-t", f"refs/tags/{TAG}") == "tag"
    assert resolve(origin, "refs/heads/main") == head


def test_zip_holds_exactly_build_files_and_nexus(release_repo: tuple[Path, Path]) -> None:
    clone, _ = release_repo
    release.main([TAG])
    members = zip_members(clone)
    assert sorted(members) == EXPECTED_MEMBERS
    assert MARKER not in members
    for name in BUILD_FILES:
        assert members[name] == b"synthetic " + name.encode()


def test_nexus_text_states_tag_disclosure_and_build_id(release_repo: tuple[Path, Path]) -> None:
    clone, _ = release_repo
    release.main([TAG])
    text = zip_members(clone)["NEXUS.md"].decode()
    assert f"Version: {TAG}" in text
    assert "Content tag: AI-Generated Content" in text
    assert "AI models trained on the characters' voices" in text
    assert "Game build: 424242" in text
    assert "$" not in text


def assert_refused(clone: Path, origin: Path, capsys: pytest.CaptureFixture[str], tag: str, message: str) -> None:
    local_before = resolve(clone, f"refs/tags/{tag}")
    origin_before = resolve(origin, "refs/heads/main")
    with pytest.raises(SystemExit) as exc:
        release.main([tag])
    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert err.startswith("ffrd: ")
    assert message in err
    assert not (clone / "dist").exists()
    assert resolve(clone, f"refs/tags/{tag}") == local_before
    assert resolve(origin, f"refs/tags/{tag}") is None
    assert resolve(origin, "refs/heads/main") == origin_before


def test_refuses_dirty_tree_before_writing(release_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    clone, origin = release_repo
    (clone / "notes.txt").write_text("synthetic\n")
    assert_refused(clone, origin, capsys, TAG, "working tree is dirty")


def test_refuses_existing_local_tag(release_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    clone, origin = release_repo
    git(clone, "tag", TAG)
    assert_refused(clone, origin, capsys, TAG, f"tag {TAG} already exists")


@pytest.mark.parametrize("name", BUILD_FILES)
def test_refuses_missing_build_file(release_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str], name: str) -> None:
    clone, origin = release_repo
    (clone / "build" / name).unlink()
    assert_refused(clone, origin, capsys, TAG, f"missing ['{name}']")


def test_refuses_missing_buildid(release_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    clone, origin = release_repo
    (Path(os.environ["FFR_VANILLA"]) / "BUILDID").unlink()
    assert_refused(clone, origin, capsys, TAG, "no BUILDID")


def test_refuses_outside_a_git_repository(release_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    clone, origin = release_repo
    shutil.rmtree(clone / ".git")
    assert_refused(clone, origin, capsys, TAG, "git status failed")


@pytest.mark.parametrize("tag", ["v1/escape", "..", "v1..0"])
def test_refuses_tag_that_is_not_a_plain_ref_name(release_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str], tag: str) -> None:
    clone, origin = release_repo
    assert_refused(clone, origin, capsys, tag, f"invalid tag name {tag!r}")


def test_failed_push_removes_local_tag(release_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    clone, origin = release_repo
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    origin_before = resolve(origin, "refs/heads/main")
    with pytest.raises(SystemExit) as exc:
        release.main([TAG])
    assert exc.value.code == 1
    assert f"push of {TAG} failed; local tag removed" in capsys.readouterr().err
    assert resolve(clone, f"refs/tags/{TAG}") is None
    assert resolve(origin, f"refs/tags/{TAG}") is None
    assert resolve(origin, "refs/heads/main") == origin_before


def test_release_recipe_runs_build_probe_then_release_at_default_tag() -> None:
    done = subprocess.run(["just", "--dry-run", "release"], cwd=WORKSPACE, check=True, capture_output=True, text=True)
    lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    assert lines == ["ffrd build", "ffrd probe", f"python3 -m ffrd.release {TAG}"]


def test_release_zip_is_git_ignored() -> None:
    done = subprocess.run(["git", "check-ignore", "-q", f"dist/{ZIP_NAME}"], cwd=WORKSPACE, capture_output=True)
    assert done.returncode == 0
