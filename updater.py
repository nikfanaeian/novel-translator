"""GitHub release auto-update.

Why this is split in two: Windows holds a lock on a running .exe, so the app
cannot replace itself while it is open. The download lands next to the executable
and a detached helper swaps the file after the process exits.

ponytail: the helper is generated at runtime rather than shipped as a second file,
because a frozen onefile build has no stable path to a sibling script. Upgrade
path if this grows: ship updater.exe as a PyInstaller artifact.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.1.0"
DEFAULT_REPO = "nikfanaeian/novel-translator"

UPDATE_URL_TEMPLATE = "https://github.com/{repo}/releases/latest"
API_URL_TEMPLATE = "https://api.github.com/repos/{repo}/releases/latest"
ASSET_SUFFIX = ".exe"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def version_tuple(version: str) -> tuple[int, int, int]:
    """Parse 'v1.2.3' -> (1, 2, 3). Unparseable versions sort lowest."""
    match = _VERSION_RE.match((version or "").strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str = VERSION) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def is_frozen() -> bool:
    """True when running as a PyInstaller bundle rather than from source."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Where the app lives and where updates get written."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


@dataclass
class Release:
    version: str
    url: str
    asset_url: str
    asset_name: str

    @property
    def notes_url(self) -> str:
        return self.url


def fetch_latest(repo: str, proxy: str | None = None, timeout: int = 30) -> Release | None:
    """Latest release from the GitHub API. Returns None when unavailable.

    Never raises: a failed update check must not disturb a run. Network errors,
    a 404 (no repo), rate limiting, and malformed JSON all collapse to None.
    """
    if not repo or "/" not in repo:
        return None
    try:
        opener = _proxy_opener(proxy)
        request = urllib.request.Request(
            API_URL_TEMPLATE.format(repo=repo),
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "NovelTranslator",
            },
        )
        with opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - offline, 404, rate limit, bad JSON
        return None
    try:
        version = (data.get("tag_name") or "").strip()
        html_url = data.get("html_url") or UPDATE_URL_TEMPLATE.format(repo=repo)
        assets = data.get("assets") or []
        exe = next((a for a in assets if a.get("name", "").endswith(ASSET_SUFFIX)), None)
        if not version or not exe:
            return None
        return Release(
            version=version,
            url=html_url,
            asset_url=exe.get("browser_download_url", ""),
            asset_name=exe.get("name") or f"NovelTranslator{ASSET_SUFFIX}",
        )
    except Exception:  # noqa: BLE001 - unexpected payload shape
        return None


def _proxy_opener(proxy: str | None):
    """Build an opener, routing through `proxy` when one is set.

    urllib only supports HTTP proxies natively; a socks5:// proxy is ignored,
    so this falls back to a plain opener rather than pretending to route. The
    GUI's own traffic (Gemini) uses httpx, which does honour SOCKS properly.
    """
    if not proxy:
        return urllib.request.build_opener()
    scheme = urllib.parse.urlsplit(proxy if "://" in proxy else f"//{proxy}").scheme
    if scheme.startswith("socks"):
        return urllib.request.build_opener()  # urllib cannot do SOCKS
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def download_release(
    release: Release, dest_dir: Path, proxy: str | None = None, timeout: int = 60,
    on_progress=None,
) -> Path:
    """Download to dest_dir/<name>.new. Returns the path written.

    Written as .new rather than the final name so a crash mid-download can never
    leave a truncated file that later looks like a valid install.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / release.asset_name
    partial = target.with_suffix(target.suffix + ".new")
    opener = _proxy_opener(proxy)
    request = urllib.request.Request(
        release.asset_url, headers={"User-Agent": "NovelTranslator"}
    )
    with opener.open(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        with open(partial, "wb") as handle:
            while True:
                block = response.read(64 * 1024)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if on_progress:
                    on_progress(written, total)
    partial.replace(target)
    return target


HELPER_TEMPLATE = '''\
"""Generated at runtime: waits for the app to exit, then swaps in the update."""
import os, sys, time
from pathlib import Path

target, staged, pid = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])

while True:  # the parent holds the lock until it exits
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(0.5)

target.parent.mkdir(parents=True, exist_ok=True)
backup = target.with_name(target.name + ".old")
if target.exists():
    target.replace(backup)
staged.replace(target)
if backup.exists():
    backup.unlink()
print("update installed")
'''


def schedule_swap(target: Path, staged: Path, pid: int | None = None) -> Path | None:
    """Spawn a detached helper to replace `target` with `staged` after exit.

    Returns the helper script path, or None when the swap is unnecessary
    (running from source, where there is no .exe to replace).
    """
    if not is_frozen() or not target.exists():
        return None
    helper = Path(tempfile.gettempdir()) / "noveltranslator_updater.py"
    helper.write_text(HELPER_TEMPLATE, encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(helper), str(target), str(staged), str(pid or os.getpid())],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
    return helper


if __name__ == "__main__":  # manual check from a terminal
    print("version:", VERSION)
    print("frozen :", is_frozen())
    print("repo   :", DEFAULT_REPO or "(not set — configure in the app)")
    if DEFAULT_REPO:
        release = fetch_latest(DEFAULT_REPO)
        print("latest :", release.version if release else "unavailable")
        if release:
            print("newer  :", is_newer(release.version))