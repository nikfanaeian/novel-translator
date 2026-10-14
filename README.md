# NovelTranslator

Windows desktop app that translates a folder of novel episodes into Persian (or 11
other languages) using the Gemini API — one episode at a time, resumable, with a
progress bar and a log file.

Built for long-running jobs: hundreds of episodes, free-tier rate limits, and the
occasional network drop. Nothing is lost when something goes wrong.

## What it does

- **Browse** any folder; picks up `.docx`, `.pdf`, and `.txt` episodes.
- **Translates one episode at a time**, in natural order (`ep2` before `ep10`).
- **Multiple API keys**, rotated automatically. One key hitting its rate limit puts
  that key on cooldown instead of stalling the run.
- **Progress bar** per episode (chunk-level) and for the job as a whole.
- **Log file** you choose, rotating at 2 MB, plus an in-app console.
- **Failsafe** — a failed episode is recorded and retried with one click. Crash or
  kill the app mid-run and it picks up exactly where it left off.
- **Output**: `episode_01.docx` → `episode_01_fa.docx`. PDFs become
  `episode_01_fa.txt` (PDFs are text-extracted, so the translation is plain text).

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Get an API key at <https://aistudio.google.com/apikey> and add it in the GUI. You can
add several.

## Download the .exe

Push a `v*` tag and GitHub Actions builds `NovelTranslator.exe` on `windows-latest`,
attaches it to the release, and skips it if the self-checks fail.

```bash
git tag v0.1.0 && git push --tags
```

Or grab the artifact from any Actions run without tagging.

## How the failsafe works

State lives in `.noveltranslator_state.json` inside the novel folder:

- Written **atomically after every episode** — a kill mid-write can't corrupt it.
- An episode counts as done if its output file exists on disk, so deleting the state
  file is harmless; the app re-derives it by scanning.
- Outputs are written to a `.part` file and renamed only on success. **A failed
  episode never leaves a partial file that a later run would mistake for complete.**

## Translation approach

Each episode is split on blank lines into ~6000-character chunks and translated
sequentially, with a slice of the previous chunk's output as context for consistent
names and tone. Gemini accepts ~1M tokens of input but caps *output* at 8K — asking it
to translate a whole chapter in one call truncates mid-sentence, so chunking is
required, not an optimization.

Add a style note to `SYSTEM_PROMPT` in `core.py` if you want a consistent voice or a
glossary for recurring terms.

## Tests

```bash
python test_core.py
```

14 checks, stdlib only. Covers chunking (budget respected, no paragraph split
mid-way, no content lost), natural sort, output naming, txt encodings (UTF-8/UTF-16/
cp1256), and the state machine including "a failed episode leaves no output file".

## Settings

Stored in `%APPDATA%/noveltranslator/`: `keys.json` (your API keys),
`settings.json` (last folder, model, language), `noveltranslator.log`.

Keys are stored in plaintext. Fine for a personal machine; if you share the computer,
don't leave keys saved.