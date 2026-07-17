"""
In-game interaction log.

Hooks `relationships.relationship_objects.relationship.Relationship.add_relationship_bit`
so we know when sims add social bits to each other (Just Chatted, Just
Flirted, Just Argued, Just Kissed, etc). For pairs where at least one
participant is in the active household, we record the timestamp + bit
name. One entry per (sim_a, sim_b) pair, key sorted so it's symmetric;
each new interaction OVERWRITES the previous entry. File stays bounded
no matter how much the household socializes.

Prompt builders ask `most_recent_for_pair(sim_a, sim_b)` -- if the
returned timestamp is newer than the last journal entry between those
sims, surface "they just X'd in person" in the prompt.

Storage: <save folder>/Interactions.json -- per-save, alongside the
journal and milestones files. Atomic .tmp + os.replace writes match the
journal hardening pattern.

Verified hook signature in simulation.zip/relationships/relationship_-
objects/relationship.pyc:
  add_relationship_bit(self, actor_sim_id, target_sim_id, bit_to_add,
                       notify_client, pending_bits, force_add,
                       from_load, send_rel_change_event)
sim ids come in as positional args, NOT attributes on self.
"""

import datetime
import json
import os
import re
import threading

from . import save_id as _save_id

_FILENAME = "Interactions.json"

# Match bits that signal a recent social interaction. Patterns derived
# from real-game diagnostic logs -- the engine doesn't use a single
# naming convention so we match a set of known interaction-indicator
# words/phrases. Substring (not prefix) match: Sims 4 prefixes bit names
# with "Special Bits", "relbit", category names, etc., and CC mods like
# WickedWhims prepend their own tags. Patterns checked case-insensitively
# against the cleaned bit name.
#
# Tested signals seen in logs:
#   "Special Bits  Greeted"                  -> first contact in a convo
#   "relbit  Social Context  Casual"         -> active conversation state
#   "T U R B O D R I V E R: ...  Recently Had Social Interaction"
#                                            -> WickedWhims STC tag
# Excluded (don't match):
#   "multi unit neighbor", "neighbor"        -> permanent state
#   "rel Bit  Attraction ..."                -> internal calc
#   "relationshipbit  Compatibility  Bad"    -> compat calc result
#   "romantic-  Significant  Other"          -> family/partner state
_INTERACTION_BIT_PATTERN = re.compile(
    # Original interaction-event bits (Sims 4's Just X'd / Greeted /
    # Social Context Y / Recently Z patterns):
    r"\b(Just|Recently|Made|Greeted|Social\s*Context)\b|"
    # Sentiment bits and short-term bits: these fire when sims actively
    # socialize (Quality Time closeness, healed negative sentiment,
    # betrayed, charmed, etc.) They're a stronger signal in modern Sims
    # 4 patches than the older "Social Context Casual" bits, which now
    # fire less often for played-sim interactions.
    r"\bSentiment\s*Bit\b|"
    r"\bShort\s*Term\s*Bits?\b",
    re.IGNORECASE,
)

# Sentiment bits we DON'T want -- they're bookkeeping, not interactions.
# The general Sentiment pattern above catches everything; this second
# pass drops the ones that fire on save load / periodic decay rather
# than on a real interaction event.
_INTERACTION_BIT_EXCLUSIONS = re.compile(
    r"\b(Attraction\s*Suppress|Persistent\s*Boredom\s*Reset|"
    r"Reset\s*S\s*T|Bookmark)\b",
    re.IGNORECASE,
)


def _bit_is_interaction(cleaned):
    """Return True if a cleaned bit name represents a real interaction
    event worth logging. Split from the raw regex so we can add
    exclusion rules for false-positive bits without making the match
    regex unreadable."""
    if not cleaned:
        return False
    if _INTERACTION_BIT_EXCLUSIONS.search(cleaned):
        return False
    return bool(_INTERACTION_BIT_PATTERN.search(cleaned))


# Map cleaned bit names to a natural English phrase for the prompt.
# "You {phrase} in person recently." -- so the phrase reads as a verb
# clause without "just" repeating. Falls back to a generic phrasing
# when an unknown bit slips through the filter.
def _humanize_kind(cleaned):
    lower = (cleaned or "").lower()
    if "kissed" in lower or "made out" in lower:
        return "kissed"
    if "flirt" in lower:
        return "flirted"
    if "fight" in lower or "argued" in lower or "argument" in lower:
        return "had an argument"
    if "woohoo" in lower:
        return "had an intimate moment"
    if "chat" in lower:
        return "chatted"
    if "greeted" in lower:
        return "saw each other"
    if "romantic" in lower and "context" in lower:
        return "shared a romantic moment"
    if "social context" in lower or "had social" in lower:
        return "had a conversation"
    if "quality time" in lower or "close to" in lower:
        return "spent quality time together"
    if "healed negative" in lower:
        return "smoothed things over"
    if "betray" in lower:
        return "had a betrayal moment"
    if "charmed" in lower:
        return "charmed each other"
    if "sentiment" in lower:
        return "spent time together"
    if "physical intimacy" in lower:
        return "were physically close"
    if "recently" in lower:
        return "spent time together"
    if "just" in lower:
        # Generic "Just X" -- pull out the X and prepend "just"
        tail = lower.split("just", 1)[1].strip()
        if tail:
            return "just " + tail
    return "spent time together"

# How old an entry can get before cleanup_old will trim it. Sized so
# even a long-running save's file stays small and a stale entry doesn't
# misleadingly surface "they interacted recently" days later.
_RETENTION_DAYS = 30


# In-memory cache + the save id it was loaded for. Same pattern as journal.py.
_cache = None
_cached_for_save_id = None
_lock = threading.RLock()

# True after install_hook patches Relationship.add_relationship_bit.
_hook_installed = False


def _log(message):
    """Best-effort log line into the main Llamafone_Log.txt."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [interactions] {message}\n")
    except Exception:
        pass


def _path():
    """Per-save Interactions.json path; None when no save is loaded."""
    return _save_id.data_path(_FILENAME)


def _pair_key(sim_a_id, sim_b_id):
    """Sorted-pair key so lookups are symmetric. Returns 'min:max' as a
    string so it survives JSON serialization without losing precision on
    64-bit sim ids."""
    a, b = int(sim_a_id), int(sim_b_id)
    if a > b:
        a, b = b, a
    return f"{a}:{b}"


def _clean_bit_name(bit_to_add):
    """Turn the bit class reference into a human-readable string for
    pattern matching and humanization. Sims 4's bit class names are
    like `RelationshipBit_Friendship_JustChatted` or
    `Special_Bits_Greeted`; CC mods prepend their own tags. We strip
    well-known prefixes, split CamelCase, and collapse runs of
    whitespace so the result is loosely-natural English."""
    raw = getattr(bit_to_add, "__name__", "") or str(bit_to_add)
    raw = re.sub(r"^(RelationshipBit_|Relationship_Bit_)", "", raw)
    raw = re.sub(r"^(Friendship|Romance|Family|Conflict|Romantic)_", "", raw)
    # Insert a space before each interior uppercase letter (CamelCase split)
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", raw).replace("_", " ")
    # Collapse multi-space runs to single spaces, then strip ends.
    spaced = re.sub(r"\s+", " ", spaced).strip()
    return spaced


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _load():
    with _lock:
        global _cache, _cached_for_save_id
        current = _save_id.get_current_save_id()
        # Don't cache during the early-load window when the save isn't
        # ready. See contact_prefs._load for the full rationale.
        if current is None:
            return {}
        if _cache is not None and _cached_for_save_id == current:
            return _cache
        _cached_for_save_id = current
        path = _path()
        if path is None or not os.path.exists(path):
            _cache = {}
            return _cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
        except Exception as e:
            _log(f"_load: parse failed ({type(e).__name__}: {e}); starting fresh")
            _cache = {}
        return _cache


def _save(data):
    with _lock:
        global _cache, _cached_for_save_id
        _cache = data
        _cached_for_save_id = _save_id.get_current_save_id()
        path = _path()
        if path is None:
            return  # no save loaded
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


def _now_ingame_ticks():
    """Sim-world time as absolute ticks, or None if the time_service
    isn't ready. Mirrors the helper past_events.py uses."""
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


def record(sim_a_id, sim_b_id, kind):
    """Record (or overwrite) the most-recent interaction between two
    sims. Keyed by sorted pair, so direction doesn't matter.

    Logs both real-time ISO (for admin/debug + same-clock comparisons
    like the last_conv_iso suppression check) AND in-game ticks (so
    cleanup / rendering can reason in sim-world time, not the player's
    wall clock). Ticks are None when no save is loaded."""
    if sim_a_id is None or sim_b_id is None or sim_a_id == sim_b_id:
        return
    with _lock:
        data = _load()
        data[_pair_key(sim_a_id, sim_b_id)] = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticks": _now_ingame_ticks(),
            "kind": str(kind) if kind else "",
        }
        _save(data)


def most_recent_for_pair(sim_a_id, sim_b_id):
    """Return {'timestamp': iso8601, 'kind': str} for the last logged
    interaction between this pair, or None if no entry."""
    if sim_a_id is None or sim_b_id is None:
        return None
    return _load().get(_pair_key(sim_a_id, sim_b_id))


def cleanup_old(days=_RETENTION_DAYS):
    """Drop entries older than `days` in-game days from the file.

    Prefers in-game ticks when the entry has them (retention should
    reflect sim-world time, not the player's real-world calendar --
    otherwise shelving the game for a real month would wipe entries
    that were logged only hours ago in-game). Falls back to the
    real-time ISO timestamp for legacy entries that predate the ticks
    field. Idempotent; safe to call on every save load."""
    # 24h * 60min * 100ticks/min per in-game day (matches past_events).
    _TICKS_PER_DAY = 24 * 60 * 100
    with _lock:
        data = _load()
        if not data:
            return
        now_ticks = _now_ingame_ticks()
        cutoff_ticks = (now_ticks - days * _TICKS_PER_DAY) if now_ticks is not None else None
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        def _keep(entry):
            if not isinstance(entry, dict):
                return False
            t = entry.get("ticks")
            if t is not None and cutoff_ticks is not None:
                try:
                    return int(t) >= cutoff_ticks
                except Exception:
                    pass
            # Legacy entry or time_service not ready -- fall back to real time.
            return entry.get("timestamp", "") >= cutoff_iso

        before = len(data)
        keep = {k: v for k, v in data.items() if _keep(v)}
        if len(keep) < before:
            _save(keep)
            _log(f"cleanup_old: trimmed {before - len(keep)} entries older than {days} in-game days (ticks-aware)")


# ---------------------------------------------------------------------------
# Live hook
# ---------------------------------------------------------------------------

def _is_in_active_household(sim_id):
    """True if `sim_id` belongs to a sim in the currently-active household
    (the one the player is controlling)."""
    if sim_id is None:
        return False
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return False
        for si in hh.sim_info_gen():
            if getattr(si, "sim_id", None) == sim_id:
                return True
    except Exception:
        pass
    return False


def install_hook():
    """Monkey-patch Relationship.add_relationship_bit so every bit add
    also fires our recorder. Idempotent. Returns True on success or if
    already patched, False if the Relationship class can't be imported.

    Verified against the game class on this patch (2026-06-17):
      relationships.relationship_objects.relationship.Relationship
      .add_relationship_bit(self, actor_sim_id, target_sim_id,
                            bit_to_add, ...)
    The sim ids come in as positional args; we don't read self for them.
    """
    global _hook_installed
    if _hook_installed:
        return True
    try:
        from relationships.relationship_objects.relationship import Relationship
    except Exception as e:
        _log(f"install_hook: cannot import Relationship: {type(e).__name__}: {e}")
        return False
    if getattr(Relationship, "_llamafone_interactions_hooked", False):
        _hook_installed = True
        return True
    original = Relationship.add_relationship_bit

    def _patched(self, actor_sim_id, target_sim_id, bit_to_add, *args, **kwargs):
        result = original(self, actor_sim_id, target_sim_id, bit_to_add, *args, **kwargs)
        try:
            # Skip sim<->object relationships (e.g. sim's relationship
            # with their gravestone). We only care about sim<->sim.
            if getattr(self, "is_object_rel", False):
                return result
            # Filter: at least one participant must be in the active
            # household. Avoids logging NPC-on-NPC interactions the
            # player will never see in a prompt.
            if not (_is_in_active_household(actor_sim_id)
                    or _is_in_active_household(target_sim_id)):
                return result
            cleaned = _clean_bit_name(bit_to_add)
            # Only "Just X'd" / "Recently Y'd" / "Greeted" / "Social
            # Context X" / "Sentiment Bit" / "Short Term Bits" style
            # signals -- see _INTERACTION_BIT_PATTERN + exclusions.
            if not _bit_is_interaction(cleaned):
                return result
            record(actor_sim_id, target_sim_id, cleaned)
            # Only log successful records -- pre-release the diagnostic
            # was one line per bit-add (dozens per social move), which
            # filled the log fast in daily play. Filter failures are
            # silent; only actual interactions and errors log now.
            _log(f"recorded {actor_sim_id}<->{target_sim_id} {cleaned!r}")
        except Exception as e:
            _log(f"hook handler failed: {type(e).__name__}: {e}")
        return result

    Relationship.add_relationship_bit = _patched
    Relationship._llamafone_interactions_hooked = True
    _hook_installed = True
    _log("installed Relationship.add_relationship_bit hook")
    return True


# ---------------------------------------------------------------------------
# Prompt-helper
# ---------------------------------------------------------------------------

def _are_sims_on_same_lot(sim_a_id, sim_b_id):
    """True if both sims are currently instantiated on the same lot.
    Used to distinguish 'you saw each other 3h ago' (parted) from
    'you're standing next to each other right now' (still together).

    Sims 4 fires the Greeted / Social Context bits ONCE at the start
    of a social exchange, then not again for the duration of the
    conversation. So a 3-in-game-hour-old timestamp could either mean
    'we parted 3h ago' or 'we've been together for 3h and are still
    chatting.' The lot check disambiguates."""
    if not sim_a_id or not sim_b_id:
        return False
    try:
        import services
        mgr = services.sim_info_manager()
        if mgr is None:
            return False
        si_a = mgr.get(int(sim_a_id))
        si_b = mgr.get(int(sim_b_id))
        if si_a is None or si_b is None:
            return False
        # Sim.zone_id is the lot id when the sim is instantiated on a
        # lot. Not-instantiated sims (offscreen NPCs) have no sim
        # instance, so get_sim_instance() returns None.
        inst_a = si_a.get_sim_instance() if hasattr(si_a, "get_sim_instance") else None
        inst_b = si_b.get_sim_instance() if hasattr(si_b, "get_sim_instance") else None
        if inst_a is None or inst_b is None:
            return False
        return getattr(inst_a, "zone_id", None) == getattr(inst_b, "zone_id", None) \
               and inst_a.zone_id is not None
    except Exception:
        return False


def format_for_prompt(sim_a_id, sim_b_id, last_conv_iso=None):
    """Return a single bracket-tagged line for inclusion in a phone prompt,
    or empty string if no in-person interaction has been recorded.

    Behavior:
      - If both sims are currently on the same lot RIGHT NOW, we say so
        explicitly AND opportunistically refresh the recorded timestamp
        to 'now'. Sims 4 only fires the Greeted / Social Context bits
        ONCE at the start of a social exchange -- without this refresh,
        the entry would keep the start-of-conversation timestamp
        forever, so a 3-hour chat that just ended would render as 'we
        saw each other 3 hours ago' when the actual parting was 30
        seconds ago. Refreshing on every same-lot prompt call means
        the entry closely tracks 'when we last knew they were
        together.'
      - Otherwise, render WHEN the in-person happened in sim-world
        time (prefers ticks; falls back to real time for legacy
        entries without a ticks field).

    NOTE: `last_conv_iso` is accepted for backward compat but no
    longer suppresses -- the journal history is a separate block in
    the prompt, no double-count risk."""
    entry = most_recent_for_pair(sim_a_id, sim_b_id)
    if not entry:
        return ""
    phrase = _humanize_kind(entry.get("kind", ""))
    if _are_sims_on_same_lot(sim_a_id, sim_b_id):
        # Refresh the entry so future prompts (after they part) have
        # a fresh 'last seen together' timestamp instead of showing
        # the ancient start-of-conversation moment. Uses the existing
        # kind so we don't overwrite what interaction it was.
        try:
            record(sim_a_id, sim_b_id, entry.get("kind", ""))
        except Exception:
            pass
        return (
            f"\n[IN-PERSON CONTACT: You two are ON THE SAME LOT together "
            f"right now. Any 'we saw each other earlier' framing would "
            f"be wrong -- you're literally in each other's presence.]"
        )
    when_str = _format_recency(entry)
    when_tail = f" {when_str}" if when_str else " recently"
    return f"\n[RECENT IN-PERSON CONTACT: You two {phrase}{when_tail}.]"


# 100 ticks per in-game minute (matches past_events / journal / contact_prefs).
_TICKS_PER_HOUR_INTX = 60 * 100
_TICKS_PER_DAY_INTX = 24 * 60 * 100


def _format_recency(entry):
    """Render 'how long ago' for an interaction entry. Prefers ticks
    (sim time); falls back to real time for legacy entries. Empty
    string when neither works out."""
    entry_ticks = entry.get("ticks")
    now_ticks = _now_ingame_ticks()
    if entry_ticks is not None and now_ticks is not None:
        try:
            diff = now_ticks - int(entry_ticks)
            if diff < 0:
                return ""
            if diff < _TICKS_PER_HOUR_INTX:
                mins = max(1, diff // 100)
                return f"about {mins} in-game min ago"
            if diff < _TICKS_PER_DAY_INTX:
                hours = diff // _TICKS_PER_HOUR_INTX
                return f"about {hours} in-game hour{'s' if hours != 1 else ''} ago"
            days = diff // _TICKS_PER_DAY_INTX
            return f"{days} in-game day{'s' if days != 1 else ''} ago"
        except Exception:
            pass
    # Legacy: use real-time delta
    ts = entry.get("timestamp", "")
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(ts)
        delta = datetime.datetime.now() - dt
        seconds = delta.total_seconds()
        if seconds < 0:
            return ""
        if seconds < 3600:
            mins = max(1, int(seconds // 60))
            return f"about {mins} min ago"
        if seconds < 86400:
            hours = int(seconds // 3600)
            return f"about {hours} hour{'s' if hours != 1 else ''} ago"
        days = int(seconds // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    except Exception:
        return ""
