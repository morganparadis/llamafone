"""
Group-text persistence and lifecycle.

Group texts are threads with 2-4 participants (plus the household anchor
sim) where each recipient replies independently to the player's message,
and each participant sees what the others said. Unlike 1:1 conversations
(which live in phone._conversations, in-memory only), group threads are
persisted to `<save folder>/GroupTexts.json` because:

  1. They span 20+ real-world minutes (staggered reply delays).
  2. The briefing is an AI-generated summary of participant relationships
     that we compute ONCE at group creation and reuse for every reply
     for the life of the thread. Regenerating on reload would be waste.
  3. Rich history (who said what in which round) is worth keeping across
     save/load so a player can pick up a group thread later.

Storage schema (Slot_XXXXXXXX/GroupTexts.json):

  {
    "groups": {
      "grp_<hex>": {
        "group_id": "grp_<hex>",
        "created_at": "2026-07-07T12:34:56",
        "last_activity": "2026-07-07T12:45:00",
        "anchor_sim_id": <household member's sim_id>,
        "participant_sim_ids": [id, id, id, id],
        "participant_names": ["Sarah", "Bob", ...],  -- best-effort snapshot
        "briefing": "<AI-generated group summary>",
        "history": [
          {"role": "you", "text": "...", "ts": "..."},
          {"role": "them", "from_id": <sid>, "from_name": "Sarah",
           "text": "...", "ts": "...", "round": 1},
          ...
        ]
      }
    }
  }

Retention: groups inactive > 14 real-world days pruned on load.

Ephemeral, in-memory-only state lives in phone.py (round in progress,
pending timers, replied-this-round set). This module ONLY stores the
historical thread state. Reload-safe by design -- if mid-round when a
save unloads, the round is lost but the thread history stays.
"""

import datetime
import json
import os
import threading
import uuid

from . import save_id as _save_id


_FILENAME = "GroupTexts.json"

# Real-world day retention window. Threads older than this get dropped
# on the next cleanup_old pass. Kept modest so the file stays small
# even for players who send lots of group texts.
_RETENTION_DAYS = 14


_cache = None
_cached_for_save_id = None
_lock = threading.RLock()


def _log(message):
    """Best-effort log line into the main Llamafone_Log.txt."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [group_texts] {message}\n")
    except Exception:
        pass


def _path():
    """Per-save GroupTexts.json path; None when no save is loaded."""
    return _save_id.data_path(_FILENAME)


def _now_iso():
    return datetime.datetime.now().isoformat()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _load():
    """Load the on-disk JSON into cache, or reuse the cached dict when
    the same save is still loaded. Save-switch invalidates and re-reads
    (mirrors journal.py / past_events.py / interactions.py)."""
    with _lock:
        global _cache, _cached_for_save_id
        current = _save_id.get_current_save_id()
        if _cache is not None and _cached_for_save_id == current:
            return _cache
        _cached_for_save_id = current
        path = _path()
        if path is None or not os.path.exists(path):
            _cache = {"groups": {}}
            return _cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {"groups": {}}
            if "groups" not in data or not isinstance(data["groups"], dict):
                data["groups"] = {}
            _cache = data
        except Exception as e:
            _log(f"_load: parse failed ({type(e).__name__}: {e}); starting fresh")
            _cache = {"groups": {}}
        return _cache


def _save(data):
    """Atomic .tmp + os.replace write. Silently no-op when no save is
    loaded -- the caller has no fallback location, matching the pattern
    established by past_events._save."""
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


# ---------------------------------------------------------------------------
# Public API -- lifecycle
# ---------------------------------------------------------------------------

def create_group(anchor_sim_id, participant_sim_ids, participant_names=None):
    """Create OR RESUME a group thread and return its stable group_id.

    If there's already a group with the same (anchor, participants set),
    we RESUME that existing thread instead of spawning a new row --
    users think of "texting the same group again" as continuing the
    thread, not starting over. Preserves the cached briefing (so the
    caller can skip the briefing regeneration API call) and the full
    history (so the AI has continuity).

    anchor_sim_id: the household sim who initiated the group.
    participant_sim_ids: list of the OTHER sims' sim_ids (2..N).
    participant_names: optional snapshot of names in the same order.

    Returns None if we can't persist (no save loaded) OR the inputs
    are malformed. Callers should treat None as a hard failure."""
    if not anchor_sim_id or not participant_sim_ids:
        return None
    ids = [int(p) for p in participant_sim_ids if p]
    if not ids:
        return None

    names = [str(n) for n in (participant_names or [])][:len(ids)]
    now = _now_iso()
    anchor_int = int(anchor_sim_id)
    wanted_set = frozenset(ids)

    with _lock:
        data = _load()
        # Look for an existing group with the same anchor + same set
        # of participants (order-independent). If found, resume it.
        for existing_id, existing in data["groups"].items():
            try:
                if int(existing.get("anchor_sim_id", -1)) != anchor_int:
                    continue
                existing_set = frozenset(
                    int(p) for p in (existing.get("participant_sim_ids") or [])
                )
                if existing_set == wanted_set:
                    existing["last_activity"] = now
                    # Refresh name snapshot if the previous one was
                    # empty (edge case where the first creation missed
                    # names but this one has them).
                    if names and not existing.get("participant_names"):
                        existing["participant_names"] = names
                    _save(data)
                    _log(f"create_group: RESUMED existing group {existing_id} (anchor={anchor_int}, participants={ids})")
                    return existing_id
            except Exception:
                continue

        # No match -- create fresh
        group_id = f"grp_{uuid.uuid4().hex[:12]}"
        data["groups"][group_id] = {
            "group_id": group_id,
            "created_at": now,
            "last_activity": now,
            "anchor_sim_id": anchor_int,
            "participant_sim_ids": ids,
            "participant_names": names,
            "briefing": "",
            "history": [],
        }
        _save(data)
    _log(f"create_group: NEW {group_id} anchor={anchor_int} participants={ids}")
    return group_id


def set_briefing(group_id, briefing):
    """Cache the AI-generated group briefing on the group. Called once
    per group, after the briefing-generation call returns."""
    if not group_id or not briefing:
        return
    with _lock:
        data = _load()
        group = data["groups"].get(group_id)
        if not group:
            _log(f"set_briefing: unknown group_id {group_id}")
            return
        group["briefing"] = str(briefing)
        group["last_activity"] = _now_iso()
        _save(data)


def add_player_turn(group_id, text, round_num=None):
    """Record the player's outgoing message in the group thread."""
    if not group_id or not text:
        return
    entry = {
        "role": "you",
        "text": str(text),
        "ts": _now_iso(),
    }
    if round_num is not None:
        entry["round"] = int(round_num)
    _append_history(group_id, entry)


def add_participant_reply(group_id, from_sim_id, from_name, text, round_num=None):
    """Record a participant's reply in the group thread."""
    if not group_id or not from_sim_id or not text:
        return
    entry = {
        "role": "them",
        "from_id": int(from_sim_id),
        "from_name": str(from_name or ""),
        "text": str(text),
        "ts": _now_iso(),
    }
    if round_num is not None:
        entry["round"] = int(round_num)
    _append_history(group_id, entry)


def _append_history(group_id, entry):
    with _lock:
        data = _load()
        group = data["groups"].get(group_id)
        if not group:
            _log(f"_append_history: unknown group_id {group_id}")
            return
        group.setdefault("history", []).append(entry)
        group["last_activity"] = _now_iso()
        _save(data)


def get_group(group_id):
    """Return the raw group dict, or None. Read-only -- do not mutate."""
    if not group_id:
        return None
    return _load()["groups"].get(group_id)


def list_active_groups(anchor_sim_id=None):
    """Return all groups (optionally filtered to a specific anchor),
    newest-first by last_activity. Used by reply-button routing and
    debug commands."""
    data = _load()
    out = []
    for gid, g in data["groups"].items():
        if anchor_sim_id is not None and g.get("anchor_sim_id") != int(anchor_sim_id):
            continue
        out.append(g)
    out.sort(key=lambda g: g.get("last_activity", ""), reverse=True)
    return out


def most_recent_group():
    """Return the single most-recently-active group across ALL anchors,
    or None. Used by reply-button routing when the player hits Reply
    with no explicit conversation context."""
    groups = list_active_groups()
    return groups[0] if groups else None


def find_shared_groups(sim_a_id, sim_b_id, max_days=3):
    """Return groups where BOTH sim_a and sim_b are participants (or
    where one is the anchor and the other is a participant). Newest
    first, filtered to those with last_activity within max_days real-
    world days.

    Used by 1:1 prompt builders so when Alice later texts Sarah, the
    AI knows they were both just in a group with Bob and Kate.
    Cross-thread continuity is a real signal -- 'we were just in that
    group text where Bob went off' is exactly the kind of context that
    makes 1:1 conversations feel natural."""
    if not sim_a_id or not sim_b_id:
        return []
    try:
        sim_a = int(sim_a_id)
        sim_b = int(sim_b_id)
    except (TypeError, ValueError):
        return []
    if sim_a == sim_b:
        return []
    cutoff = None
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=max_days)
    except Exception:
        cutoff = None
    matches = []
    for g in list_active_groups():
        try:
            anchor = g.get("anchor_sim_id")
            parts = set(int(p) for p in (g.get("participant_sim_ids") or []))
            in_group = lambda sid: (sid == anchor) or (sid in parts)
            if not (in_group(sim_a) and in_group(sim_b)):
                continue
            if cutoff is not None:
                last_iso = g.get("last_activity") or g.get("created_at") or ""
                if last_iso:
                    try:
                        last = datetime.datetime.fromisoformat(last_iso)
                        if last < cutoff:
                            continue
                    except Exception:
                        pass
            matches.append(g)
        except Exception:
            continue
    return matches


def format_shared_for_prompt(sim_a_id, sim_b_id, sim_a_name=None, sim_b_name=None, max_tail=4):
    """Prompt block surfacing recent group threads shared by two sims.
    Returns '' when nothing recent is shared.

    Structure (one block per shared group, newest first):
      [SHARED GROUP TEXT: You (Alice) and Sarah were both in a group
       text with Bob, Kate. Recent excerpt from that thread:
         Bob: hey y'all around this weekend?
         Sarah: brunch??
         (you): could be
         ...]
    """
    groups = find_shared_groups(sim_a_id, sim_b_id)
    if not groups:
        return ""
    a_name = sim_a_name or "you"
    b_name = sim_b_name or "the other sim"
    now_real = datetime.datetime.now()

    def _recency(iso_str):
        """Human-friendly 'how long ago' for the group's last activity."""
        try:
            when = datetime.datetime.fromisoformat(iso_str)
            delta = now_real - when
            seconds = delta.total_seconds()
            if seconds < 3600:
                mins = max(1, int(seconds // 60))
                return f"about {mins} minute{'s' if mins != 1 else ''} ago"
            if seconds < 86400:
                hours = int(seconds // 3600)
                return f"about {hours} hour{'s' if hours != 1 else ''} ago (earlier today)"
            if seconds < 3 * 86400:
                days = int(seconds // 86400)
                return f"{days} day{'s' if days != 1 else ''} ago"
            return when.strftime("on %b %d")
        except Exception:
            return ""

    # Dedupe by participant set: if the player has multiple group
    # threads between the SAME participants, only surface the most
    # recent one. Otherwise the same "you and Vivian were both in a
    # group text with Apollo" header appears twice with different
    # excerpts and reads like double vision. `groups` is already
    # sorted newest-first by list_active_groups, so a simple "keep
    # first-seen per key" pass gets us there.
    seen_keys = set()
    unique_groups = []
    for g in groups:
        try:
            anchor = g.get("anchor_sim_id")
            parts = frozenset(int(p) for p in (g.get("participant_sim_ids") or []))
            # Include anchor in the key so a group with (Alice, Bob, Carol)
            # doesn't dedupe with a group whose participants are (Bob, Carol)
            # but anchored on a different household member.
            key = (int(anchor) if anchor else None, parts)
        except Exception:
            key = None
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_groups.append(g)

    lines = []
    for g in unique_groups[:2]:  # cap to the two most-recent unique participant sets
        try:
            anchor_id = g.get("anchor_sim_id")
            parts = list(g.get("participant_sim_ids") or [])
            names = list(g.get("participant_names") or [])
            # Build the "others in the group" list -- excludes both
            # sim_a and sim_b so the block reads naturally.
            others = []
            for i, pid in enumerate(parts):
                try:
                    if int(pid) in (int(sim_a_id), int(sim_b_id)):
                        continue
                except Exception:
                    continue
                nm = names[i] if i < len(names) else "?"
                others.append(nm)
            # Anchor might not be in participants (they're the household
            # side). If they're not sim_a or sim_b, surface them too.
            if anchor_id and anchor_id not in (int(sim_a_id), int(sim_b_id)):
                # Only add if we don't already have their name -- we
                # don't have anchor's name cached separately, so best-
                # effort skip.
                pass
            others_str = ", ".join(others) if others else "no one else"
            # Last N turns as an excerpt
            history = g.get("history") or []
            tail = history[-max_tail:] if len(history) > max_tail else history
            excerpt_lines = []
            for turn in tail:
                if turn.get("role") == "you":
                    # In-group "you" was the anchor -- render generically
                    excerpt_lines.append(f"  (household sim): {turn.get('text','')[:120]}")
                else:
                    from_name = turn.get("from_name", "?")
                    excerpt_lines.append(f"  {from_name}: {turn.get('text','')[:120]}")
            excerpt = "\n".join(excerpt_lines) if excerpt_lines else "  (no messages yet)"
            last_iso = g.get("last_activity") or g.get("created_at") or ""
            when_str = _recency(last_iso)
            when_tail = f" Last message: {when_str}." if when_str else ""
            block = (
                f"[SHARED GROUP TEXT: {a_name} and {b_name} were both in a group "
                f"text with {others_str}.{when_tail} Excerpt from that thread:\n{excerpt}]"
            )
            lines.append(block)
        except Exception:
            continue
    return "\n\n".join(lines)


def delete_group(group_id):
    """Remove a group entirely. No-op if it doesn't exist."""
    if not group_id:
        return
    with _lock:
        data = _load()
        if group_id in data["groups"]:
            del data["groups"][group_id]
            _save(data)
            _log(f"delete_group {group_id}")


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def cleanup_old(max_days=_RETENTION_DAYS):
    """Drop groups whose last_activity is older than max_days real-world
    days. Mirrors interactions.cleanup_old / past_events.cleanup_old."""
    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=max_days)
    except Exception:
        return 0
    dropped = 0
    with _lock:
        data = _load()
        keep = {}
        for gid, g in data["groups"].items():
            try:
                last_iso = g.get("last_activity") or g.get("created_at") or ""
                if not last_iso:
                    keep[gid] = g
                    continue
                last = datetime.datetime.fromisoformat(last_iso)
                if last >= cutoff:
                    keep[gid] = g
                else:
                    dropped += 1
            except Exception:
                # If we can't parse the timestamp, keep the group --
                # better than losing data on a parse edge case.
                keep[gid] = g
        if dropped:
            data["groups"] = keep
            _save(data)
            _log(f"cleanup_old: dropped {dropped}, kept {len(keep)}")
    return dropped


# ---------------------------------------------------------------------------
# Reset on save-switch (mirrors journal / past_events pattern)
# ---------------------------------------------------------------------------

def reset_cache():
    """Force the next _load() to re-read from disk. Called by the save-
    switch hook so a load-different-save doesn't leak the previous
    save's groups into the new one."""
    with _lock:
        global _cache, _cached_for_save_id
        _cache = None
        _cached_for_save_id = None
