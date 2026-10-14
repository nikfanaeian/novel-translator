"""Self-checks for the parts where a silent bug would ruin a novel: chunking,
natural sort, output naming, encodings, and the resume/failsafe state machine.

Runs on the stdlib alone: `python test_core.py`. The Gemini call itself is not
mocked out; the key-manager cooldown logic is checked directly instead.
"""
import tempfile
from pathlib import Path

import core


def test_natural_sort():
    names = ["ch10.txt", "ch2.txt", "ch1.txt", "chapter_3.txt"]
    ordered = sorted(names, key=core.natural_key)
    # digit runs compare numerically; "ch2" < "chapter_3" because "2" < "a"
    assert ordered == ["ch1.txt", "ch2.txt", "ch10.txt", "chapter_3.txt"], ordered
    assert sorted(["ep100.txt", "ep20.txt"], key=core.natural_key) == ["ep20.txt", "ep100.txt"]


def test_discovery_skips_translated_and_sorts():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ["ep2.txt", "ep10.txt", "ep1.txt", "ep1_fa.txt", "cover.jpg", "ep3_fa.txt"]:
            (root / name).write_text("x", encoding="utf-8")
        found = [p.name for p in core.discover_episodes(root)]
        assert found == ["ep1.txt", "ep2.txt", "ep10.txt"], found


def test_output_naming():
    assert core.output_path(Path("a/ep1.docx"), "fa").name == "ep1_fa.docx"
    assert core.output_path(Path("a/ep1.txt"), "fa").name == "ep1_fa.txt"
    # PDF is text-extracted, so its translation lands beside it as .txt
    assert core.output_path(Path("a/ep1.pdf"), "fa").name == "ep1_fa.txt"


def test_chunking_respects_budget_and_keeps_paragraphs():
    text = "\n\n".join(["p" * 900] * 10)  # 10 paragraphs of 900 chars
    chunks = core.chunk_text(text, max_chars=2000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 2000, len(chunk)
        assert "p" * 900 in chunk  # no paragraph was cut mid-way
    # no content lost across chunk boundaries
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


def test_chunking_splits_run_on_paragraph_on_sentences():
    long_para = ". ".join(["word"] * 800) + "."
    chunks = core.chunk_text(long_para, max_chars=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)


def test_chunking_empty():
    assert core.chunk_text("   \n\n  ") == []


def test_utf16_and_cp1256_txt():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "u16.txt").write_bytes("سلام".encode("utf-16"))
        (root / "u8.txt").write_text("hello", encoding="utf-8-sig")
        (root / "a1256.txt").write_bytes("كتاب".encode("cp1256"))
        assert core.extract_text(root / "u16.txt") == "سلام"
        assert core.extract_text(root / "u8.txt") == "hello"
        assert core.extract_text(root / "a1256.txt") == "كتاب"


def test_state_marks_existing_output_done():
    """Resume: an output file on disk means that episode is already translated."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ep1.txt").write_text("source", encoding="utf-8")
        (root / "ep1_fa.txt").write_text("ترجمه", encoding="utf-8")
        (root / "ep2.txt").write_text("source", encoding="utf-8")
        state = core.JobState.load(root)
        state.sync(core.discover_episodes(root))
        assert state.episodes["ep1.txt"].status == core.DONE
        assert state.episodes["ep2.txt"].status == core.PENDING
        assert state.pending() == ["ep2.txt"]


def test_state_roundtrip_and_retry():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ep1.txt").write_text("s", encoding="utf-8")
        state = core.JobState.load(root)
        state.sync(core.discover_episodes(root))
        state.episodes["ep1.txt"] = core.EpisodeState("ep1.txt", core.FAILED, "429 boom", 1)
        state.save()

        reloaded = core.JobState.load(root)
        assert reloaded.episodes["ep1.txt"].status == core.FAILED
        assert reloaded.episodes["ep1.txt"].error == "429 boom"
        assert reloaded.failed() == ["ep1.txt"]
        reloaded.reset_failed()
        assert reloaded.pending() == ["ep1.txt"]


def test_state_survives_vanished_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ep1.txt").write_text("s", encoding="utf-8")
        state = core.JobState.load(root)
        state.sync(core.discover_episodes(root))
        state.save()
        (root / "ep1.txt").unlink()
        again = core.JobState.load(root)
        again.sync(core.discover_episodes(root))
        assert "ep1.txt" not in again.episodes


def test_corrupt_state_file_is_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".noveltranslator_state.json").write_text("{not json", encoding="utf-8")
        state = core.JobState.load(root)  # must not raise
        assert state.episodes == {}


def test_rate_limit_detection_and_cooldown():
    class Err(Exception):
        code = 429

    class Bad(Exception):
        code = 400

    assert core._is_rate_limit(Err()) is True
    assert core._is_retryable(Err()) is True
    assert core._is_retryable(Bad()) is False  # 400 = bad request, don't burn retries

    translator = core.Translator(core.TranslatorConfig(api_keys=["k1", "k2"]))
    translator._active = ["k1", "k2"]
    import time as _time

    translator._cooldown_until["k1"] = _time.monotonic() + 60
    assert translator._ready_keys() == ["k2"]


def test_translate_episode_writes_atomically_and_keeps_parts():
    """A stub translator stands in for Gemini so this stays offline-testable."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "ep1.txt"
        src.write_text("\n\n".join(["Sentence one. " * 200] * 4), encoding="utf-8")
        dest = core.output_path(src, "fa")

        class Stub:
            config = core.TranslatorConfig(api_keys=["x"], chunk_delay=0)

            def translate_chunk(self, text, lang_tag, history=""):
                return "ترجمه " + text[:20]

        written = core.translate_episode(src, dest, Stub(), "fa")
        assert written > 0
        assert dest.exists()
        assert not list(root.glob("*.part"))  # temp file cleaned up by the rename
        assert dest.read_text(encoding="utf-8").startswith("ترجمه")


def test_translate_episode_on_failure_leaves_no_output():
    """Critical failsafe property: a failed episode must not leave a partial file
    that a later run would see and mark DONE."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "ep1.txt"
        src.write_text("some text here", encoding="utf-8")
        dest = core.output_path(src, "fa")

        class Failing:
            config = core.TranslatorConfig(api_keys=["x"], chunk_delay=0)

            def translate_chunk(self, text, lang_tag, history=""):
                raise core.TranslationError("boom")

        try:
            core.translate_episode(src, dest, Failing(), "fa")
            assert False, "should have raised"
        except core.TranslationError:
            pass
        assert not dest.exists(), "failed episode must not be marked translated"


def test_worker_survives_a_bad_episode_and_stays_resumable():
    """End-to-end on the job loop: one failing episode must not stop the others,
    must leave no output file, and must stay in the retry set.

    Skipped without PySide6 (the worker is a QObject). core.py itself stays stdlib-only.
    """
    try:
        import app  # noqa: PLC0415
    except ImportError:
        print("(skipped test_worker_survives_a_bad_episode_and_stays_resumable: no PySide6)")
        return

    class Stub:
        config = core.TranslatorConfig(api_keys=["k"], chunk_delay=0)

        def __init__(self, *a, **k):
            pass

        def translate_chunk(self, text, lang_tag, history=""):
            if "FAILME" in text:
                raise core.TranslationError("boom")
            return "ترجمه " + text[:15]

    original, app.Translator = app.Translator, lambda *a, **k: Stub()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in (1, 2, 3):
                marker = "FAILME " if i == 2 else "Sentence here. "
                (root / f"ep{i}.txt").write_text(
                    "\n\n".join([marker] * 80), encoding="utf-8"
                )
            state = core.JobState.load(root)
            state.sync(core.discover_episodes(root))
            worker = app.TranslateWorker(
                [root / "ep1.txt", root / "ep2.txt"], "fa", Stub.config, state
            )
            done, failed = [], []
            worker.episode_done.connect(lambda n, c: done.append(n))
            worker.episode_failed.connect(lambda n, e: failed.append(n))
            worker.run()  # synchronous: no QThread event loop needed for the signal slots

            assert done == ["ep1.txt"], done
            assert failed == ["ep2.txt"], failed
            assert (root / "ep1_fa.txt").exists()
            assert not (root / "ep2_fa.txt").exists(), "failed episode left an output"
            reloaded = core.JobState.load(root)
            assert sorted(reloaded.pending()) == ["ep2.txt", "ep3.txt"], reloaded.pending()
            assert reloaded.failed() == ["ep2.txt"]
    finally:
        app.Translator = original


def test_normalize_proxy_shorthand():
    assert core.normalize_proxy("") is None
    assert core.normalize_proxy(None) is None
    assert core.normalize_proxy("   ") is None
    assert core.normalize_proxy("127.0.0.1:10808") == "socks5://127.0.0.1:10808"
    assert core.normalize_proxy("localhost:10808") == "socks5://localhost:10808"
    # a remote host must resolve DNS through the proxy
    assert core.normalize_proxy("proxy.example.com:1080") == "socks5h://proxy.example.com:1080"
    # an explicit scheme is left alone
    assert core.normalize_proxy("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert core.normalize_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"


def test_proxy_defaults_to_local_socks5():
    assert core.DEFAULT_PROXY == "socks5://127.0.0.1:10808"
    assert core.TranslatorConfig(api_keys=["k"]).proxy == core.DEFAULT_PROXY
    # empty string disables the proxy entirely
    assert core.normalize_proxy(core.TranslatorConfig(api_keys=["k"], proxy="").proxy) is None


def test_translator_reuses_one_client_per_key():
    """Guards the connection-pool fix: clients must be cached, not rebuilt per chunk."""
    import sys, types as _types

    fake = _types.ModuleType("google.genai")
    made = []

    class FakeClient:
        def __init__(self, api_key=None, **kwargs):
            self.api_key = api_key
            self.kwargs = kwargs
            made.append(api_key)

    fake.Client = FakeClient
    google_mod = _types.ModuleType("google")
    google_mod.genai = fake
    genai_mod = _types.ModuleType("google.genai")
    genai_mod.types = core_types_stub()
    genai_mod.Client = FakeClient
    fake.types = genai_mod.types
    google_mod.genai = genai_mod

    old = {k: sys.modules.get(k) for k in ("google", "google.genai")}
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    try:
        t = core.Translator(core.TranslatorConfig(api_keys=["k1", "k2"], proxy=""))
        t._client_for("k1")
        t._client_for("k1")
        t._client_for("k2")
        assert made == ["k1", "k2"], f"clients rebuilt instead of cached: {made}"
        assert t._client_for("k1") is t._client_for("k1")
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def core_types_stub():
    """Minimal stand-in for google.genai.types so the client-cache test stays offline."""
    import types as _types

    mod = _types.ModuleType("genai.types")

    class GenerateContentConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class HttpOptions:
        def __init__(self, **kw):
            self.client_args = kw.get("client_args", {})

    mod.GenerateContentConfig = GenerateContentConfig
    mod.HttpOptions = HttpOptions
    return mod


def test_proxy_transport_is_built_and_cached():
    try:
        import httpx
        import socksio  # noqa: F401
    except ImportError:
        print("(skipped test_proxy_transport_is_built_and_cached: httpx[socks] not installed)")
        return
    t = core.Translator(core.TranslatorConfig(api_keys=["k"], proxy="socks5://127.0.0.1:10808"))
    transport = t._proxy_transport()
    assert transport is not None, "proxy transport should be built when a proxy is set"
    assert t._proxy_transport() is transport, "transport must be built once and reused"
    # empty proxy -> None, meaning "go direct"
    direct = core.Translator(core.TranslatorConfig(api_keys=["k"], proxy=""))
    assert direct._proxy_transport() is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)