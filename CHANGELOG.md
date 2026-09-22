# Changelog

## 2026-09-21

- Named captures now display at the very top of the notes view, ordered by
  when they were (re)named (most recently named first), ahead of all
  unnamed entries which keep their original capture order. Naming time is
  tracked in a new per-project `capture_names.json` sidecar (gitignored);
  `QUICKREF.md` itself is untouched and stays in capture order on disk.

## 2026-09-20

- Toggling the panel visible now re-attaches the file watcher and reloads
  content first, so switching to a brand-new project whose `QUICKREF.md`
  didn't exist yet (nothing to watch until the first clipboard capture
  creates it) picks up the file once it appears.
- Captured entries can now be named: "Name this capture..." /
  "Rename this capture..." on the right-click context menu. The name is
  stored as a heading line in `QUICKREF.md` right after the entry's
  timestamp and renders bold and larger than the note body, so named
  entries stand out when scanning notes.

## 2026-08-31

Initial commit. PyQt6 always-on-top study-notes panel (`panel.py`) with a
companion clipboard-capture hotkey script (`capture.py`) and shared
transcript-lookup helpers (`transcript.py`). Current feature set:

- Single-instance window, toggled show/hide by a global hotkey via a
  QLocalServer/QLocalSocket IPC handshake.
- Multi-project layout: every folder under `~/Documents/ClaudeProject/`
  is a project with its own `QUICKREF.md` notes and `sources.json`
  provenance file; File > Project switches between them live.
- Search box with a "Search all projects" checkbox to hop into other
  projects' notes once the current one is exhausted.
- Right-click context menu on a captured entry: "Show original context"
  (pulls the surrounding conversation from the source transcript) and
  "Delete this capture."
- No taskbar entry, and the panel relocates itself to whichever virtual
  desktop is active every time the hotkey shows it (raw EWMH client
  messages, since this box's WM doesn't support sticky windows).
