"""
Per-relationship contact preferences.

Preferences are scoped to a PAIR of sims: (household_sim, other_sim).
This matters when the household has multiple sims -- if Alice muted
her ex, that pref must NOT bleed into Bob's phone activity with the
same ex. Each household member has their own view of every contact.

The UX story: Alice broke up with Chad. Alice wants Chad muted from
HER perspective. Bob (her brother) is still friends with Chad and
should keep getting Chad's texts. Storing under a compound key
(Alice_sim_id, Chad_sim_id) makes this fall out naturally.

States (all per-pair):
  muted    -- household sim never fires auto-events for this contact.
              Manual calls via llama.call still work.
  paused   -- asked for space. Auto-events fire at 20% rate. AI prompt
              tells the contact the household sim asked for space.
  priority -- favorite. Auto-events fire at 200% rate. AI treats warmly.
  null     -- no state, normal behavior.

Freeform note field always available regardless of state -- surfaces in
AI prompts as extra context ("kid's teacher", "asked for space", etc.).

Storage schema (Slot_XXXXXXXX/ContactPreferences.json):

  {
    "pairs": {
      "<household_sim_id>__<other_sim_id>": {
        "household_sim_id": <int>,
        "other_sim_id":     <int>,
        "state":            "muted" | "paused" | "priority" | null,
        "state_since_ticks": <in-game ticks int>,
        "state_since":      "<real-time ISO for admin>",
        "note":             "<freeform text>",
        "updated":          "<real-time ISO>"
      }
    },
    "_legacy_sims": {  # migration bucket, only present after upgrade
      "<other_sim_id>": {...}    # pre-pair-refactor entries
    }
  }

Migration: the pre-refactor schema used {"sims": {sim_id: entry}} with
no household context. On first load after upgrade, that dict is moved
to `_legacy_sims`. Reads for (household, other) fall back to legacy for
`other` when no pair entry exists -- so old prefs remain in effect
across the household until a per-pair write shadows them. Any
per-pair write for `other` scrubs the legacy entry so future reads
resolve deterministically.

Atomic .tmp + replace writes, RLock, save-switch cache invalidation --
mirrors past_events / interactions / group_texts.
"""

import datetime
import json
import os
import re
import threading

from . import save_id as _save_id


_FILENAME = "ContactPreferences.json"

# Valid state strings. None (or absent) means "no state, normal behavior."
STATE_MUTED = "muted"
STATE_PAUSED = "paused"
STATE_PRIORITY = "priority"
VALID_STATES = (STATE_MUTED, STATE_PAUSED, STATE_PRIORITY)

# Auto-event probability multipliers. Applied by auto_events before
# rolling the fire dice.
_STATE_MULTIPLIER = {
    STATE_MUTED:    0.0,
    STATE_PAUSED:   0.2,
    STATE_PRIORITY: 2.0,
}

# Sims 4 DateAndTime math: 100 ticks per minute. 1 in-game day = 144000
# ticks. Same convention past_events.py uses. Storing state_since as
# in-game ticks means "how long ago" in the AI prompt reflects sim-
# world time, not the player's real-world calendar -- so shelving the
# game for months doesn't rot the state.
_TICKS_PER_MINUTE = 100
_TICKS_PER_DAY = 24 * 60 * _TICKS_PER_MINUTE


_cache = None
_cached_for_save_id = None
_lock = threading.RLock()


def _log(message):
    """Best-effort log line into Llamafone_Log.txt."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [contact_prefs] {message}\n")
    except Exception:
        pass


def _path():
    """Per-save ContactPreferences.json path; None when no save loaded."""
    return _save_id.data_path(_FILENAME)


def _now_iso():
    return datetime.datetime.now().isoformat()


def _pair_key(household_id, other_id):
    """Compound key: '<household>__<other>' -- both ints stringified.
    Order matters: household first, then other. This makes 'all of
    Alice's prefs' trivially discoverable by prefix scanning the keys,
    and prevents (Alice, Bob) from colliding with (Bob, Alice)."""
    return f"{int(household_id)}__{int(other_id)}"


# ---------------------------------------------------------------------------
# In-game time helpers -- same pattern as past_events.py
# ---------------------------------------------------------------------------

def _ticks_of(dt_obj):
    """Pull a numeric tick value out of a Sims 4 DateAndTime object."""
    if dt_obj is None:
        return None
    for attr in ("absolute_ticks", "value", "ticks"):
        fn = getattr(dt_obj, attr, None)
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
    try:
        raw = str(dt_obj)
        if "(" in raw and raw.endswith(")"):
            return int(raw.split("(")[-1][:-1])
    except Exception:
        pass
    return None


def _now_ingame_ticks():
    """Current in-game time as absolute ticks. Returns None if no save
    is loaded or the time_service isn't ready yet."""
    try:
        import services
        ts = services.time_service()
        now = getattr(ts, "sim_now", None) if ts else None
        return _ticks_of(now)
    except Exception:
        return None


def _format_ingame_delta(ticks_diff):
    """Render a positive tick delta as a human-readable string."""
    if ticks_diff is None or ticks_diff < 0:
        return ""
    days = ticks_diff // _TICKS_PER_DAY
    if days == 0:
        return "earlier today (in-game)"
    if days == 1:
        return "1 in-game day ago"
    if days < 7:
        return f"{days} in-game days ago"
    weeks = days // 7
    if weeks == 1:
        return "about 1 in-game week ago"
    if weeks < 4:
        return f"{weeks} in-game weeks ago"
    months = days // 30
    if months == 1:
        return "about 1 in-game month ago"
    return f"about {months} in-game months ago"


# ---------------------------------------------------------------------------
# Storage + migration
# ---------------------------------------------------------------------------

def _load():
    """Load the on-disk JSON into cache. Auto-migrates the pre-pair
    schema (top-level `sims` dict) into `_legacy_sims` on first read,
    preserving existing entries as household-agnostic fallbacks until
    a per-pair write shadows them."""
    with _lock:
        global _cache, _cached_for_save_id
        current = _save_id.get_current_save_id()
        if _cache is not None and _cached_for_save_id == current:
            return _cache
        _cached_for_save_id = current
        path = _path()
        if path is None or not os.path.exists(path):
            _cache = {"pairs": {}}
            _log(f"_load: no file yet at {path!r}; cached empty")
            return _cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                _log(f"_load: file wasn't a dict (got {type(data).__name__}); refusing to overwrite disk. Cache empty for this session; restart to retry.")
                _cache = {"pairs": {}}
                return _cache
        except Exception as e:
            # SAFETY: don't overwrite the disk when parse fails --
            # the file might be recoverable, or the failure might be
            # a transient file-lock race. Cache empty for this session
            # only; the next successful load (e.g. after restart) will
            # populate correctly. The previous version wrote {"pairs":{}}
            # back over the parse-failed file, permanently nuking data.
            _log(f"_load: parse failed ({type(e).__name__}: {e}); refusing to overwrite disk. Cache empty for this session only.")
            _cache = {"pairs": {}}
            return _cache

        # Migrate the legacy top-level "sims" dict to a bucket that we
        # keep as a household-agnostic fallback. Never mutates entries
        # -- just moves them under a new key so writes don't stomp on
        # them and reads can fall back for household sims that haven't
        # set their own pair-specific override yet.
        did_migrate = False
        if "sims" in data and isinstance(data.get("sims"), dict):
            legacy = data.pop("sims")
            existing_legacy = data.get("_legacy_sims") or {}
            existing_legacy.update(legacy)
            data["_legacy_sims"] = existing_legacy
            did_migrate = True
            _log(f"_load: migrated {len(legacy)} legacy entries -> _legacy_sims (kept as household-agnostic fallback)")

        if "pairs" not in data or not isinstance(data.get("pairs"), dict):
            data["pairs"] = {}

        _cache = data
        n_pairs = len(data.get("pairs") or {})
        n_legacy = len(data.get("_legacy_sims") or {})
        _log(f"_load: OK -- {n_pairs} pair(s), {n_legacy} legacy entry/entries from {os.path.basename(path)}")

        # Persist the new format ONLY if we actually migrated -- previous
        # versions wrote back on every load (the branch condition was
        # always true), which meant a transient parse failure would
        # save an empty dict over good data. Now the write-back
        # happens only when there's a real structural change.
        if did_migrate:
            try:
                _save(data)
            except Exception:
                pass
        return _cache


def _save(data):
    """Atomic write. No-op when no save is loaded."""
    with _lock:
        global _cache, _cached_for_save_id
        _cache = data
        _cached_for_save_id = _save_id.get_current_save_id()
        path = _path()
        if path is None:
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, path)
        except Exception as e:
            _log(f"_save failed: {type(e).__name__}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def _scrub_legacy(other_id):
    """Called after any per-pair write for `other_id` so future reads
    resolve deterministically to the new format. Mutates the cache."""
    if not other_id:
        return
    try:
        key = str(int(other_id))
        with _lock:
            data = _load()
            legacy = data.get("_legacy_sims") or {}
            if key in legacy:
                del legacy[key]
                if not legacy:
                    data.pop("_legacy_sims", None)
                else:
                    data["_legacy_sims"] = legacy
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API -- per (household_sim_id, other_sim_id) pair
# ---------------------------------------------------------------------------

def get_prefs(household_id, other_id):
    """Return the prefs dict for a (household, other) pair, or None.
    Falls back to legacy per-sim entry if no pair entry exists, so
    pre-refactor prefs stay in effect until explicitly overridden."""
    if not household_id or not other_id:
        return None
    data = _load()
    entry = data.get("pairs", {}).get(_pair_key(household_id, other_id))
    if entry:
        return entry
    # Legacy fallback: household-agnostic entry from the pre-pair schema.
    legacy = data.get("_legacy_sims", {}).get(str(int(other_id)))
    if legacy:
        return legacy
    return None


def get_state(household_id, other_id):
    """Return the state string ('muted'/'paused'/'priority') or None."""
    e = get_prefs(household_id, other_id)
    return e.get("state") if e else None


def get_note(household_id, other_id):
    """Return the freeform note for a pair, or empty string."""
    e = get_prefs(household_id, other_id)
    return (e.get("note") or "") if e else ""


def set_state(household_id, other_id, state):
    """Set state for a pair. Passing None (or any invalid string)
    clears the state but preserves the note. Any write scrubs the
    legacy fallback so it can't shadow the new pair value later."""
    if not household_id or not other_id:
        _log(f"set_state: SKIPPED (missing id) household_id={household_id!r} other_id={other_id!r} state={state!r}")
        return
    key = _pair_key(household_id, other_id)
    with _lock:
        data = _load()
        entry = data["pairs"].get(key, {
            "household_sim_id": int(household_id),
            "other_sim_id": int(other_id),
        })
        entry["household_sim_id"] = int(household_id)
        entry["other_sim_id"] = int(other_id)
        if state in VALID_STATES:
            entry["state"] = state
            ingame = _now_ingame_ticks()
            if ingame is not None:
                entry["state_since_ticks"] = ingame
            else:
                entry.pop("state_since_ticks", None)
            entry["state_since"] = _now_iso()
        else:
            entry.pop("state", None)
            entry.pop("state_since", None)
            entry.pop("state_since_ticks", None)
        entry["updated"] = _now_iso()
        # If clearing state and no note, drop the whole entry to keep
        # the file small.
        if not entry.get("state") and not entry.get("note"):
            data["pairs"].pop(key, None)
        else:
            data["pairs"][key] = entry
        _save(data)
    _scrub_legacy(other_id)
    _log(f"set_state({household_id}, {other_id}, {state!r})")


def set_note(household_id, other_id, note):
    """Set the freeform note for a pair. Empty clears the note."""
    if not household_id or not other_id:
        _log(f"set_note: SKIPPED (missing id) household_id={household_id!r} other_id={other_id!r} note={(note or '')[:60]!r}")
        return
    key = _pair_key(household_id, other_id)
    with _lock:
        data = _load()
        entry = data["pairs"].get(key, {
            "household_sim_id": int(household_id),
            "other_sim_id": int(other_id),
        })
        entry["household_sim_id"] = int(household_id)
        entry["other_sim_id"] = int(other_id)
        note = str(note or "").strip()
        if note:
            entry["note"] = note
        else:
            entry.pop("note", None)
        entry["updated"] = _now_iso()
        if not entry.get("state") and not entry.get("note"):
            data["pairs"].pop(key, None)
        else:
            data["pairs"][key] = entry
        _save(data)
    _scrub_legacy(other_id)


def clear_prefs(household_id, other_id):
    """Wipe all prefs for a pair (state + note)."""
    if not household_id or not other_id:
        return
    key = _pair_key(household_id, other_id)
    with _lock:
        data = _load()
        if key in data.get("pairs", {}):
            del data["pairs"][key]
            _save(data)
    _scrub_legacy(other_id)


def list_all():
    """Return a dict of {pair_key: entry} for every pair with prefs
    set. Used by the management UI to enumerate overrides. Includes
    both new pair entries and any surviving legacy fallbacks (rendered
    with household_sim_id=None so the UI can flag them as unscoped)."""
    data = _load()
    out = dict(data.get("pairs", {}))
    # Include unscoped legacy entries so nothing is invisible in the UI
    for other_id_str, entry in (data.get("_legacy_sims") or {}).items():
        marker_key = f"legacy__{other_id_str}"
        legacy_view = dict(entry)
        legacy_view["household_sim_id"] = None
        legacy_view["other_sim_id"] = int(other_id_str) if other_id_str.isdigit() else other_id_str
        legacy_view["_legacy"] = True
        out[marker_key] = legacy_view
    return out


def list_for_household(household_id):
    """Return all pair entries for a specific household sim, plus any
    legacy entries (which apply to every household member as a
    fallback). Used by the per-sim contact-manager picker."""
    if not household_id:
        return {}
    data = _load()
    hh = int(household_id)
    out = {}
    for key, entry in (data.get("pairs") or {}).items():
        if entry.get("household_sim_id") == hh:
            out[key] = entry
    # Fold legacy entries in so this household sees them as active
    # prefs. Once the player interacts with a legacy contact via the
    # new UI, the legacy entry gets scrubbed and the pair entry wins.
    for other_id_str, entry in (data.get("_legacy_sims") or {}).items():
        legacy_view = dict(entry)
        legacy_view["household_sim_id"] = None
        legacy_view["other_sim_id"] = int(other_id_str) if other_id_str.isdigit() else other_id_str
        legacy_view["_legacy"] = True
        out[f"legacy__{other_id_str}"] = legacy_view
    return out


# ---------------------------------------------------------------------------
# Integration helpers -- called from auto_events and prompt builders
# ---------------------------------------------------------------------------

def auto_event_multiplier(household_id, other_id):
    """Probability multiplier for firing an auto-event for this pair.
    1.0 = normal, 0.0 = never, >1.0 = more often."""
    state = get_state(household_id, other_id)
    return _STATE_MULTIPLIER.get(state, 1.0)


def is_muted(household_id, other_id):
    """Convenience check -- True if this pair is muted."""
    return get_state(household_id, other_id) == STATE_MUTED


def format_for_prompt(household_id, other_id, other_name=None, household_name=None):
    """Render the state + note as a prompt block for the AI. Empty
    when nothing is set for this pair.

    household_name / other_name are optional -- if provided they read
    naturally ('The player asked Chad for space'); if omitted we fall
    back to generic phrasing."""
    if not household_id or not other_id:
        return ""
    e = get_prefs(household_id, other_id)
    if not e:
        return ""
    lines = []
    state = e.get("state")
    note = e.get("note") or ""
    who = other_name or "this sim"
    from_who = household_name or "the player"

    if state == STATE_PAUSED:
        since_ticks = e.get("state_since_ticks")
        when_str = ""
        try:
            if since_ticks is not None:
                now_ticks = _now_ingame_ticks()
                if now_ticks is not None and now_ticks >= since_ticks:
                    delta = _format_ingame_delta(now_ticks - since_ticks)
                    if delta:
                        when_str = f" {delta}"
        except Exception:
            pass
        lines.append(
            f"[BOUNDARY STATE] {from_who} asked {who} for space{when_str}. "
            f"{who} is aware of that boundary. This overrides any friendly "
            f"tone in older history. If any earlier chat/text history "
            f"conflicts with the boundary, treat the history as pre-dating "
            f"the request and write as {who} would today. Write ONE plausible "
            f"message from {who} -- no meta-commentary, no listing of "
            f"options, no questions to the player. Just the message text."
        )
    elif state == STATE_MUTED:
        lines.append(
            f"[BOUNDARY STATE] {from_who} has muted {who}. Any conversation "
            f"is unusual and unwelcome from {from_who}'s side. Write ONE "
            f"plausible message from {who} -- no meta-commentary, no "
            f"listing of options, no questions to the player."
        )
    elif state == STATE_PRIORITY:
        lines.append(
            f"[PRIORITY STATE] {from_who} marked {who} as a priority "
            f"contact. Relationship is warm and current."
        )

    if note:
        lines.append(
            f"### PLAYER-AUTHORED NOTE about {who} ###\n"
            f"\"{note}\"\n"
            f"This is context {from_who} wrote about how they see {who}. "
            f"Weight this heavily -- it captures nuance the game's own "
            f"relationship data doesn't."
        )

    return "\n\n".join(lines)


def reset_cache():
    """Force next _load() to re-read from disk."""
    with _lock:
        global _cache, _cached_for_save_id
        _cache = None
        _cached_for_save_id = None


# ---------------------------------------------------------------------------
# Auto-detection: parse messages for distance signals and auto-apply.
# ---------------------------------------------------------------------------

_MUTE_PATTERN = re.compile(
    r"(?i)\b("
    r"leave me alone|"
    r"don'?t (ever )?(call|text|contact|message) me( again)?|"
    r"never (call|text|contact|message) me( again)?|"
    r"stop (calling|texting|contacting|messaging) me|"
    r"we'?re (done|through|over)|"
    r"it'?s over between us|"
    r"lose my number|"
    r"we'?re not (together|dating) anymore|"
    r"i'?m breaking up with you|"
    r"i'?m done with you"
    r")\b"
)

_PAUSED_PATTERN = re.compile(
    r"(?i)\b("
    r"need (some )?space|"
    r"need (some )?time (alone|apart|to myself)|"
    r"need a break|"
    r"give me (some )?space|"
    r"back off|"
    r"time apart|"
    r"asked you for space|"
    r"asking for space"
    r")\b"
)


def detect_state_from_text(text):
    """Scan a message for distance signals. Returns 'muted' | 'paused'
    | None. Muted-tier wins if both match."""
    if not text:
        return None
    if _MUTE_PATTERN.search(text):
        return STATE_MUTED
    if _PAUSED_PATTERN.search(text):
        return STATE_PAUSED
    return None


def maybe_auto_apply(household_id, other_id, other_name, text, source_label=""):
    """Scan text; if a distance signal is detected, auto-apply the
    matching state for this (household, other) pair. Returns the
    applied state string, or None if nothing changed.

    Rules:
      - Priority-tier is never auto-changed (player favorited them).
      - Never downgrade muted -> paused.
      - Never rewrite an already-matching state (idempotent)."""
    if not household_id or not other_id or not text:
        return None
    detected = detect_state_from_text(text)
    if detected is None:
        return None
    current = get_state(household_id, other_id)
    if current == STATE_PRIORITY:
        return None
    if current == detected:
        return None
    if current == STATE_MUTED and detected == STATE_PAUSED:
        return None

    set_state(household_id, other_id, detected)

    try:
        existing = get_note(household_id, other_id) or ""
        tag_bits = ["auto"]
        if source_label:
            tag_bits.append(source_label)
        tag_bits.append(detected)
        auto_tag = "[" + " / ".join(tag_bits) + "]"
        if auto_tag not in existing:
            combined = (existing + " " + auto_tag).strip() if existing else auto_tag
            set_note(household_id, other_id, combined[:250])
    except Exception:
        pass

    _log(f"maybe_auto_apply: ({household_id}, {other_id}, {other_name!r}) -> {detected} (source: {source_label!r})")
    return detected
