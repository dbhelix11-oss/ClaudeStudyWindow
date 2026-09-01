"""Shared helpers for locating and reading Claude Code session transcripts.

Transcripts live as JSONL at:
  ~/.claude/projects/<project-path-with-slashes-as-dashes>/<session-uuid>.jsonl
one subdirectory per project you've used Claude Code in. This tool is
project-agnostic (a study aid usable for any topic/conversation), so it
searches across ALL project subdirectories rather than assuming its own
project -- the conversation you're capturing from is whatever's currently
active, which could be any of them.

Each transcript line is a record; only "user"/"assistant" records with an
actual text block are conversation turns worth showing (tool-call/tool-result
records have empty text and are skipped).
"""
import configparser
import json
from pathlib import Path

ALL_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# every project lives as one folder directly under here; each project's own
# study notes are expected at <project folder>/QUICKREF.md, provenance at
# <project folder>/sources.json. ClaudeStudyWindow's own folder (this
# module's location) is just another entry in that same list, and doubles
# as the fallback before any project has ever been picked in the panel.
PROJECT_ROOT = Path.home() / "Documents" / "ClaudeProject"
NOTES_FILENAME = "QUICKREF.md"
SOURCES_FILENAME = "sources.json"
DEFAULT_PROJECT_DIR = Path(__file__).parent

# panel.py persists its current project selection here via QSettings. Read
# directly as plain INI (Qt's native format on Linux) rather than importing
# PyQt6 -- this module is also used by capture.py, which runs on every
# global-hotkey press and has no other reason to pay Qt's import cost.
_PANEL_SETTINGS_INI = Path.home() / ".config" / "ClaudeStudyWindow" / "QuickRefPanel.conf"


def current_project_dir() -> Path:
    """Whichever project folder the live panel currently has selected --
    lets capture.py (a separate process, fired by a global hotkey) write to
    the same place the panel is showing, without any live IPC between the
    two. Falls back to ClaudeStudyWindow's own folder if the panel has
    never saved a selection, or the saved one no longer exists."""
    if _PANEL_SETTINGS_INI.exists():
        parser = configparser.ConfigParser()
        try:
            parser.read(_PANEL_SETTINGS_INI)
            saved = parser.get("General", "project_dir", fallback="")
        except configparser.Error:
            saved = ""
        if saved and Path(saved).is_dir():
            return Path(saved)
    return DEFAULT_PROJECT_DIR


def find_active_transcript() -> Path | None:
    """Most recently modified .jsonl across every Claude Code project."""
    if not ALL_PROJECTS_DIR.is_dir():
        return None
    candidates = sorted(
        ALL_PROJECTS_DIR.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _extract_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(p for p in parts if p)
    return ""


def load_text_entries(session_path: Path) -> list[dict]:
    """Parse a transcript file into an ordered list of {uuid, timestamp,
    role, text} dicts, skipping records with no actual text content."""
    entries = []
    with session_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") not in ("user", "assistant"):
                continue
            message = record.get("message") or {}
            text = _extract_text(message)
            if not text:
                continue
            entries.append(
                {
                    "uuid": record.get("uuid"),
                    "timestamp": record.get("timestamp"),
                    "role": message.get("role", record.get("type")),
                    "text": text,
                }
            )
    return entries


def find_matching_entry(entries: list[dict], needle: str) -> dict | None:
    """Search entries newest-first for one whose text contains needle."""
    needle = needle.strip()
    if not needle:
        return None
    for entry in reversed(entries):
        if needle in entry["text"]:
            return entry
    return None


def get_context(
    entries: list[dict], uuid: str, chars_before: int = 1000, chars_after: int = 1000
) -> list[dict]:
    """Entries surrounding `uuid`, expanding outward by character count
    rather than message count. Always includes at least one entry on each
    side (if one exists) even if that single entry alone exceeds the
    budget -- the budget caps how much MORE gets pulled in beyond that."""
    index = next((i for i, e in enumerate(entries) if e["uuid"] == uuid), None)
    if index is None:
        return []

    before = []
    total = 0
    i = index - 1
    while i >= 0 and total < chars_before:
        entry = entries[i]
        before.insert(0, entry)
        total += len(entry["text"])
        i -= 1

    after = []
    total = 0
    i = index + 1
    while i < len(entries) and total < chars_after:
        entry = entries[i]
        after.append(entry)
        total += len(entry["text"])
        i += 1

    return before + [entries[index]] + after
