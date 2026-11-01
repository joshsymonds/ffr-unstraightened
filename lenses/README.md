# Lenses

A lens is the set of instructions the pipeline uses to pick and judge lines.
Each lens is one TOML file at `lenses/<name>.toml`. The lens name is the file
name without `.toml`, so `lenses/made-up-lens.toml` loads as `made-up-lens`.

`ffrd/pipeline/lens.py` loads a lens and checks it. The pipeline code holds no
lens-specific prompt text. Everything a stage asks the model comes from the lens.

## Sections

A lens has four sections. Each one feeds one stage, and the stages run in this
order: triage, screen, intent, judge.

- `[[triage]]` holds one or more questions. Each question has an `id` and a
  `question`. Triage uses the questions to pick lines. Each `id` becomes the
  reason id that the report gives for a picked line, so pick ids that name the
  reason.
- `[screen]` holds `criteria`, a list of one or more strings. Screen checks each
  candidate line against every criterion.
- `[intent]` holds `rule`, one string. The intent stage applies this rule.
- `[judge]` holds `rubric`, one string. The judge stage scores lines against it.

Every stage's lens text feeds that stage's resume key. The pipeline saves each
stage's results under a key that includes the text of that stage's section.
Editing one section therefore invalidates only the saved results of that stage.

## Validation

`load_lens` checks the whole file and reports every problem it finds in one
error. Each problem is one line in the form `<file>: <field> <problem>`, for
example `lenses/made-up-lens.toml: triage[1].id duplicates triage[0].id`.

- Every `triage` entry needs an `id` and a `question`, and `triage` needs one or
  more tables.
- Each `id` must match lowercase letters and digits joined by single hyphens, such
  as `made-up-id`. Ids must be unique across all of `triage`.
- Every string must be non-empty. A string that holds only whitespace counts as
  empty.
- `screen` needs `criteria`, and `criteria` needs one or more non-empty strings.
- `intent` needs `rule`. `judge` needs `rubric`.
- Unknown keys are errors at every level, including unknown sections.

## Example

This example lens is made up. Its text is not game text. Copy it to start a new
lens, then replace each value.

```toml
# Made-up example lens. Not game text.

[[triage]]
id = "made-up-first-id"
question = "Made-up question one. Does the line mention a made-up thing?"

[[triage]]
id = "made-up-second-id"
question = "Made-up question two. Is the line a made-up greeting?"

[screen]
criteria = ["Made-up criterion one.", "Made-up criterion two."]

[intent]
rule = "Made-up intent rule."

[judge]
rubric = "Made-up judge rubric."
```
