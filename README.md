# NovelTranslator

Windows desktop app that translates a folder of novel episodes into Persian (or 11
other languages) using the Gemini API — one episode at a time, resumable, with a
progress bar and a log file.

Built for long-running jobs: hundreds of episodes, free-tier rate limits, and the
occasional network drop. Nothing is lost when something goes wrong.

## What it does

- **Browse** any folder; picks up `.docx`, `.pdf`, and `.txt` episodes.
- **Translates one episode at a time**, in natural order (`ep2` before `ep10`).
- **Model list loaded live from the Gemini API** — filtered to chat-capable models
  (embedding/image/TTS are excluded), refreshed on demand, never left empty.
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

## Auto-update

The app polls `nikfanaeian/novel-translator` for new releases. The repo field is
editable if you fork or self-host.

- Checked once a day at startup, or on demand via **Check for updates**.
- A newer version prompts to download; progress shows in the status bar.
- Downloads land beside the app as `NovelTranslator.exe`, written as `.new` first so
  a half-finished download can never look like a valid install.

**Auto-install only works for the packaged `.exe`.** Windows locks a running
executable, so the app cannot replace itself. Instead a small helper waits for the
process to exit and swaps the file in, then you restart. Running from source
(`python app.py`) the update is downloaded but you install it with `git pull`.

To publish an update: bump `VERSION` in `updater.py`, then

```bash
git tag v0.2.0 && git push --tags
```

Actions builds the `.exe` and attaches it to the release; the app picks it up.

## Network / proxy

If your network blocks `generativelanguage.googleapis.com` directly, route through a
SOCKS5 proxy. **The proxy field defaults to `socks5://127.0.0.1:10808`** and the
checkbox is on by default.

- Local proxies get `socks5://`, remote hosts get `socks5h://` so DNS resolves
  through the proxy too. Bare `host:port` input is accepted and normalized.
- Untick the checkbox (or clear the field) to connect directly.
- The setting is saved in `settings.json`.

Requires the SOCKS extra — already in `requirements.txt`:

```bash
pip install "httpx[socks]"
```

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

29 checks. Covers chunking (budget respected, no paragraph split mid-way, no content
lost), natural sort, output naming, txt encodings (UTF-8/UTF-16/cp1256), proxy
normalization and transport caching, model-list filtering and fallbacks, update version
comparison and the self-replace swap helper, and the state machine including "a failed
episode leaves no output file".

Everything except the GUI-worker and proxy-transport checks runs on the stdlib alone.

## Settings

Stored in `%APPDATA%/noveltranslator/`: `keys.json` (your API keys),
`settings.json` (last folder, model, language), `noveltranslator.log`.

Keys are stored in plaintext. Fine for a personal machine; if you share the computer,
don't leave keys saved.