# FFR Unstraightened

FINAL FANTASY RESONANCE is a lovely game with a very heteronormative world -- the townsfolk are married to the opposite sex universally, and the resident flirt is only interested in women. I wanted a world where people are LGBTQ since it feels better for me! Thus, I created this mod. It changes about 60 lines of English dialogue in the game, and leaves the other 16,000 or so untouched.

## Unstraightening

Anywhere the script assumed everyone was straight, or that everyone wanted to be in a relationship, I removed that assumption and left everything else intact -- the plot, the relationships, the characters are all unchanged.

An example of unstraightening, before and after:

<!-- image: ST_Summon680_Titan_02.Summon680_Titan_02_004 before/after -->

## Queering

Some of the minor characters are gay now and some of the townspeople have a same-sex or nonbinary partner -- and a few of them are trans.

Here is an instructive example of a townsperson getting a same-sex partner, before and after:

<!-- image: ST_INN.INN_029 before/after -->

Ditto for a nonbinary partner, before and after:

<!-- image: ST_FEV_Town_09Pha_01.FEV_Town_09Pha_01_001 before/after -->

And an added trans line, before and after:

<!-- image: ST_C01_Town_01Gra_04_01.Town_01Gra_04_01_025 before/after -->

I made sure that all queer elements must be contextualized in the scene. A queer element should only appear in a scene where it could naturally occur -- a conversation between townsfolk, a side quest, a flirtation that was already happening. It should never appear in the middle of a fight, a threat, or a moment of grief. (An early build had Dr. Lazarov mention his husband and their lake house while threatening the party with zombie Grandsheltian troops, which was hilarious, but is now on the cutting-room floor.)

### Jake

Jake, the party's flirt, now flirts with men, women, and everything in-between. The main cast's romances remain the same.

<!-- image: ST_Summon640_Shiva_02.Summon640_Shiva_02_012 before/after -->

## Everything Else

Everything else is unchanged: the plot, the main characters and their relationships, every English line the mod doesn't touch (which is nearly all of them), and every non-English line are the same as they were in the original.

Note that this does NOT change voice lines -- if the conversations are voiced, you will hear the original line but read the altered line.

Content tag: AI-Generated Content. The rewrites were made with AI models trained on the characters' voices, and all code was generated with LLMs.

Game build: $build_id
Version: $tag

## Install

Copy FFRS-Unstraightened_P.pak, FFRS-Unstraightened_P.utoc and FFRS-Unstraightened_P.ucas into FFRS/Content/Paks/~mods in the game folder.

To check it is working, look at the copyright line on the title screen: it ends with "Unstraightened $tag".

To uninstall, delete those three files.

## Compatibility

- Translation and retranslation mods that replace the English text: this mod's lines take priority over theirs, so those lines show this mod's English and every other line shows theirs.
- Mods that replace the title screen can hide the version tag. The rewrites still apply.
- For mod authors: the pak carries FFRS/Config/UserGame.ini, which puts this mod's localization folder ahead of the game's:

```
[Internationalization]
-LocalizationPaths=%GAMEDIR%Content/Localization/Game
+LocalizationPaths=%GAMEDIR%Content/Localization/Unstraightened
+LocalizationPaths=%GAMEDIR%Content/Localization/Game
```

## Source

The tools I used to perform these rewrites, and a count of all the changes, are available at https://github.com/joshsymonds/ffr-unstraightened.
