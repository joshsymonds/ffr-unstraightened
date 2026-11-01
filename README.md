# ffr-dialog

A dialogue mod for FINAL FANTASY RESONANCE (Steam app 3259780, UE 5.6).
Text only: no assets other than the English string table change.

## How the game stores text

All player-facing text is in `FFRS/Content/Localization/Game/<lang>/Game.locres`
inside `FFRS-Windows.pak` (unencrypted, Oodle-compressed). The English table is
locres V3: about 950 StringTable namespaces and 26,700 entries. Story dialogue is
in `ST_C<chapter>_*`; side events are in `ST_SubEv_*`, `ST_ChrEv_*`, `ST_FEV_*`,
`ST_LegendEV_*` and similar. Speaker names are not in the table.

A mod replaces the whole `en/Game.locres`. It ships as a `.pak` holding that
file, plus `.utoc`/`.ucas` copies of the game's empty `global` container. The
loader needs all three files.

Text conventions to keep: curly apostrophes and quotes (`’`, `“”`), `\n` line
breaks, `<text style="italic">…</>` emphasis, and `{Placeholders}`.

## Workflow

```sh
nix develop            # ffrd, repak, python with pylocres, poedit, gettext
ffrd extract           # after install or any game update: vanilla/ + vanilla/en.po
ffrd find -n '^ST_C01_023_02$'   # a whole scene, in order, as .po
ffrd find 'chocobo'    # lines matching a regex
```

Paste entries from `ffrd find` into any `dialog/*.po` and fill in `msgstr`.
Entries with an empty `msgstr` or the `fuzzy` flag are ignored. `msgid` must
equal the game's current English line, so a game patch that rewrites a line
fails the build instead of silently reverting it. The `#. ja:` comment is the
Japanese original for reference.

```sh
ffrd build             # validate overrides, write build/FFRS-Dialog_P.{pak,utoc,ucas}
ffrd install           # copy into the game's Paks/~mods
ffrd uninstall
```

Set `FFR_GAME` if the game isn't in the default Steam library on this machine.
Play with Text Language set to English.

## Toolchain

`flake.nix` pins everything: repak with Epic's Oodle runtime beside it,
pylocres (and its binsl dependency), and `ffrd` (`tools/ffrd.py`).
