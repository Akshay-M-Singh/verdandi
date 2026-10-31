"""Verdandi desktop app (D66) — Phase 0 scaffold.

`desktop/` is the downloadable application: a Tauri window (src-tauri/), a
React interface (src/), and this Python engine service — a local helper that
wraps the EXISTING pipeline (text_layer capture + extract) behind a small
HTTP API. The core project never imports anything from `desktop/`.
"""

__version__ = "1.1.0"
