"""
utils/console.py
─────────────────
Windows' default console codepage (cp1252) can't encode many characters
this app and its vendored dependencies print or log (₹, →, •, emoji). Under
cp1252 that's not just a display glitch — it raises UnicodeEncodeError:
harmless for logging (Python's logging module catches it and prints
"--- Logging error ---" instead of crashing) but FATAL for a bare print()
call, since nothing catches it there.

Call fix_windows_console_encoding() as the very first thing in any entry
point (before importing utils.logger, which grabs a reference to sys.stdout
for its handler) — backend/app.py, and any script that can also run as its
own process (tick_replay_backtest.py is spawned as a subprocess by the
dashboard's Backtest button, so backend/app.py's fix doesn't cover it; it
needs its own).
"""
import sys


def fix_windows_console_encoding():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
