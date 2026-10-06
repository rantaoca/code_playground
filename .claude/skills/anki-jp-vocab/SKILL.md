---
name: anki-jp-vocab
description: Turn a pasted list of unstructured Japanese vocab into reviewed Anki cards (furigana + short N3 example sentence + image + audio) and push them into the "Japanese Vocab AI Gen" deck via AnkiConnect.
argument-hint: "(optional) paste the vocab list directly, or leave blank and I'll ask for it"
---

## ⚠️ CRITICAL: Work directory constraint

**This skill ONLY runs in `/Users/rantaoca/Documents/code_playground/`, never in `code_playground_local` or any other location.** The venv, state.yaml, images, audio, and all pipeline paths are hardcoded to this specific directory. If you find yourself in a different directory, stop and clarify with the user before proceeding.

---

You are orchestrating the pipeline in `anki-jp-vocab/` (a sibling directory
at the repo root: `app.py`, `index.html` — that's the whole thing, `app.py`
holds all the backend logic: state, image search, ElevenLabs TTS,
AnkiConnect push, and the review HTTP server, plus a CLI you invoke
directly for the generation steps below).

All state lives in `anki-jp-vocab/state.yaml`, alongside `app.py` (with
`images/` and `audio/` as sibling directories). Treat the review UI (a
human, in the browser at http://localhost:8877) as a concurrent editor of
that file — always re-read it immediately before you write to it, never
assume your in-memory copy is still current.

## Environment gotchas — read before running any command

These have all bitten previous runs. They cost real time to re-diagnose, so
just follow them:

- **Verify you are in `/Users/rantaoca/Documents/code_playground/`** before running anything.
  If your working directory is different (e.g. `code_playground_local`), stop and ask the user
  to clarify. All paths and the venv are hardcoded to this location.
- **Always invoke the venv interpreter explicitly**: `anki-jp-vocab/.venv/bin/python3`.
  Bare `python3` is the system/Homebrew Python and has neither `pyyaml` nor
  `requests` — you'll get `ModuleNotFoundError: No module named 'yaml'`. This
  applies to your own ad-hoc YAML-editing scripts too, not just `app.py`.
- **The Bash tool's shell does not read `~/.zshrc`**, so `PIXABAY_API_KEY` /
  `ELEVENLABS_API_KEY` are absent even when they're correctly set in the
  user's profile. Any command that touches those APIs must be prefixed with
  `source ~/.zshrc &&`. Symptom without it: `PIXABAY_API_KEY is not set`
  from `images-search --source pixabay`, even though Step 0's grep found it.
- **Never paste a real API key inline on a command line** as a workaround for
  the above. It leaks the secret into the transcript and will be blocked as
  credential leakage. `source ~/.zshrc` is the only correct fix.
- **Check for an already-running server before starting one** (see Step 3).
  A stale server from a previous session silently owns port 8877 and serves
  its own state, which looks exactly like "my edits to `state.yaml` aren't
  taking effect."
- **There is no `/api/state` endpoint.** Don't probe invented URLs to inspect
  state — read `anki-jp-vocab/state.yaml` directly.

## One-time setup (skip if already done)

```bash
cd anki-jp-vocab
python3 -m venv .venv
.venv/bin/pip install requests pyyaml
```

(A venv is required on macOS Homebrew Python — it refuses global `pip
install`.)

Install the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on
in Anki (code `2055492159`) if not already installed, and make sure **Anki
is open** whenever pushing cards — AnkiConnect only runs inside the live app.

Two API keys are optional, each gated behind an env var checked at
call-time — if a key is missing, that feature just fails gracefully or is
skipped (never block the pipeline on a missing key):

- `PIXABAY_API_KEY` — fallback image source when irasutoya has nothing.
  https://pixabay.com/ (free) → key at https://pixabay.com/api/docs/
- `ELEVENLABS_API_KEY` — TTS for example-sentence audio and word audio.
  https://elevenlabs.io/app/settings/api-keys (needs Voices:Read +
  Text-to-Speech:Write permissions; free tier can't use Voice-Library
  voices via the API, only the `premade`-category voices)

## Step 0 — check API keys are set

At the start of a fresh session (skip this if you already did it earlier in
the same conversation), check which of the two keys above are set:

```bash
grep -E "PIXABAY_API_KEY|ELEVENLABS_API_KEY" ~/.zshrc
```

For any that are missing, tell the user in chat — don't just silently
proceed. For each missing key, give them the exact line to add and where to
get the value, e.g.:

```bash
echo 'export ELEVENLABS_API_KEY=your-key-here' >> ~/.zshrc
```

along with the signup URL from the list above. This is informational, not a
gate — continue to Step 0.5 regardless of what's missing; each feature
degrades gracefully on its own (see Step 2 / 3). If the user adds a key
mid-session, they'll need to tell you so you can restart the review server
(Step 3) with `source ~/.zshrc` re-run so the new env var is picked up — a
key added to `~/.zshrc` doesn't retroactively apply to an already-running
process.

## Step 0.1 — get the vocab list

If the user hasn't already pasted vocab in this conversation, ask them to
paste it now. It may be messy: numbered lists, tab-separated, mixed with
notes, dictionary-form only, whatever — you're parsing it, not them.

## Step 0.5 — confirm the parsed list before generating anything

Parse the paste into a plain numbered list of distinct vocab items (just
the headword as you'll treat it — no furigana/meaning/example yet, those
come in Step 1). Drop obvious non-vocab noise (stray timestamps, "X has
recalled a message", one-off sentence fragments that aren't targets) but
otherwise err toward including a term rather than guessing it's unwanted.

Auto-drop exact duplicates (the identical headword, same kanji/kana,
appearing more than once) without asking — keep the first occurrence. Do
NOT auto-drop near-duplicates (same reading but different kanji/word, e.g.
脂 vs 油; a plain form next to its ～すぎる/conjugated form) — those are
genuinely different vocab items or the user's call, so keep both and just
flag the pairing in your message.

Post that numbered list in chat and ask the user to tell you which numbers
to drop (or confirm the list as-is). Wait for their reply before doing any
card generation, image search, or writing to `state.yaml` — nothing in
Step 1 onward runs until the list is confirmed.

## Step 1 — generate cards

For each distinct vocab item, produce:

- **expression** — the word in kanji/kana, with furigana written as
  `kanji[reading]` immediately after each kanji run. Okurigana / kana stay
  outside the brackets. Multi-part words (e.g. compound verbs) are written
  as space-separated segments, each bracketed on its own kanji run. This
  must match the existing deck's convention exactly, e.g.:
  - `砂漠[さばく]`
  - `追[お]い 払[はら]う`
  - `訳[わけ]ではない`

  **Always put a half-width space immediately before each bracketed kanji
  run** unless it starts the field. Anki applies the reading to everything
  back to the previous space, so without one, preceding kana or punctuation
  gets swallowed under the furigana. This includes runs after kana
  (`朝[あさ]ご 飯[はん]`, `使[つか]い 方[かた]`) and after punctuation like
  brackets or commas (`「 静[しず]か」`, `、 試合[しあい]`). Applies to
  `expression` and `example_jpn` alike. (If the reading deliberately
  covers digits/kana too, e.g. `60年代[ろくじゅうねんだい]`, the space goes
  before that whole span instead.) When checking for violations with a
  regex, the lookbehind must exclude kanji as well as whitespace —
  `(?<=[^\s一-龯々〆ヶ])` — or it splits inside kanji runs.
- **meaning** — concise English gloss (a few words, not a full definition).
- **example_jpn** — one short original sentence using the word, written in
  the same `kanji[reading]` furigana style. Keep it short (one clause where
  possible) and restrict grammar/vocab to **JLPT N3 level or below** — no
  N1/N2 constructions, no rare kanji outside the target word itself.
- **example_en** — natural English translation of that sentence.

Write these into `anki-jp-vocab/state.yaml` as:

```yaml
cards:
  - id: 1
    expression: "追[お]い 払[はら]う"
    meaning: "to drive away, to scatter"
    example_jpn: "彼[かれ]は 犬[いぬ]を 追[お]い 払[はら]った。"
    example_en: "He drove the dog away."
    image_path: null
    image_source: null
    status: pending
    notes: ""
    anki_note_id: null
    audio_word_path: null
    audio_word_source: null
    audio_word_attribution: null
    audio_sentence_path: null
    audio_sentence_source: null
    audio_sentence_attribution: null
  - id: 2
    ...
```

If `state.yaml` already holds an earlier batch, **append** — don't replace
it. Previous cards may still be `pending`/`needs_edit` (unpushed), and
`images/<id>.*` / `audio/<id>_*.mp3` are keyed by id, so restarting at 1
would overwrite their files. Start new ids at `max(existing id) + 1`; if a
new item duplicates an unpushed old card, rewrite that card in place
(clearing its stale audio fields). Pushed cards are hidden in the UI by
default, so leaving them is harmless.

`id` is a simple incrementing integer, unique across the file. The
`audio_*` fields are populated later by the human in the review UI
(Step 3), not by you — leave them null when generating cards.

## Step 2 — pick an initial image per card

For each card, before starting the review server:

1. Run `anki-jp-vocab/.venv/bin/python3 anki-jp-vocab/app.py images-search --query "<keyword>" --source irasutoya --limit 5`.
   - `<keyword>` is the expression stripped of furigana brackets **only if
     it's a single noun/word**. Irasutoya returns zero hits for phrases
     (コードを書く, 風邪を引く, 口の中がぱさぱさ) — search the core concrete
     noun instead (プログラマー, 風邪, パン) or something the example
     sentence depicts (りんご for ～個, カレンダー for 一か月).
   - Irasutoya rate-limits: back-to-back searches intermittently return
     `{"error": "503 Server Error: Service Unavailable"}`. Loop over cards in
     one script with `time.sleep(1.5)` between calls, and retry any 503s.
2. Judge the top couple of results by title text for topical relevance to
   the word/meaning. Skip junk hits whose `image_url` isn't an image (e.g.
   プライバシーポリシー / 免責事項 with a `<!--Can't find substitution...-->`
   URL — returned for grammar-ish queries like について or 場合), and strip a
   stray trailing `.` from URLs ending `.jpg.`. If genuinely unsure, you may fetch a candidate's
   `thumb_url` with curl into a scratch file and view it with Read — irasutoya
   images are simple flat illustrations, cheap to eyeball.
3. If there's a decent match, download it:
   `anki-jp-vocab/.venv/bin/python3 anki-jp-vocab/app.py images-download --url <image_url> --dest "$(pwd)/anki-jp-vocab/images/<id>.jpg"`
   and set that card's `image_path` / `image_source: irasutoya` in the YAML.
   `image_path` must be an **absolute** path — that's what the review UI
   writes when the human swaps an image, and what `push` feeds to
   AnkiConnect. A relative path resolves against the server's own working
   directory and silently drops the image from the pushed note.
4. If nothing relevant turns up (or the site returns zero results), fall
   back to `--source pixabay` — but search using the card's **English
   `meaning`**, not the Japanese expression. Pixabay's index is tagged in
   English and Japanese-text queries mostly return zero results there
   (unlike irasutoya, which is Japanese-tagged and wants the Japanese word).
   The pixabay source needs `PIXABAY_API_KEY` in the environment, which the
   Bash tool does not inherit — prefix the command with `source ~/.zshrc &&`:

   ```bash
   source ~/.zshrc && anki-jp-vocab/.venv/bin/python3 anki-jp-vocab/app.py images-search --query "<english meaning>" --source pixabay --limit 5
   ```

   If the key genuinely isn't set in the profile either, just leave the card
   imageless and let the human pick one in the review UI instead of
   blocking.

This is a best-effort first pass, not a gate — abstract words and grammar
terms (動詞, 主語, さらに…) rarely have a good image; leave them imageless
rather than forcing a weak match. Cards can go into review without an image; the human can search/swap images themselves in the UI.

## Step 3 — start the review server

**First, check whether a server is already running** — a stale one from an
earlier session will hold port 8877 and keep serving its own copy of the
state, so every restart you attempt silently fails to bind and the UI never
reflects your edits:

```bash
ps aux | grep "[a]pp.py"
```

If that turns up a process, kill it by **PID** and confirm it's gone. Do not
rely on a `pkill -f` pattern — the running process's command line is the
absolute venv interpreter path plus `app.py serve --no-browser`, so patterns
like `pkill -f "python3 app.py"` match nothing and leave it alive:

```bash
kill <pid>; sleep 1; ps aux | grep "[a]pp.py" || echo "all stopped"
```

Then start the server in the background, sourcing the shell profile so the
ElevenLabs key is present in its environment (without this, audio generation
in the UI fails):

```bash
cd anki-jp-vocab && source ~/.zshrc && nohup .venv/bin/python3 app.py serve --no-browser > /tmp/anki_review.log 2>&1 &
```

Confirm it's actually up before telling the user — a bind failure is
otherwise invisible:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8877
```

Expect `200`. Then open http://localhost:8877 for the user. Tell them it's open and to approve
cards, edit fields inline, mark cards "needs edit" with a note on what they
want changed, swap/remove images, and/or generate/regenerate/remove word +
example-sentence audio (ElevenLabs, picks up whichever voice is selected in
the header dropdown). A "Generate all audio" button with a progress bar
fills in whatever's still missing across every card using the selected
voice. There's also a "Push approved to Anki" button in the header — pushing to
Anki is theirs to do, not yours; never run `app.py push`. Then come back to chat.

## Step 3.5 — report problems from this run and offer to bank them

Right after you hand the review UI back to the user (and before you settle
into the Step 4 edit loop), take stock of the run so far and post a short
list in chat of every problem you hit getting here — failed commands,
wrong assumptions, denied permissions, dead ends, anything you had to
diagnose and work around. Include the ones you recovered from silently;
those are exactly the ones that cost the next agent time.

For each, state the symptom and the actual cause in one line, e.g.
"`ModuleNotFoundError: No module named 'yaml'` — used system `python3`
instead of the venv interpreter."

If the run was genuinely clean, say so in one line and move on — don't
manufacture findings.

Then ask the user whether they want these written into this skill file
(`.claude/skills/anki-jp-vocab/SKILL.md`) so future runs avoid them. Wait
for their answer; do not edit the skill unprompted. If they say yes:

- Fold each confirmed item into the most relevant existing section —
  **Environment gotchas** for setup/env/tooling traps, or the specific
  numbered step whose instructions were wrong or incomplete.
- Prefer correcting the instruction that misled you over appending another
  warning. If a documented command doesn't work, fix the command.
- Keep it short. This file is read in full on every run; a growing pile of
  war stories makes it worse, not better.

## Step 4 — the chat/edit loop

Repeat until no cards are left `pending` or `needs_edit`:

1. Re-read `anki-jp-vocab/state.yaml`.
2. For every card with `status: needs_edit`, discuss it with the user in
   chat — read their `notes` field as the starting point, ask follow-ups if
   the request is ambiguous. Don't silently guess at a rewrite of a
   sentence/meaning if their note is vague.
3. Once you and the user agree on the change, edit that card's fields
   directly in the YAML, clear `notes`, and set `status: pending` so it
   reappears in the review UI's default (pending/needs-edit only) view.
4. Tell the user which cards you just updated so they know to re-check them
   in the browser (a page refresh picks up the new state).
5. If every card is `approved` (or `pushed` from a previous run), stop
   looping.

Don't re-litigate cards the human already approved without them raising it
again — approved means done.

If anything went wrong *after* the Step 3.5 retrospective — during the edit
loop, or with the push once they report back — raise those the same way
Step 3.5 describes: list them, and ask whether to fold them into this skill.
