"""
Life-milestone tracker — snapshots key sim attributes on each game load
and diffs against the previous snapshot to surface recent events that
calls / texts / stories can reference.

Tracked attributes per sim:
  - age stage (TEEN, ADULT, etc.)
  - active career (name + level)
  - is_dead (ghost)
  - is_pregnant
  - spouse sim_id
  - in_household (whether they live with the player)
  - aspiration

Two files live alongside llamafone.cfg in the Mods folder:
  - Llamafone_SimSnapshots.json — last known state per sim
  - Llamafone_Milestones.json   — chronological list of detected events
"""

import datetime
import json
import os
import threading

from . import config, sim_context
from . import save_id as _save_id

_SNAPSHOTS_FILENAME = "SimSnapshots.json"
_MILESTONES_FILENAME = "Milestones.json"
# Per-contact tracker of which milestones each contact has already had
# surfaced to them, so the same sim doesn't keep asking the player about
# the same job-quit / promotion / breakup across multiple calls.
_REFERENCES_FILENAME = "MilestoneRefs.json"

# Cap the milestones log so it doesn't grow unbounded.
_MAX_MILESTONES = 200

# How many recent milestones to surface for any one sim when building a prompt.
_PROMPT_MILESTONES_PER_SIM = 4

# Only include milestones from the last N real-world days in prompts.
_PROMPT_RECENCY_DAYS = 7


def _log(message):
    """Best-effort log line into the main Llamafone_Log.txt."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [milestones] {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------
#
# Reentrant lock guarding ALL snapshot / milestones / references file
# operations. The background scan daemon and the main game thread both
# read and write these files; without the lock, a phone prompt building
# milestones could hit the JSON file mid-write and get a JSONDecodeError.
# RLock so nested helpers (scan_and_record calls _load_snapshots then
# _save_snapshots while holding the lock) work cleanly.
_lock = threading.RLock()


def _atomic_write_json(path, data):
    """Write JSON via .tmp + fsync + os.replace so a crash mid-write can't
    corrupt the file. Mirrors the journal hardening pattern. Best-effort:
    on failure the on-disk file is left untouched and the tmp is cleaned
    up if possible."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass  # not all platforms; best-effort
        os.replace(tmp, path)
    except Exception as e:
        _log(f"_atomic_write_json({path}) failed: {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _snapshots_path():
    """Per-save snapshots path. Returns None when no save is loaded."""
    return _save_id.data_path(_SNAPSHOTS_FILENAME)


def _milestones_path():
    """Per-save milestones log path. Returns None when no save is loaded."""
    return _save_id.data_path(_MILESTONES_FILENAME)


def _load_snapshots():
    with _lock:
        path = _snapshots_path()
        if path is None or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("snapshots", {}) if isinstance(data, dict) else {}
        except Exception:
            return {}


def _save_snapshots(snapshots):
    with _lock:
        path = _snapshots_path()
        if path is None:
            return
        _atomic_write_json(path, {
            "schema_version": 1,
            "snapshots": snapshots,
            "last_scan_at": datetime.datetime.now().isoformat(),
        })


def _load_milestones():
    with _lock:
        path = _milestones_path()
        if path is None or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []


def _save_milestones(entries):
    with _lock:
        path = _milestones_path()
        if path is None:
            return
        trimmed = entries[-_MAX_MILESTONES:]
        _atomic_write_json(path, trimmed)


def _references_path():
    """Per-save milestone-references path. Returns None when no save loaded."""
    return _save_id.data_path(_REFERENCES_FILENAME)


def _load_references():
    """Returns nested dict: {contact_id_str: {recipient_id_str: [timestamp, ...]}}."""
    with _lock:
        path = _references_path()
        if path is None or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _save_references(refs):
    with _lock:
        path = _references_path()
        if path is None:
            return
        _atomic_write_json(path, refs)


def _referenced_timestamps(contact_id, recipient_id):
    """Return the set of milestone timestamps `contact_id` has already
    had surfaced about `recipient_id`."""
    if contact_id is None or recipient_id is None:
        return set()
    refs = _load_references()
    inner = refs.get(str(contact_id), {})
    return set(inner.get(str(recipient_id), []))


def mark_referenced(contact_id, recipient_id, milestone_entries):
    """Record that `contact_id` has been shown these milestones about
    `recipient_id`, so we don't keep re-surfacing the same ones in
    future prompts. Idempotent."""
    if contact_id is None or recipient_id is None or not milestone_entries:
        return
    timestamps = []
    for e in milestone_entries:
        if isinstance(e, dict):
            ts = e.get("timestamp")
        else:
            ts = e
        if ts:
            timestamps.append(ts)
    if not timestamps:
        return
    try:
        with _lock:
            refs = _load_references()
            ckey = str(contact_id)
            rkey = str(recipient_id)
            contact_block = refs.setdefault(ckey, {})
            existing = set(contact_block.get(rkey, []))
            existing.update(timestamps)
            contact_block[rkey] = sorted(existing)
            _save_references(refs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------

def _safe(sim_info, attr, default=None):
    try:
        return getattr(sim_info, attr, default)
    except Exception:
        return default


def _get_age_stage(sim_info):
    try:
        return str(_safe(sim_info, "age", "")).replace("Age.", "").upper().replace(" ", "") or None
    except Exception:
        return None


def _get_active_career(sim_info):
    """Return (career_name, career_level) for the sim's primary career, or (None, None)."""
    try:
        tracker = _safe(sim_info, "career_tracker", None)
        if tracker is None:
            return (None, None)
        careers = getattr(tracker, "_careers", None) or getattr(tracker, "careers", None)
        if not careers:
            return (None, None)
        for career in careers.values():
            try:
                name = type(career).__name__
                level = getattr(career, "level", None)
                if level is None:
                    level = getattr(career, "current_level", None)
                return (name, level)
            except Exception:
                continue
    except Exception:
        pass
    return (None, None)


def _get_spouse_info(sim_info):
    """Return (spouse_id, known) for this sim.

    `known=False` means we could not read this sim's relationships at all
    (tracker missing, target list empty, generator threw). For sims outside
    the active household, the relationship_tracker is loaded lazily -- a
    scan during startup can see zero targets even for a married sim, then
    a later scan sees the spouse normally. That transient None used to
    fire phantom "divorce + remarriage" milestones, so callers must skip
    the spouse diff whenever either side is unknown.

    `known=True, spouse_id=None` means we DID read relationships and
    confirmed there's no spouse bit -- a real "not married" result.
    """
    try:
        rt = _safe(sim_info, "relationship_tracker", None)
        if rt is None:
            return (None, False)
        try:
            targets = list(rt.target_sim_gen())
        except Exception:
            return (None, False)
        if not targets:
            # No targets at all almost always means the tracker hasn't
            # populated yet, not that the sim genuinely knows no one.
            return (None, False)
        for tid in targets:
            try:
                bits = list(rt.get_all_bits(tid))
                for b in bits:
                    name = sim_context._get_trait_name(b).lower()
                    if "spouse" in name or ("married" in name and "unmarried" not in name):
                        return (tid, True)
            except Exception:
                continue
        return (None, True)
    except Exception:
        return (None, False)


def _get_spouse_id(sim_info):
    """Backward-compat shim -- returns just the id, dropping the known flag."""
    spouse_id, _known = _get_spouse_info(sim_info)
    return spouse_id


def _get_in_household(sim_info, active_household_id):
    # Retained for backward-compat with old snapshots. New snapshots use
    # household_id directly so we detect actual moves between households,
    # NOT just "the player switched which family they're playing".
    try:
        hh_id = _safe(sim_info, "household_id", None)
        return hh_id == active_household_id if active_household_id is not None else False
    except Exception:
        return False


def _get_household_id(sim_info):
    try:
        return _safe(sim_info, "household_id", None)
    except Exception:
        return None


def _get_aspiration(sim_info):
    try:
        asp = sim_context.get_sim_aspiration(sim_info)
        return asp or None
    except Exception:
        return None


def _capture(sim_info, active_household_id):
    """Snapshot the relevant attributes of a sim."""
    try:
        name = f"{_safe(sim_info, 'first_name', '')} {_safe(sim_info, 'last_name', '')}".strip()
        career_name, career_level = _get_active_career(sim_info)
        spouse_id, spouse_known = _get_spouse_info(sim_info)
        return {
            "name": name,
            "age_stage": _get_age_stage(sim_info),
            "career_name": career_name,
            "career_level": career_level,
            "is_dead": bool(_safe(sim_info, "is_dead", False) or _safe(sim_info, "is_ghost", False)),
            "is_pregnant": bool(_safe(sim_info, "is_pregnant", False)),
            "spouse_id": spouse_id,
            "spouse_known": spouse_known,
            # `in_household` (active-household relative) is kept for back-
            # compat with old snapshots but no longer drives the diff.
            "in_household": _get_in_household(sim_info, active_household_id),
            # `household_id` is the absolute household identity. Only THIS
            # changing signals an actual move; toggling which household
            # the player is controlling does not.
            "household_id": _get_household_id(sim_info),
            "aspiration": _get_aspiration(sim_info),
        }
    except Exception as e:
        _log(f"_capture failed: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Diff -> milestones
# ---------------------------------------------------------------------------

_AGE_ORDER = ("BABY", "INFANT", "TODDLER", "CHILD", "TEEN", "YOUNGADULT", "YOUNG_ADULT", "ADULT", "ELDER")


def _diff(prev, curr, name):
    """Compare two snapshots; return a list of milestone dicts."""
    events = []
    if not prev:
        # First time we've seen this sim — no milestones yet.
        return events

    # Career
    if prev.get("career_name") != curr.get("career_name"):
        if curr.get("career_name"):
            old = prev.get("career_name") or "unemployed"
            events.append({
                "type": "career_changed",
                "description": f"{name} started a new career: {curr['career_name']} (previously {old})",
            })
        elif prev.get("career_name"):
            events.append({
                "type": "career_left",
                "description": f"{name} left their {prev['career_name']} career",
            })
    elif (prev.get("career_level") is not None and curr.get("career_level") is not None
          and curr["career_level"] != prev["career_level"]):
        if curr["career_level"] > prev["career_level"]:
            events.append({
                "type": "career_promotion",
                "description": f"{name} was promoted at work (now {curr.get('career_name', 'job')} level {curr['career_level']})",
            })
        else:
            events.append({
                "type": "career_demotion",
                "description": f"{name} was demoted at work (now {curr.get('career_name', 'job')} level {curr['career_level']})",
            })

    # Age stage
    if prev.get("age_stage") != curr.get("age_stage") and curr.get("age_stage"):
        events.append({
            "type": "age_up",
            "description": f"{name} aged up to {curr['age_stage'].title()}",
        })

    # Death
    if not prev.get("is_dead") and curr.get("is_dead"):
        events.append({
            "type": "death",
            "description": f"{name} passed away",
        })

    # Pregnancy
    if not prev.get("is_pregnant") and curr.get("is_pregnant"):
        events.append({
            "type": "pregnancy_start",
            "description": f"{name} is now pregnant",
        })
    elif prev.get("is_pregnant") and not curr.get("is_pregnant") and not curr.get("is_dead"):
        events.append({
            "type": "pregnancy_end",
            "description": f"{name} had a baby",
        })

    # Spouse -- only diff if BOTH snapshots had reliable spouse reads.
    # Legacy snapshots (from before spouse_known existed) default to False
    # so we DON'T diff against them -- v3.1.1 -> v3.1.2 upgrade snapshots
    # might have been taken when the relationship_tracker was still lazy,
    # which used to fire phantom divorce events. Cost: real divorces that
    # happened during upgrade are missed for one scan; they show up on
    # the next scan once both snapshots have spouse_known=True.
    prev_known = prev.get("spouse_known", False)
    curr_known = curr.get("spouse_known", False)
    if prev_known and curr_known:
        prev_spouse = prev.get("spouse_id")
        curr_spouse = curr.get("spouse_id")
        if prev_spouse != curr_spouse:
            if curr_spouse and not prev_spouse:
                events.append({
                    "type": "marriage",
                    "description": f"{name} got married",
                })
            elif prev_spouse and not curr_spouse:
                events.append({
                    "type": "divorce_or_widowed",
                    "description": f"{name} is no longer married (divorce, widowed, or breakup)",
                })
            elif prev_spouse and curr_spouse:
                events.append({
                    "type": "remarriage",
                    "description": f"{name} remarried someone new",
                })

    # Household membership -- check the ABSOLUTE household_id, not the
    # active-household-relative in_household flag. Toggling which family
    # the player is currently controlling used to fire false moved-in /
    # moved-out events for every sim every time the player swapped
    # households via Manage Households. Now we only fire when the sim's
    # actual household assignment changes.
    prev_hh = prev.get("household_id")
    curr_hh = curr.get("household_id")
    if prev_hh is not None and curr_hh is not None and prev_hh != curr_hh:
        events.append({
            "type": "moved_household",
            "description": f"{name} moved to a different household",
        })

    # Aspiration
    if prev.get("aspiration") and curr.get("aspiration") and prev["aspiration"] != curr["aspiration"]:
        events.append({
            "type": "aspiration_changed",
            "description": f"{name} switched aspirations to {curr['aspiration']}",
        })

    return events


# ---------------------------------------------------------------------------
# Scan trigger
# ---------------------------------------------------------------------------

def _collect_sims_to_scan():
    """Active household sims plus the protagonist's relationship network."""
    sims = {}  # sim_id -> sim_info (dedupe)
    try:
        import services
        hh = services.active_household()
        if hh:
            for si in hh.sim_info_gen():
                sid = _safe(si, "sim_id", None)
                if sid is not None:
                    sims[sid] = si
        # Protagonist's relationship network
        main_si = sim_context.get_main_sim_info()
        if main_si:
            rt = _safe(main_si, "relationship_tracker", None)
            sm = services.sim_info_manager()
            if rt and sm:
                for tid in rt.target_sim_gen():
                    if tid in sims:
                        continue
                    try:
                        si = sm.get(tid)
                        if si:
                            sims[tid] = si
                    except Exception:
                        continue
    except Exception as e:
        _log(f"_collect_sims_to_scan failed: {type(e).__name__}: {e}")
    return sims


def scan_and_record():
    """Scan all relevant sims, diff against last snapshot, record new milestones."""
    try:
        import services
        hh = services.active_household()
        active_hh_id = _safe(hh, "id", None) if hh else None

        sims = _collect_sims_to_scan()
        # Hold the lock across the whole load -> diff -> save cycle so a
        # second background scan or a phone-context scan_sims can't race
        # us and overwrite our snapshot updates.
        with _lock:
            snapshots = _load_snapshots()
            milestones = _load_milestones()
            now_iso = datetime.datetime.now().isoformat()

            new_count = 0
            for sid, sim_info in sims.items():
                sid_key = str(sid)
                curr = _capture(sim_info, active_hh_id)
                if not curr:
                    continue
                prev = snapshots.get(sid_key)
                events = _diff(prev, curr, curr.get("name") or "Someone")
                for ev in events:
                    ev["timestamp"] = now_iso
                    ev["sim_id"] = sid_key
                    ev["sim_name"] = curr.get("name")
                    milestones.append(ev)
                    new_count += 1
                snapshots[sid_key] = curr

            _save_snapshots(snapshots)
            if new_count > 0:
                _save_milestones(milestones)
                _log(f"Recorded {new_count} new milestone(s) across {len(sims)} sim(s).")
            else:
                _log(f"Scanned {len(sims)} sim(s), no new milestones since last scan.")
    except Exception as e:
        _log(f"scan_and_record raised: {type(e).__name__}: {e}")


def start_background_scan():
    """Fire scan_and_record on a daemon thread so startup isn't blocked."""
    threading.Thread(target=scan_and_record, daemon=True, name="Llamafone-Milestones").start()


def scan_sims(sim_infos):
    """Targeted scan of a small set of specific sims. Called right before
    building a phone prompt so any in-game event (job quit, divorce, etc.)
    that happened since the last full scan still surfaces. Fast: only
    touches the sims passed in, not the whole relationship network."""
    if not sim_infos:
        return
    try:
        import services
        hh = services.active_household()
        active_hh_id = _safe(hh, "id", None) if hh else None

        # Same locking pattern as scan_and_record: hold across the whole
        # load -> diff -> save cycle.
        with _lock:
            snapshots = _load_snapshots()
            milestones = _load_milestones()
            now_iso = datetime.datetime.now().isoformat()

            new_count = 0
            for sim_info in sim_infos:
                if sim_info is None:
                    continue
                sid = _safe(sim_info, "sim_id", None)
                if sid is None:
                    continue
                sid_key = str(sid)
                curr = _capture(sim_info, active_hh_id)
                if not curr:
                    continue
                prev = snapshots.get(sid_key)
                events = _diff(prev, curr, curr.get("name") or "Someone")
                for ev in events:
                    ev["timestamp"] = now_iso
                    ev["sim_id"] = sid_key
                    ev["sim_name"] = curr.get("name")
                    milestones.append(ev)
                    new_count += 1
                snapshots[sid_key] = curr

            _save_snapshots(snapshots)
            if new_count > 0:
                _save_milestones(milestones)
                _log(f"Targeted scan: {new_count} new milestone(s) across {len(sim_infos)} sim(s).")
    except Exception as e:
        _log(f"scan_sims raised: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

def get_recent_for_sim(sim_id, days=_PROMPT_RECENCY_DAYS, limit=_PROMPT_MILESTONES_PER_SIM,
                       exclude_for_contact=None):
    """Return a list of recent milestone dicts for one sim, newest first.

    If `exclude_for_contact` is provided, milestones that contact has
    already had surfaced are filtered out -- so the same contact doesn't
    keep asking about the same job-quit / promotion across calls."""
    sid_key = str(sim_id)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    entries = _load_milestones()
    skip_ts = _referenced_timestamps(exclude_for_contact, sim_id)
    filtered = []
    for e in entries:
        if e.get("sim_id") != sid_key:
            continue
        ts_str = e.get("timestamp")
        try:
            ts = datetime.datetime.fromisoformat(ts_str)
            if ts < cutoff:
                continue
        except Exception:
            continue
        if ts_str in skip_ts:
            continue
        filtered.append(e)
    return list(reversed(filtered))[:limit]


def format_for_prompt(sim_info, contact_id=None, mark_seen=True):
    """Build the 'Recent in their life' block for a sim, or empty string if none.

    When `contact_id` is provided, milestones that contact has already
    been told about are skipped. If `mark_seen` is True, the milestones
    that DO get surfaced are recorded against this contact so they won't
    appear again in future prompts.
    """
    try:
        sid = _safe(sim_info, "sim_id", None)
        if sid is None:
            return ""
        events = get_recent_for_sim(sid, exclude_for_contact=contact_id)
        if not events:
            return ""
        # Filter out deprecated milestone types. The old "moved_in" /
        # "moved_out" detector compared sim.household_id to the active
        # household, so it fired bogus moves whenever the player switched
        # which household they were controlling. Those entries are still
        # in users' save files; suppress them here so they stop polluting
        # prompts. New real moves are stored as "moved_household".
        _SUPPRESS_TYPES = {"moved_in", "moved_out"}
        events = [e for e in events if e.get("type") not in _SUPPRESS_TYPES]
        if not events:
            return ""
        lines = ["Recent in their life:"]
        surfaced = []
        for e in events:
            desc = e.get("description", "").strip()
            if not desc:
                continue
            try:
                date = datetime.datetime.fromisoformat(e["timestamp"]).strftime("%b %d")
            except Exception:
                date = "recently"
            lines.append(f"  - [{date}] {desc}")
            surfaced.append(e)
        if len(lines) == 1:
            return ""
        if mark_seen and contact_id is not None and surfaced:
            mark_referenced(contact_id, sid, surfaced)
        return "\n".join(lines)
    except Exception:
        return ""
