"""
Root-level loader for the Llamafone mod.
"""
import os
import datetime
import sys

_LOG = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")


def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now()}] {msg}\n")
    except Exception:
        pass


_log("=== llamafone_loader.py executed ===")
_log(f"Python version: {sys.version}")
_log(f"sys.path: {sys.path[:5]}")
_log(f"__file__: {__file__}")

try:
    import llamafone
    _log("claude_ai package imported successfully")
except Exception as e:
    _log(f"claude_ai import FAILED: {type(e).__name__}: {e}")
    import traceback
    _log(traceback.format_exc())
