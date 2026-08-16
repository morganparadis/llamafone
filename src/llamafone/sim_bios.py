"""
Per-sim biographical notes. Injected into that sim's descriptor block
in prompts, so any call/text/story involving them picks it up
automatically. Meant for private character truth -- backstory,
secrets, motivations, hidden context -- distinct from the Llamadate
bio (dating-facing profile) and the contact_prefs freeform note
(scoped to one pair).

Privacy: sim bios are treated as private personal info. Stripped from
Llamadate cold-outreach prompts when the recipient has a Llamadate
bio (bio-only mode) -- see phone.generate_text_for's recipient_override
handling.

Storage: <save>/SimBios.json -- {v: 1, sims: {sim_id_str: bio_str, ...}}
"""

import json
import os
import threading

from . import save_id

_FILENAME = "SimBios.json"
_SCHEMA_VERSION = 1

_cache_lock = threading.RLock()
_cache = {}  # {sim_id_str: bio_str}
_cached_for_save_id = None


def _log(msg):
    try:
        from . import _log as root_log
        root_log(f"[sim_bios] {msg}")
    except Exception:
        pass


def _path():
    return save_id.data_path(_FILENAME)


def _load_from_disk():
    path = _path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        sims = data.get("sims", {})
        if not isinstance(sims, dict):
            return {}
        # Coerce keys to str and drop empty values.
        return {str(k): str(v) for k, v in sims.items() if v}
    except Exception as e:
        _log(f"load failed: {type(e).__name__}: {e}")
        return {}


def _invalidate_if_save_changed():
    global _cached_for_save_id, _cache
    current = save_id.get_current_save_id()
    if current != _cached_for_save_id:
        _cache = {}
        _cached_for_save_id = current


def _all_bios():
    with _cache_lock:
        _invalidate_if_save_changed()
        sid = _cached_for_save_id
        if sid is None:
            return {}
        if not _cache:
            _cache.update(_load_from_disk())
        return dict(_cache)


def get_bio(sim_id):
    """Return the bio string for the given sim_id. Empty string if
    unset or no save is loaded. Accepts int or str sim_id."""
    if sim_id is None:
        return ""
    try:
        key = str(int(sim_id))
    except (TypeError, ValueError):
        key = str(sim_id)
    return _all_bios().get(key, "") or ""


def set_bio(sim_id, text):
    """Persist a bio for the given sim_id. Empty/blank clears it.
    Returns True on success, False if no save is loaded or write failed."""
    if sim_id is None:
        return False
    try:
        key = str(int(sim_id))
    except (TypeError, ValueError):
        key = str(sim_id)
    path = _path()
    if not path:
        return False
    text = str(text or "").strip()
    # Read-modify-write on the full dict so partial saves don't clobber
    # bios for other sims. Write is atomic per file, not per key.
    with _cache_lock:
        _invalidate_if_save_changed()
        if not _cache:
            _cache.update(_load_from_disk())
        if text:
            _cache[key] = text
        else:
            _cache.pop(key, None)
        snapshot = dict(_cache)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"v": _SCHEMA_VERSION, "sims": snapshot},
                f, ensure_ascii=False, indent=2,
            )
    except Exception as e:
        _log(f"write failed: {type(e).__name__}: {e}")
        return False
    return True


def format_for_prompt(sim_id, sim_name):
    """Return a prompt-ready block for the sim's bio, or empty string.
    Rendered as its own labeled block (not just a stray line) so it
    stands out from the routine attribute list -- the bio is
    player-authored ground truth about the sim's inner life, and the
    AI should weight it heavily when shaping voice, choices, and
    reactions.
    """
    bio = get_bio(sim_id).strip()
    if not bio or not sim_name:
        return ""
    return (
        f"\n=== {sim_name}'s BACKSTORY & CHARACTER TRUTH (player-authored) ===\n"
        f"{bio}\n"
        f"This is who {sim_name} really is. Treat it as ground truth for "
        f"how {sim_name} would act, how others would perceive them, and "
        f"how they'd be reacted to in this conversation. If {sim_name} is "
        f"the one speaking, shape their voice, choices, and emotional "
        f"register around this. If someone else is writing to or about "
        f"{sim_name}, factor it into that sim's response. Nobody blurts "
        f"this stuff out unprompted -- it's the underlying reality.\n"
        f"=== END BACKSTORY ===\n"
    )


def has_any_bios():
    """True if this save has at least one non-empty bio recorded.
    Used by the UI picker to sort 'sims-with-bios' to the top of the
    list, matching the contact_prefs / dating pattern."""
    return bool(_all_bios())
