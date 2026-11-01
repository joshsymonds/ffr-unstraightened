# Lenses

A lens is the set of instructions the pipeline uses to pick and judge lines.
Each lens is one TOML file at `lenses/<name>.toml`. The lens name is the file
name without `.toml`, so `lenses/made-up-lens.toml` loads as `made-up-lens`.

`ffrd/pipeline/lens.py` loads a lens and checks it. The pipeline code holds no
lens-specific prompt text. Each criterion, rule and rubric a stage gives the model
comes from the lens. The code adds only generic instructions, such as the output format.

## Sections

A lens has four sections. Each one feeds the stages that read it. The stages run in
this order: the Sonnet screen with the Jev recall check, Opus intents and drafts, then
the judges.

- `[[triage]]` holds one or more questions. Each question has an `id` and a
  `question`. Jev answers each question for every scene, and the screen names the
  question ids that flag a line. Each `id` is the reason id that a run records and
  the report counts, so pick ids that name the reason. The judge's `fixed` verdict
  checks that a rewrite answers no to each reason question of its line.
- `[screen]` holds `criteria`, a list of one or more strings. Screen checks each
  candidate line against every criterion.
- `[intent]` holds `rule`, one string. Opus applies this rule when it writes intents
  and drafts.
- `[judge]` holds `rubric`, one string. The scene judge, the cross-scene judge, the
  coherence check and the arc judge all read it. The rubric defines what `meaning`
  protects, and it overrides the Japanese line where the two conflict.

Each stage's saved results are keyed by the text of the sections it reads, so an edit
reruns only the stages that read that section. Editing `triage` reruns Jev triage, the
screen, the recheck of each edited question, and the scene and cross-scene judges. Editing `screen`
reruns the screen and its recheck. Editing `intent` reruns the intents and drafts.
Editing `judge` reruns both judges, the coherence check and the arc judge.

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
