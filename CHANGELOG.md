# Changelog

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
