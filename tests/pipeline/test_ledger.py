"""Ledger tests on made-up git repositories. Every hash, id and line here is made up."""

import json
import re
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ffrd.game import Fail
from ffrd.pipeline import ledger
from ffrd.pipeline.ledger import Entry

LENS = "synthetic"
NAME = f"ledger/{LENS}.jsonl"

ENTRY = Entry(
    run="run-1",
    lens=LENS,
    base=None,
    namespace="ST_Synthetic_001",
    key="1",
    vanilla_hash="0123456789abcdef",
    base_hash=None,
    text_hash="fedcba9876543210",
    reasons=("made-up-reason",),
    writer="synthetic-writer",
    plan=None,
)
SECOND = replace(ENTRY, namespace="ST_Synthetic_002", key="2")
FIELDS = list(asdict(ENTRY))


def line_of(entry: Entry) -> str:
    return json.dumps(asdict(entry), ensure_ascii=False)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def make_repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "-q")
    return root


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def test_append_to_empty_ledger_writes_one_line_per_entry(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")

    assert ledger.append(root, LENS, [ENTRY, SECOND]) == [ENTRY, SECOND]

    lines = (root / NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert list(json.loads(lines[0])) == FIELDS
    assert json.loads(lines[0])["reasons"] == ["made-up-reason"]
    assert ledger.read(root, LENS) == [ENTRY, SECOND]


def test_append_nothing_creates_no_ledger_directory(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")

    assert ledger.append(root, LENS, []) == []
    assert not (root / "ledger").exists()


def test_reappending_entry_differing_only_in_run_appends_nothing(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    ledger.append(root, LENS, [ENTRY])
    before = (root / NAME).read_bytes()

    assert ledger.append(root, LENS, [replace(ENTRY, run="run-2")]) == []
    assert (root / NAME).read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [{"reasons": ("made-up-other-reason",)}, {"text_hash": "1111111111111111"}],
    ids=["reasons", "text_hash"],
)
def test_changed_field_appends_one_entry(tmp_path: Path, change: dict[str, object]) -> None:
    root = make_repo(tmp_path / "repo")
    ledger.append(root, LENS, [ENTRY])
    changed = replace(ENTRY, run="run-2", **change)

    assert ledger.append(root, LENS, [changed]) == [changed]
    assert ledger.read(root, LENS) == [ENTRY, changed]


def test_working_file_edit_of_committed_line_fails(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    ledger.append(root, LENS, [ENTRY])
    commit_all(root, "Synthetic first version")
    path = root / NAME
    path.write_text(path.read_text(encoding="utf-8").replace(ENTRY.text_hash, "2222222222222222"), encoding="utf-8")

    with pytest.raises(Fail, match=re.escape(f"{NAME}: an existing line was changed or removed; the ledger is append-only")):
        ledger.read(root, LENS)


def test_committed_rewrite_fails(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    ledger.append(root, LENS, [ENTRY])
    commit_all(root, "Synthetic first version")
    path = root / NAME
    path.write_text(path.read_text(encoding="utf-8").replace(ENTRY.text_hash, "2222222222222222"), encoding="utf-8")
    commit_all(root, "Synthetic rewrite")

    with pytest.raises(Fail, match=re.escape(f"{NAME}: an existing line was changed or removed; the ledger is append-only")):
        ledger.read(root, LENS)


def test_committed_removal_fails(tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    ledger.append(root, LENS, [ENTRY])
    commit_all(root, "Synthetic first version")
    (root / NAME).unlink()
    commit_all(root, "Synthetic removal")

    with pytest.raises(Fail, match=re.escape(f"{NAME}: an existing line was changed or removed; the ledger is append-only")):
        ledger.read(root, LENS)


MALFORMED: dict[str, str] = {
    "not JSON": f"{line_of(ENTRY)}\nnot a ledger entry\n",
    "JSON array": f"{line_of(ENTRY)}\n{json.dumps(list(asdict(ENTRY).values()))}\n",
    "extra field": f"{line_of(ENTRY)}\n{json.dumps({**asdict(ENTRY), 'extra': 'x'}, ensure_ascii=False)}\n",
    "reasons as text": f"{line_of(ENTRY)}\n{json.dumps({**asdict(ENTRY), 'reasons': 'made-up-reason'}, ensure_ascii=False)}\n",
    "missing final newline": f"{line_of(ENTRY)}\n{line_of(SECOND)}",
}


@pytest.mark.parametrize("contents", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_line_fails_naming_its_line_number(tmp_path: Path, contents: str) -> None:
    (tmp_path / "ledger").mkdir()
    (tmp_path / NAME).write_text(contents, encoding="utf-8")

    with pytest.raises(Fail, match=re.escape(f"{NAME}:2: not a ledger entry")):
        ledger.read(tmp_path, LENS)


def test_append_validates_existing_ledger_first(tmp_path: Path) -> None:
    (tmp_path / "ledger").mkdir()
    (tmp_path / NAME).write_text("not a ledger entry\n", encoding="utf-8")

    with pytest.raises(Fail, match=re.escape(f"{NAME}:1: not a ledger entry")):
        ledger.append(tmp_path, LENS, [ENTRY])
    assert (tmp_path / NAME).read_text(encoding="utf-8") == "not a ledger entry\n"


def test_root_outside_git_has_no_committed_version(tmp_path: Path) -> None:
    (tmp_path / "ledger").mkdir()
    (tmp_path / NAME).write_text(f"{line_of(ENTRY)}\n", encoding="utf-8")

    assert ledger.read(tmp_path, LENS) == [ENTRY]


def test_latest_keeps_last_entry_per_namespace_and_key() -> None:
    newer = replace(ENTRY, run="run-2", reasons=("made-up-other-reason",))

    assert ledger.latest([ENTRY, SECOND, newer]) == {
        (ENTRY.namespace, ENTRY.key): newer,
        (SECOND.namespace, SECOND.key): SECOND,
    }


@pytest.mark.parametrize("lens", ["", ".", "..", "a/b", "a\\b"])
def test_lens_must_be_one_plain_path_segment(tmp_path: Path, lens: str) -> None:
    with pytest.raises(Fail):
        ledger.path(tmp_path, lens)
