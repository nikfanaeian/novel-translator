"""NovelTranslator GUI — PySide6.

ponytail: keys live in a plaintext file next to the config. That's deliberate for a
personal tool; upgrade path is `keyring` + OS credential storage if this ever ships
to people who share a machine.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import core
from core import DONE, FAILED, PENDING, EpisodeState, JobState, Translator, TranslatorConfig

APP_DIR = Path(
    os.environ.get("APPDATA", Path.home() / ".noveltranslator")
).expanduser()
KEYS_FILE = APP_DIR / "keys.json"
SETTINGS_FILE = APP_DIR / "settings.json"

log = logging.getLogger("noveltranslator")


def setup_logging() -> None:
    """Log to both the user-chosen file and stderr so the GUI pane and the file agree."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(
        APP_DIR / "noveltranslator.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stderr:  # PyInstaller --windowed swallows this; guard it
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        log.addHandler(stream)


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as exc:  # non-fatal: losing prefs shouldn't kill a running job
        log.warning("could not save settings: %s", exc)


# ---------------------------------------------------------------- worker


class TranslateWorker(QObject):
    """Runs the job loop off the GUI thread. Qt signals carry results back, which
    is the thread-safe way to touch widgets — never call a widget directly here."""

    progress = Signal(str, int, int, str)  # filename, chunk_index, chunk_total, status
    log_line = Signal(str)
    episode_done = Signal(str, int)  # filename, chars written
    episode_failed = Signal(str, str)
    finished = Signal(int, int)  # succeeded, failed

    def __init__(self, files: list[Path], lang_tag: str, config: TranslatorConfig,
                 state: JobState):
        super().__init__()
        self.files, self.lang_tag, self.config, self.state = files, lang_tag, config, state
        self.stop_requested = False

    @Slot()
    def run(self) -> None:
        translator = Translator(self.config, log=lambda m: self.log_line.emit(m))
        ok = failed = 0
        try:
            for path in self.files:
                if self.stop_requested:
                    self.log_line.emit("stop requested, remaining episodes skipped")
                    break
                dest = core.output_path(path, self.lang_tag)
                self.progress.emit(path.name, 0, 0, "running")
                try:
                    chars = core.translate_episode(
                        path, dest, translator, self.lang_tag,
                        on_progress=lambda i, t, n=path.name: self.progress.emit(
                            n, i, t, "running"),
                    )
                    self.state.episodes[path.name] = EpisodeState(path.name, DONE)
                    ok += 1
                    self.episode_done.emit(path.name, chars)
                except Exception as exc:  # noqa: BLE001 - one bad episode must not end the run
                    message = core._short(exc)
                    previous = self.state.episodes.get(path.name, EpisodeState(path.name))
                    self.state.episodes[path.name] = EpisodeState(
                        path.name, FAILED, message, previous.attempts + 1
                    )
                    failed += 1
                    log.exception("failed: %s", path.name)
                    self.episode_failed.emit(path.name, message)
                finally:
                    # Saves after every episode: this is the crash/resume record.
                    self.state.save()
        finally:
            self.finished.emit(ok, failed)


# ---------------------------------------------------------------- keys panel


class KeyManager(QGroupBox):
    """Multiple API keys, round-robined by Translator. Values are masked."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Gemini API Keys", parent)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._edit)
        self.field = QLineEdit()
        self.field.setPlaceholderText("AIza... paste key, then press Add")
        add = QPushButton("Add")
        add.clicked.connect(self._add)
        self.field.returnPressed.connect(self._add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove)
        top = QHBoxLayout()
        top.addWidget(self.field, 1)
        top.addWidget(add)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addLayout(top)
        layout.addWidget(remove)
        self.list.itemChanged.connect(lambda _: self.changed.emit())

    def _add(self) -> None:
        key = self.field.text().strip()
        if key and key not in self.keys():
            self.list.addItem(QListWidgetItem(key))
            self.field.clear()
            self.changed.emit()

    def _edit(self, item: QListWidgetItem) -> None:
        item.setFlags(item.flags() | Qt.ItemIsEditable)

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.changed.emit()

    def keys(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_keys(self, keys: list[str]) -> None:
        self.list.clear()
        for key in keys:
            self.list.addItem(QListWidgetItem(key))


# ---------------------------------------------------------------- main window


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NovelTranslator")
        self.resize(1000, 700)
        self.worker: TranslateWorker | None = None
        self.thread: QThread | None = None
        self.rows: dict[str, int] = {}
        self.state: JobState | None = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        self.keys_panel = KeyManager()
        self.folder = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        self.lang = QComboBox()
        for tag, name in core.LANGUAGES:
            self.lang.addItem(name, tag)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Folder:"))
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(QLabel("Language:"))
        folder_row.addWidget(self.lang)

        form = QFormLayout()
        form.addRow(folder_row)

        self.proxy = QLineEdit(core.DEFAULT_PROXY)
        self.proxy.setPlaceholderText("socks5://127.0.0.1:10808  (leave empty for direct)")
        self.proxy.setMaximumWidth(320)
        self.use_proxy = QCheckBox("Use SOCKS5 proxy")
        self.use_proxy.setChecked(True)
        self.use_proxy.toggled.connect(self.proxy.setEnabled)
        self.proxy.setEnabled(True)
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(self.use_proxy)
        proxy_row.addWidget(self.proxy, 1)
        form.addRow(QLabel("Network:"), proxy_row)
        outer.addLayout(form)
        outer.addWidget(self.keys_panel)

        self.model = QComboBox()
        self.model.addItem("gemini-2.5-flash (fast, recommended)", "gemini-2.5-flash")
        self.model.addItem("gemini-2.5-pro (highest quality, slower)", "gemini-2.5-pro")
        self.model.addItem("gemini-2.0-flash (legacy free tier)", "gemini-2.0-flash")
        self.skip_done = QCheckBox("Skip episodes already translated")
        self.skip_done.setChecked(True)
        options = QHBoxLayout()
        options.addWidget(QLabel("Model:"))
        options.addWidget(self.model)
        options.addSpacing(20)
        options.addWidget(self.skip_done)
        options.addStretch(1)
        self.log_path = QLineEdit(str(APP_DIR / "noveltranslator.log"))
        self.log_path.setReadOnly(True)
        self.log_path.setMaximumWidth(320)
        self.pick_log = QPushButton("Log file…")
        self.pick_log.clicked.connect(self.browse_log)
        options.addWidget(QLabel("Log:"))
        options.addWidget(self.log_path)
        options.addWidget(self.pick_log)
        outer.addLayout(options)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Episode", "Status", "Progress", "Error"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.console)
        outer.addWidget(splitter, 1)

        self.bar = QProgressBar()
        self.status = QLabel("Choose a folder and add at least one API key.")
        self.start_btn = QPushButton("Start")
        self.retry_btn = QPushButton("Retry failed")
        self.stop_btn = QPushButton("Stop")
        for button in (self.stop_btn, self.retry_btn):
            button.setEnabled(False)
        self.start_btn.clicked.connect(self.start)
        self.retry_btn.clicked.connect(lambda: self.start(retry_only=True))
        self.stop_btn.clicked.connect(self.stop)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.retry_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.bar, 1)
        outer.addLayout(buttons)
        outer.addWidget(self.status)

        self.load_prefs()
        self.folder.textChanged.connect(self.scan)

    # ---------------------------------------------------------- prefs

    def load_prefs(self) -> None:
        settings = load_settings()
        self.folder.setText(settings.get("folder", ""))
        self.keys_panel.set_keys(settings.get("keys", []))
        index = self.lang.findData(settings.get("lang_tag", "fa"))
        self.lang.setCurrentIndex(max(index, 0))
        model_index = self.model.findData(settings.get("model", "gemini-2.5-flash"))
        self.model.setCurrentIndex(max(model_index, 0))
        self.log_path.setText(settings.get("log_path", str(APP_DIR / "noveltranslator.log")))
        self.proxy.setText(settings.get("proxy", core.DEFAULT_PROXY))
        self.use_proxy.setChecked(bool(settings.get("use_proxy", True)))

    def save_prefs(self) -> None:
        save_settings(
            {
                "folder": self.folder.text().strip(),
                "keys": self.keys_panel.keys(),
                "lang_tag": self.lang.currentData(),
                "model": self.model.currentData(),
                "log_path": self.log_path.text().strip(),
                "proxy": self.proxy.text().strip(),
                "use_proxy": self.use_proxy.isChecked(),
            }
        )

    # ---------------------------------------------------------- scanning

    @Slot()
    def browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose novel folder")
        if chosen:
            self.folder.setText(chosen)

    def browse_log(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Log file", self.log_path.text(), "Log (*.log)")
        if chosen:
            self.log_path.setText(chosen)

    @Slot()
    def scan(self) -> None:
        path = self.folder.text().strip()
        self.rows.clear()
        self.table.setRowCount(0)
        if not path:
            self.status.setText("Choose a folder and add at least one API key.")
            return
        try:
            files = core.discover_episodes(path)
        except (NotADirectoryError, OSError) as exc:
            self.status.setText(f"Cannot read folder: {exc}")
            return

        state = JobState.load(path)
        state.lang_tag = self.lang.currentData()
        state.sync(files)
        self.state = state
        for source in files:
            entry = state.episodes[source.name]
            if self.skip_done.isChecked() and entry.status == DONE:
                continue
            self.rows[source.name] = self.table.rowCount()
            self.table.insertRow(self.table.rowCount())
            self.table.setItem(self.rows[source.name], 0, QTableWidgetItem(source.name))
            self.table.setItem(self.rows[source.name], 1, QTableWidgetItem(entry.status))
            self.table.setItem(self.rows[source.name], 2, QTableWidgetItem(""))
            self.table.setItem(self.rows[source.name], 3, QTableWidgetItem(entry.error))
        todo = len(state.pending())
        self.status.setText(f"{len(files)} episode(s) found, {todo} to translate.")

    # ---------------------------------------------------------- running

    def _running(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    @Slot()
    def start(self, retry_only: bool = False) -> None:
        if self._running():
            return
        keys = self.keys_panel.keys()
        if not keys:
            QMessageBox.warning(self, "No API key", "Add at least one Gemini API key.")
            return
        folder = self.folder.text().strip()
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, "No folder", "Choose a valid folder first.")
            return
        self.scan()
        if self.state is None:
            return
        if retry_only:
            self.state.reset_failed()

        lang_tag = self.lang.currentData()
        all_files = [Path(folder) / name for name in self.state.episodes]
        todo = all_files if not self.skip_done.isChecked() else [
            p for name, entry in self.state.episodes.items()
            if entry.status != DONE for p in [Path(folder) / name]
        ]
        if not todo:
            QMessageBox.information(self, "Nothing to do", "All episodes are already translated.")
            return

        self.save_prefs()
        setup_file_log(self.log_path.text().strip())
        proxy = self.proxy.text().strip() if self.use_proxy.isChecked() else ""
        config = TranslatorConfig(
            api_keys=keys, model=self.model.currentData(), proxy=proxy
        )
        self.bar.setRange(0, len(todo))
        self.bar.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.retry_btn.setEnabled(False)
        self._set_status("Translating…", "running")

        self.thread = QThread()
        self.worker = TranslateWorker(todo, lang_tag, config, self.state)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log_line.connect(self.append_log)
        self.worker.progress.connect(self.on_progress)
        self.worker.episode_done.connect(self.on_done)
        self.worker.episode_failed.connect(self.on_failed)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()
        log.info("started: %d episode(s), lang=%s, model=%s, keys=%d",
                 len(todo), lang_tag, config.model, len(keys))

    @Slot()
    def stop(self) -> None:
        if self.worker is not None:  # cooperative: the loop exits after this episode
            self.worker.stop_requested = True
            self.status.setText("Stopping after the current episode…")
        self.stop_btn.setEnabled(False)

    @Slot(str, int, int, str)
    def on_progress(self, name: str, index: int, total: int, status: str) -> None:
        row = self.rows.get(name)
        if row is None:
            return
        self.table.item(row, 1).setText(status)
        if total:
            self.table.item(row, 2).setText(f"{index}/{total}")

    @Slot(str, int)
    def on_done(self, name: str, chars: int) -> None:
        row = self.rows.get(name)
        if row is not None:
            self.table.item(row, 1).setText(DONE)
            self.table.item(row, 3).setText("")
        self.bar.setValue(self.bar.value() + 1)
        self.append_log(f"OK   {name}  ({chars} chars)")

    @Slot(str, str)
    def on_failed(self, name: str, error: str) -> None:
        row = self.rows.get(name)
        if row is not None:
            self.table.item(row, 1).setText(FAILED)
            self.table.item(row, 3).setText(error)
        self.bar.setValue(self.bar.value() + 1)
        self.append_log(f"FAIL {name}  {error}")

    @Slot(int, int)
    def on_finished(self, ok: int, failed: int) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
        self.worker = None
        self.start_btn.setEnabled(True)
        self.retry_btn.setEnabled(failed > 0)
        self._set_status(
            f"Finished — {ok} succeeded, {failed} failed.", "idle"
        )
        self.append_log(f"--- finished: {ok} ok, {failed} failed ---")
        log.info("finished: %d ok, %d failed", ok, failed)
        if failed:
            QMessageBox.information(
                self, "Finished with failures",
                f"{failed} episode(s) failed.\n\nUse Retry failed to try them again. "
                f"Details: {self.log_path.text()}"
            )

    # ---------------------------------------------------------- misc

    def _set_status(self, text: str, status: str) -> None:
        self.status.setText(text)
        colors = {"idle": "#555", "running": "#0a7a2f", "error": "#b00020"}
        self.status.setStyleSheet(f"color:{colors.get(status, '#555')};font-weight:600")

    @Slot(str)
    def append_log(self, line: str) -> None:
        log.info(line)
        self.console.append(line)

    def closeEvent(self, event) -> None:
        if self._running():
            answer = QMessageBox.question(
                self, "Job running", "A translation is in progress. Quit anyway?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.save_prefs()
        event.accept()


def setup_file_log(path: str) -> None:
    """Attach a handler for the user-chosen log file, replacing any previous one."""
    for handler in list(log.handlers):
        if isinstance(handler, RotatingFileHandler):
            log.removeHandler(handler)
            handler.close()
    path = path.strip() or str(APP_DIR / "noveltranslator.log")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("NovelTranslator")
    window = MainWindow()
    window.show()
    try:
        return app.exec()
    except Exception:  # noqa: BLE001 - log crashes a GUI app would otherwise swallow
        log.critical("fatal:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    raise SystemExit(main())