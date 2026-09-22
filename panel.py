#!/usr/bin/env python3
"""A small always-available reference panel: renders QUICKREF.md as
formatted markdown, live-reloads on change, warm library color scheme.
Run with: ./.venv/bin/python panel.py
"""
import datetime
import json
import re
import sys
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QFileSystemWatcher
from PyQt6.QtGui import QAction, QActionGroup, QFont
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import transcript

try:
    # used to hide the window from the taskbar/pager and to relocate it to
    # whichever virtual desktop is currently active every time the hotkey
    # shows it -- PyQt6 has no API for either, they're raw X11/EWMH requests
    from Xlib import X, display
    from Xlib.protocol import event as xevent

    _HAS_XLIB = True
except ImportError:
    _HAS_XLIB = False

QUICKREF = transcript.DEFAULT_PROJECT_DIR / transcript.NOTES_FILENAME
TIMESTAMP_RE = re.compile(r"captured (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# an optional user-given name, stored as its own heading line right after
# the "captured ..." line so it renders bold and larger than the note body
# (styled via the h4 rule in MARKDOWN_CSS) without disturbing the raw
# captured text that follows it. The pin marks it as ours to parse back out,
# as opposed to a heading the pasted content itself happens to start with.
NAME_LINE_PREFIX = "#### \U0001F4CC "
NAME_HEADER_RE = re.compile(r"^#### \U0001F4CC [^\n]+\n\n")

# matches one whole captured entry exactly as capture.py appends it -- from
# its leading "---" separator through to (but not including) the next
# entry's separator, or end of file. Deleting a match removes precisely
# what was appended for that capture, leaving neighboring entries and any
# hand-written notes above them untouched. group(2) is the optional name
# line's text, if the entry has been given one.
CAPTURE_ENTRY_RE = re.compile(
    r"\n---\n\n\*captured (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\*\n\n"
    r"(?:#### \U0001F4CC ([^\n]+)\n\n)?"
    r".*?(?=\n---\n\n\*captured \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\*\n\n|\Z)",
    re.DOTALL,
)


def delete_capture_entry(notes_path: Path, timestamp: str) -> bool:
    """Remove one captured entry (identified by its timestamp) from
    notes_path in place. Returns True if a matching entry was found."""
    if not notes_path.exists():
        return False
    text = notes_path.read_text()
    for match in CAPTURE_ENTRY_RE.finditer(text):
        if match.group(1) == timestamp:
            notes_path.write_text(text[: match.start()] + text[match.end() :])
            return True
    return False


def get_capture_name(notes_path: Path, timestamp: str) -> str | None:
    """Return the display name currently set on one captured entry, or
    None if it has no name (or the entry can't be found)."""
    if not notes_path.exists():
        return None
    text = notes_path.read_text()
    for match in CAPTURE_ENTRY_RE.finditer(text):
        if match.group(1) == timestamp:
            return match.group(2)
    return None


def rename_capture_entry(notes_path: Path, timestamp: str, name: str) -> bool:
    """Set (or, if name is empty, clear) the display name on one captured
    entry, identified by its timestamp. Returns True if the entry was found."""
    if not notes_path.exists():
        return False
    text = notes_path.read_text()
    for match in CAPTURE_ENTRY_RE.finditer(text):
        if match.group(1) != timestamp:
            continue
        prefix = f"\n---\n\n*captured {timestamp}*\n\n"
        body = NAME_HEADER_RE.sub("", match.group(0)[len(prefix) :], count=1)
        name_line = f"{NAME_LINE_PREFIX}{name}\n\n" if name else ""
        new_entry = prefix + name_line + body
        notes_path.write_text(text[: match.start()] + new_entry + text[match.end() :])
        return True
    return False


# per-project sidecar recording *when* each entry was (re)named -- kept
# separate from the name text itself (which lives in QUICKREF.md, as the
# human-readable, hand-editable source of truth) so display order can be
# driven by naming recency without stashing a hidden timestamp inside the
# visible heading line.
NAMES_FILENAME = "capture_names.json"


def _named_at_path(notes_path: Path) -> Path:
    return notes_path.parent / NAMES_FILENAME


def load_named_at(notes_path: Path) -> dict:
    path = _named_at_path(notes_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_named_at(notes_path: Path, named_at: dict) -> None:
    _named_at_path(notes_path).write_text(json.dumps(named_at, indent=2))


def reorder_named_first(text: str, named_at: dict) -> str:
    """Render-only reordering: named entries float to the top, most
    recently named first, followed by every unnamed entry in its original
    (capture) order. Anything before the first entry (a hand-written
    intro/header) is left in place. Does not touch the file on disk --
    QUICKREF.md itself stays in capture order, so appends, deletes and
    renames (which locate entries by timestamp, not position) are unaffected."""
    matches = list(CAPTURE_ENTRY_RE.finditer(text))
    if not matches:
        return text

    preamble = text[: matches[0].start()]
    named = []
    unnamed = []
    for match in matches:
        timestamp = match.group(1)
        if match.group(2) is not None:
            # fall back to the capture timestamp if a name was set by hand
            # editing the file rather than through the rename dialog, so it
            # still sorts sensibly without a recorded naming time
            sort_key = named_at.get(timestamp, timestamp)
            named.append((sort_key, match.group(0)))
        else:
            unnamed.append(match.group(0))

    named.sort(key=lambda pair: pair[0], reverse=True)
    return preamble + "".join(entry for _, entry in named) + "".join(unnamed)


# File -> Project lists every folder here. Each project's own study notes
# live at <project folder>/QUICKREF.md; ClaudeStudyWindow's own folder is
# just another entry in this same list (and is what QUICKREF above points
# at, preserved as the out-of-the-box default before any project is picked).
PROJECT_ROOT = transcript.PROJECT_ROOT
NOTES_FILENAME = transcript.NOTES_FILENAME

def _prep_window_for_show(win_id: int) -> None:
    """Called every time the panel is (re)shown by the hotkey toggle.

    Sends two EWMH client messages via raw X11 (no Qt API covers either):

    - _NET_WM_STATE_SKIP_TASKBAR/SKIP_PAGER: Qt.WindowType.Tool alone
      (set in __init__) isn't enough here -- this box's WM (Openbox) lists
      _NET_WM_WINDOW_TYPE_UTILITY as a supported window type but still
      puts Utility windows in the taskbar; SKIP_TASKBAR is the state it
      actually honors.
    - _NET_WM_DESKTOP: moves the window to whichever virtual desktop is
      currently active. This WM doesn't support _NET_WM_STATE_STICKY at
      all (confirmed absent from its _NET_SUPPORTED list), so "follow me
      to wherever I am" has to be done by actively relocating the window
      on every show, rather than making it permanently visible everywhere.

    Idempotent; silently does nothing if python-xlib isn't installed or
    any step fails (e.g. a WM that doesn't support these either)."""
    if not _HAS_XLIB:
        return
    try:
        d = display.Display()
        window = d.create_resource_object("window", win_id)
        root = d.screen().root
        mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask

        net_wm_state = d.intern_atom("_NET_WM_STATE")
        skip_taskbar = d.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        skip_pager = d.intern_atom("_NET_WM_STATE_SKIP_PAGER")
        _NET_WM_STATE_ADD = 1
        state_ev = xevent.ClientMessage(
            window=window,
            client_type=net_wm_state,
            data=(32, (_NET_WM_STATE_ADD, skip_taskbar, skip_pager, 1, 0)),
        )
        root.send_event(state_ev, event_mask=mask)

        current_desktop_prop = root.get_full_property(d.intern_atom("_NET_CURRENT_DESKTOP"), 0)
        if current_desktop_prop and current_desktop_prop.value:
            current_desktop = current_desktop_prop.value[0]
            net_wm_desktop = d.intern_atom("_NET_WM_DESKTOP")
            desktop_ev = xevent.ClientMessage(
                window=window,
                client_type=net_wm_desktop,
                data=(32, (current_desktop, 2, 0, 0, 0)),
            )
            root.send_event(desktop_ev, event_mask=mask)

        d.flush()
    except Exception:
        pass


# name of the local socket used to detect an already-running instance and
# tell it to toggle visibility, instead of opening a second window
IPC_SERVER_NAME = "ClaudeStudyWindow-QuickRefPanel"

# character budget for "Show original context", and how much more to pull
# in each time "Show more context" is clicked
CONTEXT_CHARS_INITIAL = 800
CONTEXT_CHARS_STEP = 1500

ROLE_LABELS = {"user": "You", "assistant": "Claude"}

WARM_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #f4ecd8;
    color: #3b2f2f;
}
QTextBrowser {
    background-color: #f9f3e6;
    color: #3b2f2f;
    border: 1px solid #d8c9a3;
    padding: 12px;
    selection-background-color: #d8b96a;
    selection-color: #2a1f1a;
}
QLineEdit {
    background-color: #ffffff;
    color: #3b2f2f;
    border: 1px solid #c9b78c;
    padding: 4px 6px;
    border-radius: 3px;
}
QPushButton {
    background-color: #b5794a;
    color: #fdf6e8;
    border: none;
    padding: 5px 12px;
    border-radius: 3px;
}
QPushButton:hover {
    background-color: #a5693c;
}
QCheckBox {
    color: #3b2f2f;
}
"""

MARKDOWN_CSS = """
<style>
body { font-family: Georgia, 'Noto Serif', serif; font-size: 14px; }
h1, h2, h3 { color: #7c4a2d; }
h1 { border-bottom: 2px solid #d8b96a; padding-bottom: 4px; }
h2 { border-bottom: 1px solid #d8c9a3; padding-bottom: 2px; }
h4 { color: #a5693c; font-size: 19px; font-weight: bold; }
code { background-color: #eadfc4; padding: 1px 4px; border-radius: 3px; }
pre { background-color: #eadfc4; padding: 8px; border-radius: 4px; }
a { color: #a5693c; }
hr { border: none; border-top: 1px solid #d8c9a3; }
em { color: #6b5a4a; }
</style>
"""


class ContextDialog(QDialog):
    def __init__(self, session_path: Path, uuid: str, parent=None):
        super().__init__(parent)
        self.session_path = session_path
        self.uuid = uuid
        self.chars_before = CONTEXT_CHARS_INITIAL
        self.chars_after = CONTEXT_CHARS_INITIAL

        self.setWindowTitle("Original context")
        self.resize(640, 520)

        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.document().setDefaultStyleSheet(MARKDOWN_CSS)
        layout.addWidget(self.browser)

        buttons = QHBoxLayout()
        more_btn = QPushButton("Show more context")
        more_btn.clicked.connect(self.show_more)
        buttons.addWidget(more_btn)
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.setStyleSheet(WARM_STYLESHEET)
        self.render_context()

    def show_more(self):
        self.chars_before += CONTEXT_CHARS_STEP
        self.chars_after += CONTEXT_CHARS_STEP
        self.render_context()

    def render_context(self):
        entries = transcript.load_text_entries(self.session_path)
        context = transcript.get_context(
            entries, self.uuid, self.chars_before, self.chars_after
        )
        if not context:
            self.browser.setMarkdown("*Could not locate this message anymore — the source transcript may have changed.*")
            return

        parts = []
        for entry in context:
            label = ROLE_LABELS.get(entry["role"], entry["role"])
            marker = " *(captured from here)*" if entry["uuid"] == self.uuid else ""
            parts.append(f"**{label}{marker}:**\n\n{entry['text']}\n\n---\n")
        self.browser.setMarkdown("\n".join(parts))


class QuickRefPanel(QMainWindow):
    def __init__(self):
        super().__init__()
        # Tool windows get the EWMH "utility" window type, which Openbox
        # (LXQt's WM here) uses to decide a window has no taskbar entry --
        # this is a toggle-by-hotkey panel, not a normal application window
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.settings = QSettings("ClaudeStudyWindow", "QuickRefPanel")
        self.notes_path = self._load_saved_notes_path()
        self.setWindowTitle(f"Quick Reference — {self.notes_path.parent.name}")

        self._transparency_percent = self.settings.value("transparency_percent", 0, type=int)
        self.setWindowOpacity(1.0 - self._transparency_percent / 100.0)

        self._setup_menu()

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search...")
        self.search_box.textChanged.connect(self.on_search)
        self.search_box.returnPressed.connect(self.find_next)
        toolbar.addWidget(self.search_box)

        find_next_btn = QPushButton("Next")
        find_next_btn.clicked.connect(self.find_next)
        toolbar.addWidget(find_next_btn)

        # unchecked: Next/Enter only cycles matches within the open
        # project's notes (as always). Checked: once those are exhausted,
        # it hops into other projects' QUICKREF.md, switching the view to
        # wherever the next match is found. Live highlight-as-you-type
        # always stays scoped to the currently open document either way --
        # switching projects mid-keystroke would be disruptive.
        self.all_projects_checkbox = QCheckBox("Search all projects")
        self.all_projects_checkbox.stateChanged.connect(self._save_search_scope)
        toolbar.addWidget(self.all_projects_checkbox)

        self.top_checkbox = QCheckBox("Always on top")
        self.top_checkbox.stateChanged.connect(self.toggle_always_on_top)
        toolbar.addWidget(self.top_checkbox)

        layout.addLayout(toolbar)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.browser.customContextMenuRequested.connect(self.show_browser_context_menu)
        layout.addWidget(self.browser)

        self.setStyleSheet(WARM_STYLESHEET)

        self.watcher = QFileSystemWatcher()
        self._watch_file()
        self.watcher.fileChanged.connect(self.on_file_changed)

        self.load_content()
        self.restore_geometry()

        default_top = self.settings.value("always_on_top", True, type=bool)
        self.top_checkbox.setChecked(default_top)

        default_search_all = self.settings.value("search_all_projects", False, type=bool)
        self.all_projects_checkbox.setChecked(default_search_all)

        self._start_ipc_server()

    def _start_ipc_server(self):
        # a stale socket file can be left behind if a previous instance
        # crashed; safe to clear it since we only get here after failing
        # to connect to a live instance
        QLocalServer.removeServer(IPC_SERVER_NAME)
        self.ipc_server = QLocalServer(self)
        self.ipc_server.listen(IPC_SERVER_NAME)
        self.ipc_server.newConnection.connect(self._on_ipc_connection)

    def _on_ipc_connection(self):
        socket = self.ipc_server.nextPendingConnection()
        if socket is not None:
            socket.disconnected.connect(socket.deleteLater)
            socket.close()
        self.toggle_visibility()

    def toggle_visibility(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            # a brand-new project's QUICKREF.md may not have existed yet
            # when it was selected (nothing to watch until capture.py
            # creates it on the first clipboard capture) -- retry attaching
            # the watcher and refresh in case that's happened since
            self._watch_file()
            self.load_content()
            self.showNormal()
            self.raise_()
            self.activateWindow()
            _prep_window_for_show(int(self.winId()))

    def _load_saved_notes_path(self) -> Path:
        saved_project = self.settings.value("project_dir", "")
        if saved_project and Path(saved_project).is_dir():
            return Path(saved_project) / NOTES_FILENAME
        return QUICKREF

    def _setup_menu(self):
        file_menu = self.menuBar().addMenu("&File")
        self.project_menu = file_menu.addMenu("Project")
        # rebuilt every time the submenu opens, so a project folder created
        # or removed since launch shows up without restarting the panel
        self.project_menu.aboutToShow.connect(self._populate_project_menu)

        view_menu = self.menuBar().addMenu("&View")

        # mirrors the toolbar checkbox -- kept in sync both ways via
        # toggle_always_on_top() below, which is the single source of truth
        self.top_action = view_menu.addAction("Always on top")
        self.top_action.setCheckable(True)
        self.top_action.triggered.connect(lambda checked: self.top_checkbox.setChecked(checked))

        transparency_menu = view_menu.addMenu("Transparency")
        self.transparency_group = QActionGroup(self)
        self.transparency_group.setExclusive(True)
        for percent in range(0, 100, 10):
            label = "None (0%)" if percent == 0 else f"{percent}%"
            action = transparency_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(percent == self._transparency_percent)
            action.triggered.connect(lambda checked, p=percent: self._set_transparency(p))
            self.transparency_group.addAction(action)

    def _set_transparency(self, percent: int):
        self.setWindowOpacity(1.0 - percent / 100.0)
        self.settings.setValue("transparency_percent", percent)

    def _list_project_dirs(self) -> list[Path]:
        if not PROJECT_ROOT.is_dir():
            return []
        return sorted(
            (p for p in PROJECT_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )

    def _populate_project_menu(self):
        self.project_menu.clear()
        if not PROJECT_ROOT.is_dir():
            action = self.project_menu.addAction("(project folder not found)")
            action.setEnabled(False)
            return

        project_dirs = self._list_project_dirs()
        if not project_dirs:
            action = self.project_menu.addAction("(no projects found)")
            action.setEnabled(False)
            return

        for project_dir in project_dirs:
            action = self.project_menu.addAction(project_dir.name)
            action.setCheckable(True)
            action.setChecked(project_dir == self.notes_path.parent)
            action.triggered.connect(lambda checked, d=project_dir: self._select_project(d))

    def _select_project(self, project_dir: Path):
        new_path = project_dir / NOTES_FILENAME
        if new_path == self.notes_path:
            return
        if str(self.notes_path) in self.watcher.files():
            self.watcher.removePath(str(self.notes_path))

        self.notes_path = new_path
        self.settings.setValue("project_dir", str(project_dir))
        # capture.py reads this setting straight off disk from a separate
        # process on every hotkey press -- without an explicit sync() here,
        # Qt may not flush it until the app quits, so a capture right after
        # switching projects could still land in the old one
        self.settings.sync()
        self.setWindowTitle(f"Quick Reference — {project_dir.name}")
        self._watch_file()
        self.load_content()

    def _watch_file(self):
        if str(self.notes_path) not in self.watcher.files():
            if self.notes_path.exists():
                self.watcher.addPath(str(self.notes_path))

    def on_file_changed(self, _path):
        # editors often replace-by-rename, which drops the watched inode
        self._watch_file()
        self.load_content()

    def load_content(self):
        if not self.notes_path.exists():
            self.browser.setHtml("<p><em>No study notes found.</em></p>")
            return
        text = self.notes_path.read_text()
        text = reorder_named_first(text, load_named_at(self.notes_path))
        scrollbar = self.browser.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self.browser.document().setDefaultStyleSheet(MARKDOWN_CSS)
        self.browser.setMarkdown(text)
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def find_entry_timestamp(self, pos):
        """Walk backward from the clicked block to the nearest preceding
        'captured <timestamp>' line, identifying which entry was clicked."""
        cursor = self.browser.cursorForPosition(pos)
        block = cursor.block()
        while block.isValid():
            match = TIMESTAMP_RE.search(block.text())
            if match:
                return match.group(1)
            block = block.previous()
        return None

    def show_browser_context_menu(self, pos):
        menu = self.browser.createStandardContextMenu(pos)
        timestamp = self.find_entry_timestamp(pos)
        if timestamp:
            menu.addSeparator()
            source = self.load_source_for(timestamp)
            if source:
                context_action = menu.addAction("Show original context")
                context_action.triggered.connect(lambda: self.open_context_dialog(source))
            existing_name = get_capture_name(self.notes_path, timestamp)
            rename_label = "Rename this capture..." if existing_name else "Name this capture..."
            rename_action = menu.addAction(rename_label)
            rename_action.triggered.connect(lambda: self.rename_capture(timestamp, existing_name))
            delete_action = menu.addAction("Delete this capture")
            delete_action.triggered.connect(lambda: self.delete_capture(timestamp))
        menu.exec(self.browser.mapToGlobal(pos))

    def rename_capture(self, timestamp: str, existing_name: str | None):
        name, ok = QInputDialog.getText(
            self,
            "Name this capture",
            "Name (leave blank to remove):",
            QLineEdit.EchoMode.Normal,
            existing_name or "",
        )
        if not ok:
            return

        name = name.strip()
        if not rename_capture_entry(self.notes_path, timestamp, name):
            QMessageBox.warning(
                self, "Not found", "Could not locate that capture -- the notes file may have changed."
            )
            return

        named_at = load_named_at(self.notes_path)
        if name:
            # (re)naming counts as a fresh naming action, so it bubbles back
            # to the top of the named group even if it was named before
            named_at[timestamp] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            named_at.pop(timestamp, None)
        save_named_at(self.notes_path, named_at)

        self.load_content()
        if name:
            self.statusBar().showMessage(f"Named capture from {timestamp}: {name}", 4000)
        else:
            self.statusBar().showMessage(f"Cleared name for capture from {timestamp}", 4000)

    def delete_capture(self, timestamp: str):
        reply = QMessageBox.question(
            self,
            "Delete capture",
            f"Delete the capture from {timestamp}? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if not delete_capture_entry(self.notes_path, timestamp):
            QMessageBox.warning(
                self, "Not found", "Could not locate that capture -- the notes file may have changed."
            )
            return

        self._remove_source_entry(timestamp)
        named_at = load_named_at(self.notes_path)
        if named_at.pop(timestamp, None) is not None:
            save_named_at(self.notes_path, named_at)
        self.load_content()
        self.statusBar().showMessage(f"Deleted capture from {timestamp}", 4000)

    def _remove_source_entry(self, timestamp: str):
        sources_path = self.notes_path.parent / transcript.SOURCES_FILENAME
        if not sources_path.exists():
            return
        try:
            sources = json.loads(sources_path.read_text())
        except json.JSONDecodeError:
            return
        if timestamp in sources:
            del sources[timestamp]
            sources_path.write_text(json.dumps(sources, indent=2))

    def load_source_for(self, timestamp):
        sources_path = self.notes_path.parent / transcript.SOURCES_FILENAME
        if not sources_path.exists():
            return None
        try:
            sources = json.loads(sources_path.read_text())
        except json.JSONDecodeError:
            return None
        return sources.get(timestamp)

    def open_context_dialog(self, source):
        session_path = Path(source["session_path"])
        if not session_path.exists():
            QMessageBox.warning(
                self, "Not found", f"Session transcript {session_path} no longer exists."
            )
            return
        dialog = ContextDialog(session_path, source["uuid"], parent=self)
        dialog.exec()

    def on_search(self, text):
        cursor = self.browser.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.browser.setTextCursor(cursor)
        if text:
            self.browser.find(text)

    def find_next(self):
        text = self.search_box.text()
        if not text:
            return
        if self.browser.find(text):
            return

        # nothing more below the cursor -- wrap to the top of this
        # project's notes and try once more before considering other
        # projects, same as the old single-project behavior
        cursor = self.browser.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.browser.setTextCursor(cursor)
        if self.browser.find(text):
            return

        if self.all_projects_checkbox.isChecked():
            self._find_next_across_projects(text)
        else:
            self.statusBar().showMessage(f'No matches for "{text}" in this project', 4000)

    def _find_next_across_projects(self, text: str):
        project_dirs = self._list_project_dirs()
        if not project_dirs:
            return

        current_dir = self.notes_path.parent
        try:
            start_index = project_dirs.index(current_dir)
        except ValueError:
            start_index = -1

        needle = text.lower()
        for offset in range(1, len(project_dirs) + 1):
            project_dir = project_dirs[(start_index + offset) % len(project_dirs)]
            if project_dir == current_dir:
                continue
            notes_text = self._read_project_notes(project_dir)
            if needle not in notes_text.lower():
                continue

            self._select_project(project_dir)
            cursor = self.browser.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            self.browser.setTextCursor(cursor)
            if self.browser.find(text):
                self.statusBar().showMessage(f'Found "{text}" in {project_dir.name}', 4000)
                return

        self.statusBar().showMessage(f'No matches for "{text}" in any project', 4000)

    def _read_project_notes(self, project_dir: Path) -> str:
        try:
            return (project_dir / NOTES_FILENAME).read_text()
        except OSError:
            return ""

    def _save_search_scope(self, state):
        self.settings.setValue("search_all_projects", state == Qt.CheckState.Checked.value)

    def toggle_always_on_top(self, state):
        on_top = state == Qt.CheckState.Checked.value
        self.top_action.setChecked(on_top)
        self.settings.setValue("always_on_top", on_top)
        flags = self.windowFlags()
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def restore_geometry(self):
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(480, 640)

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)


def signal_existing_instance() -> bool:
    """If another instance is already listening, connect to it (which tells
    it to toggle visibility) and return True so this process can exit."""
    socket = QLocalSocket()
    socket.connectToServer(IPC_SERVER_NAME)
    if socket.waitForConnected(200):
        socket.disconnectFromServer()
        return True
    return False


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Georgia", 10))

    if signal_existing_instance():
        sys.exit(0)

    panel = QuickRefPanel()
    panel.show()
    _prep_window_for_show(int(panel.winId()))
    if panel.top_checkbox.isChecked():
        panel.toggle_always_on_top(Qt.CheckState.Checked.value)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
