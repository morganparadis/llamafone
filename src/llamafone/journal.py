"""
Persistent journal — saves generated stories, events, and dialogue to a JSON file
so the AI can reference past events across play sessions.

The journal file lives in the same folder as llamafone.cfg (your Mods folder):
  Llamafone_Journal.json

Uses an in-memory cache so the file is only read from disk once, then kept in
memory. Writes still go to disk immediately for persistence.
"""

import datetime
import json
import os

from . import config

_JOURNAL_FILENAME = "Llamafone_Journal.json"
_MAX_ENTRIES = 150          # entries kept on disk before oldest are pruned
_PROMPT_ENTRIES = 6         # how many recent entries to include in prompts
_PREVIEW_CHARS = 220        # max chars per entry shown in prompts

# In-memory cache — loaded once, kept in sync with disk
_cache = None


def _journal_path():
    cfg = config._find_config_file()
    if cfg:
        return os.path.join(os.path.dirname(cfg), _JOURNAL_FILENAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", _JOURNAL_FILENAME)


def _load():
    """Load from disk only if cache is empty, otherwise return cache."""
    global _cache
    if _cache is not None:
        return _cache
    path = _journal_path()
    if not os.path.exists(path):
        _cache = []
        return _cache
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            _cache = data if isinstance(data, list) else []
    except Exception:
        _cache = []
    return _cache


def _save(entries):
    """Write to disk and update cache."""
    global _cache
    trimmed = entries[-_MAX_ENTRIES:]
    _cache = trimmed
    path = _journal_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def add_entry(content_type, content, sim_name=None, recipient_name=None):
    """
    Save a generated piece of content to the journal.

    Args:
        content_type:   short string label e.g. "story", "event", "dialogue", "storyline"
        content:        the full generated text
        sim_name:       optional name of the sim this was generated for / from
        recipient_name: for phone calls/texts, the household sim who received it
    """
    entries = _load()
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "type": content_type,
        "content": content,
    }
    if sim_name:
        entry["sim"] = sim_name
    if recipient_name:
        entry["recipient"] = recipient_name
    entries.append(entry)
    _save(entries)


def clear():
    """Wipe the journal file."""
    _save([])


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

def get_recent(n=_PROMPT_ENTRIES):
    """Return the last n journal entries as a list of dicts."""
    return _load()[-n:]


def format_for_prompt(n=_PROMPT_ENTRIES):
    """
    Return a compact, prompt-friendly summary of recent journal entries.
    Returns an empty string if the journal is empty.
    """
    entries = get_recent(n)
    if not entries:
        return ""

    lines = ["Story so far (recent journal entries):"]
    for e in entries:
        try:
            dt = datetime.datetime.fromisoformat(e["timestamp"])
            date_str = dt.strftime("%b %d, %Y")
        except Exception:
            date_str = "unknown date"

        label = e.get("type", "note").replace("_", " ").title()
        sim_part = f" [{e['sim']}]" if e.get("sim") else ""
        preview = e.get("content", "").replace("\n", " ").strip()[:_PREVIEW_CHARS]
        if len(e.get("content", "")) > _PREVIEW_CHARS:
            preview += "..."

        lines.append(f"  [{date_str}] {label}{sim_part}: {preview}")

    return "\n".join(lines)


def get_entry_count():
    return len(_load())


def get_sim_history(sim_name, n=6, recipient_name=None):
    """
    Return recent journal entries involving a specific sim.
    If recipient_name is given, ONLY return entries explicitly tagged with that
    recipient — legacy entries without a recipient field are excluded to prevent
    cross-recipient contamination (e.g. old texts addressed to one sim leaking
    into prompts for another).
    """
    entries = _load()
    matched = [e for e in entries if e.get("sim", "").lower() == sim_name.lower()]
    if recipient_name:
        rn = recipient_name.lower()
        matched = [e for e in matched if e.get("recipient", "").lower() == rn]
    return matched[-n:]


def format_sim_history_for_prompt(sim_name, n=6, recipient_name=None, trailing_note=None):
    """
    Return a prompt-friendly summary of recent interactions with a specific sim.
    If recipient_name is given, only includes history involving that recipient.
    Returns empty string if no history.

    `trailing_note`, if provided, is appended as a single line under the
    journal block -- used by callers to flag "these predate a relationship
    shift, treat the warmth as obsolete" without bloating the system prompt.
    """
    entries = get_sim_history(sim_name, n, recipient_name=recipient_name)
    if not entries:
        return ""

    lines = [f"Past interactions with {sim_name}:"]
    for e in entries:
        try:
            dt = datetime.datetime.fromisoformat(e["timestamp"])
            date_str = dt.strftime("%b %d")
        except Exception:
            date_str = "?"

        label = e.get("type", "note").replace("_", " ").title()
        preview = e.get("content", "").replace("\n", " ").strip()[:_PREVIEW_CHARS]
        if len(e.get("content", "")) > _PREVIEW_CHARS:
            preview += "..."

        lines.append(f"  [{date_str}] {label}: {preview}")

    if trailing_note:
        lines.append(trailing_note)

    return "\n".join(lines)


def format_recent_for_display(n=10):
    """Longer version for the llama.journal command — shows more content."""
    entries = get_recent(n)
    if not entries:
        return "Journal is empty. Generate some stories or events to start building history!"

    lines = [f"=== Llamafone Journal ({get_entry_count()} total entries) ==="]
    for e in reversed(entries):  # newest first for display
        try:
            dt = datetime.datetime.fromisoformat(e["timestamp"])
            date_str = dt.strftime("%b %d %Y %H:%M")
        except Exception:
            date_str = "?"
        label = e.get("type", "note").replace("_", " ").title()
        sim_part = f" — {e['sim']}" if e.get("sim") else ""
        preview = e.get("content", "").strip()[:400]
        lines.append(f"\n[{date_str}] {label}{sim_part}")
        lines.append(preview)

    return "\n".join(lines)
