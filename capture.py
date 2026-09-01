#!/usr/bin/env python3
"""Append the current X11 clipboard contents to QUICKREF.md, timestamped.
Also tries to locate the matching message in the active Claude Code session
transcript and records it in sources.json, keyed by timestamp, so the panel
can later show "original context" for this entry.
Bind this to a global hotkey (LXQt: Preferences -> LXQt Settings -> Shortcut keys).
"""
import datetime
import json
import subprocess
import sys
from pathlib import Path

import transcript

# how much of the captured text to search for -- long enough to be
# unambiguous, short enough to survive minor copy/paste edge trimming
MATCH_PREFIX_LEN = 200


def record_source(project_dir: Path, timestamp: str, text: str) -> None:
    session_path = transcript.find_active_transcript()
    if session_path is None:
        return
    entries = transcript.load_text_entries(session_path)
    match = transcript.find_matching_entry(entries, text[:MATCH_PREFIX_LEN])
    if match is None:
        return

    sources_path = project_dir / transcript.SOURCES_FILENAME
    sources = {}
    if sources_path.exists():
        try:
            sources = json.loads(sources_path.read_text())
        except json.JSONDecodeError:
            sources = {}

    sources[timestamp] = {"session_path": str(session_path), "uuid": match["uuid"]}
    sources_path.write_text(json.dumps(sources, indent=2))


def main() -> int:
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-o"],
        capture_output=True,
        text=True,
    )
    text = result.stdout.strip()
    if not text:
        return 0

    project_dir = transcript.current_project_dir()
    quickref_path = project_dir / transcript.NOTES_FILENAME

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n---\n\n*captured {timestamp}*\n\n{text}\n"

    with quickref_path.open("a") as f:
        f.write(entry)

    record_source(project_dir, timestamp, text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
