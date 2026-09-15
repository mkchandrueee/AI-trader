import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_formatter)

# File handler. Must be explicit UTF-8: FileHandler's default encoding is
# locale.getpreferredencoding() (cp1252 on Windows), and unlike sys.stdout
# it's completely unaffected by PYTHONIOENCODING or
# fix_windows_console_encoding() — those only ever touch the console
# streams. Any log message with a non-Latin1 character (→, ₹, ε, ...)
# silently broke this handler (Python logging swallows handler errors, so
# it never crashed the process — it just spammed "--- Logging error ---"
# plus a full traceback into whatever was watching the output, which read
# as a fake failure in job progress on the AI Models page).
_file = logging.FileHandler(LOG_DIR / "trading.log", encoding="utf-8")
_file.setFormatter(_formatter)

logger = logging.getLogger("ai_trader")
logger.setLevel(logging.INFO)
logger.addHandler(_console)
logger.addHandler(_file)


def get_logger(name: str) -> logging.Logger:
    child = logger.getChild(name)
    return child