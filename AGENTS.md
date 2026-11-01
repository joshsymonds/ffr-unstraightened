# Agent rules

Rules for coding agents working in this repository.

## Reporting

Report counts, keys and hashes. Never report game lines: not in chat, commit
messages, PR descriptions, reports or transcripts. A line is identified by its
namespace and key, and by `vanilla_hash` (the first 16 hex characters of the
SHA-256 of the English text, `ffrd/game.py`) when a fingerprint is needed.

To check whether game text has leaked into history, run `ffrd leak-check`. It
prints `0` when history is clean. Otherwise it prints one line per match, giving
the commit and path (or `message`), the language, and the namespace and key, never
the matched text. It then exits 1.

## Vanilla text

Never read, grep, list or print anything in the gitignored `vanilla/` directory
or in the game install. The only permitted way to see game text is
`ffrd find`, which is progress-gated.

`vanilla/scenes.json` is covered by this rule too. Read it only through the counts that
`ffrd scenes --check` prints, or through code that reduces it to counts and keys.

Probe-host logs are read only through `ffrd probe` and `ffrd usmap`, never directly.

## Tests

Tests use synthetic fixtures only. Write made-up lines and keep them in the
style of `SYNTHETIC_LINES` in `tests/conftest.py`, which is marked "Made-up
... lines. Not game text." Never copy a line from the game, `vanilla/` or
`ffrd find` output into a test, fixture or snapshot.
