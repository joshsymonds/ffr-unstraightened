# Dialog edits

`ffrd build` writes the `FFRS-Unstraightened_P.*` mod files from the TOML files in this
directory. Every file under `dialog/` with a `.toml` extension is read, in any
subdirectory.

## Format

Each edit is one table. The header names the namespace and the key:

- The namespace is quoted.
- The key is bare when it is a valid bare TOML key. Otherwise quote it.

Each table has exactly two fields:

- `vanilla` is the `vanilla_hash` of the game's current English line for that key. It is
  the first 16 lowercase hex characters of the SHA-256 of the UTF-8 line. The function is
  `vanilla_hash` in `ffrd/game.py`.
- `text` is your replacement line.

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
