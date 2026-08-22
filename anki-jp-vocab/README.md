# Getting Started (For Humans)

Run this pipeline from Claude Code:

```
/anki-jp-vocab
```

## Summary

Turns messy pasted Japanese vocab into finished Anki cards.

Example input:

```
ひま　free
漬物　つけもの
鳥　とり　bird
```

Example output, for 漬物:

- Expression: 漬物[つけもの]
- Meaning: Japanese pickles
- Example: 漬物[つけもの]は ご飯[はん]に 合[あ]います。
  → Pickles go well with rice.
- Image: 🖼️ (illustration of pickles)
- Audio: 🔊 word, 🔊 sentence

How it works:

1. Claude drafts the cards
2. You review/edit/approve them in a browser UI (`localhost:8877`)
3. Approved cards get pushed straight into Anki via AnkiConnect


## Human Workflow

For Shirabe iOS App:
1. Star new words to the latest anki list
2. When ready, export list as txt, copy to keep to send to computer
3. Rename old list with date of export, then create a new anki list
4. On computer, run this skill for that list