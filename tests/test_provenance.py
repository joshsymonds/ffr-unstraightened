"""Provenance check tests on synthetic repos. Every term here is made up."""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ffrd import provenance

WORKSPACE = Path(__file__).resolve().parents[1]
TERM = "zork-quill"  # made-up term; its digests are computed below
JOINED = "zorkquill"
DIGESTS = frozenset(hashlib.sha256(form.encode("utf-8")).hexdigest() for form in (TERM, JOINED))
HYPHEN_PREFIX = hashlib.sha256(TERM.encode("utf-8")).hexdigest()[:12]
JOINED_PREFIX = hashlib.sha256(JOINED.encode("utf-8")).hexdigest()[:12]
# A hyphen-separated run also checks its joined form, so a hyphen-form hit reports both prefixes.
BOTH_PREFIXES = {HYPHEN_PREFIX, JOINED_PREFIX}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def make_repo(root: Path, files: dict[str, bytes], message: str = "Synthetic commit") -> tuple[Path, str]:
    root.mkdir()
    git(root, "init", "-q")
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    git(root, "add", "-A")
    git(
        root,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
    return root, git(root, "rev-parse", "HEAD")


def test_stored_digests_are_38_sha256_hex_digests():
    assert len(provenance.STORED_DIGESTS) == 38
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in provenance.STORED_DIGESTS)


def test_term_in_path_reports_commit_and_redacted_location(tmp_path, monkeypatch):
    repo, head = make_repo(tmp_path / "repo", {f"{TERM}/notes.txt": b"clean text\n"})
    monkeypatch.chdir(repo)
    assert set(provenance.scan(DIGESTS)) == {f"{head}:<redacted>/notes.txt {p}" for p in BOTH_PREFIXES}


def test_term_in_file_text_reports_commit_and_path(tmp_path, monkeypatch):
    repo, head = make_repo(tmp_path / "repo", {"notes.txt": b"the zork-quill plan\n"})
    monkeypatch.chdir(repo)
    assert set(provenance.scan(DIGESTS)) == {f"{head}:notes.txt {p}" for p in BOTH_PREFIXES}


def test_term_in_commit_message_reports_commit_and_message(tmp_path, monkeypatch):
    repo, head = make_repo(tmp_path / "repo", {"notes.txt": b"clean\n"}, message="Follow-up on zork-quill")
    monkeypatch.chdir(repo)
    assert set(provenance.scan(DIGESTS)) == {f"{head}:message {p}" for p in BOTH_PREFIXES}


@pytest.mark.parametrize(
    ("text", "forms"),
    [
        ("zork-quill", (TERM, JOINED)),
        ("zork_quill", (TERM, JOINED)),
        ("Zork-Quill", (TERM, JOINED)),
        ("ZORK_QUILL", (TERM, JOINED)),
        ("a-zork-quill-b", (TERM, JOINED)),
        ("zorkquill", (JOINED,)),
        ("ZorkQuill", (JOINED,)),
    ],
)
def test_separator_and_case_variants_match(tmp_path, monkeypatch, text, forms):
    repo, head = make_repo(tmp_path / "repo", {"notes.txt": f"see {text} here\n".encode()})
    monkeypatch.chdir(repo)
    expected = {f"{head}:notes.txt {hashlib.sha256(form.encode('utf-8')).hexdigest()[:12]}" for form in forms}
    assert set(provenance.scan(DIGESTS)) == expected


def test_undecodable_bytes_do_not_hide_the_term(tmp_path, monkeypatch):
    repo, head = make_repo(tmp_path / "repo", {"blob.bin": b"\xff zork-quill \xfe\n"})
    monkeypatch.chdir(repo)
    assert set(provenance.scan(DIGESTS)) == {f"{head}:blob.bin {p}" for p in BOTH_PREFIXES}


def test_words_that_are_not_joined_by_a_separator_do_not_match(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path / "repo", {"notes.txt": b"zork quill and zork.quill and zorkquills\n"})
    monkeypatch.chdir(repo)
    assert provenance.scan(DIGESTS) == []


def test_clean_repo_passes(tmp_path, monkeypatch, capsys):
    repo, _ = make_repo(tmp_path / "repo", {"notes.txt": b"clean\n"}, message="Clean commit")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(provenance, "STORED_DIGESTS", DIGESTS)
    assert provenance.main() == 0
    assert capsys.readouterr().out == ""


def test_output_never_contains_the_term(tmp_path, monkeypatch, capsys):
    repo, _ = make_repo(
        tmp_path / "repo",
        {f"{TERM}/notes.txt": b"the zork-quill plan\n"},
        message="zork_quill follow-up",
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(provenance, "STORED_DIGESTS", DIGESTS)
    assert provenance.main() == 1
    out = capsys.readouterr().out.lower()
    assert out.strip()
    assert "zork" not in out
    assert "quill" not in out


def test_outside_a_repository_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(provenance, "STORED_DIGESTS", DIGESTS)
    assert provenance.main() == 2
    assert "git" in capsys.readouterr().err


def test_module_entry_point_exits_zero_on_clean_repo(tmp_path):
    repo, _ = make_repo(tmp_path / "repo", {"notes.txt": b"clean\n"})
    env = {**os.environ, "PYTHONPATH": str(WORKSPACE)}
    done = subprocess.run(
        [sys.executable, "-m", "ffrd.provenance"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
