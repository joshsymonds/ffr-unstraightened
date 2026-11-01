import pytest

from ffrd import cli

SUBCOMMAND_HELP = {
    "pairs": "write four paraphrases per story line to work/voice/, or print split and call counts",
    "classifier": "stage classifier examples and run the remote classifier training",
    "lora": "stage LoRA training records and run the remote finetune and GGUF export",
    "serve": "start or stop llama-server on the GPU host's 127.0.0.1",
    "render": "voice a line read from stdin through the GPU host's llama-server",
    "eval": "print voice-model metrics: classifier AUROC, render and revert checks",
}


def test_voice_help_lists_every_subcommand_with_its_help(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["voice", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for name, help_text in SUBCOMMAND_HELP.items():
        assert name in out
        assert help_text in out


@pytest.mark.parametrize("name", list(SUBCOMMAND_HELP))
def test_voice_subcommand_help_exits_0(name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["voice", name, "--help"])
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out
