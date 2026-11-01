# Dialog edits

`ffrd build` packs the edits from the TOML files in this directory into a mod in `build/`.
Every file under `dialog/` with a `.toml` extension is read, in any subdirectory.

## Format

Each edit is one table. The header names the namespace and the key:

- The namespace is quoted.
- The key is bare when it is a valid bare TOML key. Otherwise quote it.

`ffrd run` writes each table with seven fields, in this order:

- `vanilla` is the `vanilla_hash` of the game's current English line for that key. It is
  the first 16 lowercase hex characters of the SHA-256 of the UTF-8 line. The function is
  `vanilla_hash` in `ffrd/game.py`.
- `text` is the replacement line.
- `lens` is the lens name, which is the `lenses/<name>.toml` file that screened the line.
- `reason` is a list of the lens triage ids the screen flagged the line for, in lens
  order.
- `writer` is `"voice"` or `"opus"`.
- `verdict` is always `"passed"`.
- `run` is the run id, a UTC timestamp in `YYYYMMDDTHHMMSSZ` form.

`ffrd build` reads only `vanilla` and `text` and ignores the other five fields. A hand
edit needs only `vanilla` and `text`.

Edits are keyed by namespace and key, and matched by `vanilla` hash. A stub from
`ffrd find` sets `text` to the vanilla line, so replace it with your line before
building. `ffrd build` rejects an unedited stub, and `ffrd leak-check` fails if a
vanilla line of 20 or more characters is committed.

## Example

This example uses a made-up namespace, key and line. The made-up vanilla line is
`Example traveller, the lantern is lit.` and the `vanilla` value is its real
`vanilla_hash`. The game has no line with this namespace or key.

```toml
["FFRS_Example".EXAMPLE_LANTERN_LINE]
vanilla = "207df5aa449501fb"
text = "Example traveller, the lantern is now lit, brighter than before."
```

## What `ffrd build` rejects

`ffrd build` stops with an error for any of these:

- The namespace or key does not exist in the game.
- The `vanilla` hash no longer matches the game's line ("changed in the game").
- The edit is an unedited stub: `text` hashes to the same value as `vanilla`.
- The same key is edited in two files.
- The `text` adds a `{...}` placeholder that the vanilla line does not have.
- `<text ...>...</>` tags are unbalanced.
- A field is missing or has the wrong type, or the file is not valid TOML.

Dropped placeholders only produce a warning.

## Build shapes

`ffrd build` writes one of four shapes. Each one carries the same edits, in a different
form. Use the default unless you have a reason to pick another.

- **Default (target).** Writes `FFRS-Unstraightened_P.pak` with two files. The first is
  `FFRS/Content/Localization/Unstraightened/en/Unstraightened.locres`, which holds only the
  edited lines, each under its namespace, key and the game's source-string hash. The second is
  `FFRS/Config/UserGame.ini`, which lists that localization target ahead of the game's own
  `Game` target, so the edits take precedence. The game was seen to honour this shape.
- **`--full`.** Writes the same pak with `FFRS/Content/Localization/Game/en/Game.locres`, which
  holds the whole English table with the edits applied. It replaces the game's table, so the
  file carries every line of the game, including the edited ones. The game was seen to honour
  this shape.
- **`--partial`.** Writes `FFRS/Content/Localization/Game/en/Unstraightened.locres` with only
  the edited lines. It is kept for comparison. The game was seen to ignore it for lines that
  `Game.locres` already holds, and every line an edit can change is one of those.
- **`--ue4ss`.** Writes no pak. It writes `build/UnstraightenedText/`, a UE4SS mod folder with
  `Scripts/main.lua`, an empty `enabled.txt` and `edits.txt`, which lists each edit. The folder
  needs UE4SS installed on the player's game. The mod registers the edits only when the game
  runs in English (culture `en` or `en-*`). In any other language the game's own text stays.

Use `--ue4ss` only for players who already run UE4SS, since the folder does nothing without it.
Use `--full` when you want one table in the mod instead of a list of overrides.
