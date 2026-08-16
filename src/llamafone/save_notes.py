"""
Save-level notes: one free-form text blob per save, prepended to every
AI prompt as WORLD CONTEXT. Meant for setting overall-save flavor that
should shape every call/text/story -- custom rulesets, ongoing narrative
context, house rules -- whatever the player wants the AI to treat as
universally binding.

Distinct from:
  - contact_prefs freeform notes: per-pair, private to one relationship
  - sim_bios: per-sim, injected into that sim's descriptor block
  - Llamadate bio: per-sim, dating-facing profile

Storage: <save>/SaveNotes.json -- {v: 1, notes: "..."}
"""

import json
import os
import threading

from . import save_id

_FILENAME = "SaveNotes.json"
_SCHEMA_VERSION = 1

_cache_lock = threading.RLock()
_cache = {}  # save_id -> notes string
_cached_for_save_id = None


def _log(msg):
    try:
        from . import _log as root_log
        root_log(f"[save_notes] {msg}")
    except Exception:
        pass


def _path():
    return save_id.data_path(_FILENAME)


def _load_from_disk():
    path = _path()
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        return str(data.get("notes", "") or "")
    except Exception as e:
        _log(f"load failed: {type(e).__name__}: {e}")
        return ""


def _invalidate_if_save_changed():
    global _cached_for_save_id
    current = save_id.get_current_save_id()
    if current != _cached_for_save_id:
        _cache.clear()
        _cached_for_save_id = current


def get_notes():
    """Return the save's notes string. Empty string when none set or
    no save is loaded. Cache-backed."""
    with _cache_lock:
        _invalidate_if_save_changed()
        sid = _cached_for_save_id
        if sid is None:
            return ""
        if sid in _cache:
            return _cache[sid]
        notes = _load_from_disk()
        _cache[sid] = notes
        return notes


def set_notes(text):
    """Set the save's notes. Writes to disk immediately. Returns True
    on success, False if no save is loaded or write failed."""
    path = _path()
    if not path:
        return False
    text = str(text or "").strip()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"v": _SCHEMA_VERSION, "notes": text},
                f, ensure_ascii=False, indent=2,
            )
    except Exception as e:
        _log(f"write failed: {type(e).__name__}: {e}")
        return False
    with _cache_lock:
        _invalidate_if_save_changed()
        sid = _cached_for_save_id
        if sid is not None:
            _cache[sid] = text
    return True


def format_for_prompt():
    """Return a prompt-ready block or empty string when notes are unset.
    Prepended to every system prompt via api_client -- so all downstream
    prompt-builders inherit the world context automatically."""
    notes = get_notes().strip()
    if not notes:
        return ""
    return (
        "=== WORLD CONTEXT (FINAL AUTHORITY -- overrides everything above) ===\n"
        + notes
        + "\n\nThe rules above assume a modern Sims 4 world (phones, "
        "texts, calls, video chat, cars, internet, apps). The world "
        "context here is FINAL. Every response must fit this world:\n"
        "- Reinterpret modality words: 'text' -> whatever writing "
        "medium fits this world (letter, note, telegram, message via "
        "courier). 'Call' -> whatever spoken exchange fits (in-person "
        "visit, telegraph, etc.). Same for 'video call', 'app', etc.\n"
        "- Drop any tech, brand, or setting reference from the rules "
        "above that doesn't exist in this world.\n"
        "- Vocabulary, register, cadence, and cultural references "
        "must fit the world context, not modern speech patterns.\n"
        "- If recent journal history contradicts the world (e.g. a "
        "past reply used the word 'text' or 'video call'), IGNORE "
        "those references and stay anchored to the world context.\n"
        "This context is above every other rule in this prompt.\n"
        "=== END WORLD CONTEXT ===\n"
    )
