# Unstraightening lens

This is Josh's brief for the unstraightening lens, written down so the lens file
can be checked against it. It holds no game text. It names no characters, places
or story events. It says "the game" for the game.

Each rule, refinement, protection and precedence rule below has a label id in
code. The lens file tags its text with the same ids. A check that a label is
missing from a stage fails the lens test.

## Aims

- Keep the story: its plot, relationships and stakes.
- Do not make a queer rewrite.
- Dial back romance that is forced to matter, and earnest melodrama.
- Give women their own center of gravity. Their goals, fears and worth belong to them.
- Characters supporting themselves and each other is never a defect.
- Keep character traits.
- Keep the game's operatic moments.
- Act on every clear rule hit, and change each changed line minimally and fully.
  Josh first chose the fewest lines touched over the brief's "maximum change in
  social texture", then asked for a more aggressive lens: a clear hit is fixed,
  not left vanilla.
- Leave a line alone when fixing it needs a plot change.

## What a line must hit

A line changes only when it hits one of the seven rules below. A rule is a question
the screen asks about each line. Each rule also has a fix. The fix is the only change
allowed.

### u1-presumed-opposite-sex

- Trigger: A character in this scene assumes that another character's partner or
  future partner is of the opposite sex, or treats closeness between two characters
  of the same sex as a joke, an insult or a threat.
- Fix: Remove the presumption. Keep the sentiment. Make no one queer.
- Refinement: `first-person-desire`.

### u2-romance-outranks

- Trigger: A character in this scene says or implies that a romantic feeling matters
  more than a duty, a friendship, their own life, or the fate of other people.
- Fix: Keep the feeling. Size it to the moment.
- Refinements: `earned`, `romance-as-default`.

### u3-devotion-as-vow

- Trigger: A character in this scene describes devotion to a romantic partner as a
  sacred vow, fate, or giving up their own self.
- Fix: Make it a choice between equals, with the same commitment and action.
- Refinement: `romantic-partner-only`.

### u4-stake-through-other

- Trigger: A character in this scene explains their own goal, fear or worth in terms
  of what another specific character thinks of them or needs from them.
- Fix: When the line is about a woman, return her own stake. Her goal, fear or
  worth goes back to her, not through a man.
- The screen applies the woman clause. It receives cast notes with gender.

### u5-rivalry-over-one-person

- Trigger: Two characters in this scene compete or quarrel over the affection or
  attention of one other character.
- Fix: When the tension is women's, keep the tension and make it about them, not
  about a man.
- Refinement: `no-invented-motive`.
- The screen applies the woman clause. It receives cast notes with gender.

### u6-overwrought-declaration

- Trigger: A character in this scene declares romantic love or longing in extreme,
  breathless or life-or-death terms.
- Fix: Use an adult register.
- Refinement: `earned`.

### u7-gender-rule

- Trigger: A line in this scene states what men or women may do, must do, or are like.
- Fix: Make it neutral.
- Refinement: `gender-rule-narrow`. This rule is narrow. It is about the rule a
  line states, such as who may fight, be a man, or like a girl.

## Refinements

- `earned` (u2-romance-outranks, u6-overwrought-declaration). A stance is earned
  when the bible's backward pass records a setup and an escalation beat between
  the same characters before the declaration. A stance is also earned when it is
  the speaker's established trait. An earned stance is not a defect under u2 or u6.
  The judge reads the arc beats for this.
- `first-person-desire` (u1-presumed-opposite-sex). A character's own wish for an
  opposite-sex partner is protected. Another character presuming that partner is
  not protected. The rule targets presumptions imposed on others.
- `romantic-partner-only` (u3-devotion-as-vow). This rule covers devotion to a
  romantic partner or love interest only. Oaths to lords, causes and comrades are
  untouched.
- `no-invented-motive` (u5-rivalry-over-one-person). When the scene offers no other
  motive, the line stays vanilla. It does not gain an invented motive.
- `gender-rule-narrow` (u7-gender-rule). The fix never touches a character who is
  being vulnerable or protective.
- `romance-as-default` (u2-romance-outranks). This widens u2. Treating a character
  who has no partner as incomplete counts. So does treating marriage as what a life
  is for. The same earned and trait exemptions apply.
- `all-characters`. The rules apply to every character's romance framing, not only
  women's.

## Protections

A change that touches a protection is rejected by the judge.

- `protect-love-and-couples`. Heterosexual attraction, couples, marriages and
  promises stay as they are. So does who loves whom.
- `protect-mutual-support`. Characters supporting themselves or each other stay,
  whatever their genders: relying on, helping, encouraging or drawing strength from
  another character. This protection wins over every rule, u4 included.
- `protect-plot`. Plot facts stay as they are.
- `protect-traits`. Character traits stay, including a character's own straight
  desire. A flirt toned down is a defect.
- `protect-operatic-stakes`. The game's operatic stakes that are not about romance stay.
- `protect-earned-declarations`. Earned declarations that the story builds to stay.
- `protect-affectionate-teasing`. Affectionate teasing stays when it does not treat
  same-sex attraction as shameful.
- `exclude-queer-additions`. No new romances, queer additions or flirt retargeting.
  Those belong to a later epic, not this lens.
- `exclude-clumsy-lines`. A line that is clumsy but has no defect is out of scope.

## Precedence

- `act-on-clear-hits`. Change every flagged line that clearly hits a rule. A clear
  hit stays vanilla only when an exemption applies, when it does not really hit the
  rule, when every fix would touch a protection, or under `leave-for-plot`. The
  screen flags mild and brief hits too.
- `minimal-fix`. Change each changed line minimally: only the words the fix needs,
  and never a neighboring line. Each changed line must be fully fixed.
- `leave-for-plot`. A line that needs a plot change stays vanilla.

## How it is checked

- The screen is Sonnet. Jev is a recall check on the screen. For each rule that
  Jev scores 0.5 or more on, and that the screen flagged on no line, Opus rechecks
  the scene for that rule. Lines the recheck flags join the screen's flags.
- The judge reads the arc beats for `earned`.
- When the intent stage keeps a flagged line unchanged, the report counts it as
  untouched. The `writer` gate counts only a writer that returns no usable
  candidate after being asked to change the line.
- The report counts arc-judge and scene-coherence reverts as the `arc` and `scene`
  gate counts.

## What the lens never does

These are Josh's Must Not Ship rules. Each is a hard limit.

- No plot, relationship or stake changes. Josh wants the original story.
- No new romances, queer additions or flirt retargeting. Those belong to a later epic.
- No public push of rewrites by the loop. Josh reads the counts and runs
  `just release`. The loop never publishes rewrites.
- No line text in anything Josh reads before playing.

## Run order

The first live run is Chapter 1 only. Run it with `--through 1`. The run writes the
Chapter 1 counts to `reports/unstraightening-c01.txt`, and Josh reads them before
deciding on the full run. See the Rewrite pipeline section of the
[README](../../README.md#rewrite-pipeline).
