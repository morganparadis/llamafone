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
import threading

from . import config
from . import save_id as _save_id

_JOURNAL_FILENAME = "Journal.json"
_PROMPT_ENTRIES = 6         # how many recent entries to include in prompts
_PREVIEW_CHARS = 220        # max chars per entry shown in prompts

# In-memory cache + the save id it was loaded for. The cache is
# invalidated whenever the current save id changes (player switched
# saves) so two saves never share journal history.
_cache = None
_cached_for_save_id = None

# Reentrant lock protecting the cache and the on-disk file from
# interleaved access. Three things can call into this module concurrently:
# the main game thread (phone prompts, story commands), the auto_events
# daemon, and the milestones background scan. Without a lock, two writers
# can race the load -> append -> save sequence and lose entries. RLock
# (not Lock) is used because add_entry naturally calls _load and _save
# both of which acquire the same lock -- RLock makes reentrant grabs safe.
_lock = threading.RLock()

# 100 ticks per in-game minute. Same convention past_events / contact_prefs
# use. 1 in-game day = 24 * 60 * 100 = 144000 ticks.
_TICKS_PER_DAY = 24 * 60 * 100
_TICKS_PER_HOUR = 60 * 100


def _now_ingame_ticks():
    """Sim-world time as absolute ticks, or None if the time_service
    isn't ready. Journal entries record this alongside real-time ISO
    so 'hours ago' rendering can reflect sim time (not the player's
    calendar). Otherwise a game shelved for a real week would look like
    it happened a week ago even though only in-game minutes passed."""
    try:
        import services
        ts = services.time_service()
        now = getattr(ts, "sim_now", None) if ts else None
        if now is None:
            return None
        for attr in ("absolute_ticks", "value", "ticks"):
            fn = getattr(now, attr, None)
            if callable(fn):
                try:
                    return int(fn())
                except Exception:
                    continue
            if fn is not None:
                try:
                    return int(fn)
                except Exception:
                    continue
    except Exception:
        return None
    return None


def _journal_path():
    """Per-save journal path in the Sims 4 saves folder. Returns None
    when no save is loaded -- callers must skip reads/writes."""
    return _save_id.data_path(_JOURNAL_FILENAME)


def _log(message):
    """Diagnostic to Llamafone_Log.txt -- only fires on load/save failures
    so we can see if a user's journal ever has trouble."""
    try:
        log_path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(log_path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [journal] {message}\n")
    except Exception:
        pass


def _load():
    """Load from disk if cache is empty OR the current save changed.

    SAFETY RULE: never silently delete a journal. If parsing fails,
    rename the bad file to a timestamped .bak so the user can recover
    it manually -- never overwrite a corrupt journal blindly.
    """
    with _lock:
        global _cache, _cached_for_save_id
        current = _save_id.get_current_save_id()
        if _cache is not None and _cached_for_save_id == current:
            return _cache
        _cached_for_save_id = current
        path = _journal_path()
        if path is None or not os.path.exists(path):
            _cache = []
            return _cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                _cache = json.load(f)
            if not isinstance(_cache, list):
                _log(f"Journal at {path} parsed but isn't a list; preserving as .bak")
                try:
                    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    os.rename(path, f"{path}.notlist-{ts}.bak")
                except Exception:
                    pass
                _cache = []
        except Exception as e:
            try:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = f"{path}.corrupt-{ts}.bak"
                os.rename(path, bak)
                _log(f"Could not parse journal ({type(e).__name__}: {e}); preserved as {bak}")
            except Exception as e2:
                _log(f"Journal parse failed AND backup failed: {e} / {e2}")
            _cache = []
        return _cache


def _save(entries):
    """Atomic write -- write to .tmp, then os.replace() onto the real
    path. The original journal file is never partially-overwritten, so
    a crash mid-write can't corrupt it. No trimming: unbounded growth."""
    with _lock:
        global _cache, _cached_for_save_id
        _cache = entries
        _cached_for_save_id = _save_id.get_current_save_id()
        path = _journal_path()
        if path is None:
            return  # no save loaded -- nothing to persist
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass  # not all platforms; best-effort
            os.replace(tmp, path)  # atomic on POSIX + Windows (same volume)
        except Exception as e:
            _log(f"_save failed ({type(e).__name__}: {e}); journal on disk untouched")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def add_entry(content_type, content, sim_name=None, recipient_name=None,
              sim_id=None, recipient_id=None):
    """
    Save a generated piece of content to the journal.

    Args:
        content_type:   short string label e.g. "story", "event", "dialogue", "storyline"
        content:        the full generated text
        sim_name:       optional name of the sim this was generated for / from
        recipient_name: for phone calls/texts, the household sim who received it
        sim_id:         globally-unique sim id (preferred lookup key -- name
                        collisions like two "Bella Goth"s break name-based
                        lookups but sim_id is unique per sim, even across saves)
        recipient_id:   globally-unique sim id of the recipient

    Both names AND ids are stored when available: ids drive lookups, names
    drive display. Legacy entries with only names still match via the name
    fallback in `get_sim_history`.
    """
    # Hold the lock across the whole load -> append -> backfill -> save
    # sequence so concurrent writes (auto_events daemon, milestone scan,
    # main thread phone call) can't race and lose entries.
    with _lock:
        entries = _load()
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticks": _now_ingame_ticks(),
            "type": content_type,
            "content": content,
        }
        if sim_name:
            entry["sim"] = sim_name
        if recipient_name:
            entry["recipient"] = recipient_name
        if sim_id is not None:
            entry["sim_id"] = str(sim_id)
        if recipient_id is not None:
            entry["recipient_id"] = str(recipient_id)
        entries.append(entry)
        # Opportunistic backfill: if this write supplies a name<->id mapping,
        # apply it to any legacy entries with that name and no id. Self-heals
        # the journal as the player uses the mod -- no migration script.
        if sim_id is not None and sim_name:
            _backfill_id_for_name(entries, "sim", str(sim_id), sim_name)
        if recipient_id is not None and recipient_name:
            _backfill_id_for_name(entries, "recipient", str(recipient_id), recipient_name)
        _save(entries)


def _backfill_id_for_name(entries, field_prefix, new_id, name):
    """Walk `entries` and stamp `<field_prefix>_id = new_id` onto any legacy
    entry that has matching `<field_prefix>` name and no id yet. Mutates in
    place; caller is responsible for persisting.

    AMBIGUITY GUARD: if any existing entry already binds this name to a
    DIFFERENT id, the name is shared between two sims -- backfilling would
    misattribute one sim's legacy entries to the other. In that case we
    bail and leave the legacy entries name-only, so later lookups can still
    surface them via the name-fallback path."""
    id_field = f"{field_prefix}_id"
    name_lower = name.lower()
    # First pass: refuse to backfill if the name is already bound to another id.
    for e in entries:
        eid = e.get(id_field)
        if eid and eid != new_id and e.get(field_prefix, "").lower() == name_lower:
            return
    # Second pass: stamp the id onto legacy entries with this name.
    for e in entries:
        if e.get(id_field):
            continue
        if e.get(field_prefix, "").lower() == name_lower:
            e[id_field] = new_id


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


def last_entry_timestamp_for_pair(sim_id, recipient_id):
    """Return the ISO timestamp of the most-recent entry where both
    sim_id and recipient_id appear (in either direction), or None.
    Used by the interaction-awareness path to decide whether an
    in-game interaction is newer than the last conversation between
    these sims and worth surfacing in the next prompt."""
    if sim_id is None or recipient_id is None:
        return None
    sid, rid = str(sim_id), str(recipient_id)
    latest = None
    for e in _load():
        e_sim = e.get("sim_id")
        e_rec = e.get("recipient_id")
        if not e_sim or not e_rec:
            continue
        # Symmetric match -- either side of the pair.
        if (e_sim == sid and e_rec == rid) or (e_sim == rid and e_rec == sid):
            ts = e.get("timestamp")
            if ts and (latest is None or ts > latest):
                latest = ts
    return latest


def get_sim_history(sim_name, n=6, recipient_name=None,
                    sim_id=None, recipient_id=None):
    """
    Return recent journal entries involving a specific sim.

    Matching priority per field:
      - If the entry has an `<field>_id`, match against the provided id and
        ignore the name. IDs are authoritative -- two Bella Goths get
        separate histories.
      - If the entry has NO id (legacy data), fall back to name match.

    `sim_id` / `recipient_id` are optional. When not provided, lookup
    behaves exactly like pre-id-tracking versions (name-only).
    """
    sid = str(sim_id) if sim_id is not None else None
    rid = str(recipient_id) if recipient_id is not None else None
    sname_l = sim_name.lower() if sim_name else ""
    rname_l = recipient_name.lower() if recipient_name else None

    def _matches_sim(e):
        eid = e.get("sim_id")
        if eid:
            return sid is not None and eid == sid
        return e.get("sim", "").lower() == sname_l

    def _matches_recipient(e):
        eid = e.get("recipient_id")
        if eid:
            return rid is not None and eid == rid
        return e.get("recipient", "").lower() == rname_l

    matched = [e for e in _load() if _matches_sim(e)]
    if rname_l is not None or rid is not None:
        matched = [e for e in matched if _matches_recipient(e)]
    # Filter out legacy group_text entries. Group texts have their own
    # storage (GroupTexts.json) and their own prompt block via
    # group_texts.format_shared_for_prompt -- writing them to the
    # journal too produced duplicated 'Past interactions' entries that
    # overlapped with the SHARED GROUP TEXT block. Scrub them on read
    # so old saves that accumulated these entries stop leaking them
    # into prompts, without touching the file itself.
    matched = [e for e in matched if e.get("type") != "group_text"]
    return matched[-n:]


def format_sim_history_for_prompt(sim_name, n=6, recipient_name=None,
                                  trailing_note=None, sim_id=None,
                                  recipient_id=None, before_iso=None):
    """
    Return a prompt-friendly summary of recent interactions with a specific sim.
    If recipient_name is given, only includes history involving that recipient.
    Returns empty string if no history.

    `trailing_note`, if provided, is appended as a single line under the
    journal block -- used by callers to flag "these predate a relationship
    shift, treat the warmth as obsolete" without bloating the system prompt.

    `sim_id` / `recipient_id`, if provided, take priority over name matching.
    See `get_sim_history` for the exact precedence rules.

    `before_iso`, if provided, filters out entries with a timestamp at
    or after that value. Used by reply-flow prompts to exclude the
    current in-progress conversation from the 'Past interactions' block
    -- otherwise turns of the ongoing convo (which get logged live) show
    up in both 'Past interactions' AND 'Conversation so far'.
    """
    entries = get_sim_history(
        sim_name, n * 3 if before_iso else n,  # over-fetch if filtering to still return n after
        recipient_name=recipient_name,
        sim_id=sim_id,
        recipient_id=recipient_id,
    )
    if before_iso:
        entries = [e for e in entries if e.get("timestamp", "") < before_iso]
        entries = entries[:n]
    if not entries:
        return ""

    lines = [f"Past interactions with {sim_name}:"]
    now_real = datetime.datetime.now()
    now_ticks = _now_ingame_ticks()
    for e in entries:
        # For very recent entries, hours-ago carries urgency that a
        # bare date does not. Prevents the AI from saying 'missing
        # you today' when the last conversation was 3 hours ago.
        #
        # Prefer in-game ticks (sim time). Falls back to real time for
        # legacy entries without a ticks field OR when time_service
        # isn't ready. Real time drifts wrong when the player shelves
        # the game (11 real days = 0 in-game days), but ticks nail it.
        date_str = "?"
        try:
            entry_ticks = e.get("ticks")
            if entry_ticks is not None and now_ticks is not None and now_ticks >= int(entry_ticks):
                diff_ticks = now_ticks - int(entry_ticks)
                if diff_ticks < _TICKS_PER_HOUR:
                    mins = max(1, diff_ticks // 100)
                    date_str = f"~{mins} in-game min ago"
                elif diff_ticks < _TICKS_PER_DAY:
                    hours = diff_ticks // _TICKS_PER_HOUR
                    date_str = f"~{hours}h ago in-game, earlier today"
                elif diff_ticks < 2 * _TICKS_PER_DAY:
                    date_str = "yesterday in-game"
                elif diff_ticks < 7 * _TICKS_PER_DAY:
                    days = diff_ticks // _TICKS_PER_DAY
                    date_str = f"{days} in-game days ago"
                else:
                    # For older entries, fall back to real-time date --
                    # the exact number of in-game days gets fuzzy past
                    # a week and a real date is still useful as an anchor.
                    try:
                        dt = datetime.datetime.fromisoformat(e["timestamp"])
                        date_str = dt.strftime("%b %d")
                    except Exception:
                        date_str = f"{diff_ticks // _TICKS_PER_DAY} in-game days ago"
            else:
                # Legacy entry without ticks -- fall back to real-time delta
                dt = datetime.datetime.fromisoformat(e["timestamp"])
                delta = now_real - dt
                seconds = delta.total_seconds()
                if 0 <= seconds < 3600:
                    mins = max(1, int(seconds // 60))
                    date_str = f"~{mins} min ago"
                elif seconds < 86400:
                    hours = int(seconds // 3600)
                    date_str = f"~{hours}h ago"
                elif seconds < 2 * 86400:
                    date_str = "yesterday (real time)"
                else:
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
