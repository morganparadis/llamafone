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


# Sims 4 ticks-per-minute constant -- kept in sync with past_events.py,
# journal.py, contact_prefs.py, interactions.py. If EA changes tick
# semantics in an update the self-check in past_events.py will fire
# loudly, and the constant needs updating in all five files.
_TICKS_PER_MINUTE = 1500
_TICKS_PER_HOUR = _TICKS_PER_MINUTE * 60
_TICKS_PER_DAY = _TICKS_PER_HOUR * 24


def _ticks_of(dt):
    """Pull an int tick count from a Sims 4 DateAndTime, or None."""
    if dt is None:
        return None
    for attr in ("absolute_ticks", "value", "ticks"):
        fn = getattr(dt, attr, None)
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
        raw = str(dt)
        if "(" in raw and raw.endswith(")"):
            return int(raw.split("(")[-1][:-1])
    except Exception:
        pass
    return None


def _now_sim_ticks():
    """Current in-game tick count, or None if no time service (main menu)."""
    try:
        import services
        ts = services.time_service()
        return _ticks_of(getattr(ts, "sim_now", None)) if ts else None
    except Exception:
        return None


def _relative_sim_time(then_ticks, now_ticks):
    """Turn a delta between two in-game tick counts into a natural phrase
    a sim would actually say. Real-world dates are meaningless in Sims 4
    ("Sep 25" tells the AI nothing about season or timing), so milestones
    render as "yesterday" / "a few days ago" / "last week" / etc. Returns
    None if either tick value is missing -- caller falls back."""
    if then_ticks is None or now_ticks is None:
        return None
    delta = now_ticks - then_ticks
    if delta < 0:
        return "recently"
    hours = delta / _TICKS_PER_HOUR
    days = delta / _TICKS_PER_DAY
    if hours < 1:
        return "just now"
    if hours < 5:
        return "a few hours ago"
    if hours < 20:
        return "earlier today"
    if days < 1.75:
        return "yesterday"
    if days < 4:
        return "a few days ago"
    if days < 8:
        # A Sims 4 in-game week is one season, so this range covers "the
        # last week or so" without spilling into "last season".
        return "earlier this week"
    if days < 15:
        return "last week"
    if days < 30:
        return "a couple of weeks ago"
    return "a while back"


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
    """Return (career_name, career_level) for the sim's primary career, or (None, None).

    Reads the PUBLIC `careers` attribute first, falling back to the
    private `_careers`. The public one is the live-active list; the
    private storage can retain stale entries briefly after a sim
    switches jobs (old career object hangs around alongside the new
    one) -- iterating that first meant the diff would see the stale
    career, decide 'no change', and quietly miss a real Law-to-
    Astronaut switch. Matches the read order sim_context.get_sim_career
    uses, so milestone-scan careers match prompt-time careers."""
    try:
        tracker = _safe(sim_info, "career_tracker", None)
        if tracker is None:
            return (None, None)
        careers = getattr(tracker, "careers", None) or getattr(tracker, "_careers", None)
        if not careers:
            return (None, None)
        career_iter = careers.values() if hasattr(careers, "values") else careers
        for career in career_iter:
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


def _get_pregnancy_partner_id(sim_info):
    """The other parent's sim_id for an in-progress pregnancy, else the
    spouse's, else None. Read while pregnant so it survives into the
    snapshot the birth is diffed against."""
    try:
        pt = getattr(sim_info, "pregnancy_tracker", None)
        if pt is not None and _safe(sim_info, "is_pregnant", False):
            pid = pt.get_partner_id()
            if pid:
                return str(pid)
    except Exception:
        pass
    try:
        sid = getattr(sim_info, "spouse_sim_id", None)
        return str(sid) if sid else None
    except Exception:
        return None


def _sim_name_by_id(sim_id):
    try:
        import services
        si = services.sim_info_manager().get(int(sim_id))
        return f"{si.first_name} {si.last_name}".strip() if si else None
    except Exception:
        return None


def _mirror_for_partner(ev, partner_id, name):
    """Copy of a pregnancy event re-homed onto the other parent. The
    caller stamps timestamp / sim_ticks; we set sim_id / sim_name and
    the description from THEIR side. `mirror_of` points at the pregnant
    sim so the renderer can read visibility / circumstances from her."""
    pname = _sim_name_by_id(partner_id)
    if not pname:
        return None
    m = dict(ev)
    m["sim_id"] = str(partner_id)
    m["sim_name"] = pname
    m["mirror_of"] = None  # filled by caller with the pregnant sim's id
    if ev.get("type") == "pregnancy_start":
        m["description"] = f"{pname} is expecting a baby with {name}"
    else:
        m["description"] = f"{pname} and {name} had a baby"
    return m


def _get_pregnancy_visibility(sim_info):
    """Return one of 'none' | 'hidden' | 'confirmed' | 'visible'.

    Sims 4 flips `sim_info.is_pregnant` to True at CONCEPTION -- before
    the sim takes the pregnancy test and before anyone (including her)
    knows. Surfacing that state as a milestone leaks the pregnancy to
    contacts who couldn't possibly know, and gets called out in AI
    messages like "congrats on the baby!" while the sim herself is
    still oblivious.

    Visibility ladder (based on Sims 4's Trimester buff progression):
      none      - not pregnant
      hidden    - is_pregnant=True but no trimester buff yet
                  (post-conception, pre-test; NOBODY knows)
      confirmed - Trimester 1 buff (test taken; sim + household know,
                  not visibly showing)
      visible   - Trimester 2 or 3 buff (visibly pregnant to everyone)

    Only 'confirmed' or 'visible' trigger the pregnancy_start milestone.
    """
    try:
        if not _safe(sim_info, "is_pregnant", False):
            return "none"
    except Exception:
        return "none"
    # Path 1: active buffs. Precise for instanced sims (active household,
    # sims on the current lot). BuffComponent strips non-persisted buffs
    # on LOD drop / zone unload, so off-lot NPCs report NO trimester
    # buffs and would read as 'hidden' forever -- every pregnant NPC in
    # a save looked pre-test under this path alone.
    tier = _tier_from_buff_name_iter(_active_buff_names(sim_info))
    if tier:
        return tier
    # Path 2: the pregnancy commodity's tuned STATE. The commodity lives
    # on sim_info's statistic tracker, persists, and keeps ticking for
    # off-lot sims. Each commodity state carries the buff it would apply
    # (buff_Pregnancy_Trimester1/2/3), so we read the state the game
    # itself says she is in and map its buff name -- game thresholds,
    # not guessed ones. Verified against simulation.zip (Commodity.
    # get_state_index falls back to computing from value when the
    # cached index is None, so it works for uninstanced sims).
    try:
        tier = _tier_from_buff_name_iter(_pregnancy_commodity_state_buff_names(sim_info))
        if tier:
            return tier
    except Exception:
        pass
    # Path 3: raw progress fallback (value / max). Only reached if the
    # commodity has no usable states. Thirds approximate the trimesters.
    try:
        progress = _pregnancy_progress_from_commodity(sim_info)
        if progress is not None:
            if progress >= 1.0 / 3.0:
                return "visible"
            if progress > 0.0:
                return "confirmed"
    except Exception:
        pass
    return "hidden"


def _tier_from_buff_name_iter(names):
    """Map pregnancy buff class names to a visibility tier, or None."""
    for raw in names:
        name = str(raw or "").lower()
        if "pregnancy" not in name or "trimester" not in name:
            continue
        if any(tag in name for tag in ("trimester2", "trimester_2", "trimester3", "trimester_3")):
            return "visible"
        if "trimester1" in name or "trimester_1" in name:
            return "confirmed"
    return None


def _active_buff_names(sim_info):
    try:
        get_buffs = getattr(sim_info, "get_active_buff_types", None)
        if not callable(get_buffs):
            return []
        return [getattr(bt, "__name__", "") for bt in get_buffs()]
    except Exception:
        return []


def _pregnancy_commodity(sim_info):
    """Return (stat_type, commodity_instance_or_None, tracker_or_None)
    for this sim's species-specific pregnancy commodity."""
    from sims.pregnancy.pregnancy_tracker import PregnancyTracker
    stat_type = PregnancyTracker.PREGNANCY_COMMODITY_MAP.get(sim_info.species)
    if stat_type is None:
        return None, None, None
    tracker = sim_info.get_tracker(stat_type)
    if tracker is None:
        return stat_type, None, None
    try:
        inst = tracker.get_statistic(stat_type)
    except Exception:
        inst = None
    return stat_type, inst, tracker


def _pregnancy_commodity_state_buff_names(sim_info):
    stat_type, inst, _tracker = _pregnancy_commodity(sim_info)
    if inst is None:
        return []
    idx = inst.get_state_index()
    if idx is None:
        return []
    states = getattr(inst, "commodity_states", None) or getattr(stat_type, "commodity_states", None)
    if not states or idx < 0 or idx >= len(states):
        return []
    buff_ref = getattr(states[idx], "buff", None)
    buff_type = getattr(buff_ref, "buff_type", None) if buff_ref is not None else None
    return [getattr(buff_type, "__name__", "")] if buff_type is not None else []


def _pregnancy_progress_from_commodity(sim_info):
    stat_type, inst, tracker = _pregnancy_commodity(sim_info)
    if inst is None or tracker is None:
        return None
    value = float(tracker.get_value(stat_type))
    max_value = getattr(inst, "max_value", None) or getattr(stat_type, "max_value", None)
    max_value = float(max_value) if max_value else 100.0
    return max(0.0, min(1.0, value / max_value)) if max_value > 0 else None


_REACTION_TRAITS = (
    "hateschildren", "familyoriented", "childish", "ambitious", "noncommittal",
    "romantic", "jealous", "loner", "gloomy", "cheerful", "hotheaded",
    "erratic", "materialistic", "lazy", "overachiever", "paranoid",
)


def pregnancy_circumstances(sim_info, partner_id=None, addressee_id=None):
    """Facts that shape how a sim (and the people around them) would
    FEEL about this pregnancy -- so the AI can react in character
    instead of defaulting to 'thrilled'. Returns a list of short
    strings; empty when nothing useful could be read. Every read is
    best-effort; a failure just drops that fact.

    `partner_id`: the other parent when the live tracker can't say (it
    is cleared at birth). `addressee_id`: the sim the message is going
    to -- when that IS the other parent, say so outright instead of
    leaving the AI to connect "the other parent is Ben" with "you are
    texting Ben"."""
    facts = []
    if sim_info is None:
        return facts
    first = getattr(sim_info, "first_name", "She") or "She"
    import services
    sm = None
    try:
        sm = services.sim_info_manager()
    except Exception:
        pass

    def _name(si):
        try:
            return f"{si.first_name} {si.last_name}".strip()
        except Exception:
            return "someone"

    # Who the other parent is, and how that squares with their spouse.
    partner = None
    try:
        pt = getattr(sim_info, "pregnancy_tracker", None)
        partner = pt.get_partner() if pt is not None else None
    except Exception:
        partner = None
    if partner is None and partner_id and sm is not None:
        try:
            partner = sm.get(int(partner_id))
        except Exception:
            partner = None
    spouse = None
    try:
        sid = getattr(sim_info, "spouse_sim_id", None)
        spouse = sm.get(sid) if (sm is not None and sid) else None
    except Exception:
        spouse = None
    def _pname(si):
        # Say it outright when the other parent is who the message is to.
        n = _name(si)
        try:
            if addressee_id is not None and str(getattr(si, "sim_id", "")) == str(addressee_id):
                return f"{n} (the person this message is to)"
        except Exception:
            pass
        return n

    try:
        if partner is not None and spouse is not None:
            if getattr(partner, "sim_id", None) == getattr(spouse, "sim_id", None):
                facts.append(f"the other parent is {first}'s spouse, {_pname(spouse)}")
            else:
                facts.append(
                    f"the other parent is {_pname(partner)} -- NOT {first}'s spouse "
                    f"{_pname(spouse)} (this pregnancy is from outside the marriage)"
                )
        elif partner is not None:
            facts.append(f"{first} is not married; the other parent is {_pname(partner)}")
        elif spouse is not None:
            facts.append(f"{first} is married to {_pname(spouse)}")
        else:
            facts.append(f"{first} is not married and no other parent is recorded")
    except Exception:
        pass

    # Unusual origin (alien abduction etc.) is its own kind of news.
    try:
        origin = getattr(getattr(sim_info, "pregnancy_tracker", None), "_origin", None)
        oname = str(getattr(origin, "name", "") or "").upper()
        if oname and oname != "DEFAULT":
            facts.append(f"pregnancy origin: {oname.replace('_', ' ').lower()}")
    except Exception:
        pass

    # Life stage.
    try:
        age = str(getattr(sim_info, "age", "")).replace("Age.", "")
        if age in ("TEEN", "ELDER"):
            facts.append(f"{first} is a {age.lower()}")
    except Exception:
        pass

    # Traits that color the reaction.
    try:
        traits = sim_context.get_sim_traits(sim_info, limit=12) or []
        hits = [t for t in traits if str(t).lower().replace(" ", "").replace("-", "") in _REACTION_TRAITS]
        if hits:
            facts.append(f"{first}'s relevant traits: {', '.join(hits)}")
    except Exception:
        pass

    # Existing kids and money.
    try:
        hh = getattr(sim_info, "household", None)
        if hh is not None:
            # The newborn itself is not an "existing kid" -- counting it
            # told the AI a first-time mother already had a child.
            newborn_ids = set()
            try:
                from . import births as _births
                newborn_ids = {getattr(b, "sim_id", None) for b in
                               _births._newborns(sim_info, max_age_days=3, require_parent=True)}
            except Exception:
                newborn_ids = set()
            kids = 0
            for si in hh.sim_info_gen():
                if getattr(si, "sim_id", None) in newborn_ids:
                    continue
                a = str(getattr(si, "age", "")).replace("Age.", "")
                if a in ("BABY", "INFANT", "TODDLER", "CHILD", "TEEN"):
                    kids += 1
            facts.append("first child" if kids == 0 else f"already has {kids} kid(s) at home")
            try:
                money = int(getattr(getattr(hh, "funds", None), "money", None))
                if money < 2000:
                    facts.append(f"household is short on money (~{money} simoleons)")
            except Exception:
                pass
    except Exception:
        pass
    return facts


def pregnancy_debug_info(sim_info):
    """Everything the visibility resolver looked at, for llama.pregdebug."""
    info = {"is_pregnant": bool(_safe(sim_info, "is_pregnant", False))}
    info["buffs"] = [n for n in _active_buff_names(sim_info) if "pregnan" in str(n).lower()]
    try:
        stat_type, inst, tracker = _pregnancy_commodity(sim_info)
        info["commodity"] = getattr(stat_type, "__name__", None)
        if inst is not None and tracker is not None:
            info["value"] = tracker.get_value(stat_type)
            info["max_value"] = getattr(inst, "max_value", None)
            idx = inst.get_state_index()
            info["state_index"] = idx
            info["state_buffs"] = _pregnancy_commodity_state_buff_names(sim_info)
            states = getattr(inst, "commodity_states", None) or ()
            info["state_thresholds"] = [
                (getattr(s, "value", None),
                 getattr(getattr(getattr(s, "buff", None), "buff_type", None), "__name__", None))
                for s in states
            ]
        else:
            info["commodity_instance"] = None
    except Exception as e:
        info["commodity_error"] = f"{type(e).__name__}: {e}"
    try:
        info["pregnancy_progress_attr"] = getattr(sim_info, "pregnancy_progress", None)
    except Exception:
        pass
    try:
        pt = getattr(sim_info, "pregnancy_tracker", None)
        partner = pt.get_partner() if pt is not None else None
        info["partner"] = f"{partner.first_name} {partner.last_name}" if partner is not None else None
    except Exception:
        pass
    info["visibility"] = _get_pregnancy_visibility(sim_info)
    return info


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
            # Visibility of the pregnancy -- see _get_pregnancy_visibility.
            # The diff uses THIS, not raw is_pregnant, to decide whether
            # to fire pregnancy_start / pregnancy_end milestones.
            "pregnancy_visibility": _get_pregnancy_visibility(sim_info),
            # Other parent, so a birth / pregnancy can be mirrored onto
            # THEIR milestones too ("Aksel and Francesca had a baby").
            "pregnancy_partner_id": _get_pregnancy_partner_id(sim_info),
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

    # Pregnancy -- gated on VISIBILITY, not raw is_pregnant. Sims 4
    # flips is_pregnant at conception (before test, before anyone
    # knows). We only fire pregnancy_start when the sim's pregnancy
    # has been confirmed (test taken) or is visibly showing. That way
    # non-household contacts won't be told about a pregnancy the sim
    # herself doesn't know about yet.
    #
    # Legacy snapshots without pregnancy_visibility fall back to
    # is_pregnant-derived buckets ('hidden' if pregnant, 'none' if
    # not), which yields the same behavior these snapshots produced
    # before the upgrade -- no spurious re-fires on upgrade.
    _VIS_ORDER = {"none": 0, "hidden": 0, "confirmed": 1, "visible": 2}
    def _vis_of(snap):
        v = snap.get("pregnancy_visibility")
        if v in _VIS_ORDER:
            return v
        return "hidden" if snap.get("is_pregnant") else "none"
    prev_vis = _vis_of(prev)
    curr_vis = _vis_of(curr)
    # A birth was already recorded by the watcher while the tracker still
    # said pregnant (record_birth_now). Until the flag actually clears,
    # carry the marker and emit NO pregnancy events for this sim: no
    # re-fired pregnancy_start while she still reads as pregnant, and no
    # second pregnancy_end when the flag finally flips.
    birth_already_recorded = bool(prev.get("birth_recorded"))
    if birth_already_recorded and curr.get("is_pregnant"):
        curr["birth_recorded"] = True
    # Legacy-upgrade guard: a snapshot from before pregnancy_visibility
    # existed shows is_pregnant=True but the field is missing. The old
    # code already recorded pregnancy_start at conception for that sim
    # -- if we now also fire when visibility crosses to confirmed on
    # the next scan, we get TWO pregnancy_start milestones for one
    # pregnancy. Skip the new fire when the prev snapshot is legacy.
    prev_is_legacy_pregnant = (
        prev.get("is_pregnant") and "pregnancy_visibility" not in prev
    )
    partner_id = curr.get("pregnancy_partner_id") or prev.get("pregnancy_partner_id")
    if (_VIS_ORDER[prev_vis] == 0 and _VIS_ORDER[curr_vis] >= 1
            and not prev_is_legacy_pregnant and not birth_already_recorded):
        ev = {
            "type": "pregnancy_start",
            "description": f"{name} is now pregnant",
            "visibility": curr_vis,
        }
        events.append(ev)
        if partner_id:
            m = _mirror_for_partner(ev, partner_id, name)
            if m:
                events.append(m)
    # Pregnancy_end: fires on any is_pregnant True -> False when the sim
    # isn't dead. Independent of the visibility ladder -- if a baby
    # actually appeared, record it, even if the pregnancy never went
    # through 'confirmed' during our observation window (e.g. sim was
    # already pregnant when the mod was installed, or the scan missed
    # the confirmed phase). Otherwise legacy-upgrade users would lose
    # their birth milestones entirely.
    if (prev.get("is_pregnant") and not curr.get("is_pregnant") and not curr.get("is_dead")
            and not birth_already_recorded):
        ev = {
            "type": "pregnancy_end",
            "description": f"{name} had a baby",
        }
        events.append(ev)
        if partner_id:
            m = _mirror_for_partner(ev, partner_id, name)
            if m:
                events.append(m)

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
            # Pin the save at scan start. Every load/save below resolves
            # the folder live, so if the player switched saves mid-scan
            # (in-game Load), a scan begun in save A would write A's sims
            # into save B's folder. Checked again before writing.
            pinned = _save_id.get_current_save_id()
            if pinned is None:
                return
            snapshots = _load_snapshots()
            milestones = _load_milestones()
            now_iso = datetime.datetime.now().isoformat()
            now_sim_ticks = _now_sim_ticks()

            new_count = 0
            born = []
            for sid, sim_info in sims.items():
                sid_key = str(sid)
                curr = _capture(sim_info, active_hh_id)
                if not curr:
                    continue
                prev = snapshots.get(sid_key)
                events = _diff(prev, curr, curr.get("name") or "Someone")
                for ev in events:
                    ev["timestamp"] = now_iso
                    # In-game tick when this milestone was captured. Used
                    # to render sim-time-relative phrases like "yesterday"
                    # or "last week" instead of meaningless real-world
                    # dates like "Sep 25". Legacy entries without this
                    # field fall through to the real-world date.
                    ev["sim_ticks"] = now_sim_ticks
                    if "mirror_of" in ev:
                        # Partner mirror: keep its own sim_id / name, point
                        # back at the pregnant sim.
                        ev["mirror_of"] = sid_key
                    else:
                        ev["sim_id"] = sid_key
                        ev["sim_name"] = curr.get("name")
                        if ev.get("type") == "pregnancy_end":
                            # Any scan that records a birth (load scan,
                            # targeted scan, watcher) hands it to the
                            # announcer; dedup lives there.
                            born.append((sim_info, curr.get("pregnancy_partner_id")
                                         or (prev or {}).get("pregnancy_partner_id")))
                    milestones.append(ev)
                    new_count += 1
                snapshots[sid_key] = curr

            if _save_id.get_current_save_id() != pinned:
                _log(f"scan_and_record: save changed mid-scan ({pinned!r} -> "
                     f"{_save_id.get_current_save_id()!r}); discarding results, nothing written")
                return
            _save_snapshots(snapshots)
            _announce_births(born)
            # One-time backfill: pregnancy events recorded before partner
            # mirroring existed get a mirror onto the spouse now, so the
            # other parent's "recent life" shows the baby too.
            try:
                new_count += _backfill_partner_mirrors(milestones, now_sim_ticks)
            except Exception as e:
                _log(f"_backfill_partner_mirrors raised: {type(e).__name__}: {e}")
            if new_count > 0:
                _save_milestones(milestones)
                _log(f"Recorded {new_count} new milestone(s) across {len(sims)} sim(s).")
            else:
                _log(f"Scanned {len(sims)} sim(s), no new milestones since last scan.")
    except Exception as e:
        _log(f"scan_and_record raised: {type(e).__name__}: {e}")


def record_birth_now(sim_info, partner_id=None):
    """Record a birth for a sim whose pregnancy tracker still says
    pregnant (baby exists, flag not cleared). Writes pregnancy_end plus
    the partner mirror, and flags the snapshot so the eventual flag flip
    does not fire a second pregnancy_end -- and so a scan that still sees
    her as pregnant does not re-fire pregnancy_start."""
    sid = _safe(sim_info, "sim_id", None)
    if sid is None:
        return
    sid_key = str(sid)
    name = f"{_safe(sim_info, 'first_name', '')} {_safe(sim_info, 'last_name', '')}".strip()
    with _lock:
        snapshots = _load_snapshots()
        milestones = _load_milestones()
        now_iso = datetime.datetime.now().isoformat()
        now_sim_ticks = _now_sim_ticks()
        ev = {
            "type": "pregnancy_end",
            "description": f"{name} had a baby",
            "timestamp": now_iso,
            "sim_ticks": now_sim_ticks,
            "sim_id": sid_key,
            "sim_name": name,
            "mirrored": True,
        }
        milestones.append(ev)
        if partner_id:
            m = _mirror_for_partner(ev, partner_id, name)
            if m:
                m.pop("mirrored", None)
                m["mirror_of"] = sid_key
                milestones.append(m)
        snap = dict(snapshots.get(sid_key) or {})
        snap["birth_recorded"] = True
        snapshots[sid_key] = snap
        _save_snapshots(snapshots)
        _save_milestones(milestones)
        _log(f"record_birth_now: {name!r} (tracker still pregnant); pregnancy_end written")


def _announce_births(born):
    """Hand newly recorded births to births.announce_birth. Lazy import:
    births imports milestones."""
    if not born:
        return
    try:
        import services
        from . import births
        sm = services.sim_info_manager()
        for parent_si, partner_id in born:
            partner_si = None
            if partner_id:
                try:
                    partner_si = sm.get(int(partner_id))
                except Exception:
                    partner_si = None
            births.announce_birth(parent_si, partner_si)
    except Exception as e:
        _log(f"_announce_births raised: {type(e).__name__}: {e}")


def _backfill_partner_mirrors(milestones, now_sim_ticks):
    """For recent pregnancy_start / pregnancy_end events that have no
    partner mirror yet, add one onto the spouse (best available proxy
    for the other parent after the fact). Marks the original so this
    runs once per event. Returns the number of mirrors added."""
    import services
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=_PROMPT_RECENCY_DAYS)).isoformat()
    sm = services.sim_info_manager()
    added = 0
    for ev in list(milestones):
        if ev.get("type") not in ("pregnancy_start", "pregnancy_end"):
            continue
        if "mirror_of" in ev or ev.get("mirrored"):
            continue
        if (ev.get("timestamp") or "") < cutoff:
            continue
        try:
            si = sm.get(int(ev.get("sim_id")))
        except Exception:
            si = None
        if si is None:
            continue
        partner_id = _get_pregnancy_partner_id(si)
        if not partner_id or partner_id == str(ev.get("sim_id")):
            ev["mirrored"] = True
            continue
        name = ev.get("sim_name") or f"{si.first_name} {si.last_name}".strip()
        m = _mirror_for_partner(ev, partner_id, name)
        if not m:
            ev["mirrored"] = True
            continue
        if any(x.get("mirror_of") == str(ev.get("sim_id")) and x.get("type") == ev.get("type")
               and x.get("sim_id") == m["sim_id"] and x.get("sim_ticks") == ev.get("sim_ticks")
               for x in milestones):
            ev["mirrored"] = True  # a mirror already exists (e.g. from the scan)
            continue
        m["mirror_of"] = str(ev.get("sim_id"))
        m.pop("mirrored", None)
        milestones.append(m)
        ev["mirrored"] = True
        added += 1
        _log(f"backfilled partner mirror: {m['description']!r}")
    return added


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
        # load -> diff -> save cycle. Same save pin, too.
        with _lock:
            pinned = _save_id.get_current_save_id()
            if pinned is None:
                return
            snapshots = _load_snapshots()
            milestones = _load_milestones()
            now_iso = datetime.datetime.now().isoformat()
            now_sim_ticks = _now_sim_ticks()

            new_count = 0
            born = []
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
                    # In-game tick when this milestone was captured. Used
                    # to render sim-time-relative phrases like "yesterday"
                    # or "last week" instead of meaningless real-world
                    # dates like "Sep 25". Legacy entries without this
                    # field fall through to the real-world date.
                    ev["sim_ticks"] = now_sim_ticks
                    if "mirror_of" in ev:
                        # Partner mirror: keep its own sim_id / name, point
                        # back at the pregnant sim.
                        ev["mirror_of"] = sid_key
                    else:
                        ev["sim_id"] = sid_key
                        ev["sim_name"] = curr.get("name")
                        if ev.get("type") == "pregnancy_end":
                            # Any scan that records a birth (load scan,
                            # targeted scan, watcher) hands it to the
                            # announcer; dedup lives there.
                            born.append((sim_info, curr.get("pregnancy_partner_id")
                                         or (prev or {}).get("pregnancy_partner_id")))
                    milestones.append(ev)
                    new_count += 1
                snapshots[sid_key] = curr

            if _save_id.get_current_save_id() != pinned:
                _log(f"scan_sims: save changed mid-scan ({pinned!r} -> "
                     f"{_save_id.get_current_save_id()!r}); discarding results, nothing written")
                return
            _save_snapshots(snapshots)
            _announce_births(born)
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
    # Milestone types that should ALWAYS surface fresh every prompt,
    # ignoring the per-contact "already seen" list. These carry the
    # "you may not know" soft tag and need to reappear every
    # conversation while the underlying life event is still current
    # (pregnancy in progress, baby just born) -- close family should
    # keep referencing them, distant contacts should keep hedging.
    # Prior versions marked these seen after one prompt and then
    # dropped them forever, which is why pregnancy silently vanished
    # from a contact's prompt after their first post-test conversation.
    _NEVER_SKIP_TYPES = {"pregnancy_start", "pregnancy_end"}
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
        # Already surfaced to this contact: KEEP it, flagged, rather than
        # dropping it. Dropping meant the fact vanished from the prompt
        # after one conversation while the contact's own earlier text
        # ("congrats on the wedding!") stayed in the history -- so the
        # model re-congratulated from its own echo with nothing to
        # correct it. Flagged, the line becomes "you already know this,
        # don't bring it up as news," which is what the seen-tracker was
        # for in the first place.
        if ts_str in skip_ts and e.get("type") not in _NEVER_SKIP_TYPES:
            e = dict(e)
            e["_seen"] = True
        filtered.append(e)
    return list(reversed(filtered))[:limit]


def days_since_milestone(sim_id, mtype):
    """In-game days since the newest milestone of `mtype` for this sim,
    or None if there is none / it has no in-game timestamp (legacy)."""
    try:
        now_ticks = _now_sim_ticks()
        if now_ticks is None:
            return None
        for e in get_recent_for_sim(sim_id):
            if e.get("type") == mtype:
                st = e.get("sim_ticks")
                if st is None:
                    return None
                return max(0.0, (now_ticks - st) / _TICKS_PER_DAY)
    except Exception:
        pass
    return None


def _has_heard(e, now_ticks, knowledge):
    """News-spread model: a contact 'hears' a private life event once
    enough in-game time has passed for their closeness tier. `knowledge`
    is {"heard_after_days": float|None(never), "cold": bool}. Legacy
    entries with no in-game timestamp count as old news for anyone with
    a tier at all."""
    if not knowledge:
        return False
    days = knowledge.get("heard_after_days")
    if days is None:
        return False
    st = e.get("sim_ticks")
    if st is None or now_ticks is None:
        return True
    return (now_ticks - st) / _TICKS_PER_DAY >= days


def format_for_prompt(sim_info, contact_id=None, mark_seen=True, known_by_default=False,
                      knowledge=None, addressee_id=None):
    """Build the 'Recent in their life' block for a sim, or empty string if none.

    When `contact_id` is provided, milestones that contact has already
    been told about are skipped. If `mark_seen` is True, the milestones
    that DO get surfaced are recorded against this contact so they won't
    appear again in future prompts.

    When `known_by_default` is True, the "you may not know this" tag on
    pregnancy_start / pregnancy_end milestones is suppressed. Used when
    surfacing a sim's household milestones to a caller who IS that
    household -- Luca living with Martha absolutely knows Martha is
    pregnant; the hedge tag would be nonsense. Hidden pregnancies still
    don't surface even to household (the sim herself doesn't know yet).

    An event is always known to its own participants: when `contact_id`
    is the sim the event is about, or the pregnant sim a mirror points
    at, the news-spread gate does not apply (Ingrid was told to "assume
    you do not know" that Ben -- the father -- had a baby with her).

    `addressee_id`: who the message is going to, so the Circumstances
    line can say outright when they are the other parent.
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
        # Suppress pregnancy_start milestones when the sim's own current
        # state says the pregnancy isn't even visible to HERSELF (pre-test).
        # Catches legacy entries created before the visibility ladder
        # existed AND defensively re-suppresses if the tracker somehow
        # reverts. Post-baby (curr_vis='none' with a pregnancy_end event
        # elsewhere in the list) we leave pregnancy_start alone so the
        # AI can still reference the recent pregnancy in context.
        try:
            curr_vis = _get_pregnancy_visibility(sim_info)
        except Exception:
            curr_vis = "none"
        # Mirrored events (the other parent's copy) read visibility and
        # circumstances from the PREGNANT sim, not from this one.
        _mirror_cache = {}

        def _event_source(e):
            mid = e.get("mirror_of")
            if not mid:
                return sim_info, curr_vis
            if mid not in _mirror_cache:
                try:
                    import services
                    src = services.sim_info_manager().get(int(mid))
                    _mirror_cache[mid] = (src, _get_pregnancy_visibility(src) if src else "none")
                except Exception:
                    _mirror_cache[mid] = (None, "none")
            return _mirror_cache[mid]

        def _keep_start(e):
            if e.get("type") != "pregnancy_start":
                return True
            return _event_source(e)[1] != "hidden"
        events = [e for e in events if _keep_start(e)]
        # Dedup consecutive pregnancy_start entries -- happens for saves
        # that had a pregnancy in progress when the mod was upgraded to
        # the visibility-aware code (old code fired at conception, new
        # code fired again at confirmed transition). Keep the NEWEST
        # pregnancy_start per pregnancy; a pregnancy_end resets the
        # window so a subsequent pregnancy's start survives. `events`
        # is newest-first from get_recent_for_sim.
        seen_start_since_end = False
        deduped = []
        for e in events:
            t = e.get("type")
            if t == "pregnancy_end":
                seen_start_since_end = False
                deduped.append(e)
            elif t == "pregnancy_start":
                if seen_start_since_end:
                    continue  # older duplicate for the same pregnancy
                seen_start_since_end = True
                deduped.append(e)
            else:
                deduped.append(e)
        events = deduped
        if not events:
            return ""
        now_ticks = _now_sim_ticks()
        _viewer = str(contact_id) if contact_id is not None else None

        def _participant(e):
            return _viewer is not None and _viewer in (str(e.get("sim_id")), str(e.get("mirror_of")))

        def _known_outright(e):
            return known_by_default or _participant(e)

        # Once a BIRTH is known to this contact (heard, or they live with
        # her), the earlier "is expecting" line is stale -- drop it so the
        # prompt doesn't say "expecting" and "had the baby" side by side.
        # If the birth is NOT yet heard, keep "expecting": that is exactly
        # what this contact still believes.
        _birth_known = any(
            e.get("type") == "pregnancy_end" and (_known_outright(e) or _has_heard(e, now_ticks, knowledge))
            for e in events
        )
        _all_for_partner = None

        def _other_parent_id(e):
            """The other parent for a pregnancy event: a mirror's own sim is
            the other parent of the pregnant sim it points at; for the
            pregnant sim's own event, find its mirror."""
            nonlocal _all_for_partner
            if e.get("mirror_of"):
                return e.get("sim_id")
            if e.get("partner_id"):
                return e.get("partner_id")
            if _all_for_partner is None:
                try:
                    _all_for_partner = _load_milestones()
                except Exception:
                    _all_for_partner = []
            for m in _all_for_partner:
                if (m.get("mirror_of") == str(e.get("sim_id")) and m.get("type") == e.get("type")
                        and m.get("sim_ticks") == e.get("sim_ticks")):
                    return m.get("sim_id")
            return None
        if _birth_known:
            events = [e for e in events if e.get("type") != "pregnancy_start"]
        lines = ["Recent in their life:"]
        surfaced = []
        # Pregnancy + birth milestones get a soft "you may not know unless
        # close family" tag rather than a hard drop, so the AI hedges
        # instead of leading with private news -- but close family can
        # still reference it, and any sim can play along if the recipient
        # brings it up. Cheaper and more forgiving than a code-level
        # relationship gate: the AI already knows its own family role
        # from context, so we let it decide.
        _PRIVATE_TYPES = {"pregnancy_start", "pregnancy_end"}
        for e in events:
            desc = e.get("description", "").strip()
            if not desc:
                continue
            # In-game time first; fall back to a generic "recently" when
            # the ticks are missing (legacy entries pre-dating the field).
            # Real-world dates like "Sep 25" are meaningless in-game --
            # Sims 4 has seasons, not months.
            when = _relative_sim_time(e.get("sim_ticks"), now_ticks) or "recently"
            if e.get("_seen"):
                desc = (
                    f"{desc} [ALREADY KNOWN to you -- you have discussed this "
                    f"before; it is old news. Do NOT congratulate or react as if "
                    f"hearing it for the first time.]"
                )
            # For an in-progress pregnancy, annotate the line with
            # whether she's visibly showing right now -- the AI needs
            # this to judge the "seen in person recently + bump obvious"
            # signal. Fetched live rather than from the milestone's
            # stored visibility so it stays accurate as she progresses
            # through trimesters.
            annotation = ""
            ev_sim, ev_vis = _event_source(e)
            if e.get("type") == "pregnancy_start" and ev_vis in ("confirmed", "visible"):
                annotation = (
                    " (currently visibly showing)" if ev_vis == "visible"
                    else " (not yet visibly showing)"
                )
            # Private events: has word reached this contact yet? Visible
            # pregnancies are public (the bump). Otherwise the contact's
            # closeness tier sets how many in-game days until they've
            # heard through family / friends. Not heard + COLD prompt
            # (the sender is inventing a topic) -> the line is withheld
            # entirely, because instructions alone did not stop cold
            # messages from leading with it. Not heard + reply -> shown
            # with the strict tag so the player can bring it up.
            is_private = e.get("type") in _PRIVATE_TYPES and not _known_outright(e)
            heard = False
            if is_private:
                if e.get("type") == "pregnancy_start" and ev_vis == "visible":
                    heard = True
                else:
                    heard = _has_heard(e, now_ticks, knowledge)
                if not heard and knowledge and knowledge.get("cold"):
                    continue
            lines.append(f"  - [{when}] {desc}{annotation}")
            if is_private and heard:
                # Fresh + huge = the reason you're calling. The older
                # wording ("if it comes up, react") read as "don't raise
                # it", and a mother phoned her daughter hours after the
                # birth to ask for pasta advice.
                try:
                    _days_ago = (now_ticks - e["sim_ticks"]) / _TICKS_PER_DAY if e.get("sim_ticks") and now_ticks else 99
                except Exception:
                    _days_ago = 99
                if _days_ago <= 2.0:
                    lines.append(
                        "      [You KNOW about this -- either they told you directly "
                        "(check the past-interaction history) or word reached you "
                        "through family / friends. It is the biggest thing in their "
                        "life right now. LEAD WITH IT: this is your reason for reaching "
                        "out -- check in on them and the baby, ask how they're "
                        "holding up, offer help. Do NOT announce it as news (they "
                        "obviously know) and do NOT act surprised -- but do not "
                        "talk about anything else first.]"
                    )
                else:
                    lines.append(
                        "      [You KNOW about this -- either they told you directly "
                        "(check the past-interaction history) or word reached you "
                        "through family / friends. Do not act surprised, do not "
                        "announce it to them as if they didn't know; if it comes up, "
                        "react per the Circumstances and the recency.]"
                    )
            # Circumstances that decide whether this is joy, a shock, or
            # a crisis -- rendered for anyone who gets to see the line.
            # For a birth the tracker is cleared, so the other parent comes
            # from the mirror; only recent births (the tone still matters).
            _recent_birth = False
            if e.get("type") == "pregnancy_end":
                try:
                    _recent_birth = (e.get("sim_ticks") is not None and now_ticks is not None
                                     and (now_ticks - e["sim_ticks"]) / _TICKS_PER_DAY <= 3.0)
                except Exception:
                    _recent_birth = False
            if (e.get("type") == "pregnancy_start" and ev_vis in ("confirmed", "visible")) or _recent_birth:
                try:
                    facts = (pregnancy_circumstances(ev_sim, partner_id=_other_parent_id(e),
                                                     addressee_id=addressee_id)
                             if ev_sim is not None else [])
                except Exception:
                    facts = []
                if facts:
                    lines.append("      Circumstances: " + "; ".join(facts) + ".")
            if is_private and not heard:
                lines.append(
                    "      [ASSUME YOU DO NOT KNOW THIS. You may know "
                    "ONLY if EITHER: (a) the past-interactions history in "
                    "this prompt already shows the two of you discussing "
                    "it, OR (b) that history shows you've been together in "
                    "person recently AND -- for pregnancy -- the line "
                    "above says she is visibly showing (bump would be "
                    "obvious to anyone who saw her). If either applies, "
                    "reference it naturally and follow up. Otherwise, "
                    "regardless of your family relationship: do NOT lead "
                    "with congrats, do NOT ask about it, do NOT bring it "
                    "up. Play along naturally if the recipient mentions "
                    "it first. Player controls who knows via which "
                    "conversations happen in-mod. IF you do know: react to the "
                    "Circumstances line, not with reflexive congratulations -- "
                    "a baby outside a marriage, a teen, or money trouble calls "
                    "for concern, tact, or awkwardness in character.]"
                )
            # Pregnancy / birth get the soft "you may not know" tag and are
            # meant to STAY in the prompt across every conversation while
            # the sim is pregnant / just gave birth -- close family should
            # keep referencing it warmly across multiple calls, and
            # distant contacts should keep hedging. Marking them 'seen'
            # after one prompt (like promotions / marriages) would drop
            # them for that contact forever, which is why the pregnancy
            # milestone silently vanished from Luca's prompt after his
            # first conversation with Francesca post-test. Skip them
            # from the seen-tracker; other milestones still get marked
            # to avoid "hey how's the new job??" every call.
            if e.get("type") not in _PRIVATE_TYPES:
                surfaced.append(e)
        if len(lines) == 1:
            return ""
        if mark_seen and contact_id is not None and surfaced:
            mark_referenced(contact_id, sid, surfaced)
        return "\n".join(lines)
    except Exception:
        return ""
