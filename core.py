"""Novel translation engine: extraction, chunking, Gemini translation, failsafe state.

Deliberately GUI-free so it can be tested headlessly.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable

# ---------------------------------------------------------------- discovery

SUFFIXES = {".docx", ".doc", ".pdf", ".txt"}
SKIP_RE = re.compile(r"_([a-z]{2,3}(?:-[A-Za-z]{2,4})?)$")  # trailing _fa, _en, _en-US

# tqdm's natural-order idea, compressed: split digits out and sort numerically so
# ch2 sorts before ch10.
_NUM_RE = re.compile(r"(\d+)")


def natural_key(name: str) -> list:
    """Sort key where digit runs compare as numbers, so ch2 < ch10.

    The extension is split off first, otherwise "2.txt" is one non-numeric run and
    the comparison silently degrades back to plain lexicographic order.
    Every element is a 3-tuple of matching types, because a name like "ch1" vs
    "ch_a" would otherwise compare int to str at index 1 and raise TypeError.
    """
    stem, dot, suffix = name.rpartition(".")
    stem = stem if dot else name
    return [
        (1, int(part), "") if part.isdigit() else (0, 0, part.lower())
        for part in _NUM_RE.split(stem)
        if part
    ]


def discover_episodes(folder: str | os.PathLike) -> list[Path]:
    """Source episodes in a folder, in reading order, skipping already-translated files."""
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(root)
    found = [
        p
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in SUFFIXES and not SKIP_RE.search(p.stem)
    ]
    return sorted(found, key=lambda p: natural_key(p.name))


def output_path(src: Path, lang_tag: str) -> Path:
    """episode_01.docx -> episode_01_fa.docx  (or .txt when source was a PDF/DOCX)."""
    suffix = ".txt" if src.suffix.lower() == ".pdf" else src.suffix
    return src.with_name(f"{src.stem}_{lang_tag}{suffix}")


# ---------------------------------------------------------------- extraction


def extract_text(path: Path) -> str:
    """Plain text from .txt/.pdf/.docx. Raises on unreadable input."""
    ext = path.suffix.lower()
    if ext == ".txt":
        return _read_txt(path)
    if ext == ".pdf":
        return _read_pdf(path)
    if ext in {".docx", ".doc"}:
        return _read_docx(path)
    raise ValueError(f"unsupported file type: {ext}")


def _read_txt(path: Path) -> str:
    # Novel rips are frequently UTF-16 or Windows-1256; try the likely ones, newest
    # write wins so UTF-8 beats a BOM sniff that succeeds with garbage.
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _read_docx(path: Path) -> str:
    import docx  # python-docx

    document = docx.Document(str(path))
    return "\n\n".join(p.text for p in document.paragraphs)


# ---------------------------------------------------------------- chunking


def chunk_text(text: str, max_chars: int = 6000) -> list[str]:
    """Split on blank lines, greedily pack paragraphs up to max_chars.

    Never cuts mid-paragraph unless a single paragraph exceeds the budget, which
    only happens for run-on text. Splitting that on sentence ends keeps output sane.
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # Expand any oversized paragraph into sentence-sized pieces up front, then pack
    # everything uniformly. Doing it in one pass would mix str and list in `buffer`.
    pieces: list[str] = []
    for para in paragraphs:
        pieces.extend([para] if len(para) <= max_chars else _split_long(para, max_chars))
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        candidate = f"{buffer}\n\n{piece}" if buffer else piece
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        buffer = piece
    if buffer:
        chunks.append(buffer)
    return chunks


def _split_long(para: str, max_chars: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?؟।])\s+", para)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
        while len(sentence) > max_chars:  # pathological: no sentence breaks at all
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces


# ---------------------------------------------------------------- state

PENDING, DONE, FAILED = "pending", "done", "failed"


@dataclass
class EpisodeState:
    src: str
    status: str = PENDING
    error: str = ""
    attempts: int = 0


@dataclass
class JobState:
    """Failsafe record. Written atomically after every episode so a crash or
    kill mid-run never loses which episodes succeeded."""

    folder: str
    lang_tag: str = "fa"
    episodes: dict[str, EpisodeState] = field(default_factory=dict)

    @staticmethod
    def load(folder: str | os.PathLike) -> "JobState":
        path = state_path(folder)
        if not path.exists():
            return JobState(folder=str(folder))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return JobState(folder=str(folder))  # corrupt state: start clean, not crash
        episodes = {
            k: EpisodeState(**v) if isinstance(v, dict) else EpisodeState(src=k)
            for k, v in data.get("episodes", {}).items()
        }
        return JobState(
            folder=str(folder),
            lang_tag=data.get("lang_tag", "fa"),
            episodes=episodes,
        )

    def save(self) -> None:
        path = state_path(self.folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "folder": self.folder,
                    "lang_tag": self.lang_tag,
                    "episodes": {k: asdict(v) for k, v in self.episodes.items()},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)  # atomic on Windows and POSIX

    def sync(self, files: Iterable[Path]) -> None:
        """Drop vanished sources; mark already-written outputs done so reruns skip."""
        names = {p.name for p in files}
        for gone in [k for k in self.episodes if k not in names]:
            del self.episodes[gone]
        for path in files:
            entry = self.episodes.setdefault(path.name, EpisodeState(src=path.name))
            if entry.status != DONE and output_path(path, self.lang_tag).exists():
                entry.status = DONE

    def pending(self) -> list[str]:
        return [k for k, v in self.episodes.items() if v.status != DONE]

    def failed(self) -> list[str]:
        return [k for k, v in self.episodes.items() if v.status == FAILED]

    def reset_failed(self) -> None:
        for entry in self.episodes.values():
            if entry.status == FAILED:
                entry.status, entry.error = PENDING, ""
        self.save()


def state_path(folder: str | os.PathLike) -> Path:
    # Kept inside the folder so a job resumes when the user re-picks that folder.
    return Path(folder) / ".noveltranslator_state.json"


DEFAULT_PROXY = "socks5://127.0.0.1:10808"


def normalize_proxy(value: str | None) -> str | None:
    """Accept 'host:port' shorthand; return None to mean 'no proxy'.

    'socks5h://' resolves DNS through the proxy, which is what you want when the
    host can't be reached directly but the proxy can resolve it for you.
    """
    value = (value or "").strip()
    if not value:
        return None
    if "://" in value:
        return value
    if value.lower().startswith(("127.0.0.1", "localhost", "::1")):
        # Local proxy, so hostname-vs-IP resolution barely matters; keep it plain.
        return f"socks5://{value}"
    return f"socks5h://{value}"


# ---------------------------------------------------------------- models

# Used when the API call fails or no key is present yet, so the app is never
# left with an empty model picker.
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]


def list_models(
    api_key: str, proxy: str | None = DEFAULT_PROXY, log: Callable[[str], None] = print
) -> list[str]:
    """Model IDs available for text generation, best-first.

    Filters on supported_actions rather than name, because the API also returns
    embedding, image, and TTS models that cannot translate anything. Falls back to
    a known-good list so an offline or unauthorised key never breaks startup.
    """
    if not api_key.strip():
        return list(FALLBACK_MODELS)
    try:
        import httpx
        from google import genai
        from google.genai import types

        kwargs = {}
        transport = None
        if normalize_proxy(proxy):
            try:
                transport = httpx.HTTPTransport(proxy=normalize_proxy(proxy))
                kwargs["http_options"] = types.HttpOptions(client_args={"transport": transport})
            except ImportError:
                log("socks support missing (pip install 'httpx[socks]'), connecting direct")
        client = genai.Client(api_key=api_key.strip(), **kwargs)

        found: list[str] = []
        for model in client.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if "generateContent" not in actions:
                continue
            name = model.name or ""
            model_id = name.split("/")[-1]  # API returns "models/gemini-2.5-flash"
            # Skip legacy/retired aliases and anything not chat-capable.
            if not model_id.startswith("gemini"):
                continue
            if any(tag in model_id for tag in ("embedding", "imagen", "tts", "aqa", "vision")):
                continue
            found.append(model_id)

        if not found:
            log("no text-generation models returned; using defaults")
            return list(FALLBACK_MODELS)
        log(f"found {len(found)} model(s) available")
        return sorted(set(found))
    except Exception as exc:  # noqa: BLE001 - network/auth errors must not block the GUI
        log(f"could not fetch model list ({_short(exc)}); using defaults")
        return list(FALLBACK_MODELS)


def describe_model(model_id: str) -> str:
    """Short human label for the picker. Version suffixes are noise; keep the base."""
    return model_id.split("/")[-1]


# ---------------------------------------------------------------- translation

# Default target languages the UI offers; Persian is first because that's the
# common case for this app.
LANGUAGES = [
    ("fa", "فارسی (Persian)"),
    ("en", "English"),
    ("ar", "العربية (Arabic)"),
    ("tr", "Türkçe (Turkish)"),
    ("ur", "اردو (Urdu)"),
    ("fr", "Français (French)"),
    ("de", "Deutsch (German)"),
    ("es", "Español (Spanish)"),
    ("ru", "Русский (Russian)"),
    ("zh", "中文 (Chinese)"),
    ("ja", "日本語 (Japanese)"),
    ("ko", "한국어 (Korean)"),
]

LANGUAGE_NAMES = dict(LANGUAGES)

SYSTEM_PROMPT = """\
You are a professional literary translator. Translate the user's novel episode \
into {language} ({tag}).

Rules:
- Translate prose, dialogue, and narration faithfully and idiomatically. Produce \
finished literary prose, not a literal gloss.
- Preserve the author's voice, tense, and point of view.
- Keep proper nouns, honorifics, and coined terms consistent across chunks.
- Leave any text already in {language} untouched.
- Preserve punctuation style, paragraph breaks, and dialogue markers ("--", em \
dashes). Output ONLY the translation, with no preamble, notes, or commentary.
"""


class TranslationError(RuntimeError):
    """A single chunk failed after exhausting keys and retries."""


@dataclass
class TranslatorConfig:
    api_keys: list[str]
    model: str = "gemini-2.5-flash"
    temperature: float = 0.3
    max_output_tokens: int = 8192
    max_retries: int = 4
    chunk_delay: float = 0.35  # polite pacing; free-tier RPM is the usual wall
    proxy: str | None = DEFAULT_PROXY  # empty string disables


class Translator:
    """Round-robins across keys; on a rate-limit error marks that key cooling down
    and tries the next one, so one exhausted key never stalls the whole run."""

    def __init__(self, config: TranslatorConfig, log: Callable[[str], None] = print):
        self.config = config
        self.log = log
        self._cooldown_until: dict[str, float] = {}
        self._active: list[str] = []
        self._clients: dict[str, object] = {}  # one connection pool per key, reused
        self._transport = None

    def _ready_keys(self) -> list[str]:
        now = time.monotonic()
        return [k for k in self._active if self._cooldown_until.get(k, 0) <= now]

    def _proxy_transport(self):
        """httpx transport bound to the configured SOCKS proxy, built once.

        google-genai passes client_args straight into httpx.Client(**client_args),
        so the proxy has to arrive as a pre-built transport — an httpx.Client
        accepts proxy= but SyncHttpxClient does not forward it to a usable mount.
        """
        if self._transport is not None:
            return self._transport
        proxy = normalize_proxy(self.config.proxy)
        if not proxy:
            return None
        import httpx

        try:
            self._transport = httpx.HTTPTransport(proxy=proxy)
        except ImportError as exc:  # socksio missing
            self.log(f"proxy {proxy} unavailable (pip install 'httpx[socks]'): {exc}")
            return None
        self.log(f"using SOCKS proxy: {proxy}")
        return self._transport

    def _client_for(self, key: str):
        """One client (and connection pool) per key, reused across chunks and retries."""
        from google import genai
        from google.genai import types

        if key not in self._clients:
            kwargs = {}
            transport = self._proxy_transport()
            if transport is not None:
                kwargs["http_options"] = types.HttpOptions(client_args={"transport": transport})
            self._clients[key] = genai.Client(api_key=key, **kwargs)
        return self._clients[key]

    def translate_chunk(self, text: str, lang_tag: str, history: str = "") -> str:
        from google.genai import types

        if not self._active:
            self._active = list(self.config.api_keys)
        keys = self._ready_keys()
        if not keys:  # everything cooling down: wait out the shortest one
            wait = min(self._cooldown_until.values()) - time.monotonic()
            self.log(f"all API keys cooling down, waiting {max(wait, 1):.0f}s")
            time.sleep(max(wait, 1))
            keys = self._ready_keys()

        contents = text
        if history:
            # Names and style from the previous chunk keep the story coherent.
            contents = (
                "Previous translated chunk, for terminology and tone continuity only "
                "(do not re-translate or repeat it):\n"
                f"---\n{history}\n---\n\nCurrent chunk:\n{text}"
            )
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT.format(
                language=LANGUAGE_NAMES.get(lang_tag, lang_tag), tag=lang_tag
            ),
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
        )

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            key = keys[attempt % len(keys)]
            try:
                client = self._client_for(key)
                response = client.models.generate_content(
                    model=self.config.model, contents=contents, config=config
                )
                translated = (response.text or "").strip()
                if not translated:
                    raise TranslationError("model returned an empty response")
                return translated
            except Exception as exc:  # noqa: BLE001 - SDK raises many typed errors
                last_error = exc
                retryable = _is_retryable(exc)
                self.log(f"  chunk attempt {attempt + 1}/{self.config.max_retries} "
                         f"failed: {_short(exc)}")
                if not retryable:
                    break
                if _is_rate_limit(exc):
                    self._cooldown_until[key] = time.monotonic() + 60 * (attempt + 1)
                time.sleep(min(2**attempt * 3, 30))
        raise TranslationError(_short(last_error) if last_error else "unknown error")


def _status_code(exc: Exception) -> int | None:
    """google-genai errors expose an HTTP status on .code; be tolerant of both shapes."""
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def _is_rate_limit(exc: Exception) -> bool:
    return _status_code(exc) == 429


def _is_retryable(exc: Exception) -> bool:
    code = _status_code(exc)
    if code is not None:
        return code in {408, 429, 500, 502, 503, 504}
    return not isinstance(exc, TranslationError)  # empty response: worth one retry


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200]


def translate_episode(
    src: Path,
    dest: Path,
    translator: Translator,
    lang_tag: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Translate one episode file. Writes atomically so an interrupted write never
    leaves a half-translated file that a later run would mistake for complete.

    Returns character count written.
    """
    chunks = chunk_text(extract_text(src))
    if not chunks:
        dest.write_text("", encoding="utf-8")
        return 0
    pieces: list[str] = []
    previous = ""
    for index, chunk in enumerate(chunks):
        pieces.append(translator.translate_chunk(chunk, lang_tag, previous))
        previous = pieces[-1][:1500]  # bounded history keeps the prompt cheap
        if on_progress:
            on_progress(index + 1, len(chunks))
        if translator.config.chunk_delay:
            time.sleep(translator.config.chunk_delay)
    text = "\n\n".join(pieces)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    return len(text)