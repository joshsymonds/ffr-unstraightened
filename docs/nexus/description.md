# FFRS Unstraightened

An English dialogue mod for FINAL FANTASY RESONANCE that makes its world a little less straight. It rewrites about 60 lines in two passes:

- Unstraightening drops presumed straightness and compulsory-romance framing, keeping the story, relationships and characters as written.
- Queering adds queer people to the world. A few minor characters' romances become same-sex, and some townsfolk mention a same-sex or nonbinary partner or are trans. One party member's running flirtation now reaches men as well as women. The main cast's romances are unchanged.

Content tag: AI-Generated Content

Disclosure: the rewrites were made with AI models trained on the characters' voices.

Game build: $build_id
Version: $tag

## Install

Copy FFRS-Unstraightened_P.pak, FFRS-Unstraightened_P.utoc and FFRS-Unstraightened_P.ucas into FFRS/Content/Paks/~mods in the game folder.

To check it is working, look at the copyright line on the title screen: it ends with "Unstraightened $tag".

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
