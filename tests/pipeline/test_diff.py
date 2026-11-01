import argparse
from collections.abc import Callable
from pathlib import Path

import pytest

from ffrd.game import Fail, vanilla_hash
from ffrd.pipeline import diff

# Made-up English lines matching GATE_LINES in tests/conftest.py. Not game text.
C01 = "Made-up gate line chapter one. Not game text."
C02 = "Made-up gate line chapter two. Not game text."
SUB_C2 = "Made-up gate sub-event line chapter two. Not game text."
C03 = "Made-up later sentinel chapter three. Not game text."
SUB_C3 = "Made-up later sentinel sub-event chapter three. Not game text."
MISC = "Made-up gate line without a chapter. Not game text."
SCENE = "Made-up gate line with a non-chapter segment. Not game text."

NO_PROGRESS = "no progress recorded; run `ffrd progress <chapter>`"


def toml_entry(namespace: str, key: str, vanilla: str, text: str) -> str:
    return f'["{namespace}".{key}]\nvanilla = "{vanilla}"\ntext = "{text}"\n\n'


def main_toml() -> str:
    return (
        toml_entry("ST_C01_001_01", "1", vanilla_hash(C01), "Made-up rewrite chapter one. Not game text.")
        + toml_entry("ST_C02_001_01", "1", vanilla_hash(C02), "Made-up rewrite chapter two. Not game text.")
        + toml_entry("ST_C03_001_01", "1", vanilla_hash(C03), "Made-up later rewrite chapter three. Not game text.")
        + toml_entry("ST_SubEv_004_C2_01", "1", vanilla_hash(SUB_C2), "Made-up rewrite sub-event chapter two. Not game text.")
        + toml_entry("ST_SubEv_004_C3_01", "1", vanilla_hash(SUB_C3), "Made-up later rewrite sub-event chapter three. Not game text.")
        + toml_entry("ST_Misc_001", "1", vanilla_hash(MISC), "Made-up sentinel without a chapter. Not game text.")
        + toml_entry("ST_Scene_C1x_001", "1", vanilla_hash(SCENE), "Made-up sentinel with a non-chapter segment. Not game text.")
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ffrd")
    diff.register(parser.add_subparsers(required=True))
    return parser.parse_args(argv)


def _run(lens: str) -> None:
    args = _parse(["diff", "--lens", lens])
    args.fn(args)


def _write_lens(repo: Path, name: str, body: str) -> None:
    path = repo / "dialog" / "demo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_vanilla: Path) -> Path:
    root = tmp_path / "repo"
    (root / "ffrd").mkdir(parents=True)
    (root / "devenv.nix").write_text("{ }\n")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    path = tmp_path / "progress"
    monkeypatch.setenv("FFR_PROGRESS", str(path))

    def record(chapter: int) -> None:
        path.write_text(f"{chapter}\n")

    return record


def test_shows_rewrites_up_to_progress_and_counts_the_rest(
    repo: Path, progress: Callable[[int], None], capsys: pytest.CaptureFixture[str]
) -> None:
    progress(2)
    _write_lens(repo, "rewrites.toml", main_toml())
    _run("demo")
    out, err = capsys.readouterr()
    assert out == (
        '["ST_C01_001_01".1]\n'
        '- "Made-up gate line chapter one. Not game text."\n'
        '+ "Made-up rewrite chapter one. Not game text."\n'
        "\n"
        '["ST_C02_001_01".1]\n'
        '- "Made-up gate line chapter two. Not game text."\n'
        '+ "Made-up rewrite chapter two. Not game text."\n'
        "\n"
        '["ST_SubEv_004_C2_01".1]\n'
        '- "Made-up gate sub-event line chapter two. Not game text."\n'
        '+ "Made-up rewrite sub-event chapter two. Not game text."\n'
        "\n"
    )
    assert err == "# 3 shown, 2 withheld, 2 without a chapter\n"


def test_raising_progress_reveals_the_later_line(repo: Path, progress: Callable[[int], None], capsys: pytest.CaptureFixture[str]) -> None:
    _write_lens(repo, "rewrites.toml", main_toml())
    progress(3)
    _run("demo")
    out, err = capsys.readouterr()
    assert out == (
        '["ST_C01_001_01".1]\n'
        '- "Made-up gate line chapter one. Not game text."\n'
        '+ "Made-up rewrite chapter one. Not game text."\n'
        "\n"
        '["ST_C02_001_01".1]\n'
        '- "Made-up gate line chapter two. Not game text."\n'
        '+ "Made-up rewrite chapter two. Not game text."\n'
        "\n"
        '["ST_C03_001_01".1]\n'
        '- "Made-up later sentinel chapter three. Not game text."\n'
        '+ "Made-up later rewrite chapter three. Not game text."\n'
        "\n"
        '["ST_SubEv_004_C2_01".1]\n'
        '- "Made-up gate sub-event line chapter two. Not game text."\n'
        '+ "Made-up rewrite sub-event chapter two. Not game text."\n'
        "\n"
        '["ST_SubEv_004_C3_01".1]\n'
        '- "Made-up later sentinel sub-event chapter three. Not game text."\n'
        '+ "Made-up later rewrite sub-event chapter three. Not game text."\n'
        "\n"
    )
    assert err == "# 5 shown, 0 withheld, 2 without a chapter\n"


def test_hidden_entries_are_never_looked_up_in_vanilla(
    repo: Path, progress: Callable[[int], None], capsys: pytest.CaptureFixture[str]
) -> None:
    progress(2)
    _write_lens(
        repo,
        "rewrites.toml",
        toml_entry("ST_C03_001_01", "1", "0" * 16, "Made-up later rewrite chapter three. Not game text.")
        + toml_entry("ST_Misc_001", "1", "0" * 16, "Made-up sentinel without a chapter. Not game text."),
    )
    _run("demo")
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "# 0 shown, 1 withheld, 1 without a chapter\n"


@pytest.mark.usefixtures("progress")
def test_no_progress_refuses_before_anything_else_and_prints_nothing(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_lens(repo, "rewrites.toml", main_toml())
    with pytest.raises(Fail) as exc:
        _run("demo")
    assert str(exc.value) == NO_PROGRESS
    assert capsys.readouterr() == ("", "")


def test_missing_lens_directory_fails(repo: Path, progress: Callable[[int], None]) -> None:
    progress(2)
    with pytest.raises(Fail) as exc:
        _run("nolens")
    assert str(exc.value) == "no dialog/nolens/ directory"


@pytest.mark.parametrize("lens", ["", ".", "..", "a/b", "a\\b"])
def test_lens_must_be_one_plain_path_segment(lens: str, repo: Path, progress: Callable[[int], None]) -> None:
    progress(2)
    with pytest.raises(Fail) as exc:
        _run(lens)
    assert str(exc.value) == f"lens name {lens!r} must be one plain path segment"


def test_invalid_toml_names_the_file_and_never_echoes_the_line(repo: Path, progress: Callable[[int], None]) -> None:
    progress(2)
    _write_lens(repo, "broken.toml", '["ST_C01_001_01".1]\ntext = "Made-up broken line. Not game text."\nvanilla = \n')
    with pytest.raises(Fail) as exc:
        _run("demo")
    assert str(exc.value).startswith("dialog/demo/broken.toml: cannot read as TOML: ")
    assert "Made-up broken line" not in str(exc.value)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('ST_Scene_C1x_001 = "Made-up echo line. Not game text."\n', "dialog/demo/errs.toml: ST_Scene_C1x_001 must be a table of edits"),
        (
            '["ST_C01_001_01"]\n1 = "Made-up echo line. Not game text."\n',
            "dialog/demo/errs.toml: ST_C01_001_01,1 must be a table with vanilla and text",
        ),
        ('["ST_Misc_001"]\n1 = { vanilla = "0000000000000000" }\n', 'dialog/demo/errs.toml: ST_Misc_001,1 needs a string "text"'),
    ],
    ids=["namespace-not-a-table", "edit-not-a-table", "missing-text-chapterless"],
)
def test_structural_errors_name_the_file_and_key(body: str, expected: str, repo: Path, progress: Callable[[int], None]) -> None:
    progress(2)
    _write_lens(repo, "errs.toml", body)
    with pytest.raises(Fail) as exc:
        _run("demo")
    assert str(exc.value) == expected
    assert "Made-up echo line" not in str(exc.value)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            toml_entry("ST_C01_001_09", "1", vanilla_hash(C01), "Made-up echo line. Not game text."),
            "dialog/demo/errs.toml: ST_C01_001_09,1 is not in the game's text",
        ),
        (
            toml_entry("ST_C01_001_01", "9", vanilla_hash(C01), "Made-up echo line. Not game text."),
            "dialog/demo/errs.toml: ST_C01_001_01,9 is not in the game's text",
        ),
        (
            toml_entry("ST_C02_001_01", "1", "0" * 16, "Made-up echo line. Not game text."),
            "dialog/demo/errs.toml: ST_C02_001_01,1 changed in the game",
        ),
    ],
    ids=["namespace-absent", "key-absent", "hash-mismatch"],
)
def test_shown_entry_errors_name_the_file_and_key(body: str, expected: str, repo: Path, progress: Callable[[int], None]) -> None:
    progress(2)
    _write_lens(repo, "errs.toml", body)
    with pytest.raises(Fail) as exc:
        _run("demo")
    assert str(exc.value) == expected
    assert "Made-up echo line" not in str(exc.value)


def test_lens_is_required_and_has_help(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        _parse(["diff"])
    assert exit_info.value.code == 2
    assert "--lens" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_info:
        _parse(["diff", "--help"])
    assert exit_info.value.code == 0
    assert "lens whose dialog/<lens>/ rewrites to show" in capsys.readouterr().out
