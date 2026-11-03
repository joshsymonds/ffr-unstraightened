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
- Make a character outside the core cast's romance queer only in the shapes the triage names.
- Split the flirt's running gag, so a flirt aimed at a woman is redirected or generalized.
- Never touch the flirt's sincere thread, or a flirt aimed at a minor.
- Keep the core cast's sexuality and romances as written.
- Change only the words an edit needs, and fully fix each flagged line.

## The marker

The marker finds throwaway romance lines: lines where a character outside the core cast
refers to their own partner or a romantic interest of any sort, or flirts with or pursues
someone. Any character outside the core cast qualifies, a bigger recurring one as much as a
passer-by. It is a model judgment, not a regex. It reads the lines of characters outside the
core cast who are not antagonists, and the flirt's, and it flags them under these labels:

- `romance`: a character outside the core cast refers to their own partner or a romantic
  interest of any sort: a partner, a crush, someone they are attracted to, flirting or
  romantic pursuit.
- `gender-roles`: a line about what men or women are, may do or should do. Tracking only:
  a gender-roles flag never selects a queer line, though its named speaker can be a trans pick.
- `not-core-cast`: the marker never flags a core-cast line, with one exception: the flirt,
  the one speaker whose cast entry carries `"flirt": true`. It flags the flirt's
  running-gag flirts aimed only at a woman or women under `flirt-at-woman`. It never
  flags the flirt's sincere thread, or a flirt aimed at a minor or a child-looking
  character.

## Buckets

- `queer-bucket`: the speaker's romantic target becomes the same sex. Swap the gender of
  the partner or crush so the pairing is same-sex. For a man or woman speaker, the pursuit
  target changes gender too. Where the line can carry both a same-sex and an opposite-sex
  reading, a bi reading is fine. Keep everything else.
- `trans-bucket`: for a trans pick, every line of the speaker is drafted together. Where a
  line naturally allows it, add a light, natural trans detail: a detail that the speaker is
  trans, or, on a romance line, a nonbinary partner who uses they/them. Where it does not,
  keep the line unchanged as no-hit. The detail is never forced. The trans bucket never adds a
  "she's trans" aside.
- `same-partner`: when a speaker has several bucketed lines, they are drafted together.
  Use the same partner in every line, with one gender, one set of pronouns and the
  same details.
- `minimal-fix`: change only the words the edit needs.
- `already-queer`: a line whose pairing is already same-sex stays as written, and so does a
  line whose speaker, partner or crush is already trans or nonbinary. The intent keeps these
  as no-hit.

Selection: the queer and trans picks come from salted seeded orders, and the two are
disjoint. Queer takes romance_share of the romance pool, where the romance pool is the
eligible speakers with a romance line plus each speakerless romance line. Trans takes
trans_share of all eligible characters, the eligible speakers of the run, and the trans
pool is the eligible speakers with a romance or gender-roles line. Speakerless lines are
never trans picks. Eligible means not core cast, not an antagonist, and not the flirt. Each
share is a whole number from 0 to 100, and its target is the share of its denominator rounded
half up: a share of 100 takes the whole pool and a share of 0 draws nothing. A side that ends
short of its target is reported exhausted, which a trans target above its trans pool always is.
Each pool is sorted by a salted SHA-256 of its unit id, and the queer and trans salts keep the
two orders apart. Picks that end with no surviving change are refilled from the order until the
target or the pool runs out. Every romance line of a queer pick converts, drafted together, and
every line of a trans pick is drafted together. A gender-roles mark never puts a unit in the
queer pool.

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
- `queer-bucket`: rejects a rewrite that changes more than the target's gender, or that
  leaves the pairing other than same-sex unless the line carries a bi reading.
- `trans-bucket`: rejects a "she's trans" aside and a forced trans detail. A light, natural
  trans detail, where the line allows it, is the bucket's edit, not a new or tokenistic
  detail.
- `already-queer`: rejects a rewrite that changes a line whose pairing is already same-sex,
  or whose speaker, partner or crush is already trans or nonbinary.
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

The lens runs under the queering selection: an Opus marker, a seeded refill selection of the romance characters as queer and the eligible speakers with a romance or gender-roles line as trans, one draft call per pick, and no Jev.

The triage questions are used only for counts, with no Jev recheck. The marker reads the lines of characters outside the core cast who are not antagonists, and the flirt's. The lens test, `tests/lenses/test_queering.py`, pins the triage
questions, the label sets of each stage, the key sentences of this page and the sections
of this page. It fails when any of those is altered or removed.
