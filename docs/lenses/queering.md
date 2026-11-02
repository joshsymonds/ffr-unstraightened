# Queering lens

This is the brief for the queering lens, written down so the lens file can be checked
against it. It holds no game text. It names no characters, places or story events. It says
"the game" and "the flirt" instead of names.

Each rule, split, protection and judge label below has an id in code. The lens file
tags its text with the same ids. A check that a label is missing from a stage fails the
lens test.

## Aims

The lens chains onto the unstraightening rewrites: it starts from their output in `dialog/unstraightening/`, and the build applies the lenses in order.

- Keep the story: its plot, relationships and stakes.
- Keep every line the lens does not flag exactly as written.
- Make a minor character's romance queer only in the two shapes the triage names.
- Split the flirt's running gag, so a flirt aimed at a woman is redirected or generalized.
- Never touch the flirt's sincere thread, or a flirt aimed at a minor.
- Keep the core cast's sexuality and romances as written.
- Change only the words an edit needs, and fully fix each flagged line.

## The marker

The marker finds throwaway romance lines: lines where a minor character talks about their
own partner, attraction or crush. It is a model judgment, not a regex. It reads minor
characters' lines and the flirt's, and it flags them under these labels:

- `minor-partner`: a minor character talks about their own romantic partner, and the
  pairing reads as a cisgender man and a cisgender woman.
- `minor-attraction`: a minor character talks about their own romantic attraction or
  crush, and it is aimed at a cisgender person of the opposite sex.
- `not-core-cast`: the marker never flags a core-cast line, with one exception: the flirt,
  the one speaker whose cast entry carries `"flirt": true`. It flags the flirt's
  running-gag flirts aimed only at a woman or women under `flirt-at-woman`. It never
  flags the flirt's sincere thread, or a flirt aimed at a minor or a child-looking
  character.
- `already-queer`: the marker does not flag a line whose pairing is already same-sex.
  This also covers a speaker, partner or crush who is already trans or nonbinary.

## Buckets

- `queer-bucket`: swap the gender of the partner or crush so the pairing is same-sex.
  Keep everything else.
- `trans-bucket`: by default, make the partner a nonbinary partner who uses they/them.
  Instead, and only where the line naturally allows it, use a light detail that the
  speaker is trans.
  The trans bucket never adds a "she's trans" aside.
- `same-partner`: when a speaker has several bucketed lines, they are drafted together.
  Use the same partner in every line, with one gender, one set of pronouns and the
  same details.
- `minimal-fix`: change only the words the edit needs.

## The flirt

- `flirt-at-woman`: the party member whose running gag is flirting aims a running-gag
  flirt in this scene only at a woman or at women. This is the triage id for the flirt.
- `flirt-redirect`: for a line in the flirt's redirect half, redirect the flirt to a man
  present in the scene. Which of his running-gag lines are in that half is decided before
  drafting, by key hash.
- `generalize-when-reaction`: when the woman's reaction is in the same scene, generalize
  the address instead, for example to all genders.
- `protect-sincere-thread`: never touch the flirt's sincere thread.
- `protect-minor-target`: never touch a flirt aimed at a minor or a child-looking character.
- `protect-core-cast`: do not change the core cast's sexuality or romances beyond the
  flirt's split.

## Judge rubric

The judge reads each rewrite against its original and the label it was drafted under.
It rejects a rewrite that breaks any of these labels.

- `meaning`: meaning protects the bond, not the gender. The same partner, feeling and
  details stay. The partner's or crush's gender, or the flirt's target, changes only as
  the bucket asks.
- `minimal-fix`: rejects a rewrite that changes more than its edit needs.
- `same-partner`: rejects a rewrite that changes the partner's gender, pronouns or
  details from one of the speaker's lines to another.
- `queer-bucket`: rejects a rewrite that changes more than the partner's or crush's
  gender.
- `trans-bucket`: rejects a "she's trans" aside. A light detail that the speaker is trans,
  where the line naturally allows it, is the bucket's edit, not a new or tokenistic detail.
- `flirt-redirect`: rejects a rewrite that redirects the flirt to anyone but a man
  present in the scene, unless `generalize-when-reaction` applies.
- `generalize-when-reaction`: rejects a rewrite that does not generalize the address
  when the woman's reaction is in the same scene.
- `protect-sincere-thread`: rejects a rewrite that touches the flirt's sincere thread.
- `protect-minor-target`: rejects a rewrite that touches a flirt aimed at a minor or a
  child-looking character.
- `protect-core-cast`: rejects a rewrite that changes the core cast's sexuality or
  romances beyond the flirt's split.
- `no-gender-joke-or-menace`: rejects a rewrite whose humor or menace rests on the
  target's or partner's gender or queerness.
- `no-explaining`: rejects a rewrite that explains the change or adds a tokenistic detail.
- `keep-voice`: rejects a rewrite that breaks the speaker's voice.
- `no-new-detail`: rejects a rewrite that adds a name, object or motive the original
  line lacks.
- `no-contradiction`: rejects a rewrite that contradicts the speaker's other lines.
- `protect-plot`: rejects a rewrite that changes a plot fact, or that changes the scene's
  meaning to fit a label.

The intent stage also carries `no-explaining`, `keep-voice`, `no-new-detail`,
`no-contradiction` and `protect-plot`, so each rewrite is drafted to pass its judge.

## What the lens never does

- The queerness is never the joke or the threat.
- The trans bucket never adds a "she's trans" aside.
- The core cast's sexuality and romances stay as written, except the flirt's split.
- It never makes a villain queer-coded.
- It never uses a regex to find lines.
- It never adds a new romance.

## How it is checked

The lens runs under the queering selection: an Opus marker, per-speaker buckets, one draft call per speaker, and no Jev.

The triage questions are used only for counts, with no Jev recheck. The marker reads minor
characters' lines and the flirt's. The lens test, `tests/lenses/test_queering.py`, pins the triage
questions, the label sets of each stage, the key sentences of this page and the sections
of this page. It fails when any of those is altered or removed.
